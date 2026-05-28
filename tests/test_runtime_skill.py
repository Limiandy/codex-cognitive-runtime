import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_memory.config import Config
from codex_memory.seed_skills import is_seed_skill_eligible, relevant_seed_skills
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
                recalls = service.ledger.conn.execute("SELECT COUNT(*) FROM recall_events").fetchone()[0]
                self.assertEqual(recalls, 0)
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

    def test_runtime_skill_model_calls_use_short_timeout(self):
        class TimeoutModel:
            def __init__(self):
                self.timeouts = []

            def complete_json(self, prompt, schema, timeout_seconds=None):
                self.timeouts.append(timeout_seconds)
                if "Classify runtime skill need" in prompt:
                    return {
                        "skill_needed": True,
                        "mode": "generate_runtime_skill",
                        "intent": "brand_logo_design",
                        "domain": "brand_design",
                        "complexity": "medium",
                        "requires_memory": True,
                        "requires_clarification": True,
                        "reason": "test",
                    }
                return {
                    "name": "timeout_checked_logo",
                    "applies_to": "logo design",
                    "goal": "Clarify before design.",
                    "memory_basis_ids": [],
                    "seed_skill_ids": [],
                    "strategy": ["Ask questions first.", "Offer directions after clarification."],
                    "first_action": {"type": "ask_clarifying_questions", "questions": ["品牌名称是什么？"]},
                    "avoid": ["Do not generate immediately."],
                    "confidence": 0.8,
                }

        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            service.model = TimeoutModel()
            try:
                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                self.assertIn("Runtime Skill: timeout_checked_logo", context)
                self.assertEqual(service.model.timeouts, [12, 12])
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

    @patch("codex_memory.runtime_skill.RuntimeSkillSynthesizer._model_synthesize")
    def test_runtime_skill_cache_avoids_repeated_synthesis(self, model_synthesize):
        from codex_memory.runtime_skill import RuntimeSkill

        model_synthesize.return_value = RuntimeSkill(
            name="cached_logo_intake",
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
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s2")
                model_synthesize.assert_called_once()
                injections = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ]
                self.assertEqual(len(injections), 2)
                self.assertTrue(any((item["metadata_json"].get("latency") or {}).get("cache_hit") for item in injections))
            finally:
                service.close()

    @patch("codex_memory.runtime_skill.RuntimeSkillSynthesizer._model_synthesize")
    def test_runtime_skill_cache_invalidates_when_basis_changes(self, model_synthesize):
        from codex_memory.runtime_skill import RuntimeSkill

        model_synthesize.return_value = RuntimeSkill(
            name="cached_logo_intake",
            applies_to="logo design",
            goal="clarify before design",
            memory_basis_ids=[],
            memory_basis_summary="No clean long-term memories matched this task.",
            durable_skill_ids=[],
            durable_skill_basis_summary="No active durable skills matched this task.",
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
                skill = service.ledger.record_cognitive_record(
                    "skill",
                    "dynamic_skill",
                    "dyn:cache-logo",
                    "Dynamic skill: logo workflow.",
                    "active",
                    "global",
                    domain="brand_design",
                    metadata={"skill_type": "dynamic_skill", "title": "Logo workflow", "trigger": ["logo"], "procedure": ["Ask for brand name."], "success_count": 0, "failure_count": 0},
                )
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s2")
                self.assertEqual(model_synthesize.call_count, 1)
                service.ledger.adjust_cognitive_record_strength(str(skill["id"]), 0.1, {"success_count": 1, "failure_count": 0})
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s3")
                self.assertEqual(model_synthesize.call_count, 2)
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
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ]
                self.assertEqual(len(injections), 1)
                metadata = injections[0].get("metadata_json") or {}
                self.assertEqual(metadata["skill"]["name"], "brand_logo_design_intake")
                self.assertTrue(metadata["memory_basis_ids"])
                self.assertEqual(metadata["session_id"], "s1")
                self.assertEqual(injections[0]["layer"], "runtime_skill")
                self.assertEqual(injections[0]["record_type"], "injection")
            finally:
                service.close()

    def test_seed_skills_provide_cold_start_basis_without_memories(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                seeded = service.seed_skills(source=str(source_path), activate=True)
                self.assertTrue(seeded["ok"])
                self.assertEqual(seeded["skill_count"], 2)
                seed_record = service.ledger.get_cognitive_record("agency-agents:design/design-brand-guardian.md")
                seed_metadata = seed_record["metadata_json"]
                self.assertEqual(seed_metadata["trust_level"], "external_seed")
                self.assertEqual(seed_metadata["license"], "MIT")
                self.assertEqual(seed_metadata["trust_state"], "unverified")
                self.assertTrue(is_seed_skill_eligible(seed_record))
                self.assertIn("MIT", seed_metadata["license_detected"])
                self.assertEqual(len(seed_metadata["content_sha256"]), 64)
                self.assertFalse(seed_metadata["source_verified"])

                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")

                self.assertIn("Runtime Skill: brand_logo_design_intake", context)
                self.assertIn("Seed skill basis:", context)
                self.assertIn("Brand Guardian", context)
                self.assertIn("No clean long-term memory matched", context)
                injections = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ]
                metadata = injections[0].get("metadata_json") or {}
                self.assertEqual(metadata["seed_skill_ids"], ["agency-agents:design/design-brand-guardian.md"])
            finally:
                service.close()

    def test_local_seed_skills_without_commit_default_to_candidate(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                seeded = service.seed_skills(source=str(source_path), category="design")
                self.assertTrue(seeded["ok"])
                seed_record = service.ledger.get_cognitive_record("agency-agents:design/design-brand-guardian.md")
                self.assertEqual(seed_record["status"], "candidate")
                self.assertEqual(seed_record["metadata_json"]["trust_state"], "unverified")
                self.assertFalse(is_seed_skill_eligible(seed_record))
            finally:
                service.close()

    def test_seed_skills_with_too_many_failures_are_not_retrieved(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                service.seed_skills(source=str(source_path), activate=True)
                service.ledger.patch_cognitive_record_metadata(
                    "agency-agents:design/design-brand-guardian.md",
                    {"failure_count": 3, "success_count": 0},
                )
                skills = relevant_seed_skills(service.ledger, "帮我画一个品牌 logo")
                self.assertFalse([item for item in skills if item["id"] == "agency-agents:design/design-brand-guardian.md"])
            finally:
                service.close()

    def test_natural_feedback_updates_runtime_skill_and_seed_skill_counts(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                service.seed_skills(source=str(source_path), activate=True)
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                feedback = service.apply_natural_feedback("很好，正是这个方向", session_id="s1")
                self.assertEqual(feedback["runtime_skill_feedback"]["metadata_json"]["outcome"], "positive")
                seed = service.ledger.get_cognitive_record("agency-agents:design/design-brand-guardian.md")
                metadata = seed["metadata_json"]
                self.assertEqual(metadata["reuse_count"], 1)
                self.assertEqual(metadata["success_count"], 1)
                self.assertEqual(metadata["failure_count"], 0)
                injection = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ][0]
                self.assertEqual(injection["metadata_json"]["feedback_status"], "positive")
                self.assertEqual(injection["metadata_json"]["feedback_dimensions"]["skill_relevance"], "positive")
                self.assertEqual(feedback["runtime_skill_feedback"]["layer"], "runtime_skill")
                self.assertEqual(feedback["runtime_skill_feedback"]["record_type"], "feedback")
                self.assertEqual(feedback["runtime_skill_feedback"]["metadata_json"]["evidence"]["feedback_target"], "skill_strategy")
            finally:
                service.close()

    def test_generic_natural_feedback_records_feedback_without_seed_adjustment(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                service.seed_skills(source=str(source_path), activate=True)
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                feedback = service.apply_natural_feedback("很好", session_id="s1")

                self.assertEqual(feedback["runtime_skill_feedback"]["metadata_json"]["outcome"], "positive")
                seed = service.ledger.get_cognitive_record("agency-agents:design/design-brand-guardian.md")
                metadata = seed["metadata_json"]
                self.assertEqual(metadata["reuse_count"], 0)
                self.assertEqual(metadata["success_count"], 0)
                evidence = feedback["runtime_skill_feedback"]["metadata_json"]["evidence"]
                self.assertFalse(evidence["adjust_seed_skill_strength"])
                self.assertEqual(evidence["feedback_target"], "final_result")
            finally:
                service.close()

    def test_question_feedback_targets_first_action_and_updates_seed_skill(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                service.seed_skills(source=str(source_path), activate=True)
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                feedback = service.apply_natural_feedback("这个提问方式很好", session_id="s1")

                metadata = feedback["runtime_skill_feedback"]["metadata_json"]
                self.assertEqual(metadata["evidence"]["feedback_target"], "first_action")
                self.assertEqual(metadata["dimensions"]["first_action_quality"], "positive")
                seed = service.ledger.get_cognitive_record("agency-agents:design/design-brand-guardian.md")
                self.assertEqual(seed["metadata_json"]["reuse_count"], 1)
            finally:
                service.close()

    def test_runtime_skill_feedback_persists_classifier_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                service.seed_skills(source=str(source_path), activate=True)
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                feedback = service.apply_natural_feedback("这个模板不适合", session_id="s1")
                metadata = feedback["runtime_skill_feedback"]["metadata_json"]
                self.assertEqual(metadata["dimensions"]["seed_skill_quality"], "negative")
                self.assertEqual(metadata["dimensions"]["final_result_quality"], "unknown")
            finally:
                service.close()

    def test_manual_feedback_uses_ledger_dimension_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                injection = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ][0]
                feedback = service.runtime_skill_feedback(str(injection["id"]), "negative", target="durable_skill", note="bad durable")
                self.assertEqual(feedback["metadata_json"]["dimensions"]["durable_skill_quality"], "negative")
                self.assertEqual(feedback["metadata_json"]["dimensions"]["seed_skill_quality"], "unknown")
            finally:
                service.close()

    def test_natural_feedback_ignores_old_runtime_skill_injection(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                service.seed_skills(source=str(source_path), activate=True)
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                injection = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ][0]
                service.ledger.conn.execute(
                    "UPDATE cognitive_records SET created_at=? WHERE id=?",
                    ("2020-01-01T00:00:00Z", injection["id"]),
                )
                service.ledger.conn.commit()

                feedback = service.apply_natural_feedback("很好，正是这个方向", session_id="s1")

                self.assertNotIn("runtime_skill_feedback", feedback)
                seed = service.ledger.get_cognitive_record("agency-agents:design/design-brand-guardian.md")
                self.assertEqual(seed["metadata_json"]["reuse_count"], 0)
            finally:
                service.close()

    def test_latest_runtime_skill_injection_reads_legacy_audit_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                legacy = service.ledger.record_cognitive_record(
                    "audit",
                    "runtime_skill_injection",
                    None,
                    "legacy runtime skill injection",
                    "active",
                    "session",
                    session_id="s1",
                    metadata={"turn_id": "t1", "seed_skill_ids": []},
                    source_kind="runtime_skill_injection",
                )

                found = service.ledger.latest_runtime_skill_injection(session_id="s1", turn_id="t1")

                self.assertEqual(found["id"], legacy["id"])
                self.assertEqual(found["layer"], "audit")
                self.assertEqual(found["record_type"], "runtime_skill_injection")
            finally:
                service.close()

    def test_negative_feedback_suppresses_seed_skill_after_repeated_failures(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            service = _service(tmp)
            try:
                service.seed_skills(source=str(source_path), activate=True)
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                for _ in range(3):
                    service.apply_natural_feedback("这个方法不对，不要这样", session_id="s1")

                seed = service.ledger.get_cognitive_record("agency-agents:design/design-brand-guardian.md")
                metadata = seed["metadata_json"]
                self.assertEqual(metadata["failure_count"], 3)
                self.assertEqual(metadata["trust_state"], "suppressed")
                self.assertEqual(seed["status"], "suppressed")
                self.assertFalse(relevant_seed_skills(service.ledger, "帮我画一个品牌 logo"))
            finally:
                service.close()

    @patch("codex_memory.runtime_skill.RuntimeSkillSynthesizer._model_synthesize")
    def test_runtime_skill_reviewer_filters_unknown_basis_ids(self, model_synthesize):
        from codex_memory.runtime_skill import RuntimeSkill

        model_synthesize.return_value = RuntimeSkill(
            name="bad_basis_logo",
            applies_to="logo design",
            goal="clarify before design",
            memory_basis_ids=["mem_unknown"],
            memory_basis_summary="No clean long-term memories matched this task.",
            strategy=["Ask clarifying questions first.", "Offer directions after clarification."],
            first_action={"type": "ask_clarifying_questions", "questions": ["品牌名称是什么？"]},
            seed_skill_ids=["agency-agents:missing.md"],
            confidence=0.8,
            intent="brand_logo_design",
            domain="brand_design",
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                injection = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ][0]
                metadata = injection["metadata_json"]
                self.assertEqual(metadata["memory_basis_ids"], [])
                self.assertEqual(metadata["seed_skill_ids"], [])
                self.assertIn("filtered_unknown_memory_basis", metadata["review"]["reasons"])
            finally:
                service.close()

    @patch("codex_memory.runtime_skill.RuntimeSkillSynthesizer._model_synthesize")
    def test_runtime_skill_reviewer_corrects_missing_clarification_action(self, model_synthesize):
        from codex_memory.runtime_skill import RuntimeSkill

        model_synthesize.return_value = RuntimeSkill(
            name="logo_without_questions",
            applies_to="logo design",
            goal="clarify before design",
            memory_basis_ids=[],
            memory_basis_summary="No clean long-term memories matched this task.",
            strategy=["Ask clarifying questions first.", "Offer directions after clarification."],
            first_action={"type": "proceed_or_clarify", "questions": []},
            confidence=0.8,
            intent="brand_logo_design",
            domain="brand_design",
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                self.assertIn("First action: ask_clarifying_questions", context)
                injection = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ][0]
                self.assertEqual(injection["metadata_json"]["review"]["status"], "fallback")
            finally:
                service.close()

    @patch("codex_memory.runtime_skill.RuntimeSkillSynthesizer._model_synthesize")
    def test_runtime_skill_reviewer_fallbacks_unbacked_preference_claims(self, model_synthesize):
        from codex_memory.runtime_skill import RuntimeSkill

        model_synthesize.return_value = RuntimeSkill(
            name="unbacked_preference_logo",
            applies_to="logo design",
            goal="Use according to your preferences to shape the logo.",
            memory_basis_ids=[],
            memory_basis_summary="No clean long-term memories matched this task.",
            strategy=["According to your preferences, use a premium brand style.", "Create logo directions."],
            first_action={"type": "ask_clarifying_questions", "questions": ["品牌名称是什么？"]},
            confidence=0.8,
            intent="brand_logo_design",
            domain="brand_design",
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                self.assertIn("Runtime Skill:", context)
                self.assertNotIn("according to your preferences", context.lower())
                injection = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ][0]
                self.assertIn("removed_unbacked_user_or_org_claims", injection["metadata_json"]["review"]["reasons"])
            finally:
                service.close()

    @patch("codex_memory.runtime_skill.RuntimeSkillSynthesizer._model_synthesize")
    def test_runtime_skill_reviewer_drops_secret_like_skill(self, model_synthesize):
        from codex_memory.runtime_skill import RuntimeSkill

        model_synthesize.return_value = RuntimeSkill(
            name="secret_skill",
            applies_to="logo design",
            goal="clarify before design",
            memory_basis_ids=[],
            memory_basis_summary="No clean long-term memories matched this task.",
            strategy=["Use token api_key=secret-value before acting.", "Ask one question."],
            first_action={"type": "ask_clarifying_questions", "questions": ["品牌名称是什么？"]},
            confidence=0.8,
            intent="brand_logo_design",
            domain="brand_design",
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                self.assertNotIn("Runtime Skill:", context)
                injections = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ]
                self.assertEqual(injections, [])
            finally:
                service.close()

    def test_runtime_skill_reviewer_removes_conflicting_seed_basis(self):
        from codex_memory.runtime_skill import RuntimeSkill, RuntimeSkillReviewer

        skill = RuntimeSkill(
            name="conflicting_logo",
            applies_to="logo design",
            goal="clarify before design",
            memory_basis_ids=["mem_1"],
            memory_basis_summary="用户偏好极简，避免复杂渐变。",
            seed_skill_ids=["seed_1"],
            seed_skill_basis_summary="Use complex gradient logo systems.",
            strategy=["Ask clarifying questions first.", "Offer restrained directions."],
            first_action={"type": "ask_clarifying_questions", "questions": ["品牌名称是什么？"]},
            confidence=0.8,
            intent="brand_logo_design",
            domain="brand_design",
        )
        decision = SkillNeedDecision(True, "generate_runtime_skill", "brand_logo_design", "brand_design", "medium", True, True, "test")
        review = RuntimeSkillReviewer().review(
            skill,
            decision,
            {
                "memories": [{"id": "mem_1"}],
                "durable_skills": [],
                "seed_skills": [{"id": "seed_1"}],
            },
        )
        self.assertEqual(review["skill"].seed_skill_ids, [])
        self.assertIn("seed_skill_conflicts_with_higher_priority_basis", review["reasons"])

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

    def test_active_durable_skill_participates_in_runtime_skill_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                candidate = service.ledger.record_cognitive_record(
                    "skill",
                    "dynamic_skill",
                    "dyn:logo",
                    "Dynamic skill: logo intake workflow.",
                    "candidate",
                    "global",
                    domain="brand_design",
                    category="workflow",
                    metadata={
                        "skill_type": "dynamic_skill",
                        "title": "Logo intake workflow",
                        "trigger": ["logo", "品牌"],
                        "procedure": ["Ask for brand name before visual directions.", "Offer restrained visual directions."],
                        "success_count": 1,
                        "failure_count": 0,
                        "reuse_count": 0,
                        "review_required": True,
                    },
                )
                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                self.assertNotIn("Durable skill basis:", context)

                service.promote_dynamic_skill(str(candidate["id"]), note="validated")
                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s2")

                self.assertIn("Durable skill basis:", context)
                injection = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection" and (item.get("metadata_json") or {}).get("session_id") == "s2"
                ][0]
                metadata = injection["metadata_json"]
                self.assertEqual(metadata["durable_skill_ids"], [candidate["id"]])
                self.assertEqual(metadata["review"]["basis_precedence"], "memory_over_durable_over_seed")
            finally:
                service.close()

    def test_durable_skill_feedback_updates_strength_and_can_suppress(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                skill = service.ledger.record_cognitive_record(
                    "skill",
                    "dynamic_skill",
                    "dyn:strategy",
                    "Dynamic skill: strategy workflow.",
                    "active",
                    "global",
                    domain="brand_design",
                    metadata={
                        "skill_type": "dynamic_skill",
                        "title": "Strategy workflow",
                        "trigger": ["logo", "品牌"],
                        "procedure": ["Choose a strategy direction."],
                        "success_count": 0,
                        "failure_count": 0,
                        "reuse_count": 0,
                    },
                )
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                service.apply_natural_feedback("这个方向很好", session_id="s1")
                updated = service.ledger.get_cognitive_record(str(skill["id"]))
                self.assertEqual(updated["metadata_json"]["reuse_count"], 1)
                self.assertEqual(updated["metadata_json"]["success_count"], 1)

                for _ in range(3):
                    service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                    service.apply_natural_feedback("这个方向不对，不要这样", session_id="s1")
                suppressed = service.ledger.get_cognitive_record(str(skill["id"]))
                self.assertEqual(suppressed["status"], "suppressed")
                stats = service.dynamic_skill_stats()
                self.assertGreaterEqual(stats["by_status"].get("suppressed", 0), 1)
                self.assertTrue(any(item["id"] == skill["id"] for item in stats["needs_review"]))
            finally:
                service.close()

    def test_feedback_classifier_targets_do_not_over_adjust(self):
        from codex_memory.feedback_classifier import RuntimeSkillFeedbackClassifier

        classifier = RuntimeSkillFeedbackClassifier()
        self.assertEqual(classifier.classify("很好").feedback_target, "final_result")
        self.assertFalse(classifier.classify("很好").adjust_seed_skill_strength)
        self.assertEqual(classifier.classify("这个方向很好").feedback_target, "skill_strategy")
        self.assertTrue(classifier.classify("这个方向很好").adjust_seed_skill_strength)
        self.assertEqual(classifier.classify("这个提问方式很好").feedback_target, "first_action")
        self.assertEqual(classifier.classify("先问问题是对的").feedback_target, "first_action")
        self.assertEqual(classifier.classify("不是我的偏好").feedback_target, "memory_basis")
        self.assertEqual(classifier.classify("不是我的组织定位").feedback_target, "memory_basis")
        self.assertFalse(classifier.classify("不是我的偏好").adjust_seed_skill_strength)
        self.assertEqual(classifier.classify("这个模板不适合").feedback_target, "seed_skill")
        self.assertEqual(classifier.classify("不要用这个模板").feedback_target, "seed_skill")
        self.assertEqual(classifier.classify("dynamic skill 过时了").feedback_target, "durable_skill")
        self.assertEqual(classifier.classify("方向对，但问题太多").outcome, "mixed")
        self.assertFalse(classifier.classify("方向对，但问题太多").adjust_seed_skill_strength)

    def test_feedback_classifier_uses_model_for_complex_feedback(self):
        from codex_memory.feedback_classifier import RuntimeSkillFeedbackClassifier

        class FeedbackModel:
            def __init__(self):
                self.calls = []

            def complete_json(self, prompt, schema, timeout_seconds=None):
                self.calls.append(timeout_seconds)
                return {
                    "outcome": "positive",
                    "feedback_target": "skill_strategy",
                    "confidence": 0.9,
                    "reason": "strategy was explicitly praised",
                }

        model = FeedbackModel()
        decision = RuntimeSkillFeedbackClassifier(model).classify("方向对，但问题太多")
        self.assertEqual(model.calls, [12])
        self.assertEqual(decision.feedback_target, "skill_strategy")
        self.assertTrue(decision.adjust_seed_skill_strength)

    def test_feedback_classifier_can_disable_model(self):
        from codex_memory.feedback_classifier import RuntimeSkillFeedbackClassifier

        class FeedbackModel:
            def complete_json(self, prompt, schema, timeout_seconds=None):
                raise AssertionError("model should not be called")

        decision = RuntimeSkillFeedbackClassifier(FeedbackModel(), enable_model=False).classify("方向对，但问题太多")
        self.assertEqual(decision.outcome, "mixed")
        self.assertFalse(decision.adjust_seed_skill_strength)

    def test_feedback_classifier_low_confidence_model_does_not_adjust_strength(self):
        from codex_memory.feedback_classifier import RuntimeSkillFeedbackClassifier

        class FeedbackModel:
            def complete_json(self, prompt, schema, timeout_seconds=None):
                return {
                    "outcome": "positive",
                    "feedback_target": "skill_strategy",
                    "confidence": 0.4,
                    "reason": "too uncertain",
                }

        decision = RuntimeSkillFeedbackClassifier(FeedbackModel()).classify("方向对，但问题太多")
        self.assertFalse(decision.adjust_seed_skill_strength)
        self.assertFalse(decision.adjust_durable_skill_strength)

    def test_service_uses_feedback_model_for_multitarget_natural_feedback(self):
        class FeedbackModel:
            def __init__(self):
                self.feedback_calls = 0

            def complete_json(self, prompt, schema, timeout_seconds=None):
                if "Classify Runtime Skill feedback" in prompt:
                    self.feedback_calls += 1
                    return {
                        "outcome": "positive",
                        "feedback_target": "skill_strategy",
                        "confidence": 0.9,
                        "reason": "model attribution",
                    }
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                service.model = FeedbackModel()
                feedback = service.apply_natural_feedback("方向对，但问题太多", session_id="s1")
                self.assertEqual(service.model.feedback_calls, 1)
                self.assertEqual(feedback["runtime_skill_feedback"]["metadata_json"]["evidence"]["feedback_target"], "skill_strategy")
            finally:
                service.close()

    def test_service_respects_disabled_feedback_model_for_natural_feedback(self):
        class FeedbackModel:
            def complete_json(self, prompt, schema, timeout_seconds=None):
                raise AssertionError("feedback model should be disabled")

        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(
                Config(
                    model="gpt-5.4-mini",
                    state_dir=Path(tmp),
                    ledger_path=Path(tmp) / "ledger.sqlite3",
                    min_active_confidence=0.82,
                    min_quarantine_confidence=0.62,
                    duplicate_threshold=0.9,
                    max_evidence_quote_chars=500,
                    enable_feedback_model=False,
                )
            )
            try:
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                service.model = FeedbackModel()
                feedback = service.apply_natural_feedback("方向对，但问题太多", session_id="s1")
                evidence = feedback["runtime_skill_feedback"]["metadata_json"]["evidence"]
                self.assertEqual(evidence["feedback_target"], "first_action")
                self.assertFalse(evidence["adjust_seed_skill_strength"])
            finally:
                service.close()

    def test_runtime_skill_reviewer_filters_conflicting_seed_basis(self):
        from codex_memory.runtime_skill import RuntimeSkill, RuntimeSkillReviewer

        memory = {
            "id": "mem_1",
            "content": "用户偏好极简克制，不喜欢复杂渐变。",
            "memory_type": "user_preference",
        }
        seed = {
            "id": "seed_1",
            "metadata_json": {
                "name": "Loud Brand Template",
                "description": "Use complex gradients and noisy symbols.",
            },
        }
        skill = RuntimeSkill(
            name="logo_strategy",
            applies_to="logo design",
            goal="Design with memory first.",
            memory_basis_ids=["mem_1"],
            memory_basis_summary="用户偏好极简克制，不喜欢复杂渐变。",
            durable_skill_ids=[],
            durable_skill_basis_summary="",
            seed_skill_ids=["seed_1"],
            seed_skill_basis_summary="Use complex gradients and noisy symbols.",
            strategy=["Use memory first.", "Ask for missing brand constraints."],
            first_action={"type": "ask_clarifying_questions", "questions": ["品牌名称是什么？"]},
            confidence=0.9,
            intent="brand_logo_design",
            domain="brand_design",
        )
        decision = SkillNeedDecision(
            True,
            "generate_runtime_skill",
            "brand_logo_design",
            "brand_design",
            "medium",
            True,
            True,
            "brand design",
        )
        result = RuntimeSkillReviewer().review(
            skill,
            decision,
            {
                "memories": [memory],
                "durable_skills": [],
                "seed_skills": [seed],
            },
        )
        self.assertEqual(result["status"], "approved")
        self.assertIn("seed_skill_conflicts_with_higher_priority_basis", result["reasons"])
        self.assertEqual(result["skill"].seed_skill_ids, [])

    def test_priority_runtime_skill_templates_trigger_with_fallback(self):
        from codex_memory.runtime_skill import RuntimeSkillSynthesizer
        from codex_memory.skill_need import SkillNeedClassifier

        prompts = [
            "帮我做品牌定位",
            "帮我制定营销策略",
            "帮我调整写作风格",
            "帮我做产品分析",
            "帮我写商业计划",
            "帮我做 pitch deck",
            "帮我做代码审查",
            "帮我设计架构方案",
            "帮我制定研究计划",
        ]
        classifier = SkillNeedClassifier(model=None)
        synthesizer = RuntimeSkillSynthesizer(model=None)
        for prompt in prompts:
            decision = classifier.classify(prompt)
            self.assertTrue(decision.skill_needed, prompt)
            skill = synthesizer.synthesize(
                prompt,
                decision,
                {
                    "memories": [],
                    "durable_skills": [],
                    "seed_skills": [],
                    "memory_basis_summary": "No clean long-term memories matched this task.",
                    "durable_skill_basis_summary": "No active durable skills matched this task.",
                    "seed_skill_basis_summary": "No seed skills matched this task.",
                },
            )
            self.assertIsNotNone(skill, prompt)
            self.assertGreaterEqual(len(skill.strategy), 2)
            self.assertIn(skill.first_action["type"], {"ask_clarifying_questions", "inspect_repository", "proceed_or_clarify"})

    def test_runtime_word_does_not_trigger_time_direct_answer_path(self):
        from codex_memory.skill_need import SkillNeedClassifier

        classifier = SkillNeedClassifier(model=None)
        for prompt in [
            "实现runtime benchmark并跑 lint",
            "实现prune-runtime并跑 lint",
            "debug 这个runtime observer失败",
        ]:
            decision = classifier.classify(prompt)
            self.assertTrue(decision.skill_needed, prompt)
            self.assertEqual(decision.domain, "software_engineering")

    def test_runtime_skill_quality_evaluator_flags_unbacked_claims(self):
        from codex_memory.runtime_quality import evaluate_runtime_skill

        result = evaluate_runtime_skill(
            {
                "goal": "Use your preferences to guide this task.",
                "memory_basis_ids": [],
                "durable_skill_ids": [],
                "seed_skill_ids": [],
                "strategy": ["According to your preferences, do the work.", "Proceed."],
                "first_action": {"type": "proceed_or_clarify"},
            }
        )
        self.assertFalse(result["valid"])
        self.assertIn("unbacked_user_or_org_claim", result["issues"])


if __name__ == "__main__":
    unittest.main()
