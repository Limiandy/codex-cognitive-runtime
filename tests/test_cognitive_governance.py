import os
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
            palace_path=str(tmp_path / "palace"),
            min_active_confidence=0.82,
            min_quarantine_confidence=0.62,
            duplicate_threshold=0.9,
            max_evidence_quote_chars=500,
        )
    )


def _candidate(content):
    return MemoryCandidate(
        content=content,
        memory_type="project_context",
        proposed_action="store",
        confidence=0.95,
        importance=0.9,
        ttl="long",
        scope="project",
        domain="memory_system",
        category="architecture",
        subcategory="mcp_hook",
        triggers=["MCP", "hook", "架构"],
        evidence=[Evidence(source="test", quote=content)],
        reason="cognitive governance test",
    )


class CognitiveGovernanceTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_MEMORY_FAKE_MODEL"] = "1"
        os.environ["CODEX_MEMORY_DISABLE_MEMPALACE"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_MEMORY_FAKE_MODEL", None)
        os.environ.pop("CODEX_MEMORY_DISABLE_MEMPALACE", None)

    def test_cognitive_governance_quarantines_conflicting_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                project_key = str(Path(tmp).resolve()).lower()
                first = service.ledger.add_candidate(
                    _candidate("项目架构决策：MCP 与 hook 必须保持两条不重叠路径，不能互相调用。"),
                    "active",
                    {"status": "active"},
                    project_key=project_key,
                )
                second = service.ledger.add_candidate(
                    _candidate("项目架构决策：MCP 与 hook 应该形成统一链路并互相调用。"),
                    "active",
                    {"status": "active"},
                    project_key=project_key,
                )
                service.runtime.sync_memory(first)
                service.runtime.sync_memory(second)
                result = service.govern_cognitive(apply=True)
                self.assertTrue(any(action["action"] == "quarantine_conflicting_record" for action in result["applied_actions"]))
                self.assertEqual(service.ledger.get_cognitive_record(second)["status"], "quarantined")
            finally:
                service.close()

    def test_cognitive_governance_marks_stale_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                workflow = service.workflow_plan("实现 hook 自动注入并验证", cwd=tmp)
                result = service.govern_cognitive(apply=True)
                self.assertTrue(any(action["action"] == "mark_workflow_failed" for action in result["applied_actions"]))
                self.assertEqual(service.ledger.get_cognitive_record(workflow["workflow_id"])["status"], "quarantined")
            finally:
                service.close()

    def test_cognitive_governance_deprecates_weak_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                skill = service.ledger.record_cognitive_record(
                    "skill",
                    "execution_strategy",
                    "skill-weak",
                    "弱技能：没有证据支撑的执行策略。",
                    "active",
                    "global",
                    strength=0.2,
                    importance=0.5,
                )
                result = service.govern_cognitive(apply=True)
                self.assertTrue(any(action["action"] == "deprecate_skill" for action in result["applied_actions"]))
                self.assertEqual(service.ledger.get_cognitive_record(skill["id"])["status"], "deprecated")
            finally:
                service.close()
