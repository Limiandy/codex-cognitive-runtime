from __future__ import annotations

import hashlib
from typing import Any

from .security import redact_secrets
from .task_profile import workflow_required_steps


SURFACE_SKILL_HINTS = {
    "frontend": ("frontend", "ui", "ux", "designer", "interface", "vue", "react"),
    "backend": ("backend", "api", "architect", "database", "server"),
    "testing": ("tester", "testing", "qa", "evidence", "reality", "api tester"),
    "governance": ("architect", "reviewer", "workflow", "security", "product manager"),
    "privacy": ("security", "privacy", "threat", "risk"),
}


def distill_skill_basis(
    seed_skills: list[dict[str, Any]],
    durable_skills: list[dict[str, Any]],
    task_profile: dict[str, Any],
    limit: int = 3,
) -> dict[str, Any]:
    ranked = sorted(
        [_candidate("seed_skill", skill, task_profile) for skill in seed_skills]
        + [_candidate("dynamic_skill", skill, task_profile) for skill in durable_skills],
        key=lambda item: item["score"],
        reverse=True,
    )
    selected = [item for item in ranked if item["score"] > 0][:limit]
    distilled = [_distill(item["kind"], item["record"], task_profile, item) for item in selected]
    selected_fragments = _selected_fragments(distilled)
    fragment_rule_mappings = _fragment_rule_mappings(selected_fragments, task_profile)
    source_ids = [item["id"] for item in distilled]
    required_steps = workflow_required_steps(task_profile)
    return {
        "source_skill_ids": source_ids,
        "distilled_from": distilled,
        "selected_fragments": selected_fragments,
        "fragment_rule_mappings": fragment_rule_mappings,
        "workflow_required_steps": required_steps,
        "principles": _merged(distilled, "principles", 5),
        "workflow_steps": _merged(distilled, "workflow_steps", 6),
        "verification": _verification(task_profile, distilled),
        "avoid": _merged(distilled, "avoid", 5),
        "summary": _summary(distilled),
    }


