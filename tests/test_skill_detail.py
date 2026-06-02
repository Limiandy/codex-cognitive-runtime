import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.service import MemoryService


def _config(tmp: str) -> Config:
    tmp_path = Path(tmp)
    return Config(
        model="gpt-5.4-mini",
        state_dir=tmp_path,
        ledger_path=tmp_path / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
    )


class SkillDetailTest(unittest.TestCase):
    def test_runtime_skill_public_detail_hides_prompt_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                injection = service.ledger.record_runtime_skill_injection(
                    "用户原始需求里有 /secret/project/path",
                    {
                        "skill_type": "runtime",
                        "name": "software_change_guarded_workflow",
                        "applies_to": "code changes",
                        "goal": "Inspect, edit, verify.",
                        "strategy": ["Inspect repository context.", "Run verification."],
                        "first_action": {"type": "inspect_repository"},
                        "avoid": ["Do not skip verification."],
                        "confidence": 0.9,
                    },
                    session_id="s1",
                    turn_id="t1",
                    cwd="/secret/project/path",
                    project_key="secret-project",
                )
                public = service.get_runtime_skill(str(injection["id"]))
                metadata = public["metadata_json"]
                detail = public["public_detail"]

                self.assertNotIn("prompt_preview", metadata)
                self.assertNotIn("cwd", metadata)
                self.assertNotIn("project_key", metadata)
                self.assertIsNone(public["project_key"])
                self.assertNotIn("/secret/project/path", detail["markdown"])
                self.assertTrue(detail["privacy"]["safe_for_ui"])
                self.assertIn("Inspect, edit, verify.", detail["markdown"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
