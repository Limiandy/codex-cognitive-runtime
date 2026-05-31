from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from .taxonomy import near_duplicate_text, normalize_text, tokenize


class MemoryGovernance:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def evaluate(self, apply: bool = False) -> dict[str, Any]:
        active = self.ledger.list_memories(status="active", limit=200)
        recalls = self.ledger.list_recall_events(limit=1000)
        issues = []
        actions = []

        recall_memory_ids = _recall_counts(recalls)
        adopted_memory_ids = _adopted_counts(recalls)
        total_injected = sum(len(event.get("memory_ids_json") or []) for event in recalls)
        avg_injection = total_injected / len(recalls) if recalls else 0.0

        duplicate_groups = _duplicate_groups(active)
        for group in duplicate_groups:
            keep = _duplicate_survivor(group)
            issues.append(
                {
                    "type": "duplicate_active_memory",
                    "severity": "medium",
                    "memory_ids": [item["id"] for item in group],
                    "keep_id": keep["id"],
                }
            )
            actions.append(
                {
                    "action": "supersede_duplicates",
                    "keep_id": keep["id"],
                    "memory_ids": [item["id"] for item in group if item["id"] != keep["id"]],
                    "reasons": ["near_duplicate_active_memory"],
                }
            )

        for memory in active:
            memory_id = str(memory["id"])
            recall_count = int(memory.get("recall_count") or 0)
            positive = int(memory.get("positive_recall_count") or 0)
            negative = int(memory.get("negative_recall_count") or 0)
            strength = float(memory.get("strength") or 1.0)
            content = str(memory.get("content") or "")
            reasons = []
            severity = "low"

            if negative >= 2 and negative >= positive:
                reasons.append("negative_feedback_dominates")
                severity = "high"
            if recall_count >= 5 and positive == 0 and strength > 0.8:
                reasons.append("recalled_often_without_positive_use")
                severity = max_severity(severity, "medium")
            if recall_memory_ids.get(memory_id, 0) >= 5 and adopted_memory_ids.get(memory_id, 0) == 0 and strength > 0.8:
                reasons.append("injected_often_not_adopted")
                severity = max_severity(severity, "medium")
            if len(content) > 500:
                reasons.append("content_too_verbose_for_injection")
                severity = max_severity(severity, "medium")
            if (
                memory.get("scope") == "global"
                and memory.get("abstraction_level") == "concrete"
                and memory.get("memory_type") != "user_preference"
            ):
                reasons.append("global_memory_too_concrete")
                severity = max_severity(severity, "medium")
            if strength > 1.8 and positive == 0 and recall_count >= 3:
                reasons.append("high_strength_without_positive_signal")
                severity = max_severity(severity, "medium")

            if reasons:
                issue = {"type": "memory_quality_risk", "severity": severity, "memory_id": memory_id, "reasons": reasons}
                issues.append(issue)
                action = _action_for(memory, severity, reasons)
                actions.append(action)

        pressure = "high" if avg_injection > 4.0 else "medium" if avg_injection > 2.5 else "low"
        report = {
            "active_count": len(active),
            "recall_event_count": len(recalls),
            "avg_injected_memories": round(avg_injection, 3),
            "injection_pressure": pressure,
            "issue_count": len(issues),
            "issues": issues[:50],
            "recommended_actions": actions[:50],
        }

        applied = []
        if apply:
            self.ledger.expire_governance_policies()
            for action in actions:
                result = self._apply_action(action)
                if result:
                    applied.append(result)
            policy_actions = self._repair_policies(active, issues)
            applied.extend(policy_actions)
            report_id = self.ledger.record_governance_report(report, applied)
            report["report_id"] = report_id
        return {"report": report, "applied_actions": applied}

    def should_run_periodic(self, interval_minutes: int = 60) -> bool:
        last = self.ledger.get_governance_state("last_periodic_governance_at")
        if not last:
            return True
        try:
            parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) - parsed >= timedelta(minutes=interval_minutes)

    def run_periodic_if_due(self, interval_minutes: int = 60) -> dict[str, Any]:
        if not self.should_run_periodic(interval_minutes):
            return {"skipped": "not_due"}
        result = self.evaluate(apply=True)
        self.ledger.set_governance_state("last_periodic_governance_at", _now())
        return result

    def injection_budget(self, prompt: str, requested_limit: int) -> dict[str, Any]:
        recalls = self.ledger.list_recall_events(limit=100)
        total_injected = sum(len(event.get("memory_ids_json") or []) for event in recalls)
        avg_injection = total_injected / len(recalls) if recalls else 0.0
        budget = max(1, min(8, int(requested_limit)))
        reasons = []
        if avg_injection > 4.0:
            budget = min(budget, 3)
            reasons.append("high_recent_injection_pressure")
        elif avg_injection > 2.5:
            budget = min(budget, 4)
            reasons.append("medium_recent_injection_pressure")
        if len(prompt) < 18 and not any(term in prompt for term in ("记得", "之前", "上次", "经验", "偏好")):
            budget = min(budget, 2)
            reasons.append("short_prompt_low_memory_need")
        if any(term in prompt for term in ("总结", "复盘", "架构", "经验", "之前", "上次")):
            budget = max(budget, min(int(requested_limit), 6))
            reasons.append("memory_heavy_intent")
        return {"limit": budget, "reasons": reasons, "avg_recent_injection": round(avg_injection, 3)}

    def apply_natural_feedback(self, prompt: str, session_id: str | None = None) -> dict[str, Any]:
        sentiment = _feedback_sentiment(prompt)
        if sentiment is None:
            return {"updated": 0, "reason": "no_feedback_signal"}
        event = self.ledger.latest_recall_event(session_id=session_id)
        if not event:
            return {"updated": 0, "reason": "no_recent_recall_event"}
        memory_ids = [str(item) for item in event.get("memory_ids_json") or []]
        if not memory_ids:
            return {"updated": 0, "reason": "recent_recall_had_no_memories"}
        updated = []
        for memory_id in memory_ids:
            try:
                self.ledger.register_recall_feedback(memory_id, sentiment, f"natural_feedback:{prompt[:120]}")
                updated.append(memory_id)
            except ValueError:
                continue
        return {"updated": len(updated), "outcome": sentiment, "memory_ids": updated, "recall_id": event.get("id")}

    def _apply_action(self, action: dict[str, Any]) -> dict[str, Any] | None:
        if action["action"] == "supersede_duplicates":
            keep_id = str(action.get("keep_id") or "")
            superseded = []
            for old_id in action.get("memory_ids") or []:
                old_id = str(old_id)
                if old_id and old_id != keep_id and self.ledger.get_memory(old_id):
                    self.ledger.supersede(old_id, keep_id, "governance_near_duplicate")
                    self.ledger.add_review_feedback(old_id, "governance_supersede_duplicate", keep_id)
                    superseded.append(old_id)
            return {**action, "memory_ids": superseded} if superseded else None
        memory_id = str(action.get("memory_id") or "")
        if not memory_id:
            return None
        if action["action"] == "quarantine":
            memory = self.ledger.get_memory(memory_id)
            if not memory:
                return None
            review = dict(memory.get("review_json") or {})
            review["governance"] = {"action": "quarantine", "reasons": action.get("reasons", [])}
            self.ledger.set_status(memory_id, "quarantined", review)
            self.ledger.add_review_feedback(memory_id, "governance_quarantine", ",".join(action.get("reasons", [])))
            return action
        if action["action"] == "lower_strength":
            self.ledger.adjust_strength(memory_id, float(action.get("delta", -0.25)), ",".join(action.get("reasons", [])))
            return action
        return None

    def _repair_policies(self, active: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        applied = []
        by_id = {str(memory["id"]): memory for memory in active}
        for issue in issues:
            if issue.get("type") == "duplicate_active_memory":
                keep = by_id.get(str(issue.get("keep_id")))
                if not keep:
                    continue
                matcher = _policy_matcher(keep)
                policy_id = self.ledger.add_governance_policy(
                    "candidate_gate",
                    matcher,
                    "supersede",
                    "governance learned near-duplicate admission repair",
                    source_memory_id=str(keep["id"]),
                )
                applied.append(
                    {
                        "action": "create_policy",
                        "policy_type": "candidate_gate",
                        "policy_action": "supersede",
                        "policy_id": policy_id,
                        "matcher": matcher,
                        "source_memory_id": keep["id"],
                    }
                )
                continue
            if issue.get("type") != "memory_quality_risk":
                continue
            memory = by_id.get(str(issue.get("memory_id")))
            if not memory:
                continue
            reasons = list(issue.get("reasons") or [])
            if "negative_feedback_dominates" not in reasons:
                if any(reason in reasons for reason in ("recalled_often_without_positive_use", "injected_often_not_adopted")):
                    matcher = _recall_policy_matcher(memory)
                    policy_id = self.ledger.add_governance_policy(
                        "recall_gate",
                        matcher,
                        "suppress",
                        "governance learned low-value recall suppression",
                        source_memory_id=str(memory["id"]),
                    )
                    applied.append(
                        {
                            "action": "create_policy",
                            "policy_type": "recall_gate",
                            "policy_action": "suppress",
                            "policy_id": policy_id,
                            "matcher": matcher,
                            "source_memory_id": memory["id"],
                        }
                    )
                continue
            matcher = _policy_matcher(memory)
            policy_id = self.ledger.add_governance_policy(
                "candidate_gate",
                matcher,
                "quarantine",
                "negative feedback on similar active memory",
                source_memory_id=str(memory["id"]),
            )
            applied.append(
                {
                    "action": "create_policy",
                    "policy_type": "candidate_gate",
                    "policy_action": "quarantine",
                    "policy_id": policy_id,
                    "matcher": matcher,
                    "source_memory_id": memory["id"],
                }
            )
            applied.extend(self._apply_policy_to_existing_active(active, matcher, str(memory["id"])))
        return applied

    def _apply_policy_to_existing_active(
        self,
        active: list[dict[str, Any]],
        matcher: dict[str, Any],
        source_memory_id: str,
    ) -> list[dict[str, Any]]:
        applied = []
        for memory in active:
            memory_id = str(memory["id"])
            if memory_id == source_memory_id:
                continue
            if not _memory_matches(memory, matcher):
                continue
            review = dict(memory.get("review_json") or {})
            review["governance"] = {
                "action": "quarantine_similar",
                "source_memory_id": source_memory_id,
                "matcher": matcher,
            }
            self.ledger.set_status(memory_id, "quarantined", review)
            self.ledger.add_review_feedback(memory_id, "governance_policy_quarantine", source_memory_id)
            applied.append({"action": "quarantine_similar", "memory_id": memory_id, "source_memory_id": source_memory_id})
        return applied


def _recall_counts(recalls: list[dict[str, Any]]) -> Counter:
    counts = Counter()
    for event in recalls:
        for memory_id in event.get("memory_ids_json") or []:
            counts[str(memory_id)] += 1
    return counts


def _adopted_counts(recalls: list[dict[str, Any]]) -> Counter:
    counts = Counter()
    for event in recalls:
        outcome = event.get("outcome_json") or {}
        for memory_id in outcome.get("assistant_used_memory_ids") or []:
            counts[str(memory_id)] += 1
    return counts


def _duplicate_groups(memories: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    buckets: dict[str, list[list[dict[str, Any]]]] = {}
    for memory in memories:
        key = "|".join(
            [
                str(memory.get("memory_type") or ""),
                str(memory.get("scope") or ""),
                str(memory.get("domain") or ""),
                str(memory.get("category") or ""),
                str(_polarity(str(memory.get("content") or ""))),
                str(memory.get("project_key") or "") if memory.get("scope") == "project" else "",
            ]
        )
        groups = buckets.setdefault(key, [])
        for group in groups:
            if any(_same_memory_text(memory, item) for item in group):
                group.append(memory)
                break
        else:
            groups.append([memory])
    return [group for groups in buckets.values() for group in groups if len(group) > 1]


def _same_memory_text(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_text = str(left.get("content") or "")
    right_text = str(right.get("content") or "")
    return normalize_text(left_text) == normalize_text(right_text) or near_duplicate_text(left_text, right_text)


def _duplicate_survivor(group: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        group,
        key=lambda item: (
            -float(item.get("strength") or 1.0),
            -int(item.get("positive_recall_count") or 0),
            -int(item.get("recall_count") or 0),
            str(item.get("created_at") or ""),
        ),
    )[0]


def _polarity(text: str) -> int:
    lowered = text.lower()
    polarity_text = lowered
    for neutral_phrase in ("而不是后补", "不是后补", "不能后置", "不应后置", "不要后置"):
        polarity_text = polarity_text.replace(neutral_phrase, "")
    negative = ("不", "不是", "不能", "不要", "禁用", "关闭", "disable", "not ", "never")
    positive = ("要", "应该", "必须", "启用", "打开", "enable", "always")
    if any(item in polarity_text for item in negative):
        return -1
    if any(item in polarity_text for item in positive):
        return 1
    return 0


def _action_for(memory: dict[str, Any], severity: str, reasons: list[str]) -> dict[str, Any]:
    if severity == "high" or "negative_feedback_dominates" in reasons:
        return {"action": "quarantine", "memory_id": memory["id"], "reasons": reasons}
    delta = -0.45 if severity == "medium" else -0.2
    return {"action": "lower_strength", "memory_id": memory["id"], "delta": delta, "reasons": reasons}


def _policy_matcher(memory: dict[str, Any]) -> dict[str, Any]:
    terms = []
    for trigger in memory.get("triggers_json") or []:
        text = str(trigger).strip().lower()
        if len(text) >= 2:
            terms.append(text)
    for token in tokenize(str(memory.get("content") or "")):
        if len(token) >= 2:
            terms.append(token)
    return {
        "memory_type": memory.get("memory_type"),
        "scope": memory.get("scope"),
        "domain": memory.get("domain"),
        "category": memory.get("category"),
        "terms": _dedupe(terms)[:8],
    }


def _recall_policy_matcher(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": memory.get("id"),
        "memory_type": memory.get("memory_type"),
        "scope": memory.get("scope"),
        "domain": memory.get("domain"),
        "category": memory.get("category"),
    }


def _memory_matches(memory: dict[str, Any], matcher: dict[str, Any]) -> bool:
    for key in ("memory_type", "scope", "domain", "category", "subcategory"):
        expected = matcher.get(key)
        if expected and memory.get(key) != expected:
            return False
    memory_terms = set(str(item).lower() for item in memory.get("triggers_json") or [])
    memory_terms.update(tokenize(str(memory.get("content") or "")))
    required = set(str(item).lower() for item in matcher.get("terms") or [])
    return not required or len(memory_terms & required) >= min(2, len(required))


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _feedback_sentiment(prompt: str) -> str | None:
    text = prompt.lower()
    negative = (
        r"不对", r"错了", r"误导", r"没用", r"无效", r"不是这个", r"你又误解", r"又错",
        r"\bwrong\b", r"\bnot useful\b", r"\buseless\b", r"\bmisleading\b",
    )
    positive = (r"有用", r"对了", r"正确", r"就是这个", r"\buseful\b", r"\bright\b")
    if any(re.search(pattern, text) for pattern in negative):
        return "negative"
    if any(re.search(pattern, text) for pattern in positive):
        return "positive"
    return None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _candidate_from_memory(memory: dict[str, Any]):
    from .schema import Evidence, MemoryCandidate

    evidence = []
    for item in memory.get("evidence_json") or []:
        if isinstance(item, dict):
            evidence.append(Evidence(source=str(item.get("source", "")), quote=str(item.get("quote", ""))))
    return MemoryCandidate(
        content=str(memory.get("content") or ""),
        memory_type=str(memory.get("memory_type") or "temporary"),
        proposed_action="store",
        confidence=float(memory.get("confidence") or 0),
        importance=float(memory.get("importance") or 0),
        ttl=str(memory.get("ttl") or "session"),
        scope=str(memory.get("scope") or "session"),
        domain=memory.get("domain"),
        category=memory.get("category"),
        subcategory=memory.get("subcategory"),
        abstraction_level=memory.get("abstraction_level"),
        triggers=[str(item) for item in memory.get("triggers_json") or []],
        evidence=evidence,
        reason=str(memory.get("reason") or ""),
    )


def max_severity(left: str, right: str) -> str:
    rank = {"low": 1, "medium": 2, "high": 3}
    return left if rank[left] >= rank[right] else right
