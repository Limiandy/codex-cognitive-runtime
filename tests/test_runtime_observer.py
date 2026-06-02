import json
import os
import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config
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


def _fixture(name: str, cwd: str) -> dict:
    path = Path(__file__).parent / "fixtures" / "hooks" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    data["cwd"] = cwd
    return data


class RuntimeObserverTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL", None)

    def test_tool_observations_advance_workflow_and_inject_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                payload = {"prompt": "修复这个 bug", "session_id": "s1", "cwd": tmp}
                started = service.start_task_from_prompt(payload)
                self.assertTrue(started["started"])
                workflow_id = started["workflow_id"]

                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg bug src", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "cwd": tmp})

                workflow = service.ledger.get_cognitive_record(workflow_id)
                metadata = workflow["metadata_json"]
                self.assertIn("inspect_repository", metadata["completed_steps"])
                self.assertIn("execute_change", metadata["completed_steps"])
                self.assertTrue(metadata["changed"])

                context = service.prompt_context("继续处理", cwd=tmp, session_id="s1")
                self.assertIn("用户需求：继续处理", context)
                self.assertIn("遵循以下规则：", context)
                self.assertNotIn("Runtime control:", context)
                self.assertNotIn("pending_required_step:", context)
                status = service.runtime_status(cwd=tmp, session_id="s1")
                self.assertEqual(status["active_workflow"]["id"], workflow_id)
                self.assertEqual(status["active_workflow"]["pending_required_step"], "execute_and_verify")

                direct_context = service.prompt_context("为什么判定它不需要 skill 呢？只需要回答原因就行", cwd=tmp, session_id="s1")
                self.assertNotIn("Runtime Skill:", direct_context)
                self.assertNotIn("Runtime control:", direct_context)

                page_question_context = service.prompt_context("这是什么页面？", cwd=tmp, session_id="s1")
                self.assertNotIn("Runtime Skill:", page_question_context)
                self.assertNotIn("Runtime control:", page_question_context)
            finally:
                service.close()

    def test_direct_answer_prompt_does_not_start_observed_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                result = service.start_task_from_prompt(
                    {
                        "prompt": "失败指的是我们的 workflow 失败，并不是 Codex 本身执行任务失败对吧？",
                        "session_id": "s1",
                        "cwd": tmp,
                    }
                )
                self.assertFalse(result["started"])
                self.assertEqual(result["reason"], "runtime_skill_not_needed")
                self.assertEqual(service.runtime_status(cwd=tmp, session_id="s1")["active_workflow"], None)
            finally:
                service.close()

    def test_short_ui_engineering_prompt_starts_observed_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                result = service.start_task_from_prompt(
                    {
                        "prompt": "你看这个页面的三个部分，它们应该都有独立的滚动条",
                        "session_id": "s1",
                        "cwd": tmp,
                    }
                )
                self.assertTrue(result["started"])
                self.assertIn("execute_change", result["required_steps"])
            finally:
                service.close()

    def test_replays_realistic_hook_payload_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                prompt = _fixture("user_prompt_submit_engineering.json", cwd=tmp)
                inspect = _fixture("post_tool_use_inspect.json", cwd=tmp)
                edit = _fixture("post_tool_use_edit.json", cwd=tmp)
                verify = _fixture("post_tool_use_verify_ok.json", cwd=tmp)
                stop = _fixture("stop_success.json", cwd=tmp)

                workflow_id = service.start_task_from_prompt(prompt)["workflow_id"]
                service.observe_tool_use(inspect)
                service.observe_tool_use(edit)
                service.observe_tool_use(verify)
                result = service.observe_stop(stop)

                self.assertEqual(result["violations"], [])
                self.assertEqual(service.ledger.latest_state_for("workflow", workflow_id), "completed")
                workflow = service.ledger.get_cognitive_record(workflow_id)
                metadata = workflow["metadata_json"]
                self.assertEqual(metadata["turn_id"], "fixture-turn")
                self.assertIn("execute_and_verify", metadata["completed_steps"])
            finally:
                service.close()

    def test_fixture_verify_success_without_exit_code_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                prompt = _fixture("user_prompt_submit_engineering.json", cwd=tmp)
                inspect = _fixture("post_tool_use_inspect.json", cwd=tmp)
                edit = _fixture("post_tool_use_edit.json", cwd=tmp)
                verify = _fixture("post_tool_use_verify_ok_no_exit_code.json", cwd=tmp)
                stop = _fixture("stop_success.json", cwd=tmp)

                workflow_id = service.start_task_from_prompt(prompt)["workflow_id"]
                service.observe_tool_use(inspect)
                service.observe_tool_use(edit)
                service.observe_tool_use(verify)
                result = service.observe_stop(stop)

                self.assertEqual(result["violations"], [])
                self.assertEqual(service.ledger.latest_state_for("workflow", workflow_id), "completed")
            finally:
                service.close()

    def test_honest_failed_stop_does_not_claim_success_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                prompt = _fixture("user_prompt_submit_engineering.json", cwd=tmp)
                inspect = _fixture("post_tool_use_inspect.json", cwd=tmp)
                edit = _fixture("post_tool_use_edit.json", cwd=tmp)
                verify = _fixture("post_tool_use_verify_stderr_failed.json", cwd=tmp)
                stop = _fixture("stop_honest_failed.json", cwd=tmp)

                service.start_task_from_prompt(prompt)
                service.observe_tool_use(inspect)
                service.observe_tool_use(edit)
                service.observe_tool_use(verify)
                result = service.observe_stop(stop)

                violation_types = [
                    (item.get("metadata_json") or {}).get("violation_type")
                    for item in result["violations"]
                ]
                self.assertNotIn("verification_failed_but_claimed_success", violation_types)
            finally:
                service.close()

    def test_failed_fixture_claiming_success_records_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                prompt = _fixture("user_prompt_submit_engineering.json", cwd=tmp)
                inspect = _fixture("post_tool_use_inspect.json", cwd=tmp)
                edit = _fixture("post_tool_use_edit.json", cwd=tmp)
                verify = _fixture("post_tool_use_verify_stderr_failed.json", cwd=tmp)
                stop = _fixture("stop_claims_success_after_failure.json", cwd=tmp)

                service.start_task_from_prompt(prompt)
                service.observe_tool_use(inspect)
                service.observe_tool_use(edit)
                service.observe_tool_use(verify)
                result = service.observe_stop(stop)

                violation_types = [
                    (item.get("metadata_json") or {}).get("violation_type")
                    for item in result["violations"]
                ]
                self.assertIn("verification_failed_but_claimed_success", violation_types)
            finally:
                service.close()

    def test_stop_records_violation_when_change_is_not_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                payload = {"prompt": "实现这个功能", "session_id": "s1", "cwd": tmp}
                workflow_id = service.start_task_from_prompt(payload)["workflow_id"]
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg feature src", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "cwd": tmp})

                result = service.observe_stop({"session_id": "s1", "cwd": tmp, "last_assistant_message": "已完成"})
                violation_types = [
                    (item.get("metadata_json") or {}).get("violation_type")
                    for item in result["violations"]
                ]
                self.assertIn("changed_without_verification", violation_types)

                context = service.prompt_context("继续", cwd=tmp, session_id="s1")
                self.assertIn("用户需求：继续", context)
                self.assertIn("遵循以下规则：", context)
                self.assertNotIn("Runtime control:", context)
                self.assertEqual(service.ledger.latest_state_for("workflow", workflow_id), "failed")
                self.assertIsNone(service.runtime_status(cwd=tmp, session_id="s1")["active_workflow"])
            finally:
                service.close()

    def test_stop_records_violation_when_answering_without_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.start_task_from_prompt({"prompt": "修复这个 bug", "session_id": "s1", "cwd": tmp})
                result = service.observe_stop({"session_id": "s1", "cwd": tmp, "last_assistant_message": "已完成"})
                violation_types = [
                    (item.get("metadata_json") or {}).get("violation_type")
                    for item in result["violations"]
                ]
                self.assertIn("answered_without_inspection", violation_types)
            finally:
                service.close()

    def test_memory_statement_does_not_start_observed_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                for prompt in [
                    "经验：工程任务必须先 inspect，再最小修改，最后跑 unittest。",
                    "[trace-rerun-0098/project_exp] 经验：工程任务必须先 inspect，再最小修改，最后跑 unittest。",
                    "临时测试：api_key = sk-test-123 只是验证脱敏。",
                    "[real-0010/temporary_debug] 这次只需要临时打开 debug 日志，之后可以关闭。",
                    "[real-0014/governance_living_policy] 治理规则不能是死的，要能通过动态 policy 自我修复准入和准出。",
                    "[real-0026/water_gate] 水利工程经验：闸门调度异常时先核对上下游水位、传感器读数和执行机构状态。",
                ]:
                    result = service.start_task_from_prompt({"prompt": prompt, "session_id": "s1", "cwd": tmp})
                    self.assertFalse(result["started"], prompt)
                    self.assertIn(result["reason"], {"not_engineering_task", "runtime_skill_not_needed"})
            finally:
                service.close()

    def test_stop_records_violation_when_failed_verification_is_claimed_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.start_task_from_prompt({"prompt": "修复测试失败", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use(
                    {
                        "tool": "functions.exec_command",
                        "command": "python3 -m unittest discover -s tests -v",
                        "stdout": "FAILED (failures=1)",
                        "exit_code": 1,
                        "session_id": "s1",
                        "cwd": tmp,
                    }
                )

                result = service.observe_stop({"session_id": "s1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})
                violation_types = [
                    (item.get("metadata_json") or {}).get("violation_type")
                    for item in result["violations"]
                ]
                self.assertIn("verification_failed_but_claimed_success", violation_types)
            finally:
                service.close()

    def test_verified_workflow_completes_and_learns_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                payload = {"prompt": "修复测试失败", "session_id": "s1", "cwd": tmp}
                workflow_id = service.start_task_from_prompt(payload)["workflow_id"]
                service.prompt_context("修复测试失败", cwd=tmp, session_id="s1")
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "session_id": "s1", "cwd": tmp})
                result = service.observe_stop({"session_id": "s1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})

                self.assertEqual(result["violations"], [])
                self.assertEqual(result["runtime_skill_feedback"]["metadata_json"]["outcome"], "success")
                self.assertEqual(service.ledger.latest_state_for("workflow", workflow_id), "completed")
                skills = service.ledger.list_cognitive_records(layer="skill", status="active", limit=20)
                recipes = [item for item in skills if item.get("record_type") == "verification_recipe"]
                self.assertTrue(recipes)
                recipe_metadata = recipes[0].get("metadata_json") or {}
                self.assertIn("unittest", recipe_metadata["recipe"][0])
                self.assertEqual(recipe_metadata["exit_code"], None)
                self.assertIn("verification_stdout_preview", recipe_metadata)
                candidate_skills = service.ledger.list_cognitive_records(layer="skill", status="candidate", limit=20)
                dynamic_skills = [item for item in candidate_skills if item.get("record_type") == "dynamic_skill"]
                self.assertTrue(dynamic_skills)
                self.assertEqual(dynamic_skills[0]["status"], "candidate")
                skill_metadata = dynamic_skills[0].get("metadata_json") or {}
                self.assertEqual(skill_metadata["skill_type"], "dynamic_skill")
                self.assertTrue(skill_metadata["review_required"])
                self.assertEqual(skill_metadata["source_workflow_ids"], [workflow_id])
                self.assertIn("procedure", skill_metadata)
                self.assertIn("verification", skill_metadata)
                self.assertIn("anti_patterns", skill_metadata)
                self.assertIn("python3 -m unittest discover -s tests -v", skill_metadata["verification"])
            finally:
                service.close()

    def test_workflow_violation_records_runtime_skill_failure_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                payload = {"prompt": "实现这个功能", "session_id": "s1", "turn_id": "t1", "cwd": tmp}
                service.start_task_from_prompt(payload)
                service.prompt_context("实现这个功能", cwd=tmp, session_id="s1", turn_id="t1")
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg feature src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})

                result = service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成"})

                self.assertEqual(result["runtime_skill_feedback"]["metadata_json"]["outcome"], "failure")
                injection = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ][0]
                self.assertEqual(injection["metadata_json"]["feedback_status"], "failure")
                self.assertEqual(injection["metadata_json"]["feedback_dimensions"]["execution_compliance"], "failed")
            finally:
                service.close()

    def test_runtime_status_reports_active_workflow_and_open_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.start_task_from_prompt({"prompt": "实现这个功能", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg feature src", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "cwd": tmp, "last_assistant_message": "已完成"})

                status = service.runtime_status(cwd=tmp, session_id="s1")
                self.assertIsNone(status["active_workflow"])
                self.assertTrue(status["open_violations"])
                self.assertEqual((status["open_violations"][0].get("metadata_json") or {})["violation_type"], "changed_without_verification")
            finally:
                service.close()

    def test_workflow_violations_do_not_leak_between_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.start_task_from_prompt({"prompt": "实现这个功能", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg feature src", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "cwd": tmp, "last_assistant_message": "已完成"})

                s1_status = service.runtime_status(cwd=tmp, session_id="s1")
                s2_status = service.runtime_status(cwd=tmp, session_id="s2")

                self.assertTrue(s1_status["open_violations"])
                violations = service.workflow_violations(session_id="s1")
                self.assertTrue(violations)
                self.assertIsNone(s2_status["active_workflow"])
                self.assertEqual(s2_status["open_violations"], [])
                metadata = s1_status["open_violations"][0].get("metadata_json") or {}
                self.assertEqual(metadata["session_id"], "s1")
                self.assertEqual((violations[0].get("metadata_json") or {})["session_id"], "s1")
            finally:
                service.close()

    def test_turn_id_prevents_cross_task_observation_leakage(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.start_task_from_prompt({"prompt": "修复第一个 bug", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                second = service.start_task_from_prompt({"prompt": "修复第二个 bug", "session_id": "s1", "turn_id": "t2", "cwd": tmp})
                self.assertTrue(first["started"])
                self.assertTrue(second["started"])

                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg second src", "session_id": "s1", "turn_id": "t2", "cwd": tmp})
                first_workflow = service.ledger.get_cognitive_record(first["workflow_id"])
                second_workflow = service.ledger.get_cognitive_record(second["workflow_id"])
                self.assertNotIn("inspect_repository", first_workflow["metadata_json"]["completed_steps"])
                self.assertIn("inspect_repository", second_workflow["metadata_json"]["completed_steps"])
            finally:
                service.close()

    def test_turn_id_prevents_cross_task_runtime_control_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.start_task_from_prompt({"prompt": "修复第一个 bug", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                second = service.start_task_from_prompt({"prompt": "修复第二个 bug", "session_id": "s1", "turn_id": "t2", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg first src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})

                first_context = service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t1")
                second_context = service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t2")
                self.assertNotIn("Runtime control:", first_context)
                self.assertNotIn("Runtime control:", second_context)
                self.assertNotIn("pending_required_step:", first_context)
                self.assertNotIn("pending_required_step:", second_context)
                self.assertNotIn(first["workflow_id"], second_context)
                self.assertIn("用户需求：继续", first_context)
                self.assertIn("用户需求：继续", second_context)
                self.assertNotEqual(first["workflow_id"], second["workflow_id"])
            finally:
                service.close()

    def test_new_prompt_without_turn_id_replaces_stale_active_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.start_task_from_prompt({"prompt": "修复第一个 bug", "session_id": "s1", "cwd": tmp})
                second = service.start_task_from_prompt({"prompt": "修复第二个 bug", "session_id": "s1", "cwd": tmp})

                self.assertTrue(first["started"])
                self.assertTrue(second["started"])
                self.assertNotEqual(first["workflow_id"], second["workflow_id"])
                self.assertEqual(service.ledger.latest_state_for("workflow", first["workflow_id"]), "cancelled")
                self.assertEqual(service.ledger.latest_state_for("workflow", second["workflow_id"]), "running")
            finally:
                service.close()

    def test_non_resume_prompt_without_turn_id_does_not_reuse_stale_runtime_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.start_task_from_prompt({"prompt": "修复第一个 bug", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg first src", "session_id": "s1", "cwd": tmp})

                context = service.prompt_context("我的回答语言偏好是什么？", cwd=tmp, session_id="s1")

                self.assertNotIn(first["workflow_id"], context)
                self.assertNotIn("Codex Runtime Control", context)
            finally:
                service.close()

    def test_low_confidence_observation_is_soft_evidence_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                workflow_id = service.start_task_from_prompt({"prompt": "修复这个 bug", "session_id": "s1", "turn_id": "t1", "cwd": tmp})["workflow_id"]
                service.observe_tool_use({"tool_name": "functions.exec_command", "stdout": "some log mentions rg but no command", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                workflow = service.ledger.get_cognitive_record(workflow_id)
                metadata = workflow["metadata_json"]
                self.assertNotIn("inspect_repository", metadata["completed_steps"])
                self.assertTrue(metadata["observations"][0]["soft_evidence"])
            finally:
                service.close()

    def test_active_runtime_control_recommends_learned_verification_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.start_task_from_prompt({"prompt": "修复测试失败", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})
                self.assertEqual(service.ledger.latest_state_for("workflow", first["workflow_id"]), "completed")

                second = service.start_task_from_prompt({"prompt": "实现另一个功能", "session_id": "s1", "turn_id": "t2", "cwd": tmp})
                self.assertTrue(second["started"])
                context = service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t2")
                self.assertNotIn("Recommended dynamic skill:", context)
                self.assertNotIn("Recommended verification recipe:", context)
                self.assertNotIn("python3 -m unittest discover -s tests -v", context)
                learned_recipes = service.runtime_status(cwd=tmp, session_id="s1", turn_id="t2")["learned_recipes"]
                self.assertTrue(
                    any("python3 -m unittest discover -s tests -v" in str(item.get("metadata_json") or {}) for item in learned_recipes)
                )

                dynamic_skill = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="skill", status="candidate", limit=20)
                    if item.get("record_type") == "dynamic_skill"
                ][0]
                service.ledger.set_cognitive_record_status(str(dynamic_skill["id"]), "active", {"review_required": False})
                context_after_review = service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t2")
                self.assertNotIn("Recommended dynamic skill:", context_after_review)
                self.assertNotIn("Python unittest change workflow", context_after_review)
                self.assertIn("用户需求：继续", context_after_review)
            finally:
                service.close()

    def test_recommended_recipe_reuse_updates_counts_and_strength(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.start_task_from_prompt({"prompt": "修复测试失败", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})
                self.assertEqual(service.ledger.latest_state_for("workflow", first["workflow_id"]), "completed")
                recipe = [item for item in service.ledger.list_cognitive_records(layer="skill", status="active", limit=20) if item.get("record_type") == "verification_recipe"][0]
                initial_strength = recipe["strength"]

                second = service.start_task_from_prompt({"prompt": "实现另一个功能", "session_id": "s1", "turn_id": "t2", "cwd": tmp})
                service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t2")
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t2", "cwd": tmp})

                updated = service.ledger.get_cognitive_record(recipe["id"])
                metadata = updated["metadata_json"]
                self.assertEqual(metadata["reuse_count"], 1)
                self.assertEqual(metadata["success_count"], 2)
                self.assertEqual(metadata["failure_count"], 0)
                self.assertEqual(metadata["last_reuse_workflow_id"], second["workflow_id"])
                self.assertEqual(metadata["last_reuse_matched_command"], "python3 -m unittest discover -s tests -v")
                self.assertEqual(metadata["last_reuse_command_source"], "cmd")
                self.assertEqual(metadata["last_reuse_observation_confidence"], 0.9)
                self.assertEqual(metadata["last_reuse_exit_code"], 0)
                self.assertTrue(metadata["last_reuse_succeeded"])
                self.assertGreater(updated["strength"], initial_strength)
                self.assertTrue(metadata["last_used_at"])
                audit_records = service.ledger.list_cognitive_records(layer="audit", status="active", limit=50)
                self.assertTrue([item for item in audit_records if item.get("record_type") == "recipe_recommendation"])
                self.assertTrue([item for item in audit_records if item.get("record_type") == "recipe_reuse"])
            finally:
                service.close()

    def test_failed_recommended_recipe_reuse_lowers_strength(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.start_task_from_prompt({"prompt": "修复测试失败", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})
                recipe = [item for item in service.ledger.list_cognitive_records(layer="skill", status="active", limit=20) if item.get("record_type") == "verification_recipe"][0]
                initial_strength = recipe["strength"]

                service.start_task_from_prompt({"prompt": "实现另一个功能", "session_id": "s1", "turn_id": "t2", "cwd": tmp})
                service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t2")
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "FAILED", "exit_code": 1, "session_id": "s1", "turn_id": "t2", "cwd": tmp})

                updated = service.ledger.get_cognitive_record(recipe["id"])
                metadata = updated["metadata_json"]
                self.assertEqual(metadata["reuse_count"], 1)
                self.assertEqual(metadata["success_count"], 1)
                self.assertEqual(metadata["failure_count"], 1)
                self.assertFalse(metadata["last_reuse_succeeded"])
                self.assertLess(updated["strength"], initial_strength)
            finally:
                service.close()

    def test_similar_but_different_verification_command_does_not_count_as_recipe_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.start_task_from_prompt({"prompt": "修复测试失败", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})
                recipe = [item for item in service.ledger.list_cognitive_records(layer="skill", status="active", limit=20) if item.get("record_type") == "verification_recipe"][0]

                service.start_task_from_prompt({"prompt": "实现另一个功能", "session_id": "s1", "turn_id": "t2", "cwd": tmp})
                service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t2")
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest tests.test_runtime_observer -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t2", "cwd": tmp})

                updated = service.ledger.get_cognitive_record(recipe["id"])
                metadata = updated["metadata_json"]
                self.assertEqual(metadata["reuse_count"], 0)
                self.assertEqual(metadata["success_count"], 1)
                self.assertEqual(metadata["failure_count"], 0)
            finally:
                service.close()

    def test_command_mentioned_in_stdout_does_not_count_as_recipe_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.start_task_from_prompt({"prompt": "修复测试失败", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})
                recipe = [item for item in service.ledger.list_cognitive_records(layer="skill", status="active", limit=20) if item.get("record_type") == "verification_recipe"][0]

                workflow = service.start_task_from_prompt({"prompt": "实现另一个功能", "session_id": "s1", "turn_id": "t2", "cwd": tmp})
                service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t2")
                service.observe_tool_use(
                    {
                        "tool_name": "functions.exec_command",
                        "stdout": "Suggested command: python3 -m unittest discover -s tests -v\nOK",
                        "exit_code": 0,
                        "session_id": "s1",
                        "turn_id": "t2",
                        "cwd": tmp,
                    }
                )

                updated = service.ledger.get_cognitive_record(recipe["id"])
                metadata = updated["metadata_json"]
                self.assertEqual(metadata["reuse_count"], 0)
                self.assertNotIn("execute_and_verify", service.ledger.get_cognitive_record(workflow["workflow_id"])["metadata_json"]["completed_steps"])
            finally:
                service.close()

    def test_successful_verify_before_stop_prevents_changed_without_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                workflow_id = service.start_task_from_prompt({"prompt": "实现这个功能", "session_id": "s1", "turn_id": "t1", "cwd": tmp})["workflow_id"]
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg feature src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use(
                    {
                        "tool_name": "functions.exec_command",
                        "cmd": "python3 -m unittest discover -s tests -v",
                        "stdout": "OK",
                        "exit_code": 0,
                        "session_id": "s1",
                        "turn_id": "t1",
                        "cwd": tmp,
                    }
                )
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})
                open_types = [
                    (item.get("metadata_json") or {}).get("violation_type")
                    for item in service.ledger.list_open_workflow_violations(workflow_id=workflow_id)
                ]
                self.assertNotIn("changed_without_verification", open_types)
            finally:
                service.close()

    def test_engineering_acceptance_coverage_is_recorded_when_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                workflow_id = service.start_task_from_prompt({"prompt": "实现这个功能", "session_id": "s1", "turn_id": "t1", "cwd": tmp})["workflow_id"]
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg feature src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use(
                    {
                        "tool_name": "functions.exec_command",
                        "cmd": "python3 -m unittest discover -s tests -v",
                        "stdout": "OK",
                        "exit_code": 0,
                        "session_id": "s1",
                        "turn_id": "t1",
                        "cwd": tmp,
                    }
                )

                result = service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})
                coverage = result["acceptance_coverage"]

                self.assertTrue(coverage["summary"]["complete"])
                self.assertEqual({item["status"] for item in coverage["criteria"]}, {"covered"})
                self.assertEqual(result["violations"], [])
                metadata = service.ledger.get_cognitive_record(workflow_id)["metadata_json"]
                self.assertEqual(metadata["acceptance_coverage"]["summary"]["covered"], coverage["summary"]["covered"])
            finally:
                service.close()

    def test_ui_acceptance_missing_records_coverage_and_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                workflow_id = service.start_task_from_prompt({"prompt": "修改 UI 页面布局", "session_id": "s1", "turn_id": "t1", "cwd": tmp})["workflow_id"]
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg layout src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use(
                    {
                        "tool_name": "functions.exec_command",
                        "cmd": "pnpm run typecheck",
                        "stdout": "OK",
                        "exit_code": 0,
                        "session_id": "s1",
                        "turn_id": "t1",
                        "cwd": tmp,
                    }
                )

                active_coverage = service.runtime_status(cwd=tmp, session_id="s1", turn_id="t1")["active_workflow"]["acceptance_coverage"]
                self.assertTrue(
                    any(item["status"] == "missing" and "Chrome" in item["criterion_text"] for item in active_coverage["criteria"])
                )

                result = service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，typecheck 通过"})
                coverage = result["acceptance_coverage"]
                missing = [item for item in coverage["criteria"] if item["status"] == "missing"]
                violations = [(item.get("metadata_json") or {}) for item in result["violations"]]
                acceptance_missing = [item for item in violations if item.get("violation_type") == "acceptance_missing"]

                self.assertTrue(missing)
                self.assertTrue(any("Chrome" in item["criterion_text"] and "browser_verify" in item["missing_steps"] for item in missing))
                self.assertTrue(acceptance_missing)
                self.assertTrue(any((item.get("evidence") or {}).get("attribution_signal") == "acceptance_missing" for item in acceptance_missing))
                metadata = service.ledger.get_cognitive_record(workflow_id)["metadata_json"]
                self.assertEqual(metadata["acceptance_coverage"]["summary"]["missing"], coverage["summary"]["missing"])
                self.assertEqual(service.ledger.latest_state_for("workflow", workflow_id), "failed")
            finally:
                service.close()

    def test_typecheck_counts_as_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                workflow_id = service.start_task_from_prompt({"prompt": "修改 UI", "session_id": "s1", "turn_id": "t1", "cwd": tmp})["workflow_id"]
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg logo src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "pnpm run typecheck", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                workflow = service.ledger.get_cognitive_record(workflow_id)
                metadata = workflow["metadata_json"]
                self.assertIn("execute_and_verify", metadata["completed_steps"])
                self.assertTrue(metadata["verified"])
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，typecheck 通过"})
                self.assertFalse(service.ledger.list_open_workflow_violations(workflow_id=workflow_id))
            finally:
                service.close()

    def test_runtime_skill_required_steps_drive_fullstack_workflow_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{"typecheck":"tsc --noEmit"}}\n', encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "api.py").write_text("def list_items():\n    return []\n", encoding="utf-8")
            service = _service(tmp)
            try:
                payload = {
                    "prompt": "实现 API 分页接口和前端分类筛选，并用浏览器验证",
                    "session_id": "s1",
                    "turn_id": "t1",
                    "cwd": tmp,
                }
                workflow_id = service.start_task_from_prompt(payload)["workflow_id"]
                service.prompt_context(payload["prompt"], cwd=tmp, session_id="s1", turn_id="t1")
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg -n \"page\" src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "pnpm run typecheck", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "chrome", "command": "browser screenshot verifies pagination filter", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})

                result = service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，后端测试、前端 typecheck 和浏览器验证均通过"})

                self.assertEqual(result["violations"], [])
                metadata = service.ledger.get_cognitive_record(workflow_id)["metadata_json"]
                self.assertEqual(metadata["missing_required_steps"], [])
                self.assertIn("backend_test", metadata["completed_steps"])
                self.assertIn("frontend_typecheck", metadata["completed_steps"])
                self.assertIn("browser_verify", metadata["completed_steps"])
                self.assertEqual(service.ledger.latest_state_for("workflow", workflow_id), "completed")
            finally:
                service.close()

    def test_missing_fullstack_runtime_skill_step_records_specific_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"scripts":{"typecheck":"tsc --noEmit"}}\n', encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "api.py").write_text("def list_items():\n    return []\n", encoding="utf-8")
            service = _service(tmp)
            try:
                payload = {
                    "prompt": "实现 API 分页接口和前端分类筛选，并用浏览器验证",
                    "session_id": "s1",
                    "turn_id": "t1",
                    "cwd": tmp,
                }
                service.start_task_from_prompt(payload)
                service.prompt_context(payload["prompt"], cwd=tmp, session_id="s1", turn_id="t1")
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg -n \"page\" src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})

                result = service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成"})
                violations = [(item.get("metadata_json") or {}) for item in result["violations"]]
                missing = [item for item in violations if item.get("violation_type") == "missing_required_workflow_step"]

                self.assertTrue(missing)
                self.assertIn("frontend_typecheck", {item["evidence"].get("missing_step") for item in missing})
                self.assertIn("browser_verify", {item["evidence"].get("missing_step") for item in missing})
            finally:
                service.close()

    def test_prune_runtime_records_removes_audit_records_and_optionally_recipes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.start_task_from_prompt({"prompt": "修复测试失败", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "exit_code": 0, "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})

                self.assertTrue([item for item in service.ledger.list_cognitive_records(layer="audit", status="active", limit=50) if item.get("record_type") == "workflow_observation"])
                service.prompt_context("继续修复测试失败", cwd=tmp, session_id="s1", turn_id="t1")
                service.apply_natural_feedback("这个方法很好", session_id="s1", turn_id="t1")
                self.assertTrue([item for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=50) if item.get("record_type") == "injection"])
                self.assertTrue([item for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=50) if item.get("record_type") == "feedback"])
                self.assertTrue([item for item in service.ledger.list_cognitive_records(layer="skill", status="active", limit=50) if item.get("record_type") == "verification_recipe"])
                workflow_records = [item for item in service.ledger.list_cognitive_records(layer="workflow", status="completed", limit=50) if item.get("record_type") == "observed_workflow"]
                self.assertTrue((workflow_records[0].get("metadata_json") or {}).get("observations"))

                pruned = service.prune_runtime()
                self.assertGreater(pruned["counts"]["workflow_observation"], 0)
                self.assertGreater(pruned["counts"]["runtime_skill_layer_injection"], 0)
                self.assertGreater(pruned["counts"]["runtime_skill_layer_feedback"], 0)
                self.assertGreater(pruned["counts"]["workflow_metadata_observations"], 0)
                self.assertFalse([item for item in service.ledger.list_cognitive_records(layer="audit", status="active", limit=50) if item.get("record_type") == "workflow_observation"])
                self.assertFalse([item for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=50)])
                self.assertTrue([item for item in service.ledger.list_cognitive_records(layer="skill", status="active", limit=50) if item.get("record_type") == "verification_recipe"])
                workflow_records = [item for item in service.ledger.list_cognitive_records(layer="workflow", status="completed", limit=50) if item.get("record_type") == "observed_workflow"]
                self.assertEqual((workflow_records[0].get("metadata_json") or {}).get("observations"), [])

                pruned_with_recipes = service.prune_runtime(include_recipes=True)
                self.assertEqual(pruned_with_recipes["counts"]["verification_recipe"], 1)
                self.assertFalse([item for item in service.ledger.list_cognitive_records(layer="skill", status="active", limit=50) if item.get("record_type") == "verification_recipe"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
