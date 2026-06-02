from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ToolObservation:
    schema_version: int
    tool_name: str
    tool_kind: str
    confidence: float
    command: str
    stdout: str
    stderr: str
    exit_code: int | None
    exit_code_source: str | None
    files_changed: list[str]
    source_fields: dict[str, str]
    raw_kind_reason: str
    evidence_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_tool_observation(payload: dict[str, Any], rules: dict[str, list[str]] | None = None) -> ToolObservation:
    source_fields: dict[str, str] = {}
    tool_name = _tool_name(payload, source_fields)
    command = _command_text(payload, source_fields)
    stdout = _text_field(payload, ("stdout", "output", "tool_output"), source_fields, "stdout")
    stderr = _text_field(payload, ("stderr", "error"), source_fields, "stderr")
    exit_code, exit_code_source = _exit_code(payload)
    if exit_code_source:
        source_fields["exit_code"] = exit_code_source
    files_changed = _files_changed(payload, source_fields)
    text = _payload_text(payload)
    rules = rules if rules is not None else load_observation_rules(payload.get("cwd"))
    tool_kind, confidence, raw_kind_reason = _tool_kind(tool_name, command, text)
    tool_kind, confidence, raw_kind_reason = _apply_custom_rules(tool_kind, confidence, raw_kind_reason, tool_name, command, text, rules)
    return ToolObservation(
        schema_version=1,
        tool_name=tool_name,
        tool_kind=tool_kind,
        confidence=confidence,
        command=command[:500],
        stdout=stdout[:1000],
        stderr=stderr[:1000],
        exit_code=exit_code,
        exit_code_source=exit_code_source,
        files_changed=files_changed[:50],
        source_fields=source_fields,
        raw_kind_reason=raw_kind_reason,
        evidence_summary={
            "payload_keys": sorted(str(key) for key in payload.keys())[:30],
            "failed": _looks_failed(text, exit_code),
        },
    )


def load_observation_rules(cwd: Any = None) -> dict[str, list[str]]:
    rules = {"verify": [], "inspect": [], "edit": []}
    env_map = {
        "verify": "CODEX_COGNITIVE_RUNTIME_VERIFY_COMMANDS",
        "inspect": "CODEX_COGNITIVE_RUNTIME_INSPECT_COMMANDS",
        "edit": "CODEX_COGNITIVE_RUNTIME_EDIT_COMMANDS",
    }
    for key, env_name in env_map.items():
        rules[key].extend(_split_rule_text(os.environ.get(env_name, "")))
    config = _load_rules_file(cwd)
    for key in rules:
        value = config.get(f"{key}_commands") or config.get(key)
        if isinstance(value, list):
            rules[key].extend(str(item).strip().lower() for item in value if str(item).strip())
        elif isinstance(value, str):
            rules[key].extend(_split_rule_text(value))
    return {key: sorted(set(item for item in values if item)) for key, values in rules.items()}


def _load_rules_file(cwd: Any) -> dict[str, Any]:
    if not cwd:
        return {}
    path = Path(str(cwd)).expanduser() / ".codex-cognitive-runtime.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    runtime = data.get("runtime_observer") if isinstance(data, dict) else {}
    if isinstance(runtime, dict):
        return runtime
    return data if isinstance(data, dict) else {}


def _split_rule_text(text: str) -> list[str]:
    return [part.strip().lower() for part in text.replace("\n", ",").split(",") if part.strip()]


def _apply_custom_rules(
    tool_kind: str,
    confidence: float,
    raw_kind_reason: str,
    tool_name: str,
    command: str,
    text: str,
    rules: dict[str, list[str]],
) -> tuple[str, float, str]:
    tool_and_command = " ".join((tool_name, command)).lower()
    lowered = " ".join((tool_name, command, text)).lower()
    for kind in ("edit", "verify", "inspect"):
        for signal in rules.get(kind, []):
            if signal and signal in tool_and_command:
                return kind, 0.92, f"matched custom {kind} signal: {signal}"
    for kind in ("edit", "verify", "inspect"):
        for signal in rules.get(kind, []):
            if signal and signal in lowered:
                return kind, max(confidence, 0.64), f"matched weak custom {kind} signal from payload text: {signal}"
    return tool_kind, confidence, raw_kind_reason


