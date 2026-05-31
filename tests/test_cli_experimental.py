import json
import os
import subprocess
import sys
import tempfile
import unittest


class CliExperimentalTest(unittest.TestCase):
    def test_experimental_commands_are_hidden_from_default_help(self):
        proc = subprocess.run(
            [sys.executable, "-m", "codex_cognitive_runtime.cli", "--help"],
            cwd=".",
            env={**os.environ, "PYTHONPATH": "src"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("workflow-plan", proc.stdout)
        self.assertNotIn("workflow-simulate", proc.stdout)
        self.assertNotIn("knowledge-build", proc.stdout)
        self.assertNotIn("govern-cognitive", proc.stdout)
        self.assertIn("doctor", proc.stdout)
        self.assertIn("export", proc.stdout)

    def test_experimental_commands_are_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp, "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1"}
            for command in (["workflow-plan", "test task"], ["workflow-simulate", "test task"], ["knowledge-build"]):
                proc = subprocess.run(
                    [sys.executable, "-m", "codex_cognitive_runtime.cli", *command],
                    cwd=".",
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
                self.assertEqual(proc.returncode, 2)
                error = json.loads(proc.stderr)
                self.assertEqual(error["error"], "experimental_cli_disabled")

    def test_experimental_env_allows_workflow_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "PYTHONPATH": "src",
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
                "CODEX_COGNITIVE_RUNTIME_ENABLE_EXPERIMENTAL_CLI": "1",
            }
            proc = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "workflow-plan", "test task"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("workflow_id", json.loads(proc.stdout))

    def test_experimental_help_shows_commands_when_enabled(self):
        proc = subprocess.run(
            [sys.executable, "-m", "codex_cognitive_runtime.cli", "--help"],
            cwd=".",
            env={**os.environ, "PYTHONPATH": "src", "CODEX_COGNITIVE_RUNTIME_ENABLE_EXPERIMENTAL_CLI": "1"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("workflow-plan", proc.stdout)
        self.assertIn("workflow-simulate", proc.stdout)

    def test_regular_command_is_not_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "status"],
                cwd=".",
                env={**os.environ, "PYTHONPATH": "src", "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["primary_store"], "ledger")


if __name__ == "__main__":
    unittest.main()
