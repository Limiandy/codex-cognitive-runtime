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
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_cognitive_runtime.mcp_server"],
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
                self.assertEqual(first["result"]["serverInfo"]["name"], "codex-cognitive-runtime")
                names = {tool["name"] for tool in second["result"]["tools"]}
                self.assertIn("codex_cognitive_runtime_search", names)
                self.assertIn("codex_cognitive_runtime_diagnostics", names)
                self.assertIn("codex_cognitive_runtime_runtime_status", names)
                self.assertIn("codex_cognitive_runtime_runtime_violations", names)
                self.assertIn("codex_cognitive_runtime_verification_recipes", names)
                self.assertIn("codex_cognitive_runtime_runtime_skills", names)
                self.assertIn("codex_cognitive_runtime_runtime_skill_audit", names)
                self.assertIn("codex_cognitive_runtime_runtime_skill_feedback", names)
                self.assertIn("codex_cognitive_runtime_seed_skills", names)
                self.assertIn("codex_cognitive_runtime_seed_skill_stats", names)
                self.assertIn("codex_cognitive_runtime_dynamic_skills", names)
                self.assertIn("codex_cognitive_runtime_traces", names)
                self.assertIn("codex_cognitive_runtime_trace_show", names)
                self.assertIn("codex_cognitive_runtime_trace_events", names)
                self.assertIn("codex_cognitive_runtime_trace_summary", names)
                self.assertIn("codex_cognitive_runtime_trace_attribution", names)
                self.assertIn("codex_cognitive_runtime_trace_audit", names)
                self.assertIn("codex_cognitive_runtime_promote_dynamic_skill", names)
                self.assertIn("codex_cognitive_runtime_disable_seed_skill", names)
                self.assertIn("codex_cognitive_runtime_promote", names)
                self.assertIn("codex_cognitive_runtime_audit", names)
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
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
            }
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from codex_cognitive_runtime.config import load_config; from codex_cognitive_runtime.service import MemoryService; s=MemoryService(load_config()); s.prompt_context('帮我画一个品牌 logo', session_id='s1', turn_id='t1'); s.close()",
                ],
                cwd=".",
                env={**os.environ, **env},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=True,
            )
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_cognitive_runtime.mcp_server"],
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
                        "params": {"name": "codex_cognitive_runtime_status", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "codex_cognitive_runtime_queue", "arguments": {"limit": 5}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "codex_cognitive_runtime_runtime_status", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "codex_cognitive_runtime_verification_recipes", "arguments": {"limit": 5}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {"name": "codex_cognitive_runtime_dynamic_skill_stats", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": "codex_cognitive_runtime_runtime_skill_audit", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 8,
                        "method": "tools/call",
                        "params": {"name": "codex_cognitive_runtime_seed_skill_stats", "arguments": {}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {"name": "codex_cognitive_runtime_traces", "arguments": {"limit": 5}},
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {"name": "codex_cognitive_runtime_trace_audit", "arguments": {}},
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
                dynamic_stats = json.loads(proc.stdout.readline())
                runtime_audit = json.loads(proc.stdout.readline())
                seed_stats = json.loads(proc.stdout.readline())
                trace_list = json.loads(proc.stdout.readline())
                trace_audit = json.loads(proc.stdout.readline())
                status_text = json.loads(status["result"]["content"][0]["text"])
                queue_text = json.loads(queue["result"]["content"][0]["text"])
                runtime_status_text = json.loads(runtime_status["result"]["content"][0]["text"])
                recipes_text = json.loads(recipes["result"]["content"][0]["text"])
                dynamic_stats_text = json.loads(dynamic_stats["result"]["content"][0]["text"])
                runtime_audit_text = json.loads(runtime_audit["result"]["content"][0]["text"])
                seed_stats_text = json.loads(seed_stats["result"]["content"][0]["text"])
                trace_list_text = json.loads(trace_list["result"]["content"][0]["text"])
                trace_audit_text = json.loads(trace_audit["result"]["content"][0]["text"])
                self.assertEqual(status_text["store"]["primary"], "ledger")
                self.assertFalse(status_text["privacy"]["store_raw_events"])
                self.assertTrue(status_text["privacy"]["runtime_observer_enabled"])
                self.assertEqual(queue_text, [])
                self.assertIn("active_workflow", runtime_status_text)
                self.assertEqual(recipes_text, [])
                self.assertIn("by_status", dynamic_stats_text)
                self.assertIn("recent_candidates", dynamic_stats_text)
                self.assertIn("injection_count", runtime_audit_text)
                self.assertIn("by_trust_state", seed_stats_text)
                self.assertGreaterEqual(len(trace_list_text), 1)
                self.assertIn("trace_count", trace_audit_text)
                trace_id = trace_list_text[0]["id"]
                for index, name in enumerate(("codex_cognitive_runtime_trace_show", "codex_cognitive_runtime_trace_events", "codex_cognitive_runtime_trace_summary", "codex_cognitive_runtime_trace_attribution"), start=11):
                    proc.stdin.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": index,
                                "method": "tools/call",
                                "params": {"name": name, "arguments": {"trace_id": trace_id}},
                            }
                        )
                        + "\n"
                    )
                proc.stdin.flush()
                trace_show = json.loads(proc.stdout.readline())
                trace_events = json.loads(proc.stdout.readline())
                trace_summary = json.loads(proc.stdout.readline())
                trace_attribution = json.loads(proc.stdout.readline())
                self.assertIn("trace", json.loads(trace_show["result"]["content"][0]["text"]))
                self.assertIsInstance(json.loads(trace_events["result"]["content"][0]["text"]), list)
                self.assertIn("runtime_skill", json.loads(trace_summary["result"]["content"][0]["text"]))
                self.assertIn("layers", json.loads(trace_attribution["result"]["content"][0]["text"]))
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
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_cognitive_runtime.mcp_server"],
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
                            "params": {"name": "codex_cognitive_runtime_delete", "arguments": {"memory_id": "mem_test"}},
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
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_cognitive_runtime.mcp_server"],
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
                            "params": {"name": "codex_cognitive_runtime_ingest", "arguments": {"text": "默认使用中文回答"}},
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
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
                "CODEX_COGNITIVE_RUNTIME_ENABLE_MCP_WRITE_TOOLS": "1",
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_cognitive_runtime.mcp_server"],
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
                            "params": {"name": "codex_cognitive_runtime_ingest", "arguments": {"text": "默认使用中文回答"}},
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
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "codex_cognitive_runtime.mcp_server"],
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
                            "params": {"name": "codex_cognitive_runtime_queue", "arguments": {"limit": 0}},
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
