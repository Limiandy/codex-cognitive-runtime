from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from . import logger
from .security import summarize_payload
from .service import MemoryService


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    hook_name = argv[0] if argv else "unknown"
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        payload = {}

    logger.info("hook received", hook=hook_name, payload_summary=summarize_payload(payload))
    service = MemoryService(load_config())
    try:
        if hook_name == "session_start":
            return _session_start(service, payload)
        if hook_name == "user_message":
            return _user_message(service, payload)
        if hook_name == "after_tool_call":
            result = service.observe_tool_use(payload)
            _out(result.get("hook_output", {}))
            return 0
        if hook_name == "session_end":
            return _session_end(service, payload)
        if hook_name == "precompact":
            service.record_event(hook_name, payload, processed=True)
            _out({})
            return 0
        result = service.ingest_event(hook_name, payload)
        _out({"systemMessage": _summary(result)})
        return 0
    finally:
        service.close()


def _session_start(service: MemoryService, payload: dict[str, Any]) -> int:
    service.record_event("session_start", payload, processed=True)
    service.periodic_governance(interval_minutes=60)
    status = service.lightweight_status()
    active = status.get("ledger", {}).get("by_status", {}).get("active", 0)
    context = service.prompt_context(
        str(payload.get("cwd", "")),
        limit=4,
        cwd=str(payload.get("cwd") or ""),
        session_id=str(payload.get("session_id") or "") or None,
        turn_id=str(payload.get("turn_id") or "") or None,
    )
    data: dict[str, Any] = {"systemMessage": f"Codex Memory ready: {active} active local memories."}
    if context:
        data["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    logger.debug("session_start output prepared", active=active, context=context, output=data)
    _out(data)
    return 0


def _user_message(service: MemoryService, payload: dict[str, Any]) -> int:
    event_id = service.record_event("user_message", payload)
    runtime_task = service.start_task_from_prompt(payload)
    logger.debug("user_message event recorded", event_id=event_id, payload_summary=summarize_payload(payload))
    _spawn_worker(event_id)
    context = service.prompt_context(
        str(payload.get("prompt", "")),
        limit=6,
        cwd=str(payload.get("cwd") or ""),
        session_id=str(payload.get("session_id") or "") or None,
        turn_id=str(payload.get("turn_id") or "") or None,
    )
    if context:
        _out(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                },
                "codexMemoryRuntime": runtime_task,
            }
        )
    else:
        _out({"codexMemoryRuntime": runtime_task} if runtime_task.get("started") else {})
    return 0


def _session_end(service: MemoryService, payload: dict[str, Any]) -> int:
    runtime_result = service.observe_stop(payload)
    result = service.apply_recall_outcome(
        str(payload.get("session_id") or "") or None,
        str(payload.get("turn_id") or "") or None,
        str(payload.get("last_assistant_message") or ""),
    )
    service.periodic_governance(interval_minutes=60)
    logger.debug("session_end recall feedback processed", result=result, runtime=runtime_result)
    _out(runtime_result.get("hook_output", {}))
    return 0


def _checkpoint(service: MemoryService, hook_name: str, payload: dict[str, Any]) -> int:
    transcript = str(payload.get("transcript_path", ""))
    compact_payload = dict(payload)
    if transcript:
        compact_payload["recent_transcript"] = _tail_transcript(transcript)
    result = service.ingest_event(hook_name, compact_payload)
    _out({"systemMessage": _summary(result)})
    return 0


def _tail_transcript(path: str, max_lines: int = 40) -> list[str]:
    p = Path(path).expanduser()
    if not p.is_file():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def _summary(result: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for item in result.get("results", []):
        status = str(item.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    if not counts:
        return "Codex Memory: no durable memory candidates."
    parts = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    return f"Codex Memory reviewed {result.get('candidate_count', 0)} candidates: {parts}."


def _out(data: dict[str, Any]) -> None:
    logger.info("hook output", output=data)
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _spawn_worker(event_id: str) -> None:
    env = os.environ.copy()
    env["CODEX_MEMORY_INTERNAL_CALL"] = "1"
    env["CODEX_MEMORY_HOOK_DEPTH"] = "1"
    logger.debug("spawning worker", event_id=event_id)
    subprocess.Popen(
        [sys.executable, "-m", "codex_memory.worker", event_id],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
