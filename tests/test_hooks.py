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
            self.assertIn("hookSpecificOutput", first)
            self.assertFalse(first.get("codexMemoryRuntime", {}).get("started", False))
            self.assertTrue(_wait_for_active_user_preference(env))

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
            self.assertIn("用户需求：我的回答语言偏好是什么？", context)
            self.assertIn("基础规则：", context)
            self.assertIn("用户偏好默认使用中文回答", context)

    def test_session_start_does_not_inject_recalled_memory_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "PYTHONPATH": "src",
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
            }

            _run_hook(
                env,
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "默认使用中文回答",
                    "cwd": "/tmp/project",
                    "model": "gpt-5.5",
                },
            )
            self.assertTrue(_wait_for_active_user_preference(env))

            started = _run_hook(
                env,
                {
                    "hook_event_name": "SessionStart",
                    "cwd": "/tmp/project",
                    "session_id": "new-session",
                },
                "session_start",
            )
            self.assertIn("systemMessage", started)
            self.assertNotIn("hookSpecificOutput", started)

    def test_observed_runtime_hook_chain_records_violation_without_next_turn_control(self):
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
            self.assertIn("hookSpecificOutput", followup)
            self.assertIn("用户需求：继续处理", followup["hookSpecificOutput"]["additionalContext"])
            self.assertIn("基础规则：", followup["hookSpecificOutput"]["additionalContext"])
            self.assertFalse(followup.get("codexMemoryRuntime", {}).get("started", False))

    def test_direct_answer_prompt_does_not_start_runtime_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "PYTHONPATH": "src",
                "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1",
                "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp,
            }
            result = _run_hook(
                env,
                {
                    "session_id": "runtime-session",
                    "turn_id": "runtime-turn",
                    "cwd": tmp,
                    "model": "gpt-5.5",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "失败指的是我们的 workflow 失败，不是 Codex 本身执行失败对吧？",
                },
                "user_message",
            )
            self.assertFalse(result.get("codexMemoryRuntime", {}).get("started", False))
            if "hookSpecificOutput" in result:
                self.assertNotIn("Runtime Skill:", result["hookSpecificOutput"]["additionalContext"])


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


def _wait_for_active_user_preference(env):
    for _ in range(30):
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from codex_cognitive_runtime.config import load_config; "
                "from codex_cognitive_runtime.service import MemoryService; "
                "s=MemoryService(load_config()); "
                "p=s.user_preferences_page(page=1,page_size=20,status='active'); "
                "print(__import__('json').dumps(p,ensure_ascii=False)); "
                "s.close()",
            ],
            cwd=".",
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        page = json.loads(proc.stdout)
        if any("默认使用中文回答" in item.get("content", "") for item in page.get("items", [])):
            return True
        time.sleep(0.2)
    return False
