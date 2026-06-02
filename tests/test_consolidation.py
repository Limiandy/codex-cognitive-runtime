import os
import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.ledger import project_key_for_cwd
from codex_cognitive_runtime.schema import Evidence, MemoryCandidate
from codex_cognitive_runtime.service import MemoryService


def _service(tmp):
    config = Config(
        model="gpt-5.4-mini",
        state_dir=Path(tmp),
        ledger_path=Path(tmp) / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
    )
    return MemoryService(config)


def _project_memory(text, cwd):
    return MemoryCandidate(
        content=text,
        memory_type="experience",
        proposed_action="store",
        confidence=0.95,
        importance=0.86,
        ttl="long",
        scope="project",
        domain="software_engineering",
        category="lesson",
        subcategory="frontend",
        abstraction_level="pattern",
        evidence=[Evidence(source="user_message", quote=text)],
        reason="consolidation test",
    ), project_key_for_cwd(cwd)


class ConsolidationTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL", None)

    def test_frontend_cross_stack_memories_form_global_experience(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                for candidate, project_key in [
                    _project_memory("Vue 项目经验：组件边界和接口封装要先稳定。", "/tmp/vue-app"),
                    _project_memory("React 项目经验：状态管理和接口封装要避免散落。", "/tmp/react-app"),
                    _project_memory("jQuery 项目经验：构建调试和接口封装要形成统一约束。", "/tmp/jquery-app"),
                ]:
                    service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                result = service.consolidate_memories()
                self.assertEqual(result["created_count"], 1)
                memory = service.ledger.get_memory(result["created"][0]["id"])
                self.assertEqual(memory["scope"], "global")
                self.assertEqual(memory["subcategory"], "frontend")
                self.assertIn("前端通用经验", memory["content"])
                self.assertIn("Vue", memory["content"])
                self.assertIn("React", memory["content"])
                context = service.prompt_context("前端项目有什么通用经验？", cwd="/tmp/other-app")
                self.assertIn("用户需求：前端项目有什么通用经验？", context)
                self.assertIn("遵循以下规则：", context)
                self.assertNotIn("前端通用经验", context)
                recall = service.ledger.latest_recall_event()
                self.assertIn(memory["id"], recall["memory_ids_json"])
            finally:
                service.close()

    def test_project_types_form_global_project_type_experience(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                for candidate, project_key in [
                    _project_memory("管理平台项目经验：权限入口和发布流程要标准化。", "/tmp/admin"),
                    _project_memory("门户项目经验：信息架构和发布流程要提前沉淀。", "/tmp/portal"),
                    _project_memory("小程序项目经验：端侧约束和发布流程要单独处理。", "/tmp/miniapp"),
                ]:
                    candidate.subcategory = "project_type"
                    service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                result = service.consolidate_memories()
                self.assertEqual(result["created_count"], 1)
                memory = service.ledger.get_memory(result["created"][0]["id"])
                self.assertEqual(memory["subcategory"], "project_type")
                self.assertIn("项目类型经验", memory["content"])
                self.assertIn("管理平台", memory["content"])
                self.assertIn("小程序", memory["content"])
            finally:
                service.close()

    def test_consolidation_requires_multiple_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                project_key = project_key_for_cwd("/tmp/vue-app")
                for text in [
                    "Vue 项目经验：组件边界要稳定。",
                    "React 项目经验：状态管理要稳定。",
                ]:
                    candidate, _ = _project_memory(text, "/tmp/vue-app")
                    service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                result = service.consolidate_memories()
                self.assertEqual(result["created_count"], 0)
            finally:
                service.close()

    def test_dynamic_topic_can_form_new_global_experience(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                for text, cwd in [
                    ("SSR 项目经验：性能优化要先建立首屏性能预算和加载链路检查。", "/tmp/ssr-a"),
                    ("移动端项目经验：性能优化要记录加载链路并控制资源体积。", "/tmp/mobile-b"),
                    ("可视化项目经验：性能优化要把调试手段沉淀成检查清单。", "/tmp/chart-c"),
                ]:
                    candidate, project_key = _project_memory(text, cwd)
                    candidate.subcategory = "性能优化"
                    candidate.triggers = ["性能优化", "性能预算", "加载链路"]
                    service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                result = service.consolidate_memories()
                self.assertEqual(result["created_count"], 1)
                memory = service.ledger.get_memory(result["created"][0]["id"])
                self.assertEqual(memory["scope"], "global")
                self.assertIn("性能优化", memory["content"])
                self.assertIn("性能优化", memory["subcategory"])
            finally:
                service.close()
