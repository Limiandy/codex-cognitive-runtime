import tempfile
import unittest
from pathlib import Path

from codex_memory.config import Config
from codex_memory.service import MemoryService


def _config(
    tmp: str,
    store_raw_events: bool = False,
    enable_runtime_observer: bool = True,
    store_runtime_observation_previews: bool = False,
    strict_privacy: bool = False,
) -> Config:
    return Config(
        model="gpt-5.4-mini",
        state_dir=Path(tmp),
        ledger_path=Path(tmp) / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
        store_raw_events=store_raw_events,
        enable_runtime_observer=enable_runtime_observer,
        store_runtime_observation_previews=store_runtime_observation_previews,
        strict_privacy=strict_privacy,
    )


class PrivacyTest(unittest.TestCase):
    def test_event_payload_is_sanitized_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                event_id = service.record_event(
                    "manual",
                    {
                        "text": "use token=supersecretvalue1234567890",
                        "api_key": "sk-secretsecretsecret",
                        "attachment": "/private/tmp/raw.bin",
                    },
                    processed=True,
                )
                payload = service.ledger.get_event(event_id)["payload_json"]
                self.assertFalse(payload["_raw_payload_stored"])
                self.assertIn("_omitted_keys", payload)
                self.assertNotIn("api_key", payload)
                self.assertNotIn("supersecretvalue1234567890", str(payload))
                self.assertIn("[REDACTED]", payload["text"])
            finally:
                service.close()

    def test_raw_event_storage_requires_explicit_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, store_raw_events=True))
            try:
                event_id = service.record_event(
                    "manual",
                    {"text": "token=supersecretvalue1234567890", "api_key": "sk-secretsecretsecret"},
                    processed=True,
                )
                payload = service.ledger.get_event(event_id)["payload_json"]
                self.assertTrue(payload["_raw_payload_stored"])
                self.assertEqual(payload["api_key"], "sk-secretsecretsecret")
                self.assertIn("supersecretvalue1234567890", payload["text"])
            finally:
                service.close()

    def test_cognitive_audit_uses_payload_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                service.process_event(
                    "evt_privacy",
                    "manual",
                    {
                        "text": "token=supersecretvalue1234567890",
                        "api_key": "sk-secretsecretsecret",
                        "customer": "private customer",
                    },
                )
                records = service.ledger.list_cognitive_records(layer="audit", limit=10)
                event_records = [item for item in records if item.get("source_id") == "evt_privacy"]
                self.assertTrue(event_records)
                rendered = str(event_records[0])
                self.assertIn("payload_summary", rendered)
                self.assertNotIn("supersecretvalue1234567890", rendered)
                self.assertNotIn("sk-secretsecretsecret", rendered)
                self.assertNotIn("private customer", rendered)
            finally:
                service.close()

    def test_runtime_observation_redacts_output_previews_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                service.start_task_from_prompt({"prompt": "修复这个 bug", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use(
                    {
                        "tool_name": "functions.exec_command",
                        "cmd": "python3 -m unittest discover -s tests -v",
                        "stdout": "PRIVATE_CUSTOMER_OUTPUT OK",
                        "stderr": "PRIVATE_ERROR_OUTPUT",
                        "exit_code": 0,
                        "session_id": "s1",
                        "turn_id": "t1",
                        "cwd": tmp,
                    }
                )
                records = service.ledger.list_cognitive_records(layer="audit", status="active", limit=20)
                rendered = str([item for item in records if item.get("record_type") == "workflow_observation"])
                self.assertNotIn("PRIVATE_CUSTOMER_OUTPUT", rendered)
                self.assertNotIn("PRIVATE_ERROR_OUTPUT", rendered)
                self.assertIn("stdout_sha256", rendered)
                self.assertIn("stderr_chars", rendered)
            finally:
                service.close()

    def test_runtime_observation_redacts_secret_like_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp))
            try:
                service.start_task_from_prompt({"prompt": "修复这个 bug", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use(
                    {
                        "tool_name": "functions.exec_command",
                        "cmd": "python3 -m unittest discover -s tests -v --api_key=sk-secretsecretsecret",
                        "stdout": "OK",
                        "exit_code": 0,
                        "session_id": "s1",
                        "turn_id": "t1",
                        "cwd": tmp,
                    }
                )
                records = service.ledger.list_cognitive_records(layer="audit", status="active", limit=20)
                rendered = str([item for item in records if item.get("record_type") == "workflow_observation"])
                self.assertNotIn("sk-secretsecretsecret", rendered)
                self.assertIn("[REDACTED]", rendered)
            finally:
                service.close()

    def test_runtime_observation_preview_storage_requires_explicit_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, store_runtime_observation_previews=True))
            try:
                service.start_task_from_prompt({"prompt": "修复这个 bug", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use(
                    {
                        "tool_name": "functions.exec_command",
                        "cmd": "python3 -m unittest discover -s tests -v",
                        "stdout": "PRIVATE_CUSTOMER_OUTPUT OK",
                        "exit_code": 0,
                        "session_id": "s1",
                        "turn_id": "t1",
                        "cwd": tmp,
                    }
                )
                records = service.ledger.list_cognitive_records(layer="audit", status="active", limit=20)
                rendered = str([item for item in records if item.get("record_type") == "workflow_observation"])
                self.assertIn("PRIVATE_CUSTOMER_OUTPUT", rendered)
            finally:
                service.close()

    def test_runtime_observer_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, enable_runtime_observer=False))
            try:
                started = service.start_task_from_prompt({"prompt": "修复这个 bug", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                self.assertEqual(started["reason"], "runtime_observer_disabled")
                observed = service.observe_tool_use({"tool_name": "functions.exec_command", "cmd": "rg bug src", "session_id": "s1", "turn_id": "t1", "cwd": tmp})
                self.assertEqual(observed["reason"], "runtime_observer_disabled")
                context = service.prompt_context("继续", cwd=tmp, session_id="s1", turn_id="t1")
                self.assertNotIn("Runtime control:", context)
            finally:
                service.close()

    def test_strict_privacy_hashes_runtime_observation_and_injection_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(_config(tmp, strict_privacy=True))
            try:
                context = service.prompt_context("帮我画一个品牌 logo", cwd=tmp, session_id="s1")
                self.assertIn("Runtime Skill:", context)
                injection = [
                    item
                    for item in service.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=20)
                    if item.get("record_type") == "injection"
                ][0]
                metadata = injection["metadata_json"]
                self.assertIn("prompt_sha256", metadata)
                self.assertNotIn("prompt_preview", metadata)
                self.assertIn("cwd_sha256", metadata)
                self.assertNotIn("cwd", metadata)
                self.assertNotIn("strategy", metadata["skill"])
                feedback = service.apply_natural_feedback("很好", session_id="s1")
                evidence = feedback["runtime_skill_feedback"]["metadata_json"]["evidence"]
                self.assertIn("prompt_sha256", evidence)
                self.assertNotIn("prompt_preview", evidence)

                service.start_task_from_prompt({"prompt": "修复这个 bug", "session_id": "s2", "turn_id": "t1", "cwd": tmp})
                service.observe_tool_use(
                    {
                        "tool_name": "functions.exec_command",
                        "cmd": "python3 -m unittest discover -s tests -v",
                        "stdout": "OK",
                        "exit_code": 0,
                        "session_id": "s2",
                        "turn_id": "t1",
                        "cwd": tmp,
                    }
                )
                records = service.ledger.list_cognitive_records(layer="audit", status="active", limit=20)
                rendered = str([item for item in records if item.get("record_type") == "workflow_observation"])
                self.assertIn("command_sha256", rendered)
                self.assertNotIn("python3 -m unittest", rendered)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
