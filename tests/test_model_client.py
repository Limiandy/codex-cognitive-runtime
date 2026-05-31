import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.model_client import CodexMiniClient


class ModelClientTest(unittest.TestCase):
    def test_codex_exec_model_call_is_ephemeral_and_prompt_uses_stdin(self):
        os.environ.pop("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL", None)
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

            def fake_run(cmd, **kwargs):
                out_path = Path(cmd[cmd.index("--output-last-message") + 1])
                out_path.write_text(json.dumps({"ok": True}), encoding="utf-8")

                class Result:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return Result()

            with patch("codex_cognitive_runtime.model_client.subprocess.run", side_effect=fake_run) as run:
                result = CodexMiniClient(config).complete_json(
                    "return json",
                    {"ok": True},
                    timeout_seconds=3,
                )

            self.assertEqual(result, {"ok": True})
            cmd = run.call_args.args[0]
            kwargs = run.call_args.kwargs
            self.assertIn("--ephemeral", cmd)
            self.assertIn("--ignore-user-config", cmd)
            self.assertIn("--ignore-rules", cmd)
            self.assertEqual(cmd[-1], "-")
            self.assertNotIn(kwargs["input"], cmd)
            self.assertIn("return json", kwargs["input"])
            self.assertEqual(kwargs["env"]["CODEX_COGNITIVE_RUNTIME_INTERNAL_CALL"], "1")
            self.assertEqual(kwargs["env"]["CODEX_COGNITIVE_RUNTIME_HOOK_DEPTH"], "1")
