import os
import tempfile
import unittest
from pathlib import Path

from codex_memory.config import Config
from codex_memory.schema import Evidence, MemoryCandidate
from codex_memory.service import MemoryService


def _config(tmp):
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


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_MEMORY_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_MEMORY_FAKE_MODEL", None)

    def test_exact_duplicate_is_merged_not_refiled(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                first = service.ingest_event("manual", {"text": "默认使用中文回答"})
                second = service.ingest_event("manual", {"text": "默认使用中文回答"})
                self.assertEqual(first["results"][0]["status"], "active")
                self.assertEqual(second["results"][0]["status"], "superseded")
                self.assertEqual(len(service.list_memories(status="active")), 1)
            finally:
                service.close()

    def test_near_duplicate_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="用户默认希望用中文回答，并且尽量简洁。",
                    memory_type="user_preference",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="global",
                    evidence=[Evidence(source="user_message", quote="用户默认希望用中文回答，并且尽量简洁。")],
                    reason="test",
                )
                service.ledger.add_candidate(candidate, "active", {"status": "active"})
                matches = service.ledger.find_active_duplicates("默认用中文回答，并且尽量简洁。", "user_preference", "global")
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates("默认用中文回答，且尽量简洁。", "user_preference", "global")
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates("默认使用中文回答，并尽量简洁。", "user_preference", "global")
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_negative_preference_near_duplicate_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="不要主动使用 emoji。",
                    memory_type="user_preference",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="global",
                    evidence=[Evidence(source="user_message", quote="不要主动使用 emoji。")],
                    reason="test",
                )
                service.ledger.add_candidate(candidate, "active", {"status": "active"})
                matches = service.ledger.find_active_duplicates("用户不希望我主动使用 emoji。", "user_preference", "global")
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_architecture_near_duplicate_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="项目架构决策：MCP 和 hook 必须是两条不重叠的路径，不能互相调用。",
                    memory_type="project_context",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="memory_system",
                    category="architecture",
                    subcategory="mcp_hook",
                    triggers=["MCP", "hook", "不重叠"],
                    evidence=[Evidence(source="user_message", quote="项目架构决策：MCP 和 hook 必须是两条不重叠的路径，不能互相调用。")],
                    reason="test",
                )
                project_key = str(Path(tmp).resolve()).lower()
                service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                matches = service.ledger.find_active_duplicates(
                    "项目架构上，MCP 与 hook 需要保持两条不重叠路径，不能互相调用。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates(
                    "项目架构要求 MCP 与 hook 保持两条不重叠路径，二者不能互相调用。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_mcp_hook_architecture_conflict_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                project_key = str(Path(tmp).resolve()).lower()
                existing = MemoryCandidate(
                    content="项目架构决策：MCP 和 hook 必须是两条不重叠的路径，彼此不能互相调用。",
                    memory_type="project_context",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="memory_system",
                    category="architecture",
                    subcategory="mcp_hook",
                    triggers=["MCP", "hook", "不重叠"],
                    evidence=[Evidence(source="user_message", quote="项目架构决策：MCP 和 hook 必须是两条不重叠的路径，彼此不能互相调用。")],
                    reason="test",
                )
                service.ledger.add_candidate(existing, "active", {"status": "active"}, project_key=project_key)
                conflicts = service.ledger.find_active_conflicts(
                    "项目架构目标是让 MCP 和 hook 互相调用，形成统一链路。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(conflicts), 1)

                conflicting = MemoryCandidate(
                    content="项目架构目标是让 MCP 和 hook 互相调用，形成统一链路。",
                    memory_type="project_context",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="memory_system",
                    category="architecture",
                    subcategory="mcp_hook",
                    triggers=["MCP", "hook", "互相调用"],
                    evidence=[Evidence(source="user_message", quote="项目架构目标是让 MCP 和 hook 互相调用，形成统一链路。")],
                    reason="test",
                )
                service.engine.extract = lambda event_type, payload: [conflicting]
                result = service.ingest_event(
                    "manual",
                    {
                        "text": "项目架构目标是让 MCP 和 hook 互相调用，形成统一链路。",
                        "cwd": str(Path(tmp).resolve()),
                    },
                )
                statuses = [item["status"] for item in result["results"]]
                self.assertIn("quarantined", statuses)
                active = [
                    item
                    for item in service.list_memories(status="active")
                    if "MCP" in str(item.get("content")) and "hook" in str(item.get("content"))
                ]
                self.assertEqual(len(active), 1)
            finally:
                service.close()

    def test_project_type_near_duplicate_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="管理平台项目应把权限、审计日志、批量操作和导出流程作为基础能力来设计。",
                    memory_type="experience",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="software_engineering",
                    category="architecture",
                    subcategory="project_type",
                    triggers=["管理平台", "权限", "审计日志"],
                    evidence=[Evidence(source="user_message", quote="管理平台项目应把权限、审计日志、批量操作和导出流程作为基础能力来设计。")],
                    reason="test",
                )
                project_key = str(Path(tmp).resolve()).lower()
                service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                matches = service.ledger.find_active_duplicates(
                    "管理平台项目中，权限、审计日志、批量操作和导出流程要作为基础能力统一设计。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates(
                    "管理平台项目中，权限、审计日志、批量操作和导出流程要作为基础能力统一设计。",
                    "experience",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_project_type_extended_experience_is_not_negative_due_to_different_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="项目类型经验：管理平台应将权限、审计日志、批量操作和导出流程作为基础能力来设计；React 门户则要提前明确状态边界、接口缓存策略和首屏加载预算。",
                    memory_type="experience",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="global",
                    domain="software_engineering",
                    category="lesson",
                    subcategory="project_type",
                    triggers=["管理平台", "门户", "React"],
                    evidence=[Evidence(source="user_message", quote="项目类型经验")],
                    reason="test",
                )
                service.ledger.add_candidate(candidate, "active", {"status": "active"})
                matches = service.ledger.find_active_duplicates(
                    "项目类型经验：不同项目类型要按场景提前设计基础能力；管理平台侧重权限、审计日志、批量操作、导出流程和接口封装分层，门户侧重首屏、SEO、缓存和内容发布链路，React/Vue 都要先明确状态边界与路由权限边界。",
                    "project_context",
                    "global",
                )
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_vue_admin_layering_duplicate_with_should_by_layer_wording(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="Vue 管理平台中，表单权限、路由守卫和接口封装应按层拆分处理，避免耦合在一起。",
                    memory_type="project_context",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="software_engineering",
                    category="architecture",
                    subcategory="vue_admin",
                    triggers=["Vue", "管理平台", "表单权限", "路由守卫"],
                    evidence=[Evidence(source="user_message", quote="Vue 管理平台分层")],
                    reason="test",
                )
                project_key = str(Path(tmp).resolve()).lower()
                service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                matches = service.ledger.find_active_duplicates(
                    "Vue 管理平台中，表单权限、路由守卫和接口封装要分层处理。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_portal_early_design_duplicate_ignores_not_later_contrast(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="门户项目经验：首页首屏、SEO、缓存和内容发布链路需要在早期一起设计，而不是后补。",
                    memory_type="experience",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="software_engineering",
                    category="architecture",
                    subcategory="portal_project",
                    triggers=["门户", "首屏", "SEO", "缓存"],
                    evidence=[Evidence(source="user_message", quote="门户项目经验")],
                    reason="test",
                )
                project_key = str(Path(tmp).resolve()).lower()
                service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                matches = service.ledger.find_active_duplicates(
                    "门户项目经验：首页首屏、SEO、缓存和内容发布链路需要提前设计。",
                    "experience",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_fact_experience_near_duplicate_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="hook 内部调用 `codex exec` 时必须设置 `internal` 标记，否则会递归触发。",
                    memory_type="experience",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="memory_system",
                    category="lesson",
                    subcategory="hook_recursion",
                    triggers=["hook", "codex exec", "internal"],
                    evidence=[Evidence(source="user_message", quote="hook 内部调用 `codex exec` 时必须设置 `internal` 标记，否则会递归触发。")],
                    reason="test",
                )
                project_key = str(Path(tmp).resolve()).lower()
                service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                matches = service.ledger.find_active_duplicates(
                    "在 hook 内部调用 `codex exec` 时必须设置 `internal` 标记，否则会递归触发 hook。",
                    "fact",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_global_experience_duplicate_of_project_fact_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                project_key = str(Path(tmp).resolve()).lower()
                candidate = MemoryCandidate(
                    content="水利工程中，泵站异常时应优先检查电源、液位、泵组振动和控制柜告警。",
                    memory_type="fact",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="water_engineering",
                    category="troubleshooting",
                    subcategory="pump_station",
                    triggers=["水利", "泵站", "电源", "液位"],
                    evidence=[Evidence(source="user_message", quote="水利工程中，泵站异常时应优先检查电源、液位、泵组振动和控制柜告警。")],
                    reason="test",
                )
                service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                matches = service.ledger.find_active_duplicates(
                    "水利工程经验：泵站异常时优先检查电源、液位、泵组振动和控制柜告警。",
                    "experience",
                    "global",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_react_portal_near_duplicate_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="React 门户项目中需要明确状态边界、接口缓存策略和首屏加载预算。",
                    memory_type="project_context",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="software_engineering",
                    category="architecture",
                    subcategory="project_type",
                    triggers=["React", "门户", "接口缓存"],
                    evidence=[Evidence(source="user_message", quote="React 门户项目中需要明确状态边界、接口缓存策略和首屏加载预算。")],
                    reason="test",
                )
                project_key = str(Path(tmp).resolve()).lower()
                service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                for content in (
                    "React 门户项目要求在设计时明确状态边界、接口缓存和首屏加载预算。",
                    "React 门户项目中，状态边界、接口缓存和首屏加载预算需要提前明确。",
                    "React 门户项目要求明确状态边界、接口缓存和首屏加载预算。",
                    "react_portal 项目中，状态边界、接口缓存和首屏加载预算要明确。",
                    "在 react_portal 项目中，需明确状态边界、接口缓存策略和首屏加载预算。",
                ):
                    matches = service.ledger.find_active_duplicates(
                        content,
                        "project_context",
                        "project",
                        project_key=project_key,
                    )
                    self.assertEqual(len(matches), 1, content)
            finally:
                service.close()

    def test_governance_policy_near_duplicate_is_found_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                candidate = MemoryCandidate(
                    content="治理规则不能静态固化，需通过动态 policy 自我修复准入和准出机制。",
                    memory_type="project_context",
                    proposed_action="store",
                    confidence=0.95,
                    importance=0.9,
                    ttl="long",
                    scope="project",
                    domain="memory_system",
                    category="governance",
                    subcategory="policy",
                    triggers=["治理规则", "动态 policy", "自我修复"],
                    evidence=[Evidence(source="user_message", quote="治理规则不能静态固化，需通过动态 policy 自我修复准入和准出机制。")],
                    reason="test",
                )
                project_key = str(Path(tmp).resolve()).lower()
                service.ledger.add_candidate(candidate, "active", {"status": "active"}, project_key=project_key)
                matches = service.ledger.find_active_duplicates(
                    "治理规则应保持“活的”状态，通过动态 policy 自我修复准入和准出，而不是静态死规则。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates(
                    "治理规则需要支持动态 policy，自我修复准入和准出，不能设计成静态死规则。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates(
                    "治理规则不能是死的，需要通过动态 policy 自我修复准入和准出机制。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates(
                    "治理规则应具备动态 policy 自我修复能力，不能是静态死规则；准入和准出都应可通过 policy 动态调整。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates(
                    "治理规则不能静态固定，需要通过动态 policy 自我修复准入和准出机制。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates(
                    "治理规则应支持动态 policy 自我修复，准入和准出都不能是静态僵化的。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
                matches = service.ledger.find_active_duplicates(
                    "治理规则应采用动态 policy 机制，支持对准入和准出规则的自我修复，而不是固定死规则。",
                    "project_context",
                    "project",
                    project_key=project_key,
                )
                self.assertEqual(len(matches), 1)
            finally:
                service.close()

    def test_manual_review_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                result = service.ingest_event("manual", {"text": "默认使用中文回答"})
                memory_id = result["results"][0]["id"]
                rejected = service.reject_memory(memory_id, "not useful")
                self.assertEqual(rejected["status"], "rejected")
                promoted = service.promote_memory(memory_id, "confirmed")
                self.assertEqual(promoted["memory"]["status"], "active")
                deleted = service.delete_memory(memory_id, "cleanup")
                self.assertEqual(deleted["status"], "deleted")
            finally:
                service.close()

    def test_expire_due_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                result = service.ingest_event("manual", {"text": "默认使用中文回答"})
                memory_id = result["results"][0]["id"]
                service.ledger.conn.execute("UPDATE memories SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (memory_id,))
                service.ledger.conn.commit()
                expired = service.expire_due_memories()
                self.assertEqual(expired["expired_count"], 1)
                self.assertEqual(service.ledger.get_memory(memory_id)["status"], "superseded")
            finally:
                service.close()
