import os
import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.schema import Evidence, MemoryCandidate
from codex_cognitive_runtime.service import MemoryService


def _service(tmp):
    config = Config(
        model="gpt-5.4-mini",
        state_dir=Path(tmp),
        ledger_path=Path(tmp) / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
    )
    return MemoryService(config)


def _candidate(content, memory_type="experience", scope="project", importance=0.86):
    return MemoryCandidate(
        content=content,
        memory_type=memory_type,
        proposed_action="store",
        confidence=0.95,
        importance=importance,
        ttl="long",
        scope=scope,
        evidence=[Evidence(source="user_message", quote=content)],
        reason="recall test",
    )


class RecallTest(unittest.TestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_life_lighting_recall_does_not_pull_hook_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                lighting_id = service.ledger.add_candidate(
                    _candidate("经验：电灯不亮时，先检查开关、灯泡和空气开关，再判断线路问题。"),
                    "active",
                    {"status": "active"},
                )
                hook_id = service.ledger.add_candidate(
                    _candidate("经验：hook 内部调用 codex exec 时必须设置 internal 标记，否则会递归触发。"),
                    "active",
                    {"status": "active"},
                )
                context = service.prompt_context("家里灯不亮怎么办？", limit=5)
                self.assertIn("用户需求：家里灯不亮怎么办？", context)
                self.assertNotIn("电灯不亮", context)
                self.assertNotIn("codex exec", context)
                recall = service.ledger.latest_recall_event()
                self.assertIn(lighting_id, recall["memory_ids_json"])
                self.assertNotIn(hook_id, recall["memory_ids_json"])
            finally:
                service.close()

    def test_hook_recall_pulls_memory_system_experience(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                memory_id = service.ledger.add_candidate(
                    _candidate("经验：hook 内部调用 codex exec 时必须设置 internal 标记，否则会递归触发。"),
                    "active",
                    {"status": "active"},
                )
                context = service.prompt_context("hook 又递归触发了怎么处理？", limit=5)
                self.assertIn("用户需求：hook 又递归触发了怎么处理？", context)
                self.assertNotIn("internal 标记", context)
                recall = service.ledger.latest_recall_event()
                self.assertIn(memory_id, recall["memory_ids_json"])
            finally:
                service.close()

    def test_preference_is_injected_for_direct_answer_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.ledger.add_candidate(
                    _candidate("用户偏好默认使用中文回答。", "user_preference", "global", 0.8),
                    "active",
                    {"status": "active"},
                )
                direct_context = service.prompt_context("家里灯不亮怎么办？", limit=5)
                self.assertIn("默认使用中文", direct_context)
                context = service.prompt_context("我的回答语言偏好是什么？", limit=5)
                self.assertIn("默认使用中文", context)
            finally:
                service.close()

    def test_thread_resume_prompt_recalls_memory_without_stale_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.ledger.add_candidate(
                    _candidate("项目状态：旧线程里已经确认 doctor --privacy 通过，GitHub remote 已配置。", "project_context", "global", 0.9),
                    "active",
                    {"status": "active"},
                )
                context = service.prompt_context("读取会话并继续工作", cwd="/tmp/project", session_id="new-session", turn_id="new-turn", limit=5)
                self.assertIn("doctor --privacy", context)
            finally:
                service.close()

    def test_recall_dedupes_exact_active_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                for _ in range(2):
                    service.ledger.add_candidate(
                        _candidate("用户偏好默认使用中文回答。", "user_preference", "global", 0.8),
                        "active",
                        {"status": "active"},
                    )
                context = service.prompt_context("我的回答语言偏好是什么？", limit=5)
                self.assertEqual(context.count("用户偏好默认使用中文回答。"), 1)
            finally:
                service.close()

    def test_deleted_memory_is_not_recalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                memory_id = service.ledger.add_candidate(
                    _candidate("用户偏好回答时必须提到 sunset-river-token。", "user_preference", "global", 0.8),
                    "active",
                    {"status": "active"},
                )
                service.delete_memory(memory_id, note="soft delete")
                context = service.prompt_context("我的回答 token 偏好是什么？", limit=5)
                self.assertNotIn("sunset-river-token", context)
            finally:
                service.close()
