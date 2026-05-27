from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ToolObservation:
    tool_name: str
    tool_kind: str
    command: str
    stdout: str
    stderr: str
    exit_code: int | None
    files_changed: list[str]
    evidence_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_tool_observation(payload: dict[str, Any]) -> ToolObservation:
    tool_name = _tool_name(payload)
    command = _command_text(payload)
    stdout = _text_field(payload, ("stdout", "output", "result", "tool_output"))
    stderr = _text_field(payload, ("stderr", "error"))
    exit_code = _exit_code(payload)
    files_changed = _files_changed(payload)
    text = _payload_text(payload)
    tool_kind = _tool_kind(tool_name, command, text)
    return ToolObservation(
        tool_name=tool_name,
        tool_kind=tool_kind,
        command=command[:500],
        stdout=stdout[:1000],
        stderr=stderr[:1000],
        exit_code=exit_code,
        files_changed=files_changed[:50],
        evidence_summary={
            "payload_keys": sorted(str(key) for key in payload.keys())[:30],
            "failed": _looks_failed(text, exit_code),
        },
    )


def _tool_kind(tool_name: str, command: str, text: str) -> str:
    lowered = " ".join((tool_name, command, text)).lower()
    if any(signal in lowered for signal in ("pytest", "unittest", "npm test", "pnpm test", "yarn test", "ruff", "mypy", "build", "lint", "tsc", "go test", "cargo test")):
        return "verify"
    if any(signal in lowered for signal in ("apply_patch", "write_file", "edit", "*** begin patch", "update file", "add file", "delete file")):
        return "edit"
    if any(signal in lowered for signal in ("read_file", "grep", "search", "list", "git diff", "cat ", "sed ", "rg ", "ls ", "find ", "nl ", "wc ")):
        return "inspect"
    return "other"


def _tool_name(payload: dict[str, Any]) -> str:
    for key in ("tool_name", "tool", "name"):
        if payload.get(key):
            return str(payload.get(key))
    for nested_key in ("tool_input", "tool", "input"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            for key in ("tool_name", "tool", "name"):
                if nested.get(key):
                    return str(nested.get(key))
    return ""


def _command_text(payload: dict[str, Any]) -> str:
    for key in ("cmd", "command", "args"):
        if payload.get(key):
            return str(payload.get(key))
    for nested_key in ("tool_input", "input", "arguments"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            for key in ("cmd", "command", "args"):
                if nested.get(key):
                    return str(nested.get(key))
    return ""


def _text_field(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if payload.get(key) is not None:
            return str(payload.get(key))
    result = payload.get("result")
    if isinstance(result, dict):
        for key in keys:
            if result.get(key) is not None:
                return str(result.get(key))
    return ""


def _exit_code(payload: dict[str, Any]) -> int | None:
    for key in ("exit_code", "returncode", "code"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    result = payload.get("result")
    if isinstance(result, dict):
        return _exit_code(result)
    return None


def _files_changed(payload: dict[str, Any]) -> list[str]:
    value = payload.get("files_changed") or payload.get("changed_files") or []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _payload_text(value: Any) -> str:
    parts: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                parts.append(str(key))
                walk(child)
        elif isinstance(item, list):
            for child in item[:40]:
                walk(child)
        elif item is not None:
            parts.append(str(item))

    walk(value)
    return " ".join(parts).lower()[:20000]


def _looks_failed(text: str, exit_code: int | None) -> bool:
    if exit_code not in (None, 0):
        return True
    lowered = text.lower()
    return any(signal in lowered for signal in ("failed", "failure", "error", "exit code 1", "traceback", "失败", "报错"))
