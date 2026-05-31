import os
import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.schema import Evidence, MemoryCandidate
from codex_cognitive_runtime.service import MemoryService


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


def _experience(text):
    return MemoryCandidate(
        content=text,
        memory_type="experience",
        proposed_action="store",
        confidence=0.95,
        importance=0.9,
        ttl="long",
        scope="global",
        domain="software_engineering",
        category="lesson",
        subcategory="testing",
        triggers=["hook", "测试", "CLI", "验证"],
        evidence=[Evidence(source="test", quote=text)],
        reason="final cognitive runtime test",
    )


class FinalCognitiveRuntimeTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL", None)

    def test_knowledge_build_covers_repo_and_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                result = service.knowledge_build(source="all")
                audit = service.knowledge_audit()
                self.assertGreater(result["created_count"], 0)
                self.assertGreater(audit["by_type"].get("test_contract", 0), 0)
                self.assertGreater(audit["by_type"].get("git_evolution", 0), 0)
                self.assertTrue(service.knowledge_search("workflow hook 测试", limit=3))
            finally:
                service.close()

    def test_skill_build_versions_and_workflow_success_updates_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.ledger.add_candidate(_experience("经验：hook 自动注入后必须跑单元测试和 CLI 验证。"), "active", {"status": "active"})
                second = service.ledger.add_candidate(_experience("经验：CLI 验证要覆盖 hook 自动注入和 workflow 执行结果。"), "active", {"status": "active"})
                service.runtime.sync_memory(first)
                service.runtime.sync_memory(second)
                built = service.skill_build()
                self.assertGreaterEqual(built["created_count"], 1)
                skills = service.skill_list()
                before = {item["id"]: (item["metadata_json"], item["strength"]) for item in skills}
                executed = service.workflow_execute("实现 hook 自动注入并运行 CLI 测试", cwd=tmp)
                self.assertEqual(executed["workflow_state"], "completed")
                after = service.skill_list()
                self.assertTrue(
                    any(
                        int(item["metadata_json"].get("reuse_count") or 0) > int(before.get(item["id"], ({}, 0))[0].get("reuse_count") or 0)
                        for item in after
                    )
                )
            finally:
                service.close()

    def test_workflow_dag_failure_resume_cancel_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                failed = service.workflow_execute("实现 hook 自动注入并验证", cwd=tmp, fail_step="execute_change")
                self.assertEqual(failed["workflow_state"], "failed")
                self.assertTrue(any(step["state"] == "skipped" for step in failed["dag"]["steps"]))
                resumed = service.workflow_resume(failed["workflow_id"])
                self.assertEqual(resumed["workflow_state"], "completed")
                audit = service.workflow_audit(failed["workflow_id"])
                self.assertEqual(audit["state"], "completed")

                planned = service.workflow_plan("实现另一个可取消 workflow", cwd=tmp)
                cancelled = service.workflow_cancel(planned["workflow_id"])
                self.assertEqual(cancelled["workflow_state"], "cancelled")
            finally:
                service.close()

    def test_reasoning_policy_gate_controls_injection_and_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                self.assertEqual(service.prompt_context("家里灯不亮怎么办？", cwd=tmp), "")
                service.knowledge_build(source="repo")
                plan = service.workflow_plan("修改 hook 自动注入代码并运行测试", cwd=tmp)
                self.assertEqual(plan["policy"]["workflow_mode"], "dag")
                self.assertTrue(plan["policy"]["verification_required"])
                context = service.prompt_context("修改 hook 自动注入代码并运行测试", cwd=tmp)
                self.assertIn("policy_gate:", context)
                self.assertIn("organizational_knowledge:", context)
            finally:
                service.close()

    def test_full_cognitive_governance_rebuilds_and_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.ledger.record_cognitive_record("skill", "execution_strategy", "weak-final", "弱技能", "active", "global", strength=0.2)
                result = service.govern_cognitive(apply=True, full=True)
                self.assertTrue(result["applied_actions"])
                self.assertGreater(service.knowledge_audit()["knowledge_count"], 0)
                self.assertEqual(service.ledger.get_cognitive_record("weak-final")["status"], "deprecated")
            finally:
                service.close()
