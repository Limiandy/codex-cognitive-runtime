from __future__ import annotations

import argparse
import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .api_schema import ApiError, bool_param, error_response, int_param, new_request_id, ok, parse_json_body, require_confirm
from .config import Config, load_config
from .service import MemoryService


class RuntimeApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], config: Config):
        super().__init__(server_address, handler_class)
        self.config = config


class RuntimeApiHandler(BaseHTTPRequestHandler):
    server: RuntimeApiServer

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_OPTIONS(self) -> None:
        self._send_empty(HTTPStatus.NO_CONTENT)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/events/stream":
            self._handle_sse()
            return
        if path == "/api/logs/stream":
            self._handle_log_sse()
            return
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _handle(self, method: str) -> None:
        request_id = new_request_id()
        try:
            parsed = urlparse(self.path)
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            body = self._body() if method == "POST" else {}
            with self._service() as service:
                data = dispatch(service, method, parsed.path, query, body)
            self._send_json(HTTPStatus.OK, ok(data, request_id=request_id))
        except ApiError as exc:
            self._send_json(exc.status, error_response(exc, request_id=request_id))
        except ValueError as exc:
            err = ApiError("invalid_request", str(exc), status=400)
            self._send_json(err.status, error_response(err, request_id=request_id))
        except Exception as exc:
            err = ApiError("internal_error", "Internal server error.", status=500, details={"type": type(exc).__name__})
            self._send_json(err.status, error_response(err, request_id=request_id))

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        return parse_json_body(self.rfile.read(length))

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin") or "http://localhost:5173"
        allowed = (
            origin.startswith("http://localhost:")
            or origin.startswith("http://127.0.0.1:")
            or origin == "null"
        )
        self.send_header("Access-Control-Allow-Origin", origin if allowed else "http://localhost:5173")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")

    def _handle_sse(self) -> None:
        parsed = urlparse(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        interval = int_param(query.get("interval"), 5, minimum=1, maximum=60)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors_headers()
        self.end_headers()
        while True:
            with self._service() as service:
                payload = ok(
                    {
                        "status": service.lightweight_status(),
                        "runtime_status": _live_runtime_status(
                            service.runtime_status(
                                cwd=query.get("cwd"),
                                session_id=query.get("session_id"),
                                turn_id=query.get("turn_id"),
                            )
                        ),
                        "trace_audit": _live_trace_audit(service.trace_audit()),
                    }
                )
            try:
                self.wfile.write(f"event: runtime_status\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(interval)

    def _handle_log_sse(self) -> None:
        parsed = urlparse(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        interval = int_param(query.get("interval"), 2, minimum=1, maximum=30)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors_headers()
        self.end_headers()
        while True:
            with self._service() as service:
                payload = ok(_runtime_logs(service, query))
            try:
                self.wfile.write(f"event: runtime_logs\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(interval)

    def _service(self) -> MemoryService:
        return MemoryService(self.server.config)


def dispatch(service: MemoryService, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> Any:
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    if not parts or parts[0] != "api":
        raise ApiError("not_found", "API route not found.", status=404)
    route = parts[1:]

    if method == "GET" and route == ["status"]:
        return service.status()
    if method == "GET" and route == ["runtime-status"]:
        return service.runtime_status(cwd=query.get("cwd"), session_id=query.get("session_id"), turn_id=query.get("turn_id"))
    if method == "POST" and route == ["doctor", "run"]:
        return service.run_doctor(model_check=bool(body.get("model_check")), privacy=bool(body.get("privacy")))
    if method == "GET" and route == ["doctor", "status"]:
        return service.doctor_status()
    if method == "GET" and route == ["privacy"]:
        return service.privacy_status()
    if method == "GET" and route == ["logs"]:
        return _runtime_logs(service, query)
    if method == "GET" and route == ["user-preferences"]:
        return service.user_preferences_page(
            page=int_param(query.get("page"), 1, minimum=1, maximum=10000),
            page_size=int_param(query.get("page_size"), 20, minimum=1, maximum=100),
            status=query.get("status"),
            name=query.get("name"),
            scope=query.get("scope"),
            project_key=query.get("project_key"),
            session_id=query.get("session_id"),
        )
    if method == "POST" and route == ["user-preferences"]:
        require_confirm(body, "user_preferences.create", "write")
        return service.create_user_preference(
            str(body.get("content") or ""),
            scope=str(body.get("scope") or "global"),
            note=str(body.get("note") or ""),
        )
    if method == "GET" and len(route) == 2 and route[0] == "user-preferences":
        return _require(service.get_user_preference(route[1]), "user_preference_not_found")
    if method == "POST" and len(route) == 3 and route[0] == "user-preferences":
        memory_id, action = route[1], route[2]
        if action == "optimize":
            return service.optimize_user_preference(memory_id, instruction=str(body.get("instruction") or ""))
        if action == "edit":
            require_confirm(body, "user_preferences.edit", "write")
            return service.update_user_preference(memory_id, str(body.get("content") or ""), note=str(body.get("note") or ""))
        if action == "delete":
            require_confirm(body, "user_preferences.delete", "admin")
            return service.delete_user_preference(memory_id, note=str(body.get("note") or ""))

    if method == "GET" and route == ["memories"]:
        if "page" in query or "page_size" in query or "name" in query:
            return service.memory_page(
                page=int_param(query.get("page"), 1, minimum=1, maximum=10000),
                page_size=int_param(query.get("page_size"), 20, minimum=1, maximum=100),
                status=query.get("status"),
                memory_type=query.get("type"),
                name=query.get("name"),
                scope=query.get("scope"),
                project_key=query.get("project_key"),
                session_id=query.get("session_id"),
            )
        return service.list_memories(
            status=query.get("status"),
            memory_type=query.get("type"),
            name=query.get("name"),
            scope=query.get("scope"),
            project_key=query.get("project_key"),
            session_id=query.get("session_id"),
            limit=int_param(query.get("limit"), 20, maximum=500),
        )
    if method == "GET" and len(route) == 2 and route[0] == "memories":
        return _require(service.get_memory(route[1]), "memory_not_found")
    if method == "POST" and route == ["memories", "search"]:
        return service.search_context(
            str(body.get("query") or body.get("text") or ""),
            limit=int_param(str(body.get("limit")) if body.get("limit") is not None else None, 5, maximum=50),
            cwd=_optional_str(body.get("cwd")),
            session_id=_optional_str(body.get("session_id")),
        )
    if method == "POST" and route == ["memories", "ingest"]:
        require_confirm(body, "memories.ingest", "write")
        return service.ingest_event(str(body.get("event_type") or "manual"), {"text": str(body.get("text") or "")})
    if method == "POST" and len(route) == 3 and route[0] == "memories":
        memory_id, action = route[1], route[2]
        note = str(body.get("note") or "")
        if action == "promote":
            require_confirm(body, "memories.promote", "review")
            return service.promote_memory(memory_id, note=note)
        if action == "reject":
            require_confirm(body, "memories.reject", "review")
            return service.reject_memory(memory_id, note=note)
        if action == "delete":
            require_confirm(body, "memories.delete", "admin")
            return service.delete_memory(memory_id, note=note)
        if action == "edit":
            require_confirm(body, "memories.edit", "write")
            return service.update_memory_content(memory_id, str(body.get("content") or ""), note=note)
        if action == "optimize":
            return service.optimize_memory_content(memory_id, instruction=str(body.get("instruction") or ""))
        if action == "recall-feedback":
            require_confirm(body, "memories.recall_feedback", "write")
            return service.recall_feedback(memory_id, str(body.get("outcome") or ""), note=note)

    if method == "GET" and route == ["runtime-skills"]:
        return service.list_runtime_skills(limit=int_param(query.get("limit"), 50, maximum=500))
    if method == "GET" and route == ["runtime-skills", "audit"]:
        return service.runtime_skill_audit()
    if method == "GET" and len(route) == 2 and route[0] == "runtime-skills":
        return _require(service.get_runtime_skill(route[1]), "runtime_skill_not_found")
    if method == "POST" and len(route) == 3 and route[0] == "runtime-skills" and route[2] == "feedback":
        require_confirm(body, "runtime_skills.feedback", "write")
        return _require(
            service.runtime_skill_feedback(route[1], str(body.get("outcome") or "positive"), target=str(body.get("target") or "final_result"), note=str(body.get("note") or "")),
            "runtime_skill_not_found",
        )

    if method == "GET" and route == ["seed-skills"]:
        if any(key in query for key in ("page", "page_size", "name", "category")):
            return service.seed_skill_page(
                page=int_param(query.get("page"), 1, minimum=1, maximum=10000),
                page_size=int_param(query.get("page_size"), 20, minimum=1, maximum=100),
                name=query.get("name"),
                category=query.get("category"),
            )
        return service.list_seed_skills(limit=int_param(query.get("limit"), 50, maximum=500))
    if method == "POST" and len(route) == 3 and route[0] == "seed-skills" and route[2] == "trust-state":
        require_confirm(body, "seed_skills.trust_state", "review")
        return _require(service.set_seed_skill_trust_state(route[1], str(body.get("trust_state") or "")), "seed_skill_not_found")

    if method == "GET" and route == ["dynamic-skills"]:
        return service.list_dynamic_skills(status=query.get("status"), limit=int_param(query.get("limit"), 50, maximum=500))
    if method == "POST" and len(route) == 3 and route[0] == "dynamic-skills":
        require_confirm(body, f"dynamic_skills.{route[2]}", "review")
        return _dynamic_skill_action(service, route[1], route[2], body)

    if method == "GET" and route == ["workflows", "status"]:
        return service.runtime_status(cwd=query.get("cwd"), session_id=query.get("session_id"), turn_id=query.get("turn_id"))
    if method == "GET" and route == ["workflows", "violations"]:
        return service.workflow_violations(
            limit=int_param(query.get("limit"), 50, maximum=500),
            session_id=query.get("session_id"),
            turn_id=query.get("turn_id"),
            cwd=query.get("cwd"),
        )
    if method == "POST" and len(route) == 4 and route[:2] == ["workflows", "violations"] and route[3] == "resolve":
        require_confirm(body, "workflows.violations.resolve", "review")
        return _require(service.resolve_workflow_violation(route[2], note=str(body.get("note") or "")), "workflow_violation_not_found")
    if method == "GET" and route == ["workflows", "recipes"]:
        return service.verification_recipes(limit=int_param(query.get("limit"), 50, maximum=500))
    if method == "POST" and route == ["workflows", "prune"]:
        require_confirm(body, "workflows.prune", "admin")
        return service.prune_runtime(older_than_days=_optional_int(body.get("older_than_days")), include_recipes=bool(body.get("include_recipes")), include_skills=bool(body.get("include_skills")))

    if method == "GET" and route == ["traces"]:
        return service.list_traces(session_id=query.get("session_id"), turn_id=query.get("turn_id"), limit=int_param(query.get("limit"), 50, maximum=500))
    if method == "GET" and route == ["traces", "audit"]:
        return service.trace_audit()
    if method == "GET" and len(route) == 2 and route[0] == "traces":
        return _require(service.get_trace(route[1]), "trace_not_found")
    if method == "GET" and len(route) == 3 and route[0] == "traces":
        if route[2] == "events":
            return service.trace_events(route[1], limit=int_param(query.get("limit"), 500, maximum=5000))
        if route[2] == "summary":
            return _require(service.trace_summary(route[1]), "trace_not_found")
        if route[2] == "attribution":
            return _require(service.trace_attribution(route[1]), "trace_not_found")
    if method == "GET" and route == ["outcome-attributions"]:
        return service.list_outcome_attributions(
            trace_id=query.get("trace_id"),
            layer=query.get("layer"),
            limit=int_param(query.get("limit"), 100, maximum=5000),
        )
    if method == "POST" and route == ["traces", "prune"]:
        require_confirm(body, "traces.prune", "admin")
        return service.prune_traces(older_than_days=_optional_int(body.get("older_than_days")))
    if method == "POST" and route == ["governance", "run"]:
        if bool(body.get("apply")):
            require_confirm(body, "governance.run.apply", "admin")
        return service.govern_memories(apply=bool(body.get("apply")))
    if method == "POST" and route == ["governance", "periodic"]:
        return service.periodic_governance(interval_minutes=_optional_int(body.get("interval_minutes")) or 60)
    if method == "GET" and route == ["governance", "policies"]:
        return service.governance_policies(policy_type=query.get("policy_type"), active=bool_param(query.get("active"), True))
    if method == "POST" and route == ["consolidation", "run"]:
        require_confirm(body, "consolidation.run", "write")
        return service.consolidate_memories()
    if method == "POST" and route == ["export"]:
        return service.export_data(limit=_optional_int(body.get("limit")) or 5000)
    if method == "POST" and route == ["prune-events"]:
        require_confirm(body, "prune_events", "admin")
        return service.prune_events(older_than_days=_optional_int(body.get("older_than_days")))
    if method == "POST" and route == ["prune-runtime"]:
        require_confirm(body, "prune_runtime", "admin")
        return service.prune_runtime(older_than_days=_optional_int(body.get("older_than_days")), include_recipes=bool(body.get("include_recipes")), include_skills=bool(body.get("include_skills")))
    if method == "POST" and route == ["wipe"]:
        require_confirm(body, "wipe", "admin")
        if body.get("confirmation") != "WIPE":
            raise ApiError("second_confirmation_required", "Wipe requires confirmation='WIPE'.", status=409)
        return service.wipe_data()

    raise ApiError("not_found", "API route not found.", status=404, details={"method": method, "path": path})


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    config = load_config()
    server = RuntimeApiServer((host, port), RuntimeApiHandler, config)
    try:
        print(f"codex-cognitive-runtime API listening on http://{host}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\ncodex-cognitive-runtime API stopped", flush=True)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex Cognitive Runtime local HTTP API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Refusing to listen outside localhost.")
    run(host=args.host, port=args.port)
    return 0


def _dynamic_skill_action(service: MemoryService, skill_id: str, action: str, body: dict[str, Any]) -> Any:
    note = str(body.get("note") or "")
    if action == "promote":
        return _require(service.promote_dynamic_skill(skill_id, note=note), "dynamic_skill_not_found")
    if action == "reject":
        return _require(service.reject_dynamic_skill(skill_id, note=note), "dynamic_skill_not_found")
    if action == "deprecate":
        return _require(service.deprecate_dynamic_skill(skill_id, note=note), "dynamic_skill_not_found")
    raise ApiError("not_found", "Dynamic skill action not found.", status=404)


def _live_runtime_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "active_workflow": status.get("active_workflow"),
        "open_violations": [
            {
                "id": item.get("id"),
                "content": item.get("content"),
                "status": item.get("status"),
                "updated_at": item.get("updated_at"),
                "metadata_json": {
                    "violation_type": (item.get("metadata_json") or {}).get("violation_type"),
                    "severity": (item.get("metadata_json") or {}).get("severity"),
                },
            }
            for item in (status.get("open_violations") or [])[:5]
        ],
        "learned_recipe_count": len(status.get("learned_recipes") or []),
        "runtime_observer": status.get("runtime_observer"),
    }


def _live_trace_audit(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_count": audit.get("trace_count", 0),
        "open_count": audit.get("open_count", 0),
        "failed_count": audit.get("failed_count", 0),
        "stale_open_count": len(audit.get("stale_open_traces") or []),
        "high_violation_count": len(audit.get("high_violation_traces") or []),
    }


def _runtime_logs(service: MemoryService, query: dict[str, str]) -> dict[str, Any]:
    trace_limit = int_param(query.get("trace_limit"), 20, minimum=1, maximum=100)
    event_limit = int_param(query.get("event_limit"), 300, minimum=1, maximum=5000)
    traces = service.list_traces(
        session_id=query.get("session_id"),
        turn_id=query.get("turn_id"),
        limit=trace_limit,
    )
    selected_trace_id = query.get("trace_id") or (str(traces[0]["id"]) if traces else "")
    trace_detail = service.get_trace(selected_trace_id) if selected_trace_id else None
    trace = trace_detail.get("trace") if isinstance(trace_detail, dict) else None
    events = service.trace_events(selected_trace_id, limit=event_limit) if selected_trace_id else []
    development_events = [event for event in events if str(event.get("name") or "").startswith("development_audit_")]
    return {
        "traces": traces,
        "selected_trace_id": selected_trace_id or None,
        "trace": trace,
        "trace_detail": trace_detail,
        "events": events,
        "development_events": development_events,
        "event_count": len(events),
        "development_event_count": len(development_events),
        "development_audit_enabled": bool(getattr(service.config, "development_audit", False)),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _optional_str(value: Any) -> str | None:
    text = str(value or "")
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError("invalid_parameter", "Integer parameter is invalid.", status=400, details={"value": value}) from exc


def _require(value: Any, code: str) -> Any:
    if value is None:
        raise ApiError(code, code.replace("_", " "), status=404)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
