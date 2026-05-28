import json
import tempfile
import unittest
from pathlib import Path

from codex_memory.config import Config
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


def _fixture(name: str, cwd: str) -> dict:
    path = Path(__file__).parent / "fixtures" / "hooks" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    data["cwd"] = cwd
    return data


class RuntimeObserverTest(unittest.TestCase):
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
                self.assertIn("Runtime control:", context)
                self.assertIn("pending_required_step: execute_and_verify", context)
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
                self.assertIn("Previous workflow violation:", context)
                self.assertIn("changed_without_verification", context)
                self.assertEqual(service.ledger.latest_state_for("workflow", workflow_id), "running")
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
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg failing_test tests", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "python3 -m unittest discover -s tests -v", "stdout": "OK", "session_id": "s1", "cwd": tmp})
                result = service.observe_stop({"session_id": "s1", "cwd": tmp, "last_assistant_message": "已完成，测试通过"})

                self.assertEqual(result["violations"], [])
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

    def test_runtime_status_reports_active_workflow_and_open_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                service.start_task_from_prompt({"prompt": "实现这个功能", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg feature src", "session_id": "s1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "cwd": tmp, "last_assistant_message": "已完成"})

                status = service.runtime_status(cwd=tmp, session_id="s1")
                self.assertEqual(status["active_workflow"]["pending_required_step"], "execute_and_verify")
                self.assertTrue(status["open_violations"])
                self.assertTrue(status["recent_observations"])
                self.assertEqual((status["open_violations"][0].get("metadata_json") or {})["violation_type"], "changed_without_verification")
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
                self.assertIn("pending_required_step: execute_and_verify", first_context)
                self.assertIn("pending_required_step: inspect_repository", second_context)
                self.assertNotIn(first["workflow_id"], second_context)
                self.assertNotEqual(first["workflow_id"], second["workflow_id"])
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
                self.assertIn("Recommended verification recipe:", context)
                self.assertIn("python3 -m unittest discover -s tests -v", context)

                dynamic_skill = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="skill", status="candidate", limit=20)
                    if item.get("record_type") == "dynamic_skill"
                ][0]
                service.ledger.set_cognitive_record_status(str(dynamic_skill["id"]), "active", {"review_required": False})
                context_after_review = service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t2")
                self.assertIn("Recommended dynamic skill:", context_after_review)
                self.assertIn("Python unittest change workflow", context_after_review)
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

    def test_successful_verify_resolves_changed_without_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                workflow_id = service.start_task_from_prompt({"prompt": "实现这个功能", "session_id": "s1", "turn_id": "t1", "cwd": tmp})["workflow_id"]
                service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg feature src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use({"tool_name": "functions.apply_patch", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_stop({"session_id": "s1", "turn_id": "t1", "cwd": tmp, "last_assistant_message": "已完成"})
                self.assertTrue(service.ledger.list_open_workflow_violations(workflow_id=workflow_id))

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
                open_types = [
                    (item.get("metadata_json") or {}).get("violation_type")
                    for item in service.ledger.list_open_workflow_violations(workflow_id=workflow_id)
                ]
                self.assertNotIn("changed_without_verification", open_types)
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
                self.assertTrue([item for item in service.ledger.list_cognitive_records(layer="skill", status="active", limit=50) if item.get("record_type") == "verification_recipe"])
                workflow_records = [item for item in service.ledger.list_cognitive_records(layer="workflow", status="completed", limit=50) if item.get("record_type") == "observed_workflow"]
                self.assertTrue((workflow_records[0].get("metadata_json") or {}).get("observations"))

                pruned = service.prune_runtime()
                self.assertGreater(pruned["counts"]["workflow_observation"], 0)
                self.assertGreater(pruned["counts"]["workflow_metadata_observations"], 0)
                self.assertFalse([item for item in service.ledger.list_cognitive_records(layer="audit", status="active", limit=50) if item.get("record_type") == "workflow_observation"])
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
