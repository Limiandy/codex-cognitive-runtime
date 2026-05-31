import os
import tempfile
import unittest

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.model_client import CodexMiniClient
from codex_cognitive_runtime.review import MemoryReviewer
from codex_cognitive_runtime.schema import Evidence, MemoryCandidate


class ReviewTest(unittest.TestCase):
    def test_rejects_secret_like_content(self):
        os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

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
            reviewer = MemoryReviewer(config, CodexMiniClient(config))
            candidate = MemoryCandidate(
                content="api_key = sk-thisShouldNeverBeStored1234567890",
                memory_type="fact",
                proposed_action="store",
                confidence=0.99,
                importance=0.99,
                ttl="long",
                evidence=[Evidence(source="tool", quote="api_key = sk-thisShouldNeverBeStored1234567890")],
            )
            self.assertEqual(reviewer.review(candidate)["status"], "rejected")
