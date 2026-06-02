from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Any

from .ledger import Ledger, project_key_for_cwd
from .security import redact_secrets
from .timeutil import local_now, local_now_iso, parse_timestamp


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    session_id: str | None
    turn_id: str | None
    cwd: str | None
    project_key: str | None


class RuntimeMonitor:
    def __init__(self, ledger: Ledger, strict_privacy: bool = False, live_log: bool = False):
        self.ledger = ledger
        self.strict_privacy = strict_privacy
        self.live_log = live_log

    def start_trace(
        self,
        prompt: str,
        session_id: str | None = None,
        turn_id: str | None = None,
        cwd: str | None = None,
        root_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceContext:
        prompt_text = str(redact_secrets(prompt or ""))
        prompt_sha = _sha(prompt_text)
        started_at = _now()
        trace_id = "trace_" + hashlib.sha256(f"{session_id or ''}|{turn_id or ''}|{prompt_sha}|{started_at}".encode("utf-8")).hexdigest()[:24]
        project_key = project_key_for_cwd(cwd) if cwd else None
        trace_metadata = dict(metadata or {})
        stored_cwd = cwd
        stored_project_key = project_key
        prompt_preview = prompt_text[:500]
        if self.strict_privacy:
            trace_metadata["cwd_sha256"] = _sha(cwd or "") if cwd else None
            trace_metadata["project_key_sha256"] = _sha(project_key or "") if project_key else None
            stored_cwd = None
            stored_project_key = None
            prompt_preview = None
        self.ledger.record_trace(
            trace_id,
            session_id=session_id,
            turn_id=turn_id,
            cwd=stored_cwd,
            project_key=stored_project_key,
            prompt_sha256=prompt_sha,
            prompt_preview=prompt_preview,
            prompt_chars=len(prompt_text),
            root_event_id=root_event_id,
            metadata=trace_metadata,
        )
        context = TraceContext(trace_id, session_id, turn_id, cwd, project_key)
        self.event(context, "trace_started", metadata={"prompt_chars": len(prompt_text), **trace_metadata})
        return context

    def get_or_start_trace(
        self,
        prompt: str,
        session_id: str | None = None,
        turn_id: str | None = None,
        cwd: str | None = None,
        root_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceContext:
        existing = self.ledger.latest_trace(session_id=session_id, turn_id=turn_id)
        if existing and existing.get("status") not in {"completed", "failed"}:
            project_key = project_key_for_cwd(cwd) if cwd else None
            return TraceContext(str(existing["id"]), session_id, turn_id, cwd, project_key)
        return self.start_trace(prompt, session_id=session_id, turn_id=turn_id, cwd=cwd, root_event_id=root_event_id, metadata=metadata)

    def update_trace(self, context: TraceContext, **fields: Any) -> dict[str, Any] | None:
        return self.ledger.update_trace(context.trace_id, **fields)

    def complete_trace(self, context: TraceContext, final_outcome: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.event(context, "trace_completed", status="completed", metadata={"final_outcome": final_outcome, **(metadata or {})})
        return self.ledger.complete_trace(context.trace_id, final_outcome=final_outcome, metadata=metadata or {})

    def fail_trace(self, context: TraceContext, final_outcome: str = "failure", metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self.event(context, "trace_failed", severity="error", status="failed", metadata={"final_outcome": final_outcome, **(metadata or {})})
        return self.ledger.fail_trace(context.trace_id, final_outcome=final_outcome, metadata=metadata or {})

    def start_span(self, context: TraceContext, name: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.ledger.record_trace_span(context.trace_id, name, metadata=self._sanitize(metadata or {}))

    def end_span(self, span_id: str | None, status: str = "completed", metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not span_id:
            return None
        return self.ledger.end_trace_span(span_id, status=status, metadata=self._sanitize(metadata or {}))

    def event(
        self,
        context: TraceContext,
        name: str,
        span_id: str | None = None,
        severity: str = "info",
        status: str | None = None,
        message: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_message = None if self.strict_privacy else redact_secrets(message or "")[:500] if message else None
        event = self.ledger.record_trace_event(
            context.trace_id,
            name,
            span_id=span_id,
            severity=severity,
            status=status,
            message=safe_message,
            subject_type=subject_type,
            subject_id=subject_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            metadata=self._sanitize(metadata or {}),
        )
        if self.live_log:
            _live_log(event)
        return event

    def link(self, context: TraceContext, target_type: str, target_id: str | None, relation: str, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not target_id:
            return None
        return self.ledger.link_trace(context.trace_id, target_type, str(target_id), relation, self._sanitize(metadata or {}))

    def list_traces(self, session_id: str | None = None, turn_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.ledger.list_traces(session_id=session_id, turn_id=turn_id, limit=limit)

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        return self.ledger.get_trace(trace_id)

    def trace_events(self, trace_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return self.ledger.list_trace_events(trace_id, limit=limit)

    def trace_summary(self, trace_id: str) -> dict[str, Any] | None:
        trace = self.ledger.get_trace(trace_id)
        if not trace:
            return None
        events = self.ledger.list_trace_events(trace_id, limit=5000)
        links = self.ledger.list_trace_links(trace_id)
        return _summary(trace, events, links)

    def trace_audit(self) -> dict[str, Any]:
        traces = self.ledger.list_traces(limit=5000)
        events_by_trace: dict[str, list[dict[str, Any]]] = {}
        now = local_now()
        stale = []
        incomplete = []
        failed = []
        high_violations = []
        dropped = []
        timeouts = []
        for trace in traces:
            trace_id = str(trace["id"])
            events = self.ledger.list_trace_events(trace_id, limit=5000)
            events_by_trace[trace_id] = events
            status = str(trace.get("status") or "")
            if status not in {"completed", "failed"}:
                try:
                    updated = parse_timestamp(str(trace.get("updated_at") or ""))
                    if (now - updated).total_seconds() > 86400:
                        stale.append(trace)
                except ValueError:
                    pass
            if status == "runtime_skill_injected":
                incomplete.append(trace)
            if status == "failed" or trace.get("final_outcome") == "failure":
                failed.append(trace)
            if any(event.get("name") == "workflow_violation_detected" and event.get("severity") == "error" for event in events):
                high_violations.append(trace)
            if any(event.get("name") == "runtime_skill_dropped" for event in events):
                dropped.append(trace)
            if any((event.get("metadata_json") or {}).get("model_timeout_count") for event in events):
                timeouts.append(trace)
        return {
            "trace_count": len(traces),
            "open_count": len([item for item in traces if item.get("status") not in {"completed", "failed"}]),
            "failed_count": len(failed),
            "stale_open_traces": stale[:20],
            "incomplete_injected_traces": incomplete[:20],
            "orphan_feedback": _orphan_feedback(events_by_trace),
            "failed_traces": failed[:20],
            "high_violation_traces": high_violations[:20],
            "dropped_skill_traces": dropped[:20],
            "model_timeout_traces": timeouts[:20],
        }

    def export_trace(self, trace_id: str) -> dict[str, Any] | None:
        return self.ledger.export_trace(trace_id)

    def prune_traces(self, older_than_days: int | None = None) -> dict[str, Any]:
        return self.ledger.prune_traces(older_than_days=older_than_days)

    def _sanitize(self, metadata: dict[str, Any]) -> dict[str, Any]:
        if not self.strict_privacy:
            return redact_secrets(metadata)
        return _strict(metadata)


def _summary(trace: dict[str, Any], events: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = {event["name"]: event for event in events}
    basis = by_name.get("basis_retrieved", {}).get("metadata_json", {})
    injected = by_name.get("runtime_skill_injected", {}).get("metadata_json", {})
    reviewed = by_name.get("runtime_skill_reviewed", {}).get("metadata_json", {})
    feedback = by_name.get("runtime_skill_feedback_recorded", {}).get("metadata_json", {})
    stop = by_name.get("workflow_stop_audited", {}).get("metadata_json", {})
    acceptance = by_name.get("acceptance_coverage_evaluated", {}).get("metadata_json", {})
    attribution = by_name.get("outcome_attribution_completed", {}).get("metadata_json", {})
    latency = {}
    for event in events:
        metadata = event.get("metadata_json") or {}
        if event.get("name") == "runtime_skill_injected":
            latency = metadata.get("latency") or {}
    return {
        "trace_id": trace.get("id"),
        "status": trace.get("status"),
        "prompt": {"preview": trace.get("prompt_preview") or "omitted_by_strict_privacy", "chars": trace.get("prompt_chars")},
        "skill_need": (by_name.get("skill_need_decision") or {}).get("metadata_json") or {},
        "basis": {
            "memory_count": basis.get("memory_basis_count", 0),
            "durable_skill_count": basis.get("durable_skill_count", 0),
            "seed_skill_count": basis.get("seed_skill_count", 0),
            "memory_ids": basis.get("memory_basis_ids") or [],
            "durable_skill_ids": basis.get("durable_skill_ids") or [],
            "seed_skill_ids": basis.get("seed_skill_ids") or [],
        },
        "runtime_skill": {
            "name": injected.get("skill_name"),
            "injection_id": trace.get("runtime_skill_injection_id"),
            "review_status": reviewed.get("review_status"),
            "basis_precedence": reviewed.get("basis_precedence"),
        },
        "workflow": {
            "workflow_id": trace.get("workflow_id"),
            "observed": bool(stop),
            "completed": stop.get("completed"),
            "violations": stop.get("violations") or [],
            "acceptance_coverage": stop.get("acceptance_coverage") or {},
        },
        "feedback": {
            "outcome": feedback.get("outcome"),
            "target": feedback.get("feedback_target"),
            "dimensions": feedback.get("dimensions") or {},
        },
        "closed_loop": {
            "complete": attribution.get("closed_loop_complete"),
            "primary_failure_layer": attribution.get("primary_failure_layer"),
            "layer_results": attribution.get("layer_results") or {},
            "acceptance_coverage": acceptance,
        },
        "adjustments": {
            "seed_skills": [event.get("metadata_json") for event in events if event.get("name") == "seed_skill_adjusted"],
            "seed_skill_calibrations": [event.get("metadata_json") for event in events if event.get("name") == "seed_skill_calibration_applied"],
            "durable_skills": [event.get("metadata_json") for event in events if event.get("name") == "durable_skill_adjusted"],
        },
        "latency": latency,
        "links": links,
    }


def _orphan_feedback(events_by_trace: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    orphans = []
    for trace_id, events in events_by_trace.items():
        names = {event.get("name") for event in events}
        if "runtime_skill_feedback_recorded" in names and "runtime_skill_injected" not in names:
            orphans.append({"trace_id": trace_id})
    return orphans


def _strict(metadata: dict[str, Any]) -> dict[str, Any]:
    return _strict_value(redact_secrets(metadata), parent_key="")


def _strict_value(value: Any, parent_key: str) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if _strict_omit_key(lowered) and item not in (None, "", [], {}):
                result[f"{key_text}_sha256"] = _sha(str(item))
                result[f"{key_text}_chars"] = len(str(item))
                result[key_text] = "omitted_by_strict_privacy"
            elif lowered == "files_changed" and isinstance(item, list):
                result[f"{key_text}_hashes"] = [_sha(str(entry)) for entry in item]
                result[f"{key_text}_count"] = len(item)
                result[key_text] = []
            else:
                result[key_text] = _strict_value(item, lowered)
        return result
    if isinstance(value, list):
        return [_strict_value(item, parent_key) for item in value[:20]]
    if isinstance(value, str) and _strict_omit_key(parent_key) and value:
        return "omitted_by_strict_privacy"
    return value


def _strict_omit_key(key: str) -> bool:
    return any(
        marker in key
        for marker in (
            "prompt",
            "request",
            "context",
            "content",
            "command",
            "path",
            "cwd",
            "project_key",
            "feedback",
            "question",
            "answer",
            "message",
            "preview",
            "text",
        )
    )


def _live_log(event: dict[str, Any]) -> None:
    payload = {
        "trace_id": event.get("trace_id"),
        "span_id": event.get("span_id"),
        "name": event.get("name"),
        "severity": event.get("severity"),
        "status": event.get("status"),
        "subject_type": event.get("subject_type"),
        "subject_id": event.get("subject_id"),
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "created_at": event.get("created_at"),
        "metadata": event.get("metadata_json") or {},
    }
    sys.stderr.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stderr.flush()


def _sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()


def _now() -> str:
    return local_now_iso()
