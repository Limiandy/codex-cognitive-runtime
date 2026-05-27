import os
import tempfile
import unittest
from pathlib import Path

from codex_memory.config import Config
from codex_memory.service import MemoryService


class ServiceTest(unittest.TestCase):
    def test_ingest_fake_model_records_candidate(self):
        os.environ["CODEX_MEMORY_FAKE_MODEL"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(
                model="gpt-5.4-mini",
                state_dir=tmp_path,
                ledger_path=tmp_path / "ledger.sqlite3",
                min_active_confidence=0.82,
                min_quarantine_confidence=0.62,
                duplicate_threshold=0.9,
                max_evidence_quote_chars=500,
            )
            service = MemoryService(config)
            try:
                result = service.ingest_event("manual", {"text": "默认使用中文回答"})
                self.assertEqual(result["candidate_count"], 1)
                memories = service.list_memories(limit=5)
                self.assertTrue(memories)
                self.assertEqual(memories[0]["status"], "active")
                self.assertEqual(memories[0]["review_json"]["storage"], "ledger_only")
            finally:
                service.close()

    def test_user_opt_out_skips_memory_candidate_extraction(self):
        os.environ["CODEX_MEMORY_FAKE_MODEL"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = Config(
                model="gpt-5.4-mini",
                state_dir=tmp_path,
                ledger_path=tmp_path / "ledger.sqlite3",
                min_active_confidence=0.82,
                min_quarantine_confidence=0.62,
                duplicate_threshold=0.9,
                max_evidence_quote_chars=500,
            )
            service = MemoryService(config)
            try:
                result = service.ingest_event("user_message", {"prompt": "不要记忆这条：默认使用中文回答"})
                self.assertEqual(result["candidate_count"], 0)
                self.assertEqual(result["skipped"], "memory_storage_opt_out")
                self.assertEqual(service.list_memories(limit=5), [])
            finally:
                service.close()
