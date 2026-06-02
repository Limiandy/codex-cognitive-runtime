from __future__ import annotations

from collections import defaultdict
from typing import Any


ATTRIBUTION_LAYERS = (
    "task_understanding",
    "recall",
    "seed_scoring",
    "fragment_selection",
    "final_context",
    "execution_guard",
)

POSITIVE_OUTCOMES = {"positive", "success"}
NEGATIVE_OUTCOMES = {"negative", "failure"}
MIXED_OUTCOMES = {"mixed"}

FEEDBACK_TARGET_LAYERS = {
    "memory_basis": ("recall",),
    "seed_skill": ("seed_scoring",),
    "durable_skill": ("fragment_selection",),
    "skill_strategy": ("fragment_selection",),
    "first_action": ("task_understanding", "fragment_selection"),
    "final_result": ("final_context",),
    "workflow_execution": ("execution_guard",),
    "execution": ("execution_guard",),
}


class OutcomeAttributionEngine:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def refresh(self, trace_id: str) -> dict[str, Any] | None:
        trace = self.ledger.get_trace(trace_id)
        if not trace:
            return None
        events = self.ledger.list_trace_events(trace_id, limit=5000)
        links = self.ledger.list_trace_links(trace_id)
        attribution = build_outcome_attribution(trace, events, links)
        layers = self.ledger.record_outcome_attributions(trace_id, attribution["layers"])
        return {**attribution, "layers": layers}

    def get(self, trace_id: str, refresh: bool = True) -> dict[str, Any] | None:
        if refresh:
            refreshed = self.refresh(trace_id)
            if refreshed:
                return refreshed
        trace = self.ledger.get_trace(trace_id)
        if not trace:
            return None
        layers = self.ledger.list_outcome_attributions(trace_id=trace_id)
        return {
            "trace_id": trace_id,
            "status": trace.get("status"),
            "final_outcome": trace.get("final_outcome"),
            "overall_outcome": _overall_outcome(trace, []),
            "layers": layers,
        }

    def list(self, trace_id: str | None = None, layer: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        return self.ledger.list_outcome_attributions(trace_id=trace_id, layer=layer, limit=limit)


def build_acceptance_coverage(events: list[dict[str, Any]]) -> dict[str, Any]:
    stop_event = _latest_event(events, "workflow_stop_audited")
    stop_metadata = _event_metadata(stop_event)
    coverage = stop_metadata.get("acceptance_coverage") if isinstance(stop_metadata.get("acceptance_coverage"), dict) else {}
    if coverage:
        summary = coverage.get("summary") if isinstance(coverage.get("summary"), dict) else {}
        status = "failed" if _safe_int(summary.get("failed")) else "missing" if _safe_int(summary.get("missing")) else "passed" if summary.get("complete") else "unknown"
        return {**coverage, "status": status}
    criteria = []
    for event in events:
        if event.get("name") not in {"acceptance_missing", "acceptance_failed"}:
            continue
        metadata = _event_metadata(event)
        criteria.append(
            {
                "id": metadata.get("criterion_id"),
                "criterion_text": metadata.get("criterion_text"),
                "status": metadata.get("status") or ("failed" if event.get("name") == "acceptance_failed" else "missing"),
                "required_steps": metadata.get("required_steps") or [],
                "missing_steps": metadata.get("missing_steps") or [],
                "evidence": [_event_ref(event)],
            }
        )
    counts = defaultdict(int)
    for item in criteria:
        counts[str(item.get("status") or "missing")] += 1
    status = "failed" if counts.get("failed") else "missing" if counts.get("missing") else "unknown"
    return {
        "schema_version": 1,
        "status": status,
        "criteria": criteria,
        "summary": {
            "total": len(criteria),
            "covered": counts.get("covered", 0),
            "missing": counts.get("missing", 0),
            "failed": counts.get("failed", 0),
            "complete": bool(criteria) and not counts.get("missing") and not counts.get("failed"),
        },
        "missing_criteria": [item for item in criteria if item.get("status") == "missing"],
        "failed_criteria": [item for item in criteria if item.get("status") == "failed"],
    }


def build_outcome_attribution(
    trace: dict[str, Any] | list[dict[str, Any]],
    events: list[dict[str, Any]] | None = None,
    links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if events is None and isinstance(trace, list):
        events = trace
        trace = _trace_from_events(events)
    events = events or []
    links = links or []
    events_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        events_by_name[str(event.get("name") or "")].append(event)
    feedback_by_layer = _feedback_by_layer(events)
    layers = [
        _with_feedback(_task_understanding_layer(events_by_name), feedback_by_layer),
        _with_feedback(_recall_layer(events_by_name), feedback_by_layer),
        _with_feedback(_seed_scoring_layer(events_by_name), feedback_by_layer),
        _with_feedback(_fragment_selection_layer(trace, events_by_name), feedback_by_layer),
        _with_feedback(_final_context_layer(trace, events_by_name), feedback_by_layer),
        _with_feedback(_execution_guard_layer(trace, events_by_name), feedback_by_layer),
    ]
    return {
        "trace_id": trace.get("id"),
        "status": trace.get("status"),
        "final_outcome": trace.get("final_outcome"),
        "overall_outcome": _overall_outcome(trace, events),
        "closed_loop_complete": _closed_loop_complete(layers),
        "primary_failure_layer": _primary_failure_layer(layers),
        "layer_results": _layer_results(layers),
        "source_counts": {"events": len(events), "links": len(links)},
        "layers": layers,
    }


def _task_understanding_layer(events_by_name: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    validated_event = _latest(events_by_name, "task_understanding_validated")
    skill_event = _latest(events_by_name, "skill_need_decision")
    role_event = _latest(events_by_name, "role_profile_selected")
    validated = _event_metadata(validated_event).get("validated_task") or _event_metadata(validated_event)
    skill_need = _event_metadata(skill_event)
    corrections = _as_list(validated.get("corrections") or _event_metadata(validated_event).get("corrections"))
    violations = _as_list(validated.get("violations") or _event_metadata(validated_event).get("violations"))
    if not validated_event:
        outcome = "unknown"
        contribution = "neutral"
        confidence = 0.0
        summary = "Task understanding did not run for this trace."
    else:
        outcome = "failure" if violations else "success"
        contribution = "negative" if violations else "positive"
        confidence = 0.72 if violations else 0.84
        summary = (
            f"Validated task as {validated.get('domain') or 'unknown'}/"
            f"{validated.get('task_type') or skill_need.get('intent') or 'unknown'}; "
            f"skill_needed={bool(validated.get('skill_needed', skill_need.get('skill_needed')))}."
        )
    return _layer(
        "task_understanding",
        outcome,
        contribution,
        confidence,
        summary,
        _event_refs(validated_event, skill_event, role_event),
        {
            "validated_task": _task_public(validated),
            "skill_need": _skill_need_public(skill_need),
            "corrections": corrections,
            "violations": violations,
        },
    )


def _recall_layer(events_by_name: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    recall_event = _latest(events_by_name, "memory_recall_completed")
    classified_event = _latest(events_by_name, "memory_recall_classified")
    metadata = _event_metadata(recall_event)
    memory_ids = [str(item) for item in metadata.get("memory_ids") or [] if item]
    memory_count = _safe_int(metadata.get("memory_count") or len(memory_ids))
    classes = _event_metadata(classified_event)
    if not recall_event:
        outcome = "unknown"
        contribution = "neutral"
        confidence = 0.0
        summary = "Memory recall evidence is absent."
    else:
        outcome = "success"
        contribution = "positive" if memory_count else "neutral"
        confidence = 0.78 if memory_count else 0.62
        summary = f"Recall returned {memory_count} memory fragment(s)."
    return _layer(
        "recall",
        outcome,
        contribution,
        confidence,
        summary,
        _event_refs(recall_event, classified_event),
        {
            "recall_id": metadata.get("recall_id"),
            "route": metadata.get("route") or {},
            "memory_ids": memory_ids,
            "memory_class_counts": {key: len(value) for key, value in classes.items() if isinstance(value, list)},
        },
    )


def _seed_scoring_layer(events_by_name: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    basis_event = _latest(events_by_name, "basis_retrieved")
    metadata = _event_metadata(basis_event)
    scores = [dict(item) for item in metadata.get("seed_skill_selection_scores") or [] if isinstance(item, dict)]
    selected = [item for item in scores if item.get("selected")]
    excluded = [item for item in scores if item.get("reason")]
    if not basis_event:
        outcome = "unknown"
        contribution = "neutral"
        confidence = 0.0
        summary = "Seed skill scoring did not run."
    else:
        outcome = "success"
        contribution = "positive" if selected else "neutral"
        confidence = 0.76 if scores else 0.54
        summary = f"Scored {len(scores)} seed skill candidate(s); selected {len(selected)}."
    return _layer(
        "seed_scoring",
        outcome,
        contribution,
        confidence,
        summary,
        _event_refs(basis_event),
        {
            "scores": [_score_public(item) for item in scores[:20]],
            "selected_seed_skill_ids": [str(item.get("id")) for item in selected if item.get("id")],
            "excluded_seed_skill_ids": [str(item.get("id")) for item in excluded if item.get("id")],
        },
    )


def _fragment_selection_layer(trace: dict[str, Any], events_by_name: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    basis_event = _latest(events_by_name, "basis_retrieved")
    reviewed_event = _latest(events_by_name, "runtime_skill_reviewed")
    injected_event = _latest(events_by_name, "runtime_skill_injected")
    dropped_event = _latest(events_by_name, "runtime_skill_dropped")
    skill_event = _latest(events_by_name, "skill_need_decision")
    basis = _event_metadata(basis_event)
    reviewed = _event_metadata(reviewed_event)
    injected = _event_metadata(injected_event)
    skill_need = _event_metadata(skill_event)
    review_status = str(reviewed.get("review_status") or "")
    if dropped_event or review_status == "dropped":
        outcome = "failure"
        contribution = "negative"
        confidence = 0.82
        summary = "Runtime fragment selection dropped the synthesized skill."
    elif injected_event:
        outcome = "success"
        contribution = "positive"
        confidence = 0.84
        summary = f"Selected runtime skill {injected.get('skill_name') or trace.get('runtime_skill_injection_id') or 'unknown'}."
    elif skill_need.get("skill_needed") is False:
        outcome = "success"
        contribution = "neutral"
        confidence = 0.64
        summary = "No runtime skill fragments were required for the validated task."
    elif reviewed_event:
        outcome = "success" if review_status in {"approved", "fallback"} else "unknown"
        contribution = "positive" if outcome == "success" else "neutral"
        confidence = 0.7
        summary = f"Runtime skill review finished with status {review_status or 'unknown'}."
    else:
        outcome = "unknown"
        contribution = "neutral"
        confidence = 0.0
        summary = "Runtime fragment selection evidence is absent."
    return _layer(
        "fragment_selection",
        outcome,
        contribution,
        confidence,
        summary,
        _event_refs(basis_event, reviewed_event, injected_event, dropped_event),
        {
            "memory_basis_ids": basis.get("memory_basis_ids") or injected.get("memory_basis_ids") or [],
            "durable_skill_ids": basis.get("durable_skill_ids") or injected.get("durable_skill_ids") or [],
            "seed_skill_ids": basis.get("seed_skill_ids") or injected.get("seed_skill_ids") or [],
            "source_skill_ids": basis.get("source_skill_ids") or injected.get("source_skill_ids") or [],
            "injection_id": injected.get("injection_id") or trace.get("runtime_skill_injection_id"),
            "review_status": review_status or None,
            "review_reasons": reviewed.get("reasons") or [],
            "basis_precedence": reviewed.get("basis_precedence"),
        },
    )


def _final_context_layer(trace: dict[str, Any], events_by_name: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    final_event = _latest(events_by_name, "final_context_built")
    audit_event = _latest(events_by_name, "development_audit_prompt_context_built")
    runtime_skill_event = _latest(events_by_name, "runtime_skill_injected")
    metadata = _event_metadata(final_event) or _event_metadata(audit_event)
    runtime_skill = _event_metadata(runtime_skill_event)
    chars = metadata.get("final_context_chars", metadata.get("final_combined_context_chars", 0))
    char_count = _safe_int(chars)
    sha = metadata.get("final_context_sha256", metadata.get("final_combined_context_sha256"))
    if final_event or audit_event:
        outcome = "success"
        contribution = "positive" if char_count > 0 else "neutral"
        confidence = 0.78
        summary = f"Final additional context was built with {char_count} character(s)."
    else:
        outcome = "unknown"
        contribution = "neutral"
        confidence = 0.0
        summary = "Final context build evidence is absent."
    return _layer(
        "final_context",
        outcome,
        contribution,
        confidence,
        summary,
        _event_refs(final_event, audit_event, runtime_skill_event),
        {
            "final_context_sha256": sha,
            "final_context_chars": char_count,
            "formatted_context_version": metadata.get("formatted_context_version"),
            "has_runtime_skill_context": bool(metadata.get("has_runtime_skill_context") or runtime_skill.get("runtime_skill_context_sha256")),
            "has_runtime_control_context": bool(metadata.get("has_runtime_control_context")),
            "has_memory_context": bool(metadata.get("has_memory_context")),
            "runtime_skill_context_sha256": runtime_skill.get("runtime_skill_context_sha256"),
            "trace_final_outcome": trace.get("final_outcome"),
        },
    )


def _execution_guard_layer(trace: dict[str, Any], events_by_name: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    guard_event = _latest(events_by_name, "workflow_guard_context_injected")
    tool_events = events_by_name.get("tool_observed") or []
    step_events = events_by_name.get("workflow_step_completed") or []
    verification_failed = events_by_name.get("verification_failed") or []
    stop_event = _latest(events_by_name, "workflow_stop_audited")
    violation_events = events_by_name.get("workflow_violation_detected") or []
    stop = _event_metadata(stop_event)
    high_count = _safe_int(stop.get("high_violation_count")) + len([event for event in violation_events if event.get("severity") == "error"])
    completed = bool(stop.get("completed"))
    if high_count:
        outcome = "failure"
        contribution = "negative"
        confidence = 0.9
        summary = f"Execution guard found {high_count} high-severity violation(s)."
    elif completed:
        outcome = "success"
        contribution = "positive"
        confidence = 0.88
        summary = "Execution guard observed a completed workflow."
    elif stop_event:
        outcome = "unknown"
        contribution = "neutral"
        confidence = 0.72
        summary = "Execution guard audited stop without a completed workflow."
    elif guard_event or tool_events:
        outcome = "pending"
        contribution = "pending"
        confidence = 0.58
        summary = "Execution guard has partial workflow evidence."
    else:
        outcome = "neutral"
        contribution = "neutral"
        confidence = 0.4 if trace.get("final_outcome") == "direct_answer_no_runtime_skill" else 0.0
        summary = "No execution guard was required or observed."
    return _layer(
        "execution_guard",
        outcome,
        contribution,
        confidence,
        summary,
        _event_refs(guard_event, stop_event, *tool_events[-10:], *step_events[-10:], *verification_failed[-5:], *violation_events[-10:]),
        {
            "workflow_id": stop.get("workflow_id") or trace.get("workflow_id") or _event_metadata(guard_event).get("active_workflow"),
            "guard_injected": bool(guard_event),
            "tool_observation_count": len(tool_events),
            "completed_steps": [
                _event_metadata(event).get("matched_step_id")
                for event in step_events
                if _event_metadata(event).get("matched_step_id")
            ],
            "verification_failed_count": len(verification_failed),
            "violation_count": len(violation_events),
            "high_violation_count": high_count,
            "stop_completed": completed,
            "trace_final_outcome": trace.get("final_outcome"),
        },
    )


def _with_feedback(layer: dict[str, Any], feedback_by_layer: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    feedback_events = feedback_by_layer.get(str(layer.get("layer") or ""), [])
    if not feedback_events:
        return layer
    latest = feedback_events[-1]
    metadata = _event_metadata(latest)
    outcome = str(metadata.get("outcome") or "").lower()
    if outcome in POSITIVE_OUTCOMES:
        layer["outcome"] = "success"
        layer["contribution"] = "positive"
        layer["confidence"] = max(float(layer.get("confidence") or 0), 0.82)
    elif outcome in NEGATIVE_OUTCOMES:
        layer["outcome"] = "failure"
        layer["contribution"] = "negative"
        layer["confidence"] = max(float(layer.get("confidence") or 0), 0.86)
    elif outcome in MIXED_OUTCOMES:
        layer["outcome"] = "mixed"
        layer["contribution"] = "mixed"
        layer["confidence"] = max(float(layer.get("confidence") or 0), 0.72)
    layer["summary"] = f"{layer.get('summary', '').rstrip()} Feedback target={metadata.get('feedback_target') or 'unknown'} outcome={outcome or 'unknown'}.".strip()
    layer.setdefault("evidence", []).extend(_event_refs(*feedback_events[-5:]))
    layer["feedback"] = [_feedback_public(event) for event in feedback_events[-5:]]
    return layer


def _feedback_by_layer(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("name") != "runtime_skill_feedback_recorded":
            continue
        metadata = _event_metadata(event)
        target = str(metadata.get("feedback_target") or "unknown")
        layers = FEEDBACK_TARGET_LAYERS.get(target, ("final_context",))
        for layer in layers:
            result[layer].append(event)
    return result


def _layer(
    layer: str,
    outcome: str,
    contribution: str,
    confidence: float,
    summary: str,
    evidence: list[dict[str, Any]],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "layer": layer,
        "outcome": outcome,
        "contribution": contribution,
        "confidence": round(float(confidence or 0), 3),
        "summary": summary,
        "evidence": evidence,
        "inputs": inputs,
    }


def _overall_outcome(trace: dict[str, Any], events: list[dict[str, Any]]) -> str:
    final = str(trace.get("final_outcome") or "")
    if final:
        return final
    feedback = [event for event in events if event.get("name") == "runtime_skill_feedback_recorded"]
    if feedback:
        outcome = str(_event_metadata(feedback[-1]).get("outcome") or "")
        if outcome:
            return outcome
    return "pending" if trace.get("status") not in {"completed", "failed"} else str(trace.get("status") or "unknown")


def _primary_failure_layer(layers: list[dict[str, Any]]) -> str | None:
    for layer in layers:
        if layer.get("outcome") == "failure" or layer.get("contribution") == "negative":
            return str(layer.get("layer") or "") or None
    return None


def _closed_loop_complete(layers: list[dict[str, Any]]) -> bool:
    outcomes = {str(layer.get("layer") or ""): str(layer.get("outcome") or "") for layer in layers}
    final_ready = outcomes.get("final_context") in {"success", "failure", "mixed"}
    guard_done = outcomes.get("execution_guard") in {"success", "failure"} or outcomes.get("execution_guard") == "neutral"
    return bool(final_ready and guard_done)


def _layer_results(layers: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(layer.get("layer")): {
            "outcome": layer.get("outcome"),
            "contribution": layer.get("contribution"),
            "confidence": layer.get("confidence"),
            "summary": layer.get("summary"),
        }
        for layer in layers
        if layer.get("layer")
    }


def _trace_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    trace_id = str((events[0] if events else {}).get("trace_id") or "")
    completed = _latest_event(events, "trace_completed")
    failed = _latest_event(events, "trace_failed")
    terminal = failed or completed
    metadata = _event_metadata(terminal)
    status = "failed" if failed else "completed" if completed else "started"
    return {
        "id": trace_id,
        "status": status,
        "final_outcome": metadata.get("final_outcome"),
        "workflow_id": _event_metadata(_latest_event(events, "workflow_stop_audited")).get("workflow_id"),
    }


def _latest_event(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("name") == name:
            return event
    return None


def _latest(events_by_name: dict[str, list[dict[str, Any]]], name: str) -> dict[str, Any] | None:
    events = events_by_name.get(name) or []
    return events[-1] if events else None


def _event_metadata(event: dict[str, Any] | None) -> dict[str, Any]:
    if not event:
        return {}
    metadata = event.get("metadata_json")
    return metadata if isinstance(metadata, dict) else {}


def _event_refs(*events: dict[str, Any] | None) -> list[dict[str, Any]]:
    refs = []
    for event in events:
        if not event:
            continue
        refs.append(_event_ref(event))
    return refs


def _event_ref(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "trace_event",
        "event_id": event.get("id"),
        "name": event.get("name"),
        "severity": event.get("severity"),
        "status": event.get("status"),
        "subject_type": event.get("subject_type"),
        "subject_id": event.get("subject_id"),
        "metadata": _compact_metadata(_event_metadata(event)),
    }


def _compact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in metadata.items():
        lowered = str(key).lower()
        if lowered == "validated_task" and isinstance(value, dict):
            compact[key] = _task_public(value)
            continue
        if lowered == "runtime_skill" and isinstance(value, dict):
            compact[key] = {
                "name": value.get("name"),
                "intent": value.get("intent"),
                "domain": value.get("domain"),
                "confidence": value.get("confidence"),
            }
            continue
        if any(marker in lowered for marker in ("sha256", "hash", "id", "ids", "count", "status", "outcome", "target", "score", "selected", "rank", "reason", "severity", "violation", "correction", "surface", "domain", "task_type", "request_type", "skill_needed", "confidence", "latency", "required_step", "completed", "failed", "verified", "changed", "source", "dimension", "feedback")):
            compact[key] = _compact_value(value)
    return compact


def _compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _compact_value(item) for key, item in list(value.items())[:20]}
    if isinstance(value, list):
        return [_compact_value(item) for item in value[:20]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _task_public(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_type": task.get("request_type"),
        "is_followup": bool(task.get("is_followup")),
        "parent_trace_id": task.get("parent_trace_id"),
        "continuity_confidence": task.get("continuity_confidence"),
        "skill_needed": bool(task.get("skill_needed")),
        "domain": task.get("domain"),
        "task_type": task.get("task_type"),
        "surfaces": task.get("surfaces") or [],
        "role_profile": _role_public(task.get("role_profile") if isinstance(task.get("role_profile"), dict) else {}),
        "implementation_scope_count": len(task.get("implementation_scope") or []),
        "out_of_scope_count": len(task.get("out_of_scope") or []),
        "acceptance_criteria_count": len(task.get("acceptance_criteria") or []),
        "clarification_required": bool(task.get("clarification_required")),
        "uncertainty_count": len(task.get("uncertainties") or []),
        "corrections": task.get("corrections") or [],
        "violations": task.get("violations") or [],
        "degraded": bool(task.get("degraded")),
        "source": task.get("source"),
    }


def _role_public(role: dict[str, Any]) -> dict[str, Any]:
    return {
        "primary": role.get("primary"),
        "supporting": role.get("supporting") or [],
        "locked_for_task": bool(role.get("locked_for_task", True)),
    }


def _skill_need_public(skill_need: dict[str, Any]) -> dict[str, Any]:
    return {
        "skill_needed": bool(skill_need.get("skill_needed")),
        "mode": skill_need.get("mode"),
        "intent": skill_need.get("intent"),
        "domain": skill_need.get("domain"),
        "complexity": skill_need.get("complexity"),
        "requires_memory": bool(skill_need.get("requires_memory")),
        "requires_clarification": bool(skill_need.get("requires_clarification")),
        "reason": skill_need.get("reason"),
    }


def _score_public(score: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": score.get("id"),
        "name": score.get("name"),
        "rank": score.get("rank"),
        "score": score.get("score"),
        "selected": bool(score.get("selected")),
        "reason": score.get("reason"),
        "target_surfaces": score.get("target_surfaces") or [],
        "target_domains": score.get("target_domains") or [],
    }


def _feedback_public(event: dict[str, Any]) -> dict[str, Any]:
    metadata = _event_metadata(event)
    return {
        "event_id": event.get("id"),
        "outcome": metadata.get("outcome"),
        "feedback_target": metadata.get("feedback_target"),
        "dimensions": metadata.get("dimensions") or {},
    }


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
