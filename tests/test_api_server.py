import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from codex_cognitive_runtime.api_schema import ApiError
from codex_cognitive_runtime.api_server import dispatch
from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.schema import Evidence, MemoryCandidate
from codex_cognitive_runtime.service import MemoryService


def _config(tmp_path: Path) -> Config:
    return Config(
        model="gpt-5.4-mini",
        state_dir=tmp_path,
        ledger_path=tmp_path / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
        development_audit=True,
        store_raw_events=True,
        store_runtime_observation_previews=True,
    )


class ApiServerTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = "1"

    def test_status_and_memory_routes_use_common_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                memory_id = service.ledger.add_candidate(
                    MemoryCandidate(
                        content="项目上下文：实时日志页面显示完整流程",
                        memory_type="project_context",
                        scope="project",
                        ttl="long",
                        confidence=0.9,
                        importance=0.8,
                        evidence=[Evidence(source="test", quote="实时日志页面")],
                        reason="test",
                        proposed_action="store",
                    ),
                    "active",
                    {"status": "active"},
                )
                status = dispatch(service, "GET", "/api/status", {}, {})
                memories = dispatch(service, "GET", "/api/memories", {"status": "active", "limit": "5"}, {})
                non_default = [
                    memory
                    for memory in memories
                    if (memory.get("review_json") or {}).get("source_id") != "default:global_agents_collaboration_rules"
                ]
                self.assertEqual(status["primary_store"], "ledger")
                self.assertIn("user", status["ledger_layers"])
                self.assertIn("baseline", status["ledger_layers"])
                self.assertEqual([memory["id"] for memory in non_default], [memory_id])
                detail = dispatch(service, "GET", f"/api/memories/{memory_id}", {}, {})
                self.assertEqual(detail["status"], "active")
            finally:
                service.close()

    def test_export_target_baseline_does_not_include_user_private_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                sentinel = "PRIVATE_USER_SENTINEL_9f3a"
                service.ledger.add_candidate(
                    MemoryCandidate(
                        content=f"用户私有偏好：{sentinel}",
                        memory_type="user_preference",
                        scope="global",
                        ttl="long",
                        confidence=0.9,
                        importance=0.8,
                        evidence=[Evidence(source="test", quote=sentinel)],
                        reason="leak guard",
                        proposed_action="store",
                    ),
                    "active",
                    {"status": "active"},
                )
                service.seed_skills(category="design")

                user_export = dispatch(service, "POST", "/api/export", {}, {"target": "user"})
                baseline_export = dispatch(service, "POST", "/api/export", {}, {"target": "baseline"})

                self.assertIn(sentinel, json.dumps(user_export, ensure_ascii=False))
                self.assertNotIn(sentinel, json.dumps(baseline_export, ensure_ascii=False))
                self.assertTrue(baseline_export["github_safe"])
                self.assertEqual(baseline_export["target_ledger"], "baseline")
                self.assertTrue(all(record.get("record_type") == "seed_skill" for record in baseline_export["cognitive_records"]))
            finally:
                service.close()

    def test_write_routes_require_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                with self.assertRaises(ApiError) as ctx:
                    dispatch(service, "POST", "/api/memories/ingest", {}, {"text": "默认使用中文回答"})
                self.assertEqual(ctx.exception.code, "confirmation_required")
                self.assertEqual(ctx.exception.status, 409)
            finally:
                service.close()

    def test_memories_route_supports_paged_name_and_type_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                preference_id = service.ledger.add_candidate(
                    MemoryCandidate(
                        content="用户偏好：默认使用中文回答",
                        memory_type="user_preference",
                        scope="global",
                        ttl="long",
                        confidence=0.9,
                        importance=0.8,
                        evidence=[Evidence(source="test", quote="默认使用中文回答")],
                        reason="test",
                        proposed_action="store",
                    ),
                    "active",
                    {"status": "active"},
                )
                service.ledger.add_candidate(
                    MemoryCandidate(
                        content="项目上下文：实时日志页面显示完整流程",
                        memory_type="project_context",
                        scope="project",
                        ttl="long",
                        confidence=0.9,
                        importance=0.8,
                        evidence=[Evidence(source="test", quote="实时日志页面")],
                        reason="test",
                        proposed_action="store",
                    ),
                    "quarantined",
                    {"status": "quarantined"},
                )

                page = dispatch(
                    service,
                    "GET",
                    "/api/memories",
                    {"page": "1", "page_size": "10", "name": "用户偏好"},
                    {},
                )

                self.assertEqual(page["total"], 0)
                explicit = dispatch(
                    service,
                    "GET",
                    "/api/memories",
                    {"page": "1", "page_size": "10", "type": "user_preference"},
                    {},
                )
                self.assertGreaterEqual(explicit["total"], 1)
                self.assertTrue(any(item["id"] == preference_id for item in explicit["items"]))
                self.assertTrue(all(item["memory_type"] == "user_preference" for item in explicit["items"]))
            finally:
                service.close()

    def test_user_preferences_route_filters_edits_and_optimizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                preference_id = service.ledger.add_candidate(
                    MemoryCandidate(
                        content="用户偏好：默认使用中文回答，回答要简洁。",
                        memory_type="user_preference",
                        scope="global",
                        ttl="long",
                        confidence=0.9,
                        importance=0.8,
                        evidence=[Evidence(source="test", quote="默认使用中文回答")],
                        reason="test",
                        proposed_action="store",
                    ),
                    "active",
                    {"status": "active"},
                )
                service.ledger.add_candidate(
                    MemoryCandidate(
                        content="项目上下文：实时日志页面显示完整流程",
                        memory_type="project_context",
                        scope="project",
                        ttl="long",
                        confidence=0.9,
                        importance=0.8,
                        evidence=[Evidence(source="test", quote="实时日志页面")],
                        reason="test",
                        proposed_action="store",
                    ),
                    "active",
                    {"status": "active"},
                )

                page = dispatch(service, "GET", "/api/user-preferences", {"page": "1", "page_size": "10"}, {})
                ids = {item["id"] for item in page["items"]}
                self.assertIn(preference_id, ids)
                self.assertTrue(all(item["memory_type"] == "user_preference" for item in page["items"]))

                optimized = dispatch(service, "POST", f"/api/user-preferences/{preference_id}/optimize", {}, {"instruction": "更清晰"})
                self.assertEqual(optimized["memory_id"], preference_id)
                self.assertIn("optimized_content", optimized)
                self.assertEqual(service.ledger.get_memory(preference_id)["content"], "用户偏好：默认使用中文回答，回答要简洁。")

                with self.assertRaises(ApiError) as ctx:
                    dispatch(service, "POST", f"/api/user-preferences/{preference_id}/edit", {}, {"content": "用户偏好：默认中文回答。"})
                self.assertEqual(ctx.exception.code, "confirmation_required")

                edited = dispatch(
                    service,
                    "POST",
                    f"/api/user-preferences/{preference_id}/edit",
                    {},
                    {"content": "用户偏好：默认中文回答。", "note": "manual cleanup", "confirm": True},
                )
                self.assertEqual(edited["content"], "用户偏好：默认中文回答。")
                self.assertEqual(edited["review_feedback_json"][-1]["action"], "manual_edit")
            finally:
                service.close()

    def test_user_preferences_crud_is_separate_from_ledger_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                with self.assertRaises(ApiError) as ctx:
                    dispatch(service, "POST", "/api/user-preferences", {}, {"content": "用户偏好：少说废话。"})
                self.assertEqual(ctx.exception.code, "confirmation_required")

                created = dispatch(
                    service,
                    "POST",
                    "/api/user-preferences",
                    {},
                    {"content": "用户偏好：少说废话。", "scope": "global", "note": "manual", "confirm": True},
                )
                self.assertEqual(created["memory_type"], "user_preference")
                preference_id = created["id"]
                detail = dispatch(service, "GET", f"/api/user-preferences/{preference_id}", {}, {})
                self.assertEqual(detail["id"], preference_id)

                ledger_page = dispatch(service, "GET", "/api/memories", {"page": "1", "page_size": "50"}, {})
                self.assertNotIn(preference_id, {item["id"] for item in ledger_page["items"]})
                preferences_page = dispatch(service, "GET", "/api/user-preferences", {"page": "1", "page_size": "50"}, {})
                self.assertIn(preference_id, {item["id"] for item in preferences_page["items"]})

                updated = dispatch(
                    service,
                    "POST",
                    f"/api/user-preferences/{preference_id}/edit",
                    {},
                    {"content": "用户偏好：回答直接。", "confirm": True},
                )
                self.assertEqual(updated["content"], "用户偏好：回答直接。")

                deleted = dispatch(
                    service,
                    "POST",
                    f"/api/user-preferences/{preference_id}/delete",
                    {},
                    {"note": "cleanup", "confirm": True},
                )
                self.assertEqual(deleted["status"], "deleted")
                with self.assertRaises(ApiError):
                    dispatch(service, "GET", f"/api/user-preferences/{preference_id}", {}, {})
                with self.assertRaises(ApiError):
                    dispatch(service, "GET", f"/api/memories/{preference_id}", {}, {})

                ordinary_id = service.ledger.add_candidate(
                    MemoryCandidate(
                        content="项目上下文：普通记忆。",
                        memory_type="project_context",
                        scope="project",
                        ttl="long",
                        confidence=0.9,
                        importance=0.8,
                        evidence=[Evidence(source="test", quote="普通记忆")],
                        reason="test",
                        proposed_action="store",
                    ),
                    "active",
                    {"status": "active"},
                )
                service.delete_memory(ordinary_id, note="soft delete ordinary memory")
                ledger_page_after_delete = dispatch(service, "GET", "/api/memories", {"page": "1", "page_size": "50"}, {})
                self.assertNotIn(ordinary_id, {item["id"] for item in ledger_page_after_delete["items"]})
                with self.assertRaises(ApiError):
                    dispatch(service, "GET", f"/api/memories/{ordinary_id}", {}, {})
                with self.assertRaises(ValueError):
                    dispatch(
                        service,
                        "POST",
                        f"/api/user-preferences/{ordinary_id}/edit",
                        {},
                        {"content": "用户偏好：不应成功。", "confirm": True},
                    )
            finally:
                service.close()

    def test_user_preferences_are_activated_on_service_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            service = MemoryService(config)
            try:
                preference_id = service.ledger.add_candidate(
                    MemoryCandidate(
                        content="用户偏好：默认直接回答。",
                        memory_type="user_preference",
                        scope="global",
                        ttl="long",
                        confidence=0.9,
                        importance=0.8,
                        evidence=[Evidence(source="test", quote="默认直接回答")],
                        reason="test",
                        proposed_action="store",
                    ),
                    "quarantined",
                    {"status": "quarantined"},
                )
            finally:
                service.close()

            service = MemoryService(config)
            try:
                self.assertEqual(service.user_preferences_activation_count, 1)
                page = dispatch(service, "GET", "/api/user-preferences", {"page": "1", "page_size": "10"}, {})
                self.assertIn(preference_id, {item["id"] for item in page["items"]})
                self.assertEqual(service.ledger.get_memory(preference_id)["status"], "active")
            finally:
                service.close()

    def test_trace_and_workflow_read_routes_are_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                self.assertIsInstance(dispatch(service, "GET", "/api/traces", {}, {}), list)
                self.assertIsInstance(dispatch(service, "GET", "/api/outcome-attributions", {}, {}), list)
                self.assertIn("active_workflow", dispatch(service, "GET", "/api/workflows/status", {}, {}))
                self.assertIsInstance(dispatch(service, "GET", "/api/workflows/violations", {}, {}), list)
                self.assertIsInstance(dispatch(service, "GET", "/api/governance/policies", {}, {}), list)
            finally:
                service.close()

    def test_runtime_logs_route_returns_trace_events_and_development_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                service.start_task_from_prompt({"prompt": "帮我修复这个 bug", "cwd": tmp, "session_id": "s1", "turn_id": "t1"})
                service.prompt_context("帮我修复这个 bug", cwd=tmp, session_id="s1", turn_id="t1")

                logs = dispatch(service, "GET", "/api/logs", {"session_id": "s1", "turn_id": "t1"}, {})

                self.assertTrue(logs["traces"])
                self.assertTrue(logs["events"])
                self.assertTrue(logs["development_events"])
                self.assertTrue(logs["development_audit_enabled"])
                self.assertEqual(logs["selected_trace_id"], logs["trace"]["id"])
                self.assertIn("development_audit_skill_need_decision", {event["name"] for event in logs["development_events"]})
                skill_need = [event for event in logs["events"] if event["name"] == "skill_need_decision"][0]["metadata_json"]
                self.assertIn("decision_chain", skill_need)
                attribution = dispatch(service, "GET", f"/api/traces/{logs['selected_trace_id']}/attribution", {}, {})
                self.assertEqual(
                    {layer["layer"] for layer in attribution["layers"]},
                    {"task_understanding", "recall", "seed_scoring", "fragment_selection", "final_context", "execution_guard"},
                )
            finally:
                service.close()

    def test_doctor_result_is_persisted_for_overview(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                self.assertIsNone(dispatch(service, "GET", "/api/doctor/status", {}, {})["last_run"])
                result = dispatch(service, "POST", "/api/doctor/run", {}, {"privacy": True})
                status = dispatch(service, "GET", "/api/doctor/status", {}, {})
                self.assertEqual(status["last_run"]["result"]["ok"], result["ok"])
                self.assertTrue(status["last_run"]["ran_at"])
            finally:
                service.close()

    def test_workflow_violation_resolve_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(Path(tmp)))
            try:
                violation = service.ledger.record_runtime_violation("workflow-1", "changed_without_verification", "high", {})
                path = f"/api/workflows/violations/{quote(violation['id'], safe='')}/resolve"
                with self.assertRaises(ApiError):
                    dispatch(service, "POST", path, {}, {"note": "false positive"})
                resolved = dispatch(service, "POST", path, {}, {"confirm": True, "note": "false positive"})
                self.assertEqual(resolved["status"], "resolved")
                self.assertEqual(resolved["metadata_json"]["resolution_note"], "false positive")
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
