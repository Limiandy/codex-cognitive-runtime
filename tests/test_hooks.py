import json
import os
import subprocess
import sys
import tempfile
import time
import unittest


class HookTest(unittest.TestCase):
    def test_user_message_worker_stores_and_next_turn_injects_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "PYTHONPATH": "src",
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
            }

            first = _run_hook(
                env,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "默认使用中文回答",
                    "cwd": "/tmp/project",
                    "model": "gpt-5.5",
                },
            )
            self.assertEqual(first, {})
            self.assertTrue(_wait_for_active_memory(env))

            second = _run_hook(
                env,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "我的回答语言偏好是什么？",
                    "cwd": "/tmp/project",
                    "model": "gpt-5.5",
                },
            )
            context = second["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Codex Cognitive Runtime context:", context)
            self.assertIn("用户偏好默认使用中文回答", context)

    def test_observed_runtime_hook_chain_records_violation_and_injects_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "PYTHONPATH": "src",
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
            }
            base = {
                "session_id": "runtime-session",
                "turn_id": "runtime-turn",
                "cwd": tmp,
                "model": "gpt-5.5",
            }

            first = _run_hook(
                env,
                {
                    **base,
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "修复这个 bug，并跑测试验证",
                },
                "user_message",
            )
            self.assertTrue(first["codexMemoryRuntime"]["started"])
            _run_hook(env, {**base, "hook_event_name": "PostToolUse", "tool_name": "functions.exec_command", "cmd": "rg bug src"}, "after_tool_call")
            _run_hook(env, {**base, "hook_event_name": "PostToolUse", "tool_name": "functions.apply_patch"}, "after_tool_call")
            _run_hook(env, {**base, "hook_event_name": "Stop", "last_assistant_message": "已完成"}, "session_end")

            followup = _run_hook(
                env,
                {
                    **base,
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "继续处理",
                },
                "user_message",
            )
            context = followup["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Runtime control:", context)
            self.assertIn("changed_without_verification", context)


def _run_hook(env, payload, hook_name="user_message"):
    proc = subprocess.run(
        [sys.executable, "-m", "codex_cognitive_runtime.hooks", hook_name],
        cwd=".",
        env=env,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


def _wait_for_active_memory(env):
    for _ in range(30):
        proc = subprocess.run(
            [sys.executable, "-m", "codex_cognitive_runtime.cli", "queue", "--status", "active", "--limit", "5"],
            cwd=".",
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        memories = json.loads(proc.stdout)
        if memories:
            return True
        time.sleep(0.2)
    return False
