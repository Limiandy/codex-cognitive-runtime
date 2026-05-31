from __future__ import annotations

from collections import Counter
from typing import Any


class CognitiveGovernance:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def evaluate(self, apply: bool = False) -> dict[str, Any]:
        records = self.ledger.list_cognitive_records(limit=2000)
        edges = self.ledger.list_cognitive_edges(limit=5000)
        transitions = self.ledger.latest_state_transitions(limit=1000)
        issues = []
        actions = []

        issues.extend(self._conflict_issues(edges))
        issues.extend(self._workflow_issues(records, transitions))
        issues.extend(self._skill_issues(records, edges))
        issues.extend(self._policy_issues(records))

        for issue in issues:
            action = self._action_for(issue)
            if action:
                actions.append(action)

        applied = []
        if apply:
            for action in actions:
                result = self._apply(action)
                if result:
                    applied.append(result)

        return {
            "report": {
                "record_count": len(records),
                "edge_count": len(edges),
                "transition_count": len(transitions),
                "issue_count": len(issues),
                "issues": issues[:100],
                "recommended_actions": actions[:100],
            },
            "applied_actions": applied,
        }

    def _conflict_issues(self, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        issues = []
        for edge in edges:
            if edge.get("relation") != "contradicts":
                continue
            issues.append(
                {
                    "type": "cognitive_conflict",
                    "severity": "high",
                    "source_id": edge.get("source_id"),
                    "target_id": edge.get("target_id"),
                    "reason": "active_cognitive_records_contradict",
                }
            )
        return issues

    def _workflow_issues(self, records: list[dict[str, Any]], transitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest = {}
        for transition in reversed(transitions):
            latest[(transition.get("subject_type"), transition.get("subject_id"))] = transition.get("state")
        issues = []
        for record in records:
            if record.get("layer") != "workflow" or record.get("record_type") != "dynamic_workflow":
                continue
            state = latest.get(("workflow", record.get("id")))
            if state in {"planned", "running"}:
                issues.append(
                    {
                        "type": "stale_workflow",
                        "severity": "medium",
                        "record_id": record.get("id"),
                        "state": state,
                        "reason": "workflow_not_completed",
                    }
                )
        return issues

    def _skill_issues(self, records: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        used = Counter()
        for edge in edges:
            if edge.get("relation") == "uses_skill":
                used[str(edge.get("target_id"))] += 1
        issues = []
        for record in records:
            if record.get("layer") != "skill" or record.get("status") != "active":
                continue
            if float(record.get("strength") or 1.0) <= 0.3:
                issues.append(
                    {
                        "type": "weak_skill",
                        "severity": "medium",
                        "record_id": record.get("id"),
                        "reason": "skill_strength_too_low",
                    }
                )
            elif record.get("record_type") == "execution_strategy" and used[str(record.get("id"))] == 0:
                issues.append(
                    {
                        "type": "unproven_skill",
                        "severity": "low",
                        "record_id": record.get("id"),
                        "reason": "skill_not_used_by_workflows",
                    }
                )
        return issues

    def _policy_issues(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        issues = []
        for record in records:
            if record.get("layer") != "policy" or record.get("status") != "active":
                continue
            metadata = record.get("metadata_json") or {}
            if int(metadata.get("hit_count") or 0) >= 20 and float(record.get("strength") or 1) < 1.5:
                issues.append(
                    {
                        "type": "policy_needs_promotion",
                        "severity": "medium",
                        "record_id": record.get("id"),
                        "reason": "frequent_policy_hits_should_strengthen_policy_layer",
                    }
                )
        return issues

    def _action_for(self, issue: dict[str, Any]) -> dict[str, Any] | None:
        issue_type = issue.get("type")
        if issue_type == "cognitive_conflict":
            return {
                "action": "quarantine_conflicting_record",
                "record_id": issue.get("source_id"),
                "reason": issue.get("reason"),
            }
        if issue_type == "stale_workflow":
            return {
                "action": "mark_workflow_failed",
                "record_id": issue.get("record_id"),
                "reason": issue.get("reason"),
            }
        if issue_type == "weak_skill":
            return {
                "action": "deprecate_skill",
                "record_id": issue.get("record_id"),
                "reason": issue.get("reason"),
            }
        if issue_type == "policy_needs_promotion":
            return {
                "action": "strengthen_policy",
                "record_id": issue.get("record_id"),
                "reason": issue.get("reason"),
            }
        return None

    def _apply(self, action: dict[str, Any]) -> dict[str, Any] | None:
        record_id = str(action.get("record_id") or "")
        if not record_id:
            return None
        if action["action"] in {"quarantine_conflicting_record", "mark_workflow_failed"}:
            self.ledger.set_cognitive_record_status(record_id, "quarantined", {"governance_action": action})
            return action
        if action["action"] == "deprecate_skill":
            self.ledger.set_cognitive_record_status(record_id, "deprecated", {"governance_action": action})
            return action
        if action["action"] == "strengthen_policy":
            self.ledger.adjust_cognitive_record_strength(record_id, 0.25, {"governance_action": action})
            return action
        return None
