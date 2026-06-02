from __future__ import annotations

import json
import uuid
from typing import Any

from .timeutil import local_now_iso


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def ok(data: Any, request_id: str | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "meta": {
            "request_id": request_id or new_request_id(),
            "generated_at": local_now_iso(),
        },
    }


def error_response(exc: ApiError, request_id: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
        "meta": {
            "request_id": request_id or new_request_id(),
            "generated_at": local_now_iso(),
        },
    }


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:16]


def parse_json_body(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("invalid_json", "Request body must be valid JSON.", status=400) from exc
    if not isinstance(data, dict):
        raise ApiError("invalid_json", "Request body must be a JSON object.", status=400)
    return data


def require_confirm(body: dict[str, Any], action: str, level: str = "write") -> None:
    if body.get("confirm") is True:
        return
    raise ApiError(
        "confirmation_required",
        "This action requires explicit confirmation.",
        status=409,
        details={"action": action, "level": level, "required": {"confirm": True}},
    )


def int_param(value: str | None, default: int, minimum: int = 1, maximum: int = 5000) -> int:
    if value in {None, ""}:
        return default
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise ApiError("invalid_parameter", "Integer parameter is invalid.", status=400, details={"value": value}) from exc
    return max(minimum, min(parsed, maximum))


def bool_param(value: str | None, default: bool = False) -> bool:
    if value in {None, ""}:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
