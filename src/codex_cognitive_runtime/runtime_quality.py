from __future__ import annotations

from typing import Any


def evaluate_runtime_skill(skill: dict[str, Any]) -> dict[str, Any]:
    issues = []
    memory_ids = skill.get("memory_basis_ids") or []
    durable_ids = skill.get("durable_skill_ids") or []
    seed_ids = skill.get("seed_skill_ids") or []
    strategy = [str(item) for item in skill.get("strategy") or [] if str(item).strip()]
    first_action = skill.get("first_action") if isinstance(skill.get("first_action"), dict) else {}
    text = " ".join([str(skill.get("goal") or ""), " ".join(strategy), str(skill.get("avoid") or "")]).lower()
    if len(strategy) < 2:
        issues.append("insufficient_strategy")
    if not first_action.get("type"):
        issues.append("missing_first_action")
    if not (memory_ids or durable_ids or seed_ids):
        basis_grounding = 0.55
    else:
        basis_grounding = 0.9
    if not memory_ids and any(signal in text for signal in ("your preference", "你的偏好", "your organization", "你的组织")):
        issues.append("unbacked_user_or_org_claim")
        basis_grounding = 0.2
    scores = {
        "basis_grounding": basis_grounding,
        "clarification_quality": 0.9 if first_action.get("type") == "ask_clarifying_questions" else 0.7,
        "strategy_actionability": 0.85 if len(strategy) >= 2 else 0.3,
        "safety": 0.2 if "api_key" in text or "token=" in text else 1.0,
    }
    return {"valid": not issues and min(scores.values()) >= 0.55, "scores": scores, "issues": issues}
