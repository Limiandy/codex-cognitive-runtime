import os
import tempfile
import unittest
from pathlib import Path

from codex_memory.config import Config
from codex_memory.schema import Evidence, MemoryCandidate
from codex_memory.service import MemoryService


def _config(tmp):
    tmp_path = Path(tmp)
    return Config(
        model="gpt-5.4-mini",
        state_dir=tmp_path,
        ledger_path=tmp_path / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
    )


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_MEMORY_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_MEMORY_FAKE_MODEL", None)

    def test_exact_duplicate_is_merged_not_refiled(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                first = service.ingest_event("manual", {"text": "默认使用中文回答"})
                second = service.ingest_event("manual", {"text": "默认使用中文回答"})
                self.assertEqual(first["results"][0]["status"], "active")
                self.assertEqual(second["results"][0]["status"], "superseded")
                self.assertEqual(len(service.list_memories(status="active")), 1)
            finally:
                service.close()

    def test_near_duplicate_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="用户默认希望用中文回答，并且尽量简洁。",
                    memory_type="user_preference",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="global",
                    evidence=[Evidence(source="user_message", quote="用户默认希望用中文回答，并且尽量简洁。")],
                    reason="test",
                )
                service.ledger.add_candidate(candidate, "active", {"status": "active"})
                matches = service.ledger.find_active_duplicates("默认用中文回答，并且尽量简洁。", "user_preference", "global")
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates("默认用中文回答，且尽量简洁。", "user_preference", "global")
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_architecture_near_duplicate_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="项目架构决策：MCP 和 hook 必须是两条不重叠的路径，不能互相调用。",
                    memory_type="project_context",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="memory_system",
                    category="architecture",
                    subcategory="mcp_hook",
                    triggers=["MCP", "hook", "不重叠"],
                    evidence=[Evidence(source="user_message", quote="项目架构决策：MCP 和 hook 必须是两条不重叠的路径，不能互相调用。")],
                    reason="test",
                )
                project_key = str(Path(tmp).resolve()).lower()
                service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                matches = service.ledger.find_active_duplicates(
                    "项目架构上，MCP 与 hook 需要保持两条不重叠路径，不能互相调用。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_manual_review_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                result = service.ingest_event("manual", {"text": "默认使用中文回答"})
                memory_id = result["results"][0]["id"]
                rejected = service.reject_memory(memory_id, "not useful")
                self.assertEqual(rejected["status"], "rejected")
                promoted = service.promote_memory(memory_id, "confirmed")
                self.assertEqual(promoted["memory"]["status"], "active")
                deleted = service.delete_memory(memory_id, "cleanup")
                self.assertEqual(deleted["status"], "deleted")
            finally:
                service.close()

    def test_expire_due_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                result = service.ingest_event("manual", {"text": "默认使用中文回答"})
                memory_id = result["results"][0]["id"]
                service.ledger.conn.execute("UPDATE memories SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (memory_id,))
                service.ledger.conn.commit()
                expired = service.expire_due_memories()
                self.assertEqual(expired["expired_count"], 1)
                self.assertEqual(service.ledger.get_memory(memory_id)["status"], "superseded")
            finally:
                service.close()
