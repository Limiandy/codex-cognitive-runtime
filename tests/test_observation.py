import unittest
import os
import tempfile
from pathlib import Path

from codex_cognitive_runtime.observation import normalize_tool_observation


class ToolObservationNormalizerTest(unittest.TestCase):
    def test_normalizes_nested_exec_payload(self):
        observation = normalize_tool_observation(
            {
                "tool_name": "functions.exec_command",
                "tool_input": {"cmd": "rg failing_test tests"},
                "result": {"stdout": "tests/test_app.py:12", "exit_code": 0},
            }
        )
        self.assertEqual(observation.tool_kind, "inspect")
        self.assertEqual(observation.command, "rg failing_test tests")
        self.assertEqual(observation.exit_code, 0)
        self.assertEqual(observation.schema_version, 1)
        self.assertGreaterEqual(observation.confidence, 0.8)
        self.assertEqual(observation.source_fields["command"], "tool_input.cmd")
        self.assertEqual(observation.exit_code_source, "result.exit_code")
        self.assertIn("matched inspect signal", observation.raw_kind_reason)

    def test_normalizes_patch_payload_as_edit(self):
        observation = normalize_tool_observation(
            {
                "tool_name": "functions.apply_patch",
                "patch": "*** Begin Patch\n*** Update File: src/app.py\n+python3 -m unittest discover -s tests -v\n",
                "files_changed": ["src/app.py"],
            }
        )
        self.assertEqual(observation.tool_kind, "edit")
        self.assertEqual(observation.files_changed, ["src/app.py"])
        self.assertIn("files_changed", observation.source_fields)

    def test_normalizes_failed_verification_payload(self):
        observation = normalize_tool_observation(
            {
                "tool": "functions.exec_command",
                "command": "python3 -m unittest discover -s tests -v",
                "stdout": "FAILED (failures=1)",
                "exit_code": 1,
            }
        )
        self.assertEqual(observation.tool_kind, "verify")
        self.assertTrue(observation.evidence_summary["failed"])
        self.assertEqual(observation.exit_code_source, "exit_code")

    def test_typecheck_commands_are_verification(self):
        for command in ("pnpm run typecheck", "npm run typecheck", "vue-tsc -b --noEmit"):
            with self.subTest(command=command):
                observation = normalize_tool_observation({"tool_name": "functions.exec_command", "cmd": command})
                self.assertEqual(observation.tool_kind, "verify")
                self.assertGreaterEqual(observation.confidence, 0.9)

    def test_compileall_is_verification(self):
        observation = normalize_tool_observation({"tool_name": "functions.exec_command", "cmd": "PYTHONPATH=src python3 -m compileall -q src tests"})
        self.assertEqual(observation.tool_kind, "verify")
        self.assertGreaterEqual(observation.confidence, 0.9)

    def test_stdout_only_command_mention_is_low_confidence(self):
        observation = normalize_tool_observation(
            {
                "tool_name": "functions.exec_command",
                "stdout": "Suggested command: python3 -m unittest discover -s tests -v\nOK",
                "exit_code": 0,
            }
        )
        self.assertEqual(observation.tool_kind, "verify")
        self.assertLess(observation.confidence, 0.8)
        self.assertNotIn("command", observation.source_fields)

    def test_custom_verify_command_from_env(self):
        previous = os.environ.get("CODEX_COGNITIVE_RUNTIME_VERIFY_COMMANDS")
        os.environ["CODEX_COGNITIVE_RUNTIME_VERIFY_COMMANDS"] = "make verify,tox"
        try:
            observation = normalize_tool_observation({"tool_name": "functions.exec_command", "cmd": "make verify"})
            self.assertEqual(observation.tool_kind, "verify")
            self.assertGreaterEqual(observation.confidence, 0.9)
            self.assertIn("custom verify", observation.raw_kind_reason)
        finally:
            if previous is None:
                os.environ.pop("CODEX_COGNITIVE_RUNTIME_VERIFY_COMMANDS", None)
            else:
                os.environ["CODEX_COGNITIVE_RUNTIME_VERIFY_COMMANDS"] = previous

    def test_custom_rules_from_project_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".codex-cognitive-runtime.json").write_text(
                '{"runtime_observer": {"verify_commands": ["pnpm check"], "inspect_commands": ["fd "]}}',
                encoding="utf-8",
            )
            verify = normalize_tool_observation({"tool_name": "functions.exec_command", "cmd": "pnpm check", "cwd": tmp})
            inspect = normalize_tool_observation({"tool_name": "functions.exec_command", "cmd": "fd runtime src", "cwd": tmp})
            self.assertEqual(verify.tool_kind, "verify")
            self.assertEqual(inspect.tool_kind, "inspect")


if __name__ == "__main__":
    unittest.main()
