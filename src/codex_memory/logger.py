from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{12,}"),
    re.compile(r"(?i)(token|secret|api[_-]?key|password)(['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----", re.S),
]


def debug(message: str, **fields: Any) -> None:
    log("DEBUG", message, **fields)


def info(message: str, **fields: Any) -> None:
    log("INFO", message, **fields)


def warn(message: str, **fields: Any) -> None:
    log("WARN", message, **fields)


def error(message: str, **fields: Any) -> None:
    log("ERROR", message, **fields)


def log(level: str, message: str, **fields: Any) -> None:
    level = level.upper()
    configured = os.environ.get("CODEX_MEMORY_LOG_LEVEL", "INFO").upper()
    if LEVELS.get(level, 20) < LEVELS.get(configured, 20):
        return
    log_dir = Path(os.environ.get("CODEX_MEMORY_LOG_DIR", "~/.codex-memory/logs")).expanduser()
    record = _redact({
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "level": level,
        "message": message,
        **fields,
    })
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "debug.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        with (log_dir / "debug.pretty.log").open("a", encoding="utf-8") as handle:
            handle.write(_format_pretty(record) + "\n")
    except OSError:
        return


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if _sensitive_key(str(key)):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in SECRET_PATTERNS:
            if pattern.pattern.startswith("(?i)"):
                text = pattern.sub(r"\1\2[redacted]", text)
            else:
                text = pattern.sub("[redacted]", text)
        return text
    return value


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in ("token", "secret", "api_key", "apikey", "password"))


def _format_pretty(record: dict[str, Any]) -> str:
    ts = record.get("ts", "")
    level = record.get("level", "")
    message = record.get("message", "")
    lines = [f"[{ts}] {level} {message}"]
    for key, value in record.items():
        if key in {"ts", "level", "message"}:
            continue
        lines.extend(_format_field(key, value, indent=2))
    return "\n".join(lines)


def _format_field(key: str, value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        if _is_compact_dict(value):
            return [f"{pad}{key}: {json.dumps(value, ensure_ascii=False)}"]
        lines = [f"{pad}{key}:"]
        for child_key, child_value in value.items():
            lines.extend(_format_field(str(child_key), child_value, indent + 2))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []"]
        lines = [f"{pad}{key}: [{len(value)} items]"]
        for index, item in enumerate(value[:5]):
            lines.extend(_format_field(f"- {index}", item, indent + 2))
        if len(value) > 5:
            lines.append(f"{pad}  ... {len(value) - 5} more")
        return lines
    if isinstance(value, str):
        return _format_string_field(key, value, indent)
    return [f"{pad}{key}: {value}"]


def _format_string_field(key: str, value: str, indent: int) -> list[str]:
    pad = " " * indent
    text = value.rstrip("\n")
    if "\n" not in text and len(text) <= 180:
        return [f"{pad}{key}: {text}"]
    lines = text.splitlines()
    preview = lines[:12]
    output = [f"{pad}{key}: | ({len(text)} chars, {len(lines)} lines)"]
    output.extend(f"{pad}  {line[:240]}" for line in preview)
    if len(lines) > len(preview):
        output.append(f"{pad}  ... {len(lines) - len(preview)} more lines")
    return output


def _is_compact_dict(value: dict[str, Any]) -> bool:
    if len(value) > 4:
        return False
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    return len(rendered) <= 180 and "\n" not in rendered
