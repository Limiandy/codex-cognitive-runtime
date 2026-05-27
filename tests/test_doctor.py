import json
import os
import subprocess
import sys
import tempfile
import unittest

from codex_memory.doctor import config_text_is_portable, run_doctor
from codex_memory.config import Config


def _config(tmp: str) -> Config:
    from pathlib import Path

    return Config(
        model="gpt-5.4-mini",
        state_dir=Path(tmp),
        ledger_path=Path(tmp) / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
    )


class DoctorTest(unittest.TestCase):
    def test_doctor_cli_returns_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "doctor"],
                cwd=".",
                env={**os.environ, "PYTHONPATH": "src", "CODEX_MEMORY_STATE_DIR": tmp},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertIn("checks", result)
            self.assertIn("summary", result)
            self.assertIn("sqlite_ledger", result["checks"])
            self.assertIn("python_version", result["checks"])
            self.assertIn("sqlite_version", result["checks"])
            self.assertEqual(result["checks"]["sqlite_ledger"]["level"], "fatal")
            self.assertIn("fix_hint", result["checks"]["codex_cli"])

    def test_doctor_checks_sqlite_and_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_doctor(_config(tmp))
            self.assertTrue(result["checks"]["sqlite_ledger"]["ok"])
            self.assertTrue(result["checks"]["mcp_server"]["ok"])
            self.assertEqual(result["checks"]["model_smoke"]["level"], "info")

    def test_doctor_privacy_report_lists_storage_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "doctor", "--privacy"],
                cwd=".",
                env={**os.environ, "PYTHONPATH": "src", "CODEX_MEMORY_STATE_DIR": tmp},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["privacy"]["event_storage"], "sanitized")
            self.assertIn("ledger_path", result["privacy"])

    def test_raw_event_storage_is_warn_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            config = Config(**{**config.__dict__, "store_raw_events": True})
            result = run_doctor(config)
            self.assertFalse(result["checks"]["raw_event_storage"]["ok"])
            self.assertEqual(result["checks"]["raw_event_storage"]["level"], "warn")
            self.assertEqual(result["summary"]["warn_failed"], 1)

    def test_portable_config_detects_absolute_user_paths(self):
        self.assertFalse(config_text_is_portable('{"command": "/Users/limengkai/plugins/codex-memory/script"}'))
        self.assertTrue(config_text_is_portable('"$HOME/plugins/codex-memory/scripts/codex-memory-mcp"'))


if __name__ == "__main__":
    unittest.main()
