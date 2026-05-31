import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.engine import MemoryEngine
from codex_cognitive_runtime.logger import info
from codex_cognitive_runtime.security import redact_secrets, sanitize_payload


class _CaptureModel:
    def __init__(self):
        self.prompt = ""

    def complete_json(self, prompt, schema_hint=None):
        self.prompt = prompt
        return {"candidates": []}


def _config(tmp: str) -> Config:
    return Config(
        model="fake",
        state_dir=Path(tmp),
        ledger_path=Path(tmp) / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
    )


class SecurityTest(unittest.TestCase):
    def test_payload_is_redacted_before_model_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = _CaptureModel()
            engine = MemoryEngine(_config(tmp), model)
            engine.extract(
                "user_message",
                {
                    "prompt": "token = github_pat_abcdefghijklmnopqrstuvwxyz1234567890",
                    "OPENAI_API_KEY": "sk-abcdefghijklmnopqrstuvwxyz",
                    "large_blob": "x" * 100,
                },
            )
            self.assertIn("[REDACTED]", model.prompt)
            self.assertNotIn("github_pat_abcdefghijklmnopqrstuvwxyz1234567890", model.prompt)
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", model.prompt)
            self.assertIn("_omitted_keys", model.prompt)

    def test_redaction_covers_common_secret_shapes(self):
        text = "\n".join(
            [
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
                "aws=AKIA1234567890ABCDEF",
                "npm=npm_abcdefghijklmnopqrstuvwxyz123456",
                "jwt=eyJaaaaaaaaaaa.bbbbbbbbbbbbb.ccccccccccccc",
                "google=AIzaabcdefghijklmnopqrstuvwxyz123456",
            ]
        )
        redacted = redact_secrets(text)
        self.assertNotIn("AKIA1234567890ABCDEF", redacted)
        self.assertNotIn("npm_abcdefghijklmnopqrstuvwxyz123456", redacted)
        self.assertNotIn("Bearer abcdefghijklmnopqrstuvwxyz123456", redacted)
        self.assertNotIn("AIzaabcdefghijklmnopqrstuvwxyz123456", redacted)

    def test_logger_writes_redacted_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CODEX_COGNITIVE_RUNTIME_LOG_DIR"] = tmp
            try:
                info("security test", payload_summary=sanitize_payload({"prompt": "api_key=sk-abcdefghijklmnopqrstuvwxyz"}))
                log_text = (Path(tmp) / "debug.jsonl").read_text(encoding="utf-8")
                record = json.loads(log_text.splitlines()[0])
                self.assertIn("[REDACTED]", json.dumps(record))
                self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", log_text)
            finally:
                os.environ.pop("CODEX_COGNITIVE_RUNTIME_LOG_DIR", None)

    def test_hook_received_log_uses_payload_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "PYTHONPATH": "src",
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": str(Path(tmp) / "state"),
                "CODEX_COGNITIVE_RUNTIME_LOG_DIR": str(Path(tmp) / "logs"),
            }
            payload = {
                "hook_event_name": "PreCompact",
                "prompt": "token=supersecretvalue1234567890",
                "customer": "private customer",
            }
            proc = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.hooks", "precompact"],
                cwd=".",
                env=env,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            log_text = (Path(tmp) / "logs" / "debug.jsonl").read_text(encoding="utf-8")
            self.assertIn("payload_summary", log_text)
            self.assertNotIn("supersecretvalue1234567890", log_text)
            self.assertNotIn("private customer", log_text)
