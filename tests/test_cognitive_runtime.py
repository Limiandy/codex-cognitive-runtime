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
            min_active_confidence=0.82,
            min_quarantine_confidence=0.62,
            duplicate_threshold=0.9,
            max_evidence_quote_chars=500,
        )
    )


def _candidate(content, memory_type="experience", scope="global", domain="software_engineering", category="lesson", subcategory="frontend"):
    return MemoryCandidate(
        content=content,
        memory_type=memory_type,
        proposed_action="store",
        confidence=0.95,
        importance=0.88,
        ttl="long",
        scope=scope,
        domain=domain,
        category=category,
        subcategory=subcategory,
        abstraction_level="principle",
        triggers=["前端", "工程", "workflow", "hook", "MCP"],
        evidence=[Evidence(source="test", quote=content)],
        reason="cognitive runtime test",
    )


class CognitiveRuntimeTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_MEMORY_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_MEMORY_FAKE_MODEL", None)

    def test_memory_is_materialized_into_cognitive_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                skill_id = service.ledger.add_candidate(
                    _candidate("前端经验：Vue、React、jQuery 项目都要先稳定组件边界和接口封装。"),
                    "active",
                    {"status": "active"},
                )
                knowledge_id = service.ledger.add_candidate(
                    _candidate(
                        "项目架构决策：MCP 与 hook 必须保持两条不重叠路径。",
                        memory_type="project_context",
                        scope="project",
                        domain="memory_system",
                        category="architecture",
                        subcategory="mcp_hook",
                    ),
                    "active",
                    {"status": "active"},
                    project_key=str(Path(tmp).resolve()).lower(),
                )
                service.runtime.sync_memory(skill_id)
                service.runtime.sync_memory(knowledge_id)

                snapshot = service.cognitive_snapshot()
                self.assertGreaterEqual(snapshot["records"]["by_layer"].get("skill", 0), 2)
                self.assertGreaterEqual(snapshot["records"]["by_layer"].get("knowledge", 0), 2)
                self.assertGreaterEqual(snapshot["records"]["by_layer"].get("runtime_state", 0), 0)
                self.assertTrue(snapshot["ontology"]["experience"]["layer"], "skill")
            finally:
                service.close()

    def test_conflict_graph_records_contradictory_project_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                project_key = str(Path(tmp).resolve()).lower()
                first = service.ledger.add_candidate(
                    _candidate(
                        "项目架构决策：MCP 与 hook 必须保持两条不重叠路径，不能互相调用。",
                        memory_type="project_context",
                        scope="project",
                        domain="memory_system",
                        category="architecture",
                        subcategory="mcp_hook",
                    ),
                    "active",
                    {"status": "active"},
                    project_key=project_key,
                )
                second = service.ledger.add_candidate(
                    _candidate(
                        "项目架构决策：MCP 与 hook 应该形成统一链路并互相调用。",
                        memory_type="project_context",
                        scope="project",
                        domain="memory_system",
                        category="architecture",
                        subcategory="mcp_hook",
                    ),
                    "active",
                    {"status": "active"},
                    project_key=project_key,
                )
                service.runtime.sync_memory(first)
                service.runtime.sync_memory(second)
                edges = service.ledger.list_cognitive_edges(relation="contradicts")
                self.assertTrue(any(edge["source_id"] == second and edge["target_id"] == first for edge in edges))
            finally:
                service.close()

    def test_dynamic_workflow_uses_memory_knowledge_and_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.ledger.add_candidate(
                    _candidate("前端经验：React 与 Vue 管理平台都应先抽离权限、路由、接口封装策略。"),
                    "active",
                    {"status": "active"},
                )
                service.ledger.add_candidate(
                    _candidate(
                        "组织知识：当前 codex-memory 项目要求 hook 与 MCP 不混用，hook 负责自动注入。",
                        memory_type="project_context",
                        scope="project",
                        domain="memory_system",
                        category="architecture",
                        subcategory="hook",
                    ),
                    "active",
                    {"status": "active"},
                    project_key=str(Path(tmp).resolve()).lower(),
                )
                plan = service.workflow_plan("继续实现前端工程里的 hook 自动注入并跑测试", cwd=tmp)
                step_names = [step["name"] for step in plan["steps"]]
                self.assertIn("recall_memory", step_names)
                self.assertIn("select_skill", step_names)
                self.assertIn("execute_and_verify", step_names)
                self.assertTrue(plan["skills"])
                self.assertTrue(plan["knowledge"])
                edges = service.ledger.list_cognitive_edges()
                self.assertTrue(any(edge["relation"] == "uses_skill" for edge in edges))
                self.assertTrue(service.ledger.list_cognitive_records(layer="reasoning"))
            finally:
                service.close()

    def test_workflow_execute_records_stateful_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.ledger.add_candidate(
                    _candidate("工程经验：实现 hook 自动注入后必须跑单元测试和 CLI 验证。"),
                    "active",
                    {"status": "active"},
                )
                result = service.workflow_execute("实现 hook 自动注入并验证", cwd=tmp)
                self.assertEqual(result["workflow_state"], "completed")
                self.assertEqual(result["mode"], "legacy_simulation")
                self.assertIn("legacy simulation", result["warning"])
                self.assertTrue(result["executed_steps"])
                states = service.ledger.latest_state_transitions(limit=100)
                self.assertTrue(any(item["subject_id"] == result["workflow_id"] and item["state"] == "completed" for item in states))
                self.assertTrue(any(item["subject_type"] == "workflow_step" and item["state"] == "completed" for item in states))
            finally:
                service.close()

    def test_invalid_runtime_state_transition_is_rejected_and_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                with self.assertRaises(ValueError):
                    service.runtime.transition("workflow", "wf-invalid", "completed")
                audit = service.ledger.list_cognitive_records(layer="audit")
                self.assertTrue(any(item["record_type"] == "invalid_state_transition" for item in audit))
            finally:
                service.close()

    def test_prompt_context_includes_cognitive_runtime_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.ledger.add_candidate(
                    _candidate("前端经验：React 与 Vue 管理平台都应先抽离权限、路由、接口封装策略。"),
                    "active",
                    {"status": "active"},
                )
                service.ledger.add_candidate(
                    _candidate(
                        "组织知识：当前 codex-memory 项目要求 hook 与 MCP 不混用，hook 负责自动注入。",
                        memory_type="project_context",
                        scope="project",
                        domain="memory_system",
                        category="architecture",
                        subcategory="hook",
                    ),
                    "active",
                    {"status": "active"},
                    project_key=str(Path(tmp).resolve()).lower(),
                )
                context = service.prompt_context("继续实现前端工程里的 hook 自动注入并跑测试", cwd=tmp)
                self.assertIn("Codex Cognitive Runtime context:", context)
                self.assertIn("reasoning_policy:", context)
                self.assertIn("workflow:", context)
            finally:
                service.close()

    def test_governance_policy_is_materialized_as_policy_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.ledger.add_governance_policy(
                    "recall_gate",
                    {"memory_type": "experience", "terms": ["测试", "质量"]},
                    "suppress",
                    "low-value recall suppression",
                    ttl_days=30,
                )
                snapshot = service.cognitive_snapshot()
                self.assertGreaterEqual(snapshot["records"]["by_layer"].get("policy", 0), 1)
                policies = service.ledger.list_cognitive_records(layer="policy")
                self.assertEqual(policies[0]["record_type"], "recall_gate")
            finally:
                service.close()

    def test_event_processing_records_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                result = service.ingest_event("manual", {"text": "默认使用中文回答"})
                states = service.ledger.latest_state_transitions(limit=20)
                self.assertTrue(any(item["subject_id"] == result["event_id"] and item["state"] == "received" for item in states))
                self.assertTrue(any(item["subject_id"] == result["event_id"] and item["state"] == "processed" for item in states))
                audit = service.ledger.list_cognitive_records(layer="audit")
                self.assertTrue(any(item["source_id"] == result["event_id"] for item in audit))
            finally:
                service.close()
