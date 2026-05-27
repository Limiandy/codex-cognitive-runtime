import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliDataManagementTest(unittest.TestCase):
    def test_export_prune_and_wipe_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            export_path = Path(tmp) / "export.json"
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_MEMORY_STATE_DIR": tmp, "CODEX_MEMORY_FAKE_MODEL": "1"}
            ingest = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "ingest", "默认使用中文回答"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(ingest.returncode, 0, ingest.stderr)
            runtime_status = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "runtime-status"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(runtime_status.returncode, 0, runtime_status.stderr)
            self.assertIn("active_workflow", json.loads(runtime_status.stdout))
            runtime_status_pretty = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "runtime-status", "--pretty"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(runtime_status_pretty.returncode, 0, runtime_status_pretty.stderr)
            self.assertIn("Codex Memory Runtime Status", runtime_status_pretty.stdout)
            export = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "export", "--output", str(export_path)],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(export.returncode, 0, export.stderr)
            self.assertTrue(export_path.exists())
            self.assertIn("memories", json.loads(export_path.read_text(encoding="utf-8")))
            prune = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "prune-events"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(prune.returncode, 0, prune.stderr)
            wipe_without_confirm = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "wipe"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(wipe_without_confirm.returncode, 2)
            wipe = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "wipe", "--yes"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(wipe.returncode, 0, wipe.stderr)
            self.assertIn("wiped", json.loads(wipe.stdout))


if __name__ == "__main__":
    unittest.main()
