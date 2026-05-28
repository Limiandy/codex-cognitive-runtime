from __future__ import annotations

import json
import sys
from typing import Any, Callable

from . import __version__
from .config import ensure_state_dir, load_config
from .ledger import Ledger
from .service import MemoryService
from .schema import STATUSES


TOOLS = {
    "codex_memory_status": {
        "description": "Show lightweight Codex Memory ledger, store, and privacy status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "codex_memory_search": {
        "description": "Search reviewed long-term memory for context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "cwd": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    "codex_memory_ingest": {
        "description": "Submit content to the memory engine for review and possible storage.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "event_type": {"type": "string", "default": "manual"}},
            "required": ["text"],
        },
    },
    "codex_memory_queue": {
        "description": "List local memory ledger entries by status.",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string"}, "limit": {"type": "integer", "default": 20}},
        },
    },
    "codex_memory_runtime_status": {
        "description": "Show observed workflow runtime status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "session_id": {"type": "string"},
                "turn_id": {"type": "string"},
            },
        },
    },
    "codex_memory_runtime_violations": {
        "description": "List open observed workflow violations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    "codex_memory_verification_recipes": {
        "description": "List learned verification recipes.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    "codex_memory_promote": {
        "description": "Promote a reviewed memory to active in the local Ledger.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "note": {"type": "string", "default": ""},
            },
            "required": ["memory_id"],
        },
    },
    "codex_memory_reject": {
        "description": "Reject a memory candidate with optional review note.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}, "note": {"type": "string", "default": ""}},
            "required": ["memory_id"],
        },
    },
    "codex_memory_delete": {
        "description": "Soft-delete a memory from local active use.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}, "note": {"type": "string", "default": ""}},
            "required": ["memory_id"],
        },
    },
    "codex_memory_recall_feedback": {
        "description": "Mark an explicit recall result as useful or harmful.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "outcome": {"type": "string", "enum": ["positive", "negative"]},
                "note": {"type": "string", "default": ""},
            },
            "required": ["memory_id", "outcome"],
        },
    },
    "codex_memory_expire": {
        "description": "Expire active memories whose TTL has elapsed.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "codex_memory_reconcile": {
        "description": "Reconcile old audit-only hook events so pending counts reflect real work.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "codex_memory_consolidate": {
        "description": "Consolidate repeated project memories into global cross-project experience.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "codex_memory_govern": {
        "description": "Evaluate global memory quality and optionally apply safe optimization actions.",
        "inputSchema": {
            "type": "object",
            "properties": {"apply": {"type": "boolean", "default": False}},
        },
    },
    "codex_memory_govern_periodic": {
        "description": "Run global memory governance only if its periodic interval is due.",
        "inputSchema": {
            "type": "object",
            "properties": {"interval_minutes": {"type": "integer", "default": 60}},
        },
    },
    "codex_memory_audit": {
        "description": "Summarize memory review health and queue counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "codex_memory_diagnostics": {
        "description": "Run heavier local Ledger diagnostics.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "codex_memory_prune_runtime": {
        "description": "Prune local runtime audit records, optionally including learned verification recipes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "older_than_days": {"type": "integer"},
                "include_recipes": {"type": "boolean", "default": False},
                "include_skills": {"type": "boolean", "default": False},
            },
        },
    },
    "codex_memory_runtime_skills": {
        "description": "List Runtime Skill injections.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
    },
    "codex_memory_runtime_skill_feedback": {
        "description": "Record explicit feedback for a Runtime Skill injection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "injection_id": {"type": "string"},
                "outcome": {"type": "string", "enum": ["positive", "negative", "mixed", "unknown"]},
                "target": {"type": "string", "default": "final_result"},
                "note": {"type": "string", "default": ""},
            },
            "required": ["injection_id", "outcome"],
        },
    },
    "codex_memory_seed_skills": {
        "description": "List seed skills and trust state.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
    },
    "codex_memory_dynamic_skills": {
        "description": "List dynamic durable skill candidates or active skills.",
        "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}, "limit": {"type": "integer", "default": 20}}},
    },
    "codex_memory_promote_dynamic_skill": {
        "description": "Promote a dynamic skill candidate to active durable skill.",
        "inputSchema": {"type": "object", "properties": {"skill_id": {"type": "string"}, "note": {"type": "string", "default": ""}}, "required": ["skill_id"]},
    },
    "codex_memory_disable_seed_skill": {
        "description": "Disable a seed skill so it is no longer used as Runtime Skill basis.",
        "inputSchema": {"type": "object", "properties": {"skill_id": {"type": "string"}}, "required": ["skill_id"]},
    },
}

