import os
import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.ledger import project_key_for_cwd
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
        reason="memory loop test",
    )


class MemoryLoopTest(unittest.TestCase):
    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_project_scoped_memory_does_not_cross_project_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.ledger.add_candidate(
                    _candidate("A 项目约定：hook 与 MCP 必须完全分离。", "project_context", "project"),
                    "active",
                    {"status": "active"},
                    project_key=project_key_for_cwd("/tmp/project-a"),
                )
                context = service.prompt_context("hook 与 MCP 的约定是什么？", cwd="/tmp/project-b", limit=5)
                self.assertIn("用户需求：", context)
                self.assertIn("hook", context)
                self.assertIn("MCP", context)
                self.assertIn("项目规则：无", context)
                self.assertNotIn("hook 与 MCP 必须完全分离", context)
                context = service.prompt_context("hook 与 MCP 的约定是什么？", cwd="/tmp/project-a", limit=5)
                self.assertIn("hook 与 MCP 必须完全分离", context)
            finally:
                service.close()

    def test_recall_outcome_updates_strength_and_positive_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                memory_id = service.ledger.add_candidate(
                    _candidate("用户偏好默认使用中文回答。", "user_preference", "global", 0.8),
                    "active",
                    {"status": "active"},
                )
                context = service.prompt_context(
                    "我的回答语言偏好是什么？",
                    session_id="session-1",
                    turn_id="turn-1",
                    limit=5,
                )
                self.assertIn("默认使用中文", context)
                service.apply_recall_outcome("session-1", "turn-1", "我会继续默认使用中文回答。")
                memory = service.ledger.get_memory(memory_id)
                self.assertEqual(memory["recall_count"], 1)
                self.assertEqual(memory["positive_recall_count"], 1)
                self.assertGreater(memory["strength"], 1.0)
            finally:
                service.close()

    def test_related_active_memories_get_association_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.ledger.add_candidate(
                    _candidate("经验：hook 内部调用 codex exec 时必须设置 internal 标记，否则会递归触发。"),
                    "active",
                    {"status": "active"},
                    project_key=project_key_for_cwd("/tmp/project-a"),
                )
                second = service.ledger.add_candidate(
                    _candidate("经验：hook 死循环排查时先检查 CODEX_COGNITIVE_RUNTIME_INTERNAL_CALL 和 HOOK_DEPTH。"),
                    "active",
                    {"status": "active"},
                    project_key=project_key_for_cwd("/tmp/project-a"),
                )
                service.ledger.link_related_active_memories(second)
                edges = service.ledger.list_edges([first, second])
                self.assertTrue(edges)
                self.assertTrue(any(edge["source_id"] == first or edge["target_id"] == first for edge in edges))
            finally:
                service.close()
