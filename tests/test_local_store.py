import os
import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config, load_config
from codex_cognitive_runtime.schema import Evidence, MemoryCandidate
from codex_cognitive_runtime.service import MemoryService


class LocalStoreTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL", None)
        os.environ.pop("CODEX_COGNITIVE_RUNTIME_STATE_DIR", None)

    def test_default_config_uses_ledger_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CODEX_COGNITIVE_RUNTIME_STATE_DIR"] = tmp
            config = load_config()
            self.assertEqual(config.primary_store, "ledger")
            self.assertEqual(config.ledger_path, Path(tmp) / "ledger.sqlite3")
            self.assertEqual(config.baseline_ledger_path, Path(tmp) / "baseline-ledger.sqlite3")
            self.assertIsNone(config.team_ledger_path)

    def test_config_supports_user_baseline_and_team_ledger_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["CODEX_COGNITIVE_RUNTIME_STATE_DIR"] = str(root / "state")
            os.environ["CODEX_COGNITIVE_RUNTIME_USER_LEDGER_PATH"] = str(root / "user.sqlite3")
            os.environ["CODEX_COGNITIVE_RUNTIME_BASELINE_LEDGER_PATH"] = str(root / "baseline.sqlite3")
            os.environ["CODEX_COGNITIVE_RUNTIME_TEAM_LEDGER_PATH"] = str(root / "team.sqlite3")
            try:
                config = load_config()
                self.assertEqual(config.ledger_path, root / "user.sqlite3")
                self.assertEqual(config.baseline_ledger_path, root / "baseline.sqlite3")
                self.assertEqual(config.team_ledger_path, root / "team.sqlite3")
            finally:
                os.environ.pop("CODEX_COGNITIVE_RUNTIME_USER_LEDGER_PATH", None)
                os.environ.pop("CODEX_COGNITIVE_RUNTIME_BASELINE_LEDGER_PATH", None)
                os.environ.pop("CODEX_COGNITIVE_RUNTIME_TEAM_LEDGER_PATH", None)

    def test_active_memory_is_usable_with_ledger_only_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                model="gpt-5.4-mini",
                state_dir=Path(tmp),
                ledger_path=Path(tmp) / "ledger.sqlite3",
                min_active_confidence=0.82,
                min_quarantine_confidence=0.62,
                duplicate_threshold=0.9,
                max_evidence_quote_chars=500,
            )
            service = MemoryService(config)
            try:
                memory_id = service.ledger.add_candidate(
                    MemoryCandidate(
                        content="经验：自有 ledger 数据层必须作为唯一主存储。",
                        memory_type="experience",
                        proposed_action="store",
                        confidence=0.95,
                        importance=0.9,
                        ttl="long",
                        scope="global",
                        domain="memory_system",
                        category="architecture",
                        subcategory="ledger",
                        triggers=["ledger", "主存储"],
                        evidence=[Evidence(source="test", quote="自有 ledger 数据层必须作为主存储")],
                        reason="local store test",
                    ),
                    "active",
                    {"status": "active"},
                )
                service.runtime.sync_memory(memory_id)
                status = service.status()
                self.assertTrue(status["store"]["primary"])
                self.assertNotIn("mempalace", status)
                result = service.store.search("ledger 主存储", limit=3)
                self.assertEqual(result[0]["id"], memory_id)
            finally:
                service.close()
