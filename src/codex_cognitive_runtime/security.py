from __future__ import annotations

import re
from typing import Any


MAX_PAYLOAD_STRING_CHARS = 4000
MAX_SUMMARY_STRING_CHARS = 240
ALLOWED_PAYLOAD_FIELDS = {
    "session_id",
    "turn_id",
    "cwd",
    "hook_event_name",
    "model",
    "permission_mode",
    "prompt",
    "text",
    "last_assistant_message",
}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"npm_[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----", re.S),
    re.compile(r"(?i)\b(token|secret|api[_-]?key|password|oauth[_-]?token|access[_-]?token)\b(['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
]


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if _sensitive_key(key) else redact_secrets(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, str):
        text = value
        for pattern in SECRET_PATTERNS:
            if "(token|secret|api" in pattern.pattern:
                text = pattern.sub(r"\1\2[REDACTED]", text)
            else:
                text = pattern.sub("[REDACTED]", text)
        return text
    return value


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key in sorted(ALLOWED_PAYLOAD_FIELDS):
        if key not in payload:
            continue
        sanitized[key] = _sanitize_value(payload[key], MAX_PAYLOAD_STRING_CHARS)
    if payload.keys() - ALLOWED_PAYLOAD_FIELDS:
        sanitized["_omitted_keys"] = sorted(str(key) for key in payload.keys() - ALLOWED_PAYLOAD_FIELDS)[:20]
    return sanitized


def summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "keys": sorted(str(key) for key in payload.keys())[:30],
        "omitted_key_count": max(0, len(payload) - 30),
    }
    for key in ("session_id", "turn_id", "cwd", "hook_event_name", "model", "permission_mode"):
        if key in payload:
            summary[key] = _sanitize_value(payload[key], MAX_SUMMARY_STRING_CHARS)
    text = payload.get("prompt", payload.get("text"))
    if isinstance(text, str):
        summary["text_preview"] = _truncate(str(redact_secrets(text)), MAX_SUMMARY_STRING_CHARS)
        summary["text_length"] = len(text)
    return summary


def sanitize_model_result(result: dict[str, Any]) -> dict[str, Any]:
    redacted = redact_secrets(result)
    if not isinstance(redacted, dict):
        return {}
    return _limit_nested(redacted, max_string_chars=1000, max_list_items=20, depth=8)


def summarize_candidate(candidate: Any) -> dict[str, Any]:
    data = candidate.to_dict() if hasattr(candidate, "to_dict") else dict(candidate or {})
    return {
        "type": data.get("type") or data.get("memory_type"),
        "scope": data.get("scope"),
        "ttl": data.get("ttl"),
        "confidence": data.get("confidence"),
        "importance": data.get("importance"),
        "content_preview": _truncate(str(redact_secrets(data.get("content") or "")), 160),
        "evidence_count": len(data.get("evidence") or []),
    }


def _sanitize_value(value: Any, max_string_chars: int) -> Any:
    value = redact_secrets(value)
    if isinstance(value, str):
        return _truncate(value, max_string_chars)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_sanitize_value(item, max_string_chars) for item in value[:10]]
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item, max_string_chars) for key, item in list(value.items())[:20]}
    return _truncate(str(value), max_string_chars)


def _limit_nested(value: Any, max_string_chars: int, max_list_items: int, depth: int) -> Any:
    if depth <= 0:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {str(key): _limit_nested(item, max_string_chars, max_list_items, depth - 1) for key, item in list(value.items())[:50]}
    if isinstance(value, list):
        return [_limit_nested(item, max_string_chars, max_list_items, depth - 1) for item in value[:max_list_items]]
    if isinstance(value, str):
        return _truncate(value, max_string_chars)
    return value


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _sensitive_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in ("token", "secret", "api_key", "apikey", "password", "credential"))
