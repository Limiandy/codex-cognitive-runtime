import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.hooks import _session_start
from codex_cognitive_runtime.schema import Evidence, MemoryCandidate
from codex_cognitive_runtime.service import MemoryService


def _config(tmp: str, strict_privacy: bool = False, live_log: bool = False, enable_feedback_model: bool = True) -> Config:
    return Config(
        model="gpt-5.4-mini",
        state_dir=Path(tmp),
        ledger_path=Path(tmp) / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
        strict_privacy=strict_privacy,
        trace_live_log=live_log,
        enable_feedback_model=enable_feedback_model,
    )


def _candidate(content: str) -> MemoryCandidate:
    return MemoryCandidate(
        content=content,
        memory_type="user_preference",
        proposed_action="store",
        confidence=0.94,
        importance=0.84,
        ttl="long",
        scope="global",
        evidence=[Evidence(source="user_message", quote=content)],
        reason="trace test",
    )


class RuntimeTraceTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL", None)

    def test_direct_answer_trace_completes_with_recall_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, enable_feedback_model=False))
            try:
                context = service.prompt_context("现在天气怎么样？", cwd=tmp, session_id="s1", turn_id="t1")
                self.assertEqual(context, "")
                traces = service.list_traces(session_id="s1", turn_id="t1")
                self.assertEqual(len(traces), 1)
                self.assertEqual(traces[0]["status"], "completed")
                self.assertEqual(traces[0]["final_outcome"], "direct_answer_no_runtime_skill")
                events = service.trace_events(str(traces[0]["id"]))
                self.assertIn("skill_need_decision", {event["name"] for event in events})
                self.assertIn("recall_skipped", {event["name"] for event in events})
                self.assertNotIn("runtime_skill_injected", {event["name"] for event in events})
            finally:
                service.close()

    def test_logo_runtime_skill_trace_links_basis_and_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, enable_feedback_model=False))
            try:
                memory_id = service.ledger.add_candidate(
                    _candidate("用户偏好极简、专业、克制的视觉风格。"),
                    "active",
                    {"status": "active", "risk_flags": []},
                )
                seed = service.ledger.record_cognitive_record(
                    "skill",
                    "seed_skill",
                    "seed:brand",
                    "Brand Guardian seed skill",
                    "active",
                    "global",
                    domain="brand_design",
                    metadata={
                        "skill_type": "seed_skill",
                        "name": "Brand Guardian",
                        "description": "logo brand visual identity",
                        "trust_level": "external_seed",
                        "trust_state": "trusted",
                        "source_verified": True,
                        "success_count": 0,
                        "failure_count": 0,
                        "reuse_count": 0,
                    },
                )
                durable = service.ledger.record_cognitive_record(
                    "skill",
                    "dynamic_skill",
                    "dyn:logo",
                    "Dynamic logo intake skill",
                    "active",
                    "global",
                    domain="brand_design",
                    metadata={"skill_type": "dynamic_skill", "title": "Logo intake", "trigger": ["logo", "品牌"], "procedure": ["Ask brand name."], "success_count": 0, "failure_count": 0},
                )
                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1", turn_id="t1")
                self.assertIn("Runtime Skill:", context)
                trace = service.list_traces(session_id="s1", turn_id="t1")[0]
                events = service.trace_events(str(trace["id"]))
                names = {event["name"] for event in events}
                self.assertIn("basis_retrieved", names)
                self.assertIn("runtime_skill_synthesized", names)
                self.assertIn("runtime_skill_reviewed", names)
                self.assertIn("runtime_skill_injected", names)
                links = service.ledger.list_trace_links(str(trace["id"]))
                self.assertTrue(any(link["target_id"] == memory_id and link["target_type"] == "memory" for link in links))
                self.assertTrue(any(link["target_id"] == seed["id"] and link["target_type"] == "seed_skill" for link in links))
                self.assertTrue(any(link["target_id"] == durable["id"] and link["target_type"] == "durable_skill" for link in links))
                summary = service.trace_summary(str(trace["id"]))
                self.assertEqual(summary["basis"]["memory_count"], 1)
                self.assertEqual(summary["basis"]["durable_skill_count"], 1)
                self.assertEqual(summary["basis"]["seed_skill_count"], 1)
            finally:
                service.close()

    def test_workflow_violation_marks_trace_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                service.prompt_context("帮我修复这个 bug", cwd=tmp, session_id="s1", turn_id="t1")
                service.start_task_from_prompt({"prompt": "帮我修复这个 bug", "cwd": tmp, "session_id": "s1", "turn_id": "t1"})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成"})
                trace = service.list_traces(session_id="s1", turn_id="t1")[0]
                self.assertEqual(trace["status"], "failed")
                events = service.trace_events(str(trace["id"]))
                self.assertIn("workflow_violation_detected", {event["name"] for event in events})
            finally:
                service.close()

    def test_engineering_success_trace_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, enable_feedback_model=False))
            try:
                service.prompt_context("帮我修复这个 bug", cwd=tmp, session_id="s1", turn_id="t1")
                service.start_task_from_prompt({"prompt": "帮我修复这个 bug", "cwd": tmp, "session_id": "s1", "turn_id": "t1"})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "测试通过，已完成"})
                trace = service.list_traces(session_id="s1", turn_id="t1")[0]
                self.assertEqual(trace["status"], "completed")
                self.assertEqual(trace["final_outcome"], "success")
                names = [event["name"] for event in service.trace_events(str(trace["id"]))]
                self.assertIn("workflow_step_completed", names)
                self.assertIn("workflow_stop_audited", names)
                self.assertIn("runtime_skill_feedback_recorded", names)
                self.assertIn("verification_recipe_learned", names)
                self.assertIn("dynamic_skill_candidate_created", names)
            finally:
                service.close()

    def test_user_message_memory_extraction_is_traced(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, enable_feedback_model=False))
            try:
                payload = {"prompt": "默认使用中文回答", "cwd": tmp, "session_id": "s1", "turn_id": "t1"}
                event_id = service.record_event("user_message", payload)
                trace = service.start_trace_from_payload(payload, event_id=event_id)
                result = service.process_event_id(event_id)
                self.assertEqual(result["candidate_count"], 1)
                events = service.trace_events(trace.trace_id)
                names = {event["name"] for event in events}
                self.assertIn("memory_extraction_started", names)
                self.assertIn("memory_candidates_extracted", names)
                self.assertIn("memory_candidate_reviewed", names)
                self.assertIn("memory_candidate_stored", names)
                links = service.ledger.list_trace_links(trace.trace_id)
                self.assertTrue(any(link["target_type"] == "memory" and link["relation"] == "created" for link in links))
            finally:
                service.close()

    def test_session_start_does_not_create_user_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, enable_feedback_model=False))
            try:
                with redirect_stdout(StringIO()):
                    _session_start(service, {"cwd": tmp, "session_id": "s1", "turn_id": "t0"})
                self.assertEqual(service.list_traces(), [])
            finally:
                service.close()

    def test_natural_and_mixed_feedback_trace_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, enable_feedback_model=False))
            try:
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1", turn_id="t1")
                feedback = service.apply_natural_feedback("方向对，但问题太多", session_id="s1", turn_id="t1")
                self.assertIn("runtime_skill_feedback", feedback)
                trace = service.list_traces(session_id="s1", turn_id="t1")[0]
                events = service.trace_events(str(trace["id"]))
                recorded = [event for event in events if event["name"] == "runtime_skill_feedback_recorded"][-1]
                self.assertEqual(recorded["metadata_json"]["outcome"], "mixed")
                self.assertEqual(recorded["metadata_json"]["dimensions"]["skill_relevance"], "positive")
                self.assertEqual(recorded["metadata_json"]["dimensions"]["first_action_quality"], "negative")
                self.assertFalse(recorded["metadata_json"]["adjust_seed_skill_strength"])
                self.assertFalse(recorded["metadata_json"]["adjust_durable_skill_strength"])
            finally:
                service.close()

    def test_strict_privacy_trace_hashes_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, strict_privacy=True))
            try:
                service.prompt_context("帮我画一个 PRIVATE_BRAND logo", cwd="/tmp/PRIVATE_PROJECT", session_id="s1", turn_id="t1")
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg PRIVATE_SECRET /tmp/PRIVATE_PROJECT", "session_id": "s1", "turn_id": "t1", "cwd": "/tmp/PRIVATE_PROJECT"})
                trace = service.list_traces(session_id="s1", turn_id="t1")[0]
                rendered = json.dumps(service.export_trace(str(trace["id"])), ensure_ascii=False)
                self.assertNotIn("PRIVATE_BRAND", rendered)
                self.assertNotIn("PRIVATE_PROJECT", rendered)
                self.assertNotIn("PRIVATE_SECRET", rendered)
                self.assertIn("prompt_sha256", rendered)
            finally:
                service.close()

    def test_cli_traces_and_live_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp, "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1"}
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from codex_cognitive_runtime.config import load_config; from codex_cognitive_runtime.service import MemoryService; s=MemoryService(load_config()); s.prompt_context('帮我画一个品牌 logo', session_id='s1', turn_id='t1'); s.close()",
                ],
                cwd=".",
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=True,
            )
            listed = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "traces", "list"],
                cwd=".",
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=True,
            )
            traces = json.loads(listed.stdout)
            self.assertGreaterEqual(len(traces), 1)
            trace_id = traces[0]["id"]
            for cmd in ("show", "events", "summary", "audit", "export"):
                proc = subprocess.run(
                    [sys.executable, "-m", "codex_cognitive_runtime.cli", "traces", cmd, trace_id] if cmd not in {"audit"} else [sys.executable, "-m", "codex_cognitive_runtime.cli", "traces", cmd],
                    cwd=".",
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                    check=True,
                )
                self.assertTrue(json.loads(proc.stdout) is not None)
            live = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from codex_cognitive_runtime.config import load_config; from codex_cognitive_runtime.service import MemoryService; s=MemoryService(load_config()); s.prompt_context('帮我画一个品牌 logo', session_id='s2', turn_id='t1'); s.close()",
                ],
                cwd=".",
                env={**env, "CODEX_COGNITIVE_RUNTIME_TRACE_LIVE_LOG": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=True,
            )
            self.assertIn('"trace_id"', live.stderr)

    def test_export_and_prune_traces_do_not_delete_memory_or_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, enable_feedback_model=False))
            try:
                memory_id = service.ledger.add_candidate(_candidate("用户偏好极简。"), "active", {"status": "active", "risk_flags": []})
                skill = service.ledger.record_cognitive_record(
                    "skill",
                    "seed_skill",
                    "seed:test",
                    "Seed content",
                    "active",
                    "global",
                    metadata={"skill_type": "seed_skill", "name": "Seed", "trust_level": "external_seed", "trust_state": "trusted", "source_verified": True},
                )
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1", turn_id="t1")
                trace = service.list_traces(session_id="s1", turn_id="t1")[0]
                exported = service.export_trace(str(trace["id"]))
                self.assertIn("trace", exported)
                self.assertIn("spans", exported)
                self.assertIn("events", exported)
                self.assertIn("links", exported)
                pruned = service.prune_traces()
                self.assertGreaterEqual(pruned["deleted_traces"], 1)
                self.assertEqual(service.list_traces(), [])
                self.assertIsNotNone(service.ledger.get_memory(memory_id))
                self.assertIsNotNone(service.ledger.get_cognitive_record(str(skill["id"])))
            finally:
                service.close()

    def test_trace_migration_and_global_export_include_trace_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, enable_feedback_model=False))
            try:
                service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1", turn_id="t1")
                migration = service.ledger.runtime_trace_migration_status()
                self.assertTrue(migration["ok"])
                exported = service.ledger.export_data()
                self.assertIn("runtime_traces", exported)
                self.assertIn("runtime_trace_spans", exported)
                self.assertIn("runtime_trace_events", exported)
                self.assertIn("runtime_trace_links", exported)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
