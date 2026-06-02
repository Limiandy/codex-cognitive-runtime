import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.default_memories import (
    DEFAULT_AGENTS_MEMORY_CONTENT,
    DEFAULT_AGENTS_MEMORY_OLD_TITLE,
    DEFAULT_AGENTS_MEMORY_TITLE,
    DEFAULT_AGENTS_MEMORY_VERSION,
)
from codex_cognitive_runtime.ledger import Ledger
from codex_cognitive_runtime.schema import Evidence, MemoryCandidate
from codex_cognitive_runtime.service import MemoryService


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


class DefaultMemoriesTest(unittest.TestCase):
    def test_service_installs_bundled_global_agents_memory_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            service = MemoryService(config)
            try:
                memories = service.list_memories(status="active", memory_type="user_preference", scope="global", limit=20)
                matches = [memory for memory in memories if DEFAULT_AGENTS_MEMORY_TITLE in str(memory.get("content") or "")]
                self.assertEqual(len(matches), 1)
                self.assertIsNone(matches[0].get("project_key"))
                self.assertEqual(matches[0]["review_json"]["source_id"], "default:global_agents_collaboration_rules")
            finally:
                service.close()

            service = MemoryService(config)
            try:
                memories = service.list_memories(status="active", memory_type="user_preference", scope="global", limit=20)
                matches = [memory for memory in memories if DEFAULT_AGENTS_MEMORY_TITLE in str(memory.get("content") or "")]
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_deleted_bundled_default_memory_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            service = MemoryService(config)
            try:
                memory = [
                    item
                    for item in service.list_memories(status="active", memory_type="user_preference", scope="global", limit=20)
                    if DEFAULT_AGENTS_MEMORY_TITLE in str(item.get("content") or "")
                ][0]
                service.ledger.set_status(str(memory["id"]), "deleted", {**memory["review_json"], "user_deleted": True})
            finally:
                service.close()

            service = MemoryService(config)
            try:
                memories = service.list_memories(memory_type="user_preference", scope="global", limit=20)
                active_matches = [
                    memory
                    for memory in memories
                    if memory.get("status") == "active" and DEFAULT_AGENTS_MEMORY_TITLE in str(memory.get("content") or "")
                ]
                self.assertEqual(active_matches, [])
            finally:
                service.close()

    def test_existing_old_title_memory_is_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            ledger = Ledger(config.ledger_path)
            try:
                old_content = DEFAULT_AGENTS_MEMORY_CONTENT.replace(DEFAULT_AGENTS_MEMORY_TITLE, DEFAULT_AGENTS_MEMORY_OLD_TITLE, 1)
                candidate = MemoryCandidate(
                    content=old_content,
                    memory_type="user_preference",
                    proposed_action="store",
                    confidence=0.98,
                    importance=0.96,
                    ttl="long",
                    scope="global",
                    evidence=[Evidence(source="user_message", quote=DEFAULT_AGENTS_MEMORY_OLD_TITLE)],
                    reason="old title compatibility test",
                )
                ledger.add_candidate(candidate, "active", {"status": "active"})
            finally:
                ledger.close()

            service = MemoryService(config)
            try:
                memories = service.list_memories(status="active", memory_type="user_preference", scope="global", limit=20)
                matches = [memory for memory in memories if "AGENTS" in str(memory.get("content") or "")]
                self.assertEqual(len(matches), 1)
                self.assertIn(DEFAULT_AGENTS_MEMORY_TITLE, matches[0]["content"])
                self.assertNotIn(DEFAULT_AGENTS_MEMORY_OLD_TITLE, matches[0]["content"])
            finally:
                service.close()

    def test_existing_bundled_memory_is_upgraded_to_latest_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            ledger = Ledger(config.ledger_path)
            try:
                old_content = DEFAULT_AGENTS_MEMORY_CONTENT.split("多任务或主线/支线并行时", 1)[0]
                candidate = MemoryCandidate(
                    content=old_content,
                    memory_type="user_preference",
                    proposed_action="store",
                    confidence=0.98,
                    importance=0.96,
                    ttl="long",
                    scope="global",
                    evidence=[Evidence(source="bundled_default_memory", quote=DEFAULT_AGENTS_MEMORY_TITLE)],
                    reason="old bundled memory version",
                )
                ledger.add_candidate(
                    candidate,
                    "active",
                    {
                        "status": "active",
                        "source_id": "default:global_agents_collaboration_rules",
                        "version": 1,
                        "title": DEFAULT_AGENTS_MEMORY_TITLE,
                    },
                )
            finally:
                ledger.close()

            service = MemoryService(config)
            try:
                memories = service.list_memories(status="active", memory_type="user_preference", scope="global", limit=20)
                matches = [memory for memory in memories if DEFAULT_AGENTS_MEMORY_TITLE in str(memory.get("content") or "")]
                self.assertEqual(len(matches), 1)
                self.assertIn("多任务或主线/支线并行时", matches[0]["content"])
                self.assertEqual(matches[0]["review_json"]["version"], DEFAULT_AGENTS_MEMORY_VERSION)
            finally:
                service.close()