MCP_TOOL_LEVELS = {
    "codex_memory_ingest": "write",
    "codex_memory_recall_feedback": "write",
    "codex_memory_expire": "write",
    "codex_memory_promote": "review",
    "codex_memory_reject": "review",
    "codex_memory_delete": "admin",
    "codex_memory_reconcile": "admin",
    "codex_memory_consolidate": "admin",
    "codex_memory_prune_runtime": "admin",
    "codex_memory_runtime_skill_feedback": "write",
    "codex_memory_promote_dynamic_skill": "review",
    "codex_memory_disable_seed_skill": "admin",
}


def main() -> int:
    config = load_config()
    ensure_state_dir(config)
    ledger = Ledger(config.ledger_path)
    handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
        "codex_memory_status": lambda args: _status(config, ledger),
        "codex_memory_search": lambda args: _with_service(
            lambda service: service.search_context(
                str(args["query"]),
                _limit(args.get("limit", 5)),
                cwd=args.get("cwd"),
                session_id=args.get("session_id"),
            )
        ),
        "codex_memory_ingest": lambda args: _with_service(
            lambda service: service.ingest_event(_event_type(args.get("event_type", "manual")), {"text": _text(args["text"]), "source": "mcp"})
        ),
        "codex_memory_queue": lambda args: ledger.list_memories(_status_arg(args.get("status")), _limit(args.get("limit", 20))),
        "codex_memory_runtime_status": lambda args: _with_service(
            lambda service: service.runtime_status(
                cwd=args.get("cwd"),
                session_id=args.get("session_id"),
                turn_id=args.get("turn_id"),
            )
        ),
        "codex_memory_runtime_violations": lambda args: ledger.list_open_workflow_violations(
            workflow_id=args.get("workflow_id"),
            limit=_limit(args.get("limit", 20)),
        ),
        "codex_memory_verification_recipes": lambda args: _verification_recipes(ledger, _limit(args.get("limit", 20))),
        "codex_memory_promote": lambda args: _with_service(
            lambda service: service.promote_memory(_id_arg(args["memory_id"], "memory_id"), str(args.get("note", "")))
        ),
        "codex_memory_reject": lambda args: _with_service(
            lambda service: service.reject_memory(_id_arg(args["memory_id"], "memory_id"), str(args.get("note", "")))
        ),
        "codex_memory_delete": lambda args: _with_service(
            lambda service: service.delete_memory(_id_arg(args["memory_id"], "memory_id"), str(args.get("note", "")))
        ),
        "codex_memory_recall_feedback": lambda args: _with_service(
            lambda service: service.recall_feedback(
                _id_arg(args["memory_id"], "memory_id"),
                _outcome(args["outcome"]),
                str(args.get("note", "")),
            )
        ),
        "codex_memory_expire": lambda args: _with_service(lambda service: service.expire_due_memories()),
        "codex_memory_reconcile": lambda args: _with_service(lambda service: service.reconcile()),
        "codex_memory_consolidate": lambda args: _with_service(lambda service: service.consolidate_memories()),
        "codex_memory_govern": lambda args: _with_service(lambda service: service.govern_memories(_bool_arg(args.get("apply", False), "apply"))),
        "codex_memory_govern_periodic": lambda args: _with_service(
            lambda service: service.periodic_governance(_limit(args.get("interval_minutes", 60), minimum=1, maximum=1440))
        ),
        "codex_memory_audit": lambda args: _audit(ledger),
        "codex_memory_diagnostics": lambda args: _diagnostics(config, ledger),
        "codex_memory_prune_runtime": lambda args: _with_service(
            lambda service: service.prune_runtime(
                older_than_days=_optional_nonnegative_int(args.get("older_than_days"), "older_than_days"),
                include_recipes=_bool_arg(args.get("include_recipes", False), "include_recipes"),
                include_skills=_bool_arg(args.get("include_skills", False), "include_skills"),
            )
        ),
        "codex_memory_runtime_skills": lambda args: _with_service(lambda service: service.list_runtime_skills(_limit(args.get("limit", 20)))),
        "codex_memory_runtime_skill_feedback": lambda args: _with_service(
            lambda service: service.runtime_skill_feedback(
                _id_arg(args["injection_id"], "injection_id"),
                str(args["outcome"]),
                target=str(args.get("target") or "final_result"),
                note=str(args.get("note") or ""),
            )
        ),
        "codex_memory_seed_skills": lambda args: _with_service(lambda service: service.list_seed_skills(_limit(args.get("limit", 20)))),
        "codex_memory_dynamic_skills": lambda args: _with_service(lambda service: service.list_dynamic_skills(status=args.get("status"), limit=_limit(args.get("limit", 20)))),
        "codex_memory_promote_dynamic_skill": lambda args: _with_service(
            lambda service: service.promote_dynamic_skill(_id_arg(args["skill_id"], "skill_id"), note=str(args.get("note") or ""))
        ),
        "codex_memory_disable_seed_skill": lambda args: _with_service(
            lambda service: service.set_seed_skill_trust_state(_id_arg(args["skill_id"], "skill_id"), "disabled")
        ),
    }
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            _handle(line, handlers, ledger)
    finally:
        ledger.close()
    return 0


