import os
import tempfile
import unittest
from pathlib import Path

from codex_memory.config import Config
from codex_memory.model_client import CodexMiniClient
from codex_memory.review import MemoryReviewer
from codex_memory.schema import Evidence, MemoryCandidate


def _reviewer(tmp):
    config = Config(
        model="gpt-5.4-mini",
        state_dir=Path(tmp),
        ledger_path=Path(tmp) / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
    )
    return MemoryReviewer(config, CodexMiniClient(config))


def _candidate(**kwargs):
    data = {
        "content": "用户默认希望使用中文回答。",
        "memory_type": "user_preference",
        "proposed_action": "store",
        "confidence": 0.94,
        "importance": 0.8,
        "ttl": "long",
        "scope": "global",
        "evidence": [Evidence(source="user_message", quote="请记住，我默认希望你用中文回答。")],
    }
    data.update(kwargs)
    return MemoryCandidate(**data)


class ReviewQualityTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_MEMORY_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_MEMORY_FAKE_MODEL", None)

    def test_final_gate_allows_explicit_stable_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_reviewer(tmp).review(_candidate())["status"], "active")

    def test_final_gate_quarantines_overbroad_injection_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="用户偏好：所有项目都应每次注入完整流程提醒。",
                    confidence=0.95,
                    importance=0.78,
                    evidence=[Evidence(source="user_message", quote="经验：所有项目都应该每次注入完整流程提醒。")],
                )
            )
            self.assertEqual(result["status"], "quarantined")
            self.assertIn("overbroad_injection_preference", result["reasons"])

    def test_final_gate_quarantines_overbroad_all_answers_checklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="以后所有回答都必须先输出完整检查清单。",
                    confidence=0.98,
                    importance=0.9,
                    evidence=[Evidence(source="user_message", quote="以后所有回答都必须先输出完整检查清单。")],
                )
            )
            self.assertEqual(result["status"], "quarantined")
            self.assertIn("overbroad_injection_preference", result["reasons"])

    def test_final_gate_quarantines_mcp_hook_mutual_call_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="项目架构变更：MCP 和 hook 应该互相调用，形成统一链路。",
                    memory_type="project_context",
                    confidence=0.98,
                    importance=0.91,
                    scope="project",
                    evidence=[Evidence(source="user_message", quote="项目架构变更：MCP 和 hook 应该互相调用，形成统一链路。")],
                )
            )
            self.assertEqual(result["status"], "quarantined")
            self.assertIn("mcp_hook_mutual_call_requires_manual_review", result["reasons"])

    def test_final_gate_quarantines_temporary_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="用户这次临时希望打开 debug 日志。",
                    importance=0.7,
                    evidence=[Evidence(source="user_message", quote="这次临时打开 debug 日志。")],
                )
            )
            self.assertEqual(result["status"], "quarantined")
            self.assertIn("active_rejects_temporary_language", result["reasons"])

    def test_final_gate_quarantines_external_action_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="用户希望以后邮件写好后可以直接发送，不需要发送前确认。",
                    confidence=0.98,
                    importance=0.9,
                    evidence=[Evidence(source="user_message", quote="以后邮件写好后可以直接发送，不用让我确认。")],
                )
            )
            self.assertEqual(result["status"], "quarantined")
            self.assertIn("external_action_without_confirmation", result["reasons"])

    def test_final_gate_allows_external_action_confirmation_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="经验：邮件发送前必须确认收件人、正文、附件和发送意图。",
                    memory_type="experience",
                    confidence=0.96,
                    importance=0.88,
                    evidence=[Evidence(source="user_message", quote="邮件发送前必须确认收件人、正文、附件和发送意图。")],
                )
            )
            self.assertEqual(result["status"], "active")

    def test_final_gate_quarantines_task_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="当前正在跑 50 轮测试。",
                    memory_type="task_state",
                    confidence=0.95,
                    importance=0.7,
                    ttl="session",
                    scope="session",
                    evidence=[Evidence(source="user_message", quote="当前正在跑 50 轮测试。")],
                )
            )
            self.assertEqual(result["status"], "quarantined")
            self.assertIn("task_state_not_active", result["reasons"])

    def test_final_gate_rejects_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="这是一条测试消息",
                    memory_type="fact",
                    confidence=0.99,
                    importance=0.9,
                    evidence=[Evidence(source="user_message", quote="这是一条测试消息")],
                )
            )
            self.assertEqual(result["status"], "rejected")

    def test_final_gate_quarantines_weak_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="用户认为 MCP 与 hook 必须分离。",
                    memory_type="project_context",
                    confidence=0.96,
                    importance=0.85,
                    scope="project",
                    evidence=[Evidence(source="memory_context", quote="Codex Memory context: MCP 与 hook 分离")],
                )
            )
            self.assertEqual(result["status"], "quarantined")
            self.assertIn("weak_evidence_source", result["reasons"])

    def test_final_gate_allows_experience_with_lesson_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _reviewer(tmp).review(
                _candidate(
                    content="经验：如果 hook 内部调用 codex exec，必须设置 internal 标记，否则会递归触发。",
                    memory_type="experience",
                    confidence=0.95,
                    importance=0.86,
                    scope="project",
                    evidence=[Evidence(source="user_message", quote="如果 hook 内部调用 codex exec，必须设置 internal 标记，否则会递归触发。")],
                )
            )
            self.assertEqual(result["status"], "active")