def _tool_kind(tool_name: str, command: str, text: str) -> tuple[str, float, str]:
    tool_and_command = " ".join((tool_name, command)).lower()
    lowered = " ".join((tool_name, command, text)).lower()
    verify_signals = (
        "pytest",
        "unittest",
        "npm test",
        "pnpm test",
        "yarn test",
        "npm run typecheck",
        "pnpm run typecheck",
        "yarn typecheck",
        "typecheck",
        "vue-tsc",
        "npm run check",
        "pnpm run check",
        "pnpm check",
        "yarn check",
        "python -m compileall",
        "python3 -m compileall",
        "compileall",
        "ruff",
        "mypy",
        "build",
        "lint",
        "tsc",
        "go test",
        "cargo test",
        "chrome",
        "browser",
        "playwright",
        "screenshot",
    )
    edit_signals = ("apply_patch", "write_file", "edit", "*** begin patch", "update file", "add file", "delete file")
    inspect_signals = ("read_file", "grep", "search", "list", "git diff", "cat ", "sed ", "rg ", "ls ", "find ", "nl ", "wc ")
    for signal in edit_signals:
        if signal in tool_and_command or signal in lowered and signal in ("*** begin patch", "update file", "add file", "delete file"):
            return "edit", 0.9 if signal in tool_and_command else 0.76, f"matched edit signal: {signal}"
    for signal in verify_signals:
        if signal in tool_and_command:
            return "verify", 0.9, f"matched verify signal: {signal}"
    for signal in inspect_signals:
        if signal in tool_and_command:
            return "inspect", 0.86, f"matched inspect signal: {signal}"
    for signal in verify_signals:
        if signal in lowered:
            return "verify", 0.62, f"matched weak verify signal from payload text: {signal}"
    for signal in inspect_signals:
        if signal in lowered:
            return "inspect", 0.58, f"matched weak inspect signal from payload text: {signal}"
    return "other", 0.2, "no known tool signal matched"


def _tool_name(payload: dict[str, Any], source_fields: dict[str, str]) -> str:
    for key in ("tool_name", "tool", "name"):
        if payload.get(key):
            source_fields["tool_name"] = key
            return str(payload.get(key))
    for nested_key in ("tool_input", "tool", "input"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            for key in ("tool_name", "tool", "name"):
                if nested.get(key):
                    source_fields["tool_name"] = f"{nested_key}.{key}"
                    return str(nested.get(key))
    return ""


def _command_text(payload: dict[str, Any], source_fields: dict[str, str]) -> str:
    for key in ("cmd", "command", "args"):
        if payload.get(key):
            source_fields["command"] = key
            return str(payload.get(key))
    for nested_key in ("tool_input", "input", "arguments"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            for key in ("cmd", "command", "args"):
                if nested.get(key):
                    source_fields["command"] = f"{nested_key}.{key}"
                    return str(nested.get(key))
    return ""


def _text_field(payload: dict[str, Any], keys: tuple[str, ...], source_fields: dict[str, str], field_name: str) -> str:
    for key in keys:
        if payload.get(key) is not None:
            source_fields[field_name] = key
            return str(payload.get(key))
    result = payload.get("result")
    if isinstance(result, dict):
        for key in keys:
            if result.get(key) is not None:
                source_fields[field_name] = f"result.{key}"
                return str(result.get(key))
    return ""


def _exit_code(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    for key in ("exit_code", "returncode", "code"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value), key
            except (TypeError, ValueError):
                return None, key
    result = payload.get("result")
    if isinstance(result, dict):
        value, source = _exit_code(result)
        return value, f"result.{source}" if source else None
    return None, None


def _files_changed(payload: dict[str, Any], source_fields: dict[str, str]) -> list[str]:
    value = payload.get("files_changed") or payload.get("changed_files") or []
    if value:
        source_fields["files_changed"] = "files_changed" if payload.get("files_changed") else "changed_files"
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
