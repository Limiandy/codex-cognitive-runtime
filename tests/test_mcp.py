import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class McpTest(unittest.TestCase):
    def test_mcp_initialize_and_list_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "PYTHONPATH": "src",
                "CODEX_MEMORY_FAKE_MODEL": "1",
                "CODEX_MEMORY_STATE_DIR": tmp,
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_memory.mcp_server"],
                cwd=".",
                env={**os.environ, **env},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdin is not None
                assert proc.stdout is not None
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
                proc.stdin.flush()
                first = json.loads(proc.stdout.readline())
                second = json.loads(proc.stdout.readline())
                self.assertEqual(first["result"]["serverInfo"]["name"], "codex-memory")
                names = {tool["name"] for tool in second["result"]["tools"]}
                self.assertIn("codex_memory_search", names)
                self.assertIn("codex_memory_diagnostics", names)
                self.assertIn("codex_memory_runtime_status", names)
                self.assertIn("codex_memory_runtime_violations", names)
                self.assertIn("codex_memory_verification_recipes", names)
                self.assertIn("codex_memory_runtime_skills", names)
                self.assertIn("codex_memory_runtime_skill_feedback", names)
                self.assertIn("codex_memory_seed_skills", names)
                self.assertIn("codex_memory_dynamic_skills", names)
                self.assertIn("codex_memory_promote_dynamic_skill", names)
                self.assertIn("codex_memory_disable_seed_skill", names)
                self.assertIn("codex_memory_promote", names)
                self.assertIn("codex_memory_audit", names)
                self.assertFalse(any("mempalace" in name for name in names))
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

    def test_mcp_status_and_queue_are_lightweight(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "PYTHONPATH": "src",
                "CODEX_MEMORY_STATE_DIR": tmp,
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_memory.mcp_server"],
                cwd=".",
                env={**os.environ, **env},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdin is not None
                assert proc.stdout is not None
                calls = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "codex_memory_status", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "codex_memory_queue", "arguments": {"limit": 5}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "codex_memory_runtime_status", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "codex_memory_verification_recipes", "arguments": {"limit": 5}},
                    },
                ]
                for call in calls:
                    proc.stdin.write(json.dumps(call) + "\n")
                proc.stdin.flush()
                json.loads(proc.stdout.readline())
                status = json.loads(proc.stdout.readline())
                queue = json.loads(proc.stdout.readline())
                runtime_status = json.loads(proc.stdout.readline())
                recipes = json.loads(proc.stdout.readline())
                status_text = json.loads(status["result"]["content"][0]["text"])
                queue_text = json.loads(queue["result"]["content"][0]["text"])
                runtime_status_text = json.loads(runtime_status["result"]["content"][0]["text"])
                recipes_text = json.loads(recipes["result"]["content"][0]["text"])
                self.assertEqual(status_text["store"]["primary"], "ledger")
                self.assertFalse(status_text["privacy"]["store_raw_events"])
                self.assertTrue(status_text["privacy"]["runtime_observer_enabled"])
                self.assertEqual(queue_text, [])
                self.assertIn("active_workflow", runtime_status_text)
                self.assertEqual(recipes_text, [])
                self.assertTrue((Path(tmp) / "ledger.sqlite3").exists())
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

    def test_dangerous_tool_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "PYTHONPATH": "src",
                "CODEX_MEMORY_STATE_DIR": tmp,
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_memory.mcp_server"],
                cwd=".",
                env={**os.environ, **env},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdin is not None
                assert proc.stdout is not None
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
                proc.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "codex_memory_delete", "arguments": {"memory_id": "mem_test"}},
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
                json.loads(proc.stdout.readline())
                response = json.loads(proc.stdout.readline())
                self.assertEqual(response["error"]["data"]["error_code"], "mcp_admin_tool_disabled")
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

    def test_write_tool_requires_write_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "PYTHONPATH": "src",
                "CODEX_MEMORY_STATE_DIR": tmp,
                "CODEX_MEMORY_FAKE_MODEL": "1",
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_memory.mcp_server"],
                cwd=".",
                env={**os.environ, **env},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdin is not None
                assert proc.stdout is not None
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
                proc.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "codex_memory_ingest", "arguments": {"text": "默认使用中文回答"}},
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
                json.loads(proc.stdout.readline())
                response = json.loads(proc.stdout.readline())
                self.assertEqual(response["error"]["data"]["error_code"], "mcp_write_tool_disabled")
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

    def test_enabled_write_tool_records_action_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "PYTHONPATH": "src",
                "CODEX_MEMORY_STATE_DIR": tmp,
                "CODEX_MEMORY_FAKE_MODEL": "1",
                "CODEX_MEMORY_ENABLE_MCP_WRITE_TOOLS": "1",
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_memory.mcp_server"],
                cwd=".",
                env={**os.environ, **env},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdin is not None
                assert proc.stdout is not None
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
                proc.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "codex_memory_ingest", "arguments": {"text": "默认使用中文回答"}},
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
                json.loads(proc.stdout.readline())
                response = json.loads(proc.stdout.readline())
                text = json.loads(response["result"]["content"][0]["text"])
                self.assertTrue(text["mcp_action"]["action_applied"])
                self.assertEqual(text["mcp_action"]["permission_level"], "write")
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

    def test_invalid_args_return_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "PYTHONPATH": "src",
                "CODEX_MEMORY_STATE_DIR": tmp,
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_memory.mcp_server"],
                cwd=".",
                env={**os.environ, **env},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdin is not None
                assert proc.stdout is not None
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
                proc.stdin.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "codex_memory_queue", "arguments": {"limit": 0}},
                        }
                    )
                    + "\n"
                )
                proc.stdin.flush()
                json.loads(proc.stdout.readline())
                response = json.loads(proc.stdout.readline())
                self.assertEqual(response["error"]["data"]["error_code"], "invalid_arguments")
            finally:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
