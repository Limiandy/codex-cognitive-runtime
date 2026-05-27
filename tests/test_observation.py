import unittest

from codex_memory.observation import normalize_tool_observation


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

    def test_normalizes_patch_payload_as_edit(self):
        observation = normalize_tool_observation(
            {
                "tool_name": "functions.apply_patch",
                "patch": "*** Begin Patch\n*** Update File: src/app.py\n",
                "files_changed": ["src/app.py"],
            }
        )
        self.assertEqual(observation.tool_kind, "edit")
        self.assertEqual(observation.files_changed, ["src/app.py"])

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


if __name__ == "__main__":
    unittest.main()
