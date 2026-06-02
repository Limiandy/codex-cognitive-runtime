from __future__ import annotations

from typing import Any


CALIBRATION_METADATA_KEY = "seed_scoring_calibration"
CALIBRATION_VERSION = 1
MAX_CALIBRATION_PROFILES = 50


def calibration_profile(task_profile: dict[str, Any] | None, domain: str | None = None) -> dict[str, Any]:
    profile = task_profile if isinstance(task_profile, dict) else {}
    normalized_domain = _norm(domain or profile.get("domain") or profile.get("task_domain") or "")
    task_type = _norm(profile.get("task_type") or "general_task") or "general_task"
    surfaces = sorted({_norm(item) for item in profile.get("surfaces") or [] if _norm(item)})
    project_type = _norm(profile.get("project_type") or "")
    return {
        "task_type": task_type,
        "domain": normalized_domain,
        "surfaces": surfaces,
        "project_type": project_type,
    }


def calibration_profile_key(profile: dict[str, Any]) -> str:
    surfaces = ",".join(str(item) for item in profile.get("surfaces") or []) or "none"
    domain = str(profile.get("domain") or "unknown")
    task_type = str(profile.get("task_type") or "general_task")
    project_type = str(profile.get("project_type") or "unknown")
    return f"task:{task_type}|domain:{domain}|surfaces:{surfaces}|project:{project_type}"


def apply_seed_scoring_feedback(
    metadata: dict[str, Any],
    *,
    task_profile: dict[str, Any] | None,
    domain: str | None,
    outcome: str,
    now: str,
    evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = dict(metadata or {})
    profile = calibration_profile(task_profile, domain=domain)
    key = calibration_profile_key(profile)
    calibration = _calibration(updated)
    profiles = dict(calibration.get("profiles") or {})
    entry = dict(profiles.get(key) or {})
    positive = outcome in {"positive", "success"}
    negative = outcome in {"negative", "failure"}
    if not positive and not negative:
        action = {
            "action": "unchanged",
            "reason": "outcome_not_calibrating",
            "profile_key": key,
            "profile": profile,
            "outcome": outcome,
        }
        return updated, action

    entry.update(
        {
            "profile_key": key,
            "task_type": profile["task_type"],
            "domain": profile["domain"],
            "surfaces": profile["surfaces"],
            "project_type": profile["project_type"],
            "updated_at": now,
            "last_feedback_at": now,
            "last_outcome": outcome,
        }
    )
    entry["positive_count"] = int(entry.get("positive_count") or 0) + (1 if positive else 0)
    entry["negative_count"] = int(entry.get("negative_count") or 0) + (1 if negative else 0)
    delta = 3.0 if positive else -8.0
    entry["weight_delta"] = _clamp(float(entry.get("weight_delta") or 0.0) + delta, -24.0, 12.0)
    entry["penalty"] = max(0.0, -float(entry["weight_delta"]))
    if evidence:
        entry["last_feedback_target"] = str(evidence.get("feedback_target") or "")
        entry["last_source"] = str(evidence.get("source") or "")
    profiles[key] = entry
    calibration["profiles"] = _trim_profiles(profiles)
    calibration["updated_at"] = now
    calibration["last_action"] = "profile_penalty" if negative else "profile_boost"
    updated[CALIBRATION_METADATA_KEY] = calibration
    action = {
        "action": calibration["last_action"],
        "profile_key": key,
        "profile": profile,
        "outcome": outcome,
        "score_delta": round(float(entry["weight_delta"]), 3),
        "penalty": round(float(entry["penalty"]), 3),
        "positive_count": entry["positive_count"],
        "negative_count": entry["negative_count"],
        "feedback_target": str((evidence or {}).get("feedback_target") or ""),
    }
    return updated, action


def seed_scoring_adjustment(
    metadata: dict[str, Any] | None,
    *,
    task_profile: dict[str, Any] | None,
    domain: str | None = None,
) -> dict[str, Any]:
    calibration = _calibration(metadata or {})
    profiles = calibration.get("profiles") or {}
    if not profiles:
        return {"applied": False, "score_delta": 0.0, "reason": "no_seed_scoring_calibration"}
    target = calibration_profile(task_profile, domain=domain)
    matches = []
    for key, raw_entry in profiles.items():
        if not isinstance(raw_entry, dict):
            continue
        strength = _profile_match_strength(target, raw_entry)
        if strength <= 0:
            continue
        weight_delta = float(raw_entry.get("weight_delta") or 0.0)
        if weight_delta == 0:
            continue
        matches.append((strength, abs(weight_delta), key, raw_entry, weight_delta * strength))
    if not matches:
        return {"applied": False, "score_delta": 0.0, "reason": "no_matching_profile_calibration"}
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    strength, _, key, entry, weighted_delta = matches[0]
    score_delta = round(weighted_delta, 3)
    return {
        "applied": True,
        "action": "profile_boost" if score_delta > 0 else "profile_penalty",
        "score_delta": score_delta,
        "matched_profile_key": key,
        "match_strength": round(strength, 3),
        "positive_count": int(entry.get("positive_count") or 0),
        "negative_count": int(entry.get("negative_count") or 0),
        "penalty": round(max(0.0, -float(entry.get("weight_delta") or 0.0)) * strength, 3),
    }


def _calibration(metadata: dict[str, Any]) -> dict[str, Any]:
    raw = metadata.get(CALIBRATION_METADATA_KEY)
    calibration = dict(raw) if isinstance(raw, dict) else {}
    calibration.setdefault("version", CALIBRATION_VERSION)
    calibration.setdefault("profiles", {})
    return calibration


def _profile_match_strength(target: dict[str, Any], entry: dict[str, Any]) -> float:
    target_domain = str(target.get("domain") or "")
    entry_domain = str(entry.get("domain") or "")
    target_task = str(target.get("task_type") or "")
    entry_task = str(entry.get("task_type") or "")
    target_surfaces = set(str(item) for item in target.get("surfaces") or [])
    entry_surfaces = set(str(item) for item in entry.get("surfaces") or [])
    domain_match = bool(target_domain and entry_domain and target_domain == entry_domain)
    task_match = bool(target_task and entry_task and target_task == entry_task)
    surface_overlap = len(target_surfaces & entry_surfaces)
    exact_surfaces = target_surfaces == entry_surfaces
    if domain_match and task_match and exact_surfaces:
        return 1.0
    if domain_match and exact_surfaces:
        return 0.9
    if domain_match and surface_overlap:
        return 0.82
    if domain_match:
        return 0.68
    if task_match and surface_overlap:
        return 0.58
    if surface_overlap:
        return 0.42
    return 0.0


def _trim_profiles(profiles: dict[str, Any]) -> dict[str, Any]:
    items = sorted(
        profiles.items(),
        key=lambda item: str((item[1] if isinstance(item[1], dict) else {}).get("updated_at") or ""),
        reverse=True,
    )
    return {key: value for key, value in items[:MAX_CALIBRATION_PROFILES]}


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("-", "_").split())


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