def _status(config, ledger: Ledger) -> dict[str, Any]:
    return {
        "ledger": ledger.stats(),
        "model": config.model,
        "store": {"primary": "ledger", "external_mirror": "unsupported"},
        "privacy": {
            "store_raw_events": config.store_raw_events,
            "runtime_observer_enabled": config.enable_runtime_observer,
            "runtime_observation_previews": "stored" if config.store_runtime_observation_previews else "redacted",
            "strict_privacy": config.strict_privacy,
        },
    }


def _diagnostics(config, ledger: Ledger) -> dict[str, Any]:
    return {
        "ledger": ledger.stats(),
        "model": config.model,
        "store": {"primary": "ledger", "external_mirror": "unsupported"},
    }


def _audit(ledger: Ledger) -> dict[str, Any]:
    stats = ledger.stats()
    return {
        "stats": stats,
        "quarantine_sample": ledger.list_memories("quarantined", 10),
        "rejected_sample": ledger.list_memories("rejected", 10),
    }


def _verification_recipes(ledger: Ledger, limit: int) -> list[dict[str, Any]]:
    recipes = []
    for record in ledger.list_cognitive_records(layer="skill", status="active", limit=max(limit, 100)):
        if record.get("record_type") == "verification_recipe":
            recipes.append(record)
        if len(recipes) >= limit:
            break
    return recipes


def _with_service(callback: Callable[[MemoryService], Any]) -> Any:
    service = MemoryService(load_config())
    try:
        return callback(service)
    finally:
        service.close()


def _handle(line: str, handlers: dict[str, Callable[[dict[str, Any]], Any]], ledger: Ledger) -> None:
    try:
        request = json.loads(line)
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "codex-memory", "version": __version__}}})
            return
        if method == "notifications/initialized":
            return
        if method == "tools/list":
            tools = [{"name": name, **spec} for name, spec in TOOLS.items()]
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
            return
        if method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in handlers:
                raise ValueError(f"unknown tool: {name}")
            config = load_config()
            permission_level = _permission_level(name, args)
            if not _mcp_permission_enabled(config, permission_level):
                raise McpPermissionError(permission_level, str(name))
            result = handlers[name](args)
            if permission_level != "read":
                _record_mcp_action(ledger, str(name), permission_level, args)
                result = _annotate_action(result, str(name), permission_level)
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}})
            return
        _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unknown method: {method}"}})
    except Exception as exc:
        request_id = None
        try:
            request_id = json.loads(line).get("id")
        except Exception:
            pass
        code = -32001 if isinstance(exc, PermissionError) else -32602 if isinstance(exc, (TypeError, ValueError)) else -32000
        _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": str(exc), "data": {"error_code": _error_code(exc), "hint": _error_hint(exc)}}})


