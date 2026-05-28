import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_memory.config import Config
from codex_memory.schema import Evidence, MemoryCandidate
from codex_memory.service import MemoryService
from codex_memory.skill_need import SkillNeedDecision


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


def _write_seed_source(root: Path):
    (root / "design").mkdir(parents=True)
    (root / "engineering").mkdir(parents=True)
    (root / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (root / "design" / "design-brand-guardian.md").write_text(
        """---
name: Brand Guardian
description: Expert brand strategist for cohesive visual identity, logos, and brand systems.
color: blue
---
# Brand Guardian Agent Personality

Use brand context, logo constraints, visual identity, and audience fit before producing design directions.
""",
        encoding="utf-8",
    )
    (root / "engineering" / "engineering-code-reviewer.md").write_text(
        """---
name: Code Reviewer
description: Reviews code changes with attention to defects, tests, and maintainability.
color: green
---
# Code Reviewer Agent Personality

Inspect code changes, check test coverage, and point out verification gaps.
""",
        encoding="utf-8",
    )


class RuntimeSkillTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_MEMORY_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_MEMORY_FAKE_MODEL", None)

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

    @patch("codex_memory.skill_need.SkillNeedClassifier._model_classify")
    def test_ambiguous_short_prompt_does_not_call_model(self, model_classify):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                context = service.prompt_context("测试", cwd=tmp, session_id="s1")
                self.assertEqual(context, "")
                model_classify.assert_not_called()
            finally:
                service.close()

    @patch("codex_memory.skill_need.SkillNeedClassifier._model_classify")
    def test_complex_task_uses_model_skill_need_decision(self, model_classify):
        model_classify.return_value = SkillNeedDecision(
            True,
            "generate_runtime_skill",
            "brand_logo_design",
            "brand_design",
            "medium",
            True,
            True,
            "model classified brand design",
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                context = service.prompt_context("请帮我设计一套视觉识别方向", cwd=tmp, session_id="s1")
                self.assertIn("Runtime Skill: brand_logo_design_intake", context)
                model_classify.assert_called_once()
            finally:
                service.close()

    @patch("codex_memory.runtime_skill.RuntimeSkillSynthesizer._model_synthesize")
    def test_runtime_skill_generation_uses_model_synthesizer(self, model_synthesize):
        from codex_memory.runtime_skill import RuntimeSkill

        model_synthesize.return_value = RuntimeSkill(
            name="model_generated_logo_intake",
            applies_to="logo design",
            goal="clarify before design",
            memory_basis_ids=[],
            memory_basis_summary="No clean long-term memories matched this task.",
            strategy=["Ask clarifying questions first.", "Offer directions after clarification."],
            first_action={"type": "ask_clarifying_questions", "questions": ["品牌名称是什么？"]},
            avoid=["Do not generate immediately."],
            confidence=0.8,
            intent="brand_logo_design",
            domain="brand_design",
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                self.assertIn("Runtime Skill: model_generated_logo_intake", context)
                model_synthesize.assert_called_once()
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
                injections = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="audit", status="active", limit=20)
                    if item.get("record_type") == "runtime_skill_injection"
                ]
                self.assertEqual(len(injections), 1)
                metadata = injections[0].get("metadata_json") or {}
                self.assertEqual(metadata["skill"]["name"], "brand_logo_design_intake")
                self.assertTrue(metadata["memory_basis_ids"])
                self.assertEqual(metadata["session_id"], "s1")
            finally:
                service.close()

    def test_seed_skills_provide_cold_start_basis_without_memories(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                seeded = service.seed_skills(source=str(source_path))
                self.assertEqual(seeded["skill_count"], 2)

                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")

                self.assertIn("Runtime Skill: brand_logo_design_intake", context)
                self.assertIn("Seed skill basis:", context)
                self.assertIn("Brand Guardian", context)
                self.assertIn("No clean long-term memory matched", context)
                injections = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="audit", status="active", limit=20)
                    if item.get("record_type") == "runtime_skill_injection"
                ]
                metadata = injections[0].get("metadata_json") or {}
                self.assertEqual(metadata["seed_skill_ids"], ["agency-agents:design/design-brand-guardian.md"])
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