def _candidate(kind: str, record: dict[str, Any], task_profile: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata_json") or {}
    text = " ".join(
        [
            str(metadata.get("name") or metadata.get("title") or ""),
            str(metadata.get("description") or ""),
            str(metadata.get("category") or record.get("domain") or ""),
            str(record.get("content") or "")[:1200],
        ]
    ).lower()
    surfaces = set(task_profile.get("surfaces") or [])
    score = float(record.get("importance") or 0) + float(record.get("strength") or 1)
    matched_surfaces = []
    for surface in surfaces:
        if any(hint in text for hint in SURFACE_SKILL_HINTS.get(surface, ())):
            score += 4
            matched_surfaces.append(surface)
    success_count = int((metadata.get("success_count") or 0))
    failure_count = int((metadata.get("failure_count") or 0))
    score += success_count - failure_count * 1.5
    risk_flags = _risk_flags(kind, record)
    return {
        "kind": kind,
        "record": record,
        "score": round(score, 3),
        "reason": _candidate_reason(matched_surfaces, success_count, failure_count),
        "risk": _risk_label(risk_flags),
        "risk_flags": risk_flags,
    }


def _distill(kind: str, record: dict[str, Any], task_profile: dict[str, Any], candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = record.get("metadata_json") or {}
    name = str(metadata.get("name") or metadata.get("title") or record.get("id") or "skill")
    surfaces = set(task_profile.get("surfaces") or [])
    principles = [f"Use {name} only as task-specific guidance, not as a persona to paste into context."]
    steps = []
    verification = []
    avoid = ["Do not paste full seed or dynamic skill source content into the prompt."]
    if "backend" in surfaces:
        principles.append("Keep API parameters, response shape, persistence semantics, and error handling explicit.")
        steps.append("Define and verify backend API contract before relying on frontend state.")
        verification.append("Exercise API parameters and response metadata directly.")
    if "frontend" in surfaces:
        principles.append("Keep frontend state derived from backend truth, including pagination totals and filters.")
        steps.append("Wire UI controls to real API state and reset pagination when filters change.")
        verification.append("Verify UI behavior in browser, including filters, pagination, reset, and empty placeholders.")
    if "testing" in surfaces:
        principles.append("Treat tests, typechecks, builds, or browser checks as completion evidence.")
        verification.append("Run the narrowest backend and frontend checks that cover the changed surfaces.")
    if "governance" in surfaces:
        principles.append("Keep runtime governance observable through metadata instead of hidden prompt text.")
        steps.append("Record task profile, source skills, and required workflow checks with the injection.")
    if "privacy" in surfaces:
        principles.append("Do not expose raw prompts, secrets, local paths, or private session data unnecessarily.")
        avoid.append("Do not store private content in public skill summaries.")
    content = str(redact_secrets(record.get("content") or ""))
    for line in content.splitlines():
        clean = " ".join(line.strip("-*# `").split())
        if not clean or len(clean) < 24:
            continue
        lowered = clean.lower()
        if any(token in lowered for token in ("verify", "test", "qa", "validation", "验证", "测试")) and len(verification) < 4:
            verification.append(clean[:180])
        elif any(token in lowered for token in ("avoid", "do not", "never", "不要", "避免")) and len(avoid) < 4:
            avoid.append(clean[:180])
        elif len(steps) < 4 and any(token in lowered for token in ("workflow", "process", "step", "start", "first", "流程", "步骤")):
            steps.append(clean[:180])
        if len(steps) >= 4 and len(verification) >= 3 and len(avoid) >= 3:
            break
    return {
        "id": str(record.get("id")),
        "kind": kind,
        "name": name,
        "category": str(metadata.get("category") or record.get("domain") or ""),
        "source_path": str(metadata.get("source_path") or metadata.get("source_id") or ""),
        "selection_reason": str((candidate or {}).get("reason") or "selected by task compatibility"),
        "selection_score": float((candidate or {}).get("score") or 0),
        "risk": str((candidate or {}).get("risk") or "low"),
        "risk_flags": [str(item) for item in (candidate or {}).get("risk_flags") or [] if item],
        "principles": _dedupe(principles)[:4],
        "workflow_steps": _dedupe(steps)[:4],
        "verification": _dedupe(verification)[:4],
        "avoid": _dedupe(avoid)[:4],
    }


def _selected_fragments(distilled: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    for item in distilled:
        for source_field in ("principles", "workflow_steps", "verification", "avoid"):
            values = [str(value) for value in item.get(source_field) or [] if str(value)]
            for index, text in enumerate(values):
                fragments.append(
                    {
                        "fragment_id": _fragment_id(str(item.get("id") or ""), str(item.get("kind") or ""), source_field, index, text),
                        "source_skill_id": str(item.get("id") or ""),
                        "source_kind": str(item.get("kind") or ""),
                        "source_name": str(item.get("name") or ""),
                        "source_category": str(item.get("category") or ""),
                        "source_path": str(item.get("source_path") or ""),
                        "source_field": source_field,
                        "fragment_index": index,
                        "text_preview": text[:240],
                        "text_sha256": _sha256(text),
                        "reason": f"{item.get('selection_reason') or 'selected by task compatibility'}; distilled as {source_field}",
                        "score": round(max(0.0, float(item.get("selection_score") or 0) - (index * 0.05)), 3),
                        "risk": str(item.get("risk") or "low"),
                        "risk_flags": [str(flag) for flag in item.get("risk_flags") or [] if flag],
                    }
                )
    return fragments[:40]


def _fragment_rule_mappings(fragments: list[dict[str, Any]], task_profile: dict[str, Any]) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    profile_hash = _sha256(
        "|".join(
            [
                ",".join(str(item) for item in task_profile.get("surfaces") or []),
                str(task_profile.get("task_type") or ""),
                str((task_profile.get("evidence") or {}).get("validated_task") or ""),
            ]
        )
    )
    for fragment in fragments:
        source_field = str(fragment.get("source_field") or "")
        if source_field == "workflow_steps":
            rule_field = "implementation_scope"
            influence = "adds_or_orders_execution_step"
        elif source_field == "verification":
            rule_field = "acceptance_criteria"
            influence = "requires_completion_evidence"
        elif source_field == "avoid":
            rule_field = "out_of_scope"
            influence = "adds_guardrail_or_negative_constraint"
        else:
            rule_field = "final_context.task_rules"
            influence = "adds_general_task_guidance"
        mappings.append(
            {
                "fragment_id": fragment.get("fragment_id"),
                "source_skill_id": fragment.get("source_skill_id"),
                "source_kind": fragment.get("source_kind"),
                "rule_field": rule_field,
                "influence": influence,
                "reason": fragment.get("reason"),
                "score": fragment.get("score"),
                "risk": fragment.get("risk"),
                "risk_flags": fragment.get("risk_flags") or [],
                "rule_hash": _sha256("|".join([profile_hash, rule_field, str(fragment.get("text_sha256") or "")])),
            }
        )
    return mappings[:40]


def _verification(task_profile: dict[str, Any], distilled: list[dict[str, Any]]) -> list[str]:
    verification = _merged(distilled, "verification", 5)
    required = workflow_required_steps(task_profile)
    if "backend_test" in required:
        verification.append("Run backend unit/API tests or a direct API check covering changed parameters.")
    if "frontend_typecheck" in required:
        verification.append("Run frontend typecheck/build for changed UI state and contracts.")
    if "browser_verify" in required:
        verification.append("Verify the user-facing workflow in a browser.")
    return _dedupe(verification)[:6]


def _merged(items: list[dict[str, Any]], key: str, limit: int) -> list[str]:
    values: list[str] = []
    for item in items:
        values.extend(str(value) for value in item.get(key) or [] if value)
    return _dedupe(values)[:limit]


def _summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No seed or dynamic skills contributed to this runtime skill."
    return " | ".join(f"{item['name']} ({item['kind']})" for item in items[:3])


def _candidate_reason(matched_surfaces: list[str], success_count: int, failure_count: int) -> str:
    parts = []
    if matched_surfaces:
        parts.append("matched task surfaces: " + ", ".join(sorted(set(matched_surfaces))))
    if success_count or failure_count:
        parts.append(f"feedback success={success_count} failure={failure_count}")
    if not parts:
        parts.append("ranked by reusable strength and task compatibility")
    return "; ".join(parts)


def _risk_flags(kind: str, record: dict[str, Any]) -> list[str]:
    metadata = record.get("metadata_json") or {}
    flags = []
    if record.get("status") not in {None, "active"}:
        flags.append("non_active_source")
    if kind == "seed_skill" and not metadata.get("source_verified"):
        flags.append("unverified_seed_source")
    failure_count = int(metadata.get("failure_count") or 0)
    success_count = int(metadata.get("success_count") or 0)
    if failure_count > success_count:
        flags.append("prior_failures_exceed_successes")
    if metadata.get("trust_state") in {"suppressed", "disabled"}:
        flags.append(f"trust_state_{metadata.get('trust_state')}")
    return flags


def _risk_label(flags: list[str]) -> str:
    if any(flag in {"non_active_source", "trust_state_suppressed", "trust_state_disabled"} for flag in flags):
        return "high"
    if flags:
        return "medium"
    return "low"


def _fragment_id(source_id: str, kind: str, source_field: str, index: int, text: str) -> str:
    return "frag_" + _sha256("|".join([source_id, kind, source_field, str(index), text]))[:24]


def _sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()


def _dedupe(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        clean = " ".join(str(value).split())
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result
