import tempfile
import unittest
from pathlib import Path

from codex_memory.config import Config
from codex_memory.schema import Evidence, MemoryCandidate
from codex_memory.service import MemoryService


def _service(tmp):
    tmp_path = Path(tmp)
    return MemoryService(
        Config(
            model="gpt-5.4-mini",
            state_dir=tmp_path,
            ledger_path=tmp_path / "ledger.sqlite3",
            min_active_confidence=0.82,
            min_quarantine_confidence=0.62,
            duplicate_threshold=0.9,
            max_evidence_quote_chars=500,
        )
    )


def _candidate(content, memory_type="user_preference", scope="global"):
    return MemoryCandidate(
        content=content,
        memory_type=memory_type,
        proposed_action="store",
        confidence=0.94,
        importance=0.84,
        ttl="long",
        scope=scope,
        evidence=[Evidence(source="user_message", quote=content)],
        reason="runtime skill test",
    )


class RuntimeSkillTest(unittest.TestCase):
    def test_simple_weather_query_does_not_generate_runtime_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                context = service.prompt_context("现在天气怎么样？", cwd=tmp, session_id="s1")
                self.assertNotIn("Runtime Skill:", context)
                self.assertNotIn("Codex Cognitive Runtime context:", context)
                self.assertEqual(context, "")
            finally:
                service.close()

    def test_logo_request_generates_memory_grounded_intake_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                project_key = str(Path(tmp).resolve()).lower()
                service.ledger.add_candidate(
                    _candidate("用户偏好极简、专业、克制的视觉风格。", "user_preference", "global"),
                    "active",
                    {"status": "active", "risk_flags": []},
                )
                service.ledger.add_candidate(
                    _candidate("组织定位是高端 B2B SaaS。", "project_context", "project"),
                    "active",
                    {"status": "active", "risk_flags": []},
                    project_key=project_key,
                )

                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")

                self.assertIn("Runtime Skill: brand_logo_design_intake", context)
                self.assertIn("First action: ask_clarifying_questions", context)
                self.assertIn("品牌名称是什么？", context)
                self.assertIn("极简", context)
                self.assertIn("高端 B2B SaaS", context)
                self.assertNotIn("Codex Memory context:", context)
            finally:
                service.close()

    def test_engineering_request_generates_runtime_skill_and_keeps_guard_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.ledger.add_candidate(
                    _candidate("工程经验：修复 bug 后必须运行项目测试并报告结果。", "experience", "project"),
                    "active",
                    {"status": "active", "risk_flags": []},
                    project_key=str(Path(tmp).resolve()).lower(),
                )

                context = service.prompt_context("帮我修复这个 bug", cwd=tmp, session_id="s1")

                self.assertIn("Runtime Skill: software_change_guarded_workflow", context)
                self.assertIn("Inspect the relevant repository context", context)
                self.assertIn("工程经验", context)
                self.assertIn("Codex Cognitive Runtime context:", context)
                self.assertIn("workflow:", context)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