def _send(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _limit(value: Any, minimum: int = 1, maximum: int = 100) -> int:
    if isinstance(value, bool):
        raise ValueError("limit must be an integer")
    limit = int(value)
    if limit < minimum or limit > maximum:
        raise ValueError(f"limit must be between {minimum} and {maximum}")
    return limit


def _optional_nonnegative_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _status_arg(value: Any) -> str | None:
    if value is None:
        return None
    status = str(value)
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    return status


def _event_type(value: Any) -> str:
    event_type = str(value or "manual")
    if len(event_type) > 64 or not event_type.replace("_", "").replace("-", "").isalnum():
        raise ValueError("event_type must be a short alphanumeric identifier")
    return event_type


def _id_arg(value: Any, name: str) -> str:
    item = str(value or "")
    if not item or len(item) > 128:
        raise ValueError(f"{name} must be a non-empty id under 128 chars")
    return item


def _text(value: Any) -> str:
    text = str(value or "")
    if not text.strip():
        raise ValueError("text must not be empty")
    return text[:12000]


def _outcome(value: Any) -> str:
    outcome = str(value)
    if outcome not in {"positive", "negative"}:
        raise ValueError("outcome must be positive or negative")
    return outcome


def _bool_arg(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


class McpPermissionError(PermissionError):
    def __init__(self, level: str, tool: str):
        self.level = level
        self.tool = tool
        super().__init__(f"MCP {level} tool disabled: {tool}")


def _permission_level(name: Any, args: dict[str, Any]) -> str:
    if name == "codex_memory_govern":
        return "admin" if bool(args.get("apply", False)) else "read"
    return MCP_TOOL_LEVELS.get(str(name), "read")


def _mcp_permission_enabled(config: Any, level: str) -> bool:
    if level == "read":
        return True
    if level == "write":
        return bool(config.enable_mcp_write_tools)
    if level == "review":
        return bool(config.enable_mcp_review_tools)
    if level == "admin":
        return bool(config.enable_mcp_admin_tools)
    return False


def _record_mcp_action(ledger: Ledger, tool: str, level: str, args: dict[str, Any]) -> None:
    ledger.add_event(
        "mcp_action",
        {
            "tool": tool,
            "permission_level": level,
            "argument_keys": sorted(str(key) for key in args.keys()),
            "action_applied": True,
            "_raw_payload_stored": False,
        },
    )


def _annotate_action(result: Any, tool: str, level: str) -> Any:
    marker = {"tool": tool, "permission_level": level, "action_applied": True}
    if isinstance(result, dict):
        return {**result, "mcp_action": marker}
    return {"result": result, "mcp_action": marker}


def _error_code(exc: Exception) -> str:
    if isinstance(exc, McpPermissionError):
        return f"mcp_{exc.level}_tool_disabled"
    if isinstance(exc, PermissionError):
        return "mcp_tool_disabled"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_arguments"
    return "internal_error"


def _error_hint(exc: Exception) -> str:
    if isinstance(exc, McpPermissionError):
        return _permission_hint(exc.level)
    if isinstance(exc, PermissionError):
        return "Enable the required MCP permission level before retrying."
    if isinstance(exc, (TypeError, ValueError)):
        return "Check the tool input schema and argument ranges."
    return "See Codex Memory logs for details."


def _permission_hint(level: str) -> str:
    if level == "write":
        return "Set CODEX_MEMORY_ENABLE_MCP_WRITE_TOOLS=1 or enable_mcp_write_tools=true to allow write MCP tools."
    if level == "review":
        return "Set CODEX_MEMORY_ENABLE_MCP_REVIEW_TOOLS=1 or enable_mcp_review_tools=true to allow promote/reject MCP tools."
    if level == "admin":
        return "Set CODEX_MEMORY_ENABLE_MCP_ADMIN_TOOLS=1 or enable_mcp_admin_tools=true to allow admin MCP tools."
    return "Enable the required MCP permission level before retrying."


if __name__ == "__main__":
    raise SystemExit(main())
