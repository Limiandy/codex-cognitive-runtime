import json
import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.regression_proof import run_regression_proof


class RegressionProofHarnessTest(unittest.TestCase):
    def test_regression_proof_writes_json_and_markdown_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_regression_proof(
                state_dir=root / "state",
                json_report=root / "proof.json",
                markdown_report=root / "proof.md",
                clear_before=True,
            )

            self.assertTrue(report["passed"])
            self.assertTrue(report["fake_model"])
            self.assertFalse(report["external_model_calls"])
            self.assertTrue((root / "proof.json").exists())
            self.assertTrue((root / "proof.md").exists())

            loaded = json.loads((root / "proof.json").read_text(encoding="utf-8"))
            self.assertTrue(loaded["passed"])
            self.assertEqual(
                set(loaded["summary"]["coverage"]),
                {
                    "brand_logo_task",
                    "wechat_mini_program_ui",
                    "generic_frontend_ui",
                    "memory_statement",
                    "wrong_sort_feedback_calibration",
                },
            )

            calibration = [item for item in loaded["scenarios"] if item["name"] == "wrong_sort_feedback_calibration"][0]
            self.assertEqual(calibration["before"]["bad_rank"], 1)
            self.assertLess(calibration["after"]["bad_profile_weight_delta"], 0)
            self.assertTrue(calibration["assertions"]["feedback_attributed_to_seed_skill"])
            self.assertTrue(calibration["assertions"]["after_bad_not_selected"])

            markdown = (root / "proof.md").read_text(encoding="utf-8")
            self.assertIn("Regression Proof Harness Report", markdown)
            self.assertIn("wrong_sort_feedback_calibration", markdown)


if __name__ == "__main__":
    unittest.main()
