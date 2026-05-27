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
                self.assertIn("unittest", (recipes[0].get("metadata_json") or {})["recipe"][0])
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
                self.assertEqual((status["open_violations"][0].get("metadata_json") or {})["violation_type"], "changed_without_verification")
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
