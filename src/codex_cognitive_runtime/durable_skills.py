from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .ledger import project_key_for_cwd
from .taxonomy import tokenize


ACTIVE_DURABLE_STATUSES = {"active"}
INACTIVE_DURABLE_STATUSES = {"candidate", "rejected", "deprecated", "suppressed", "deleted"}


class DurableSkillManager:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        records = self.ledger.list_cognitive_records(layer="skill", status=status, limit=max(limit, 200) if status else 1000)
        skills = [record for record in records if record.get("record_type") == "dynamic_skill"]
        return skills[:limit]

    def get(self, skill_id: str) -> dict[str, Any] | None:
        record = self.ledger.get_cognitive_record(skill_id)
        if not record or record.get("record_type") != "dynamic_skill":
            return None
        return record

    def promote(self, skill_id: str, note: str = "") -> dict[str, Any] | None:
        return self._set_status(skill_id, "active", note=note, source="manual_promote", extra={"review_required": False, "last_promoted_at": _utc_now()})

    def reject(self, skill_id: str, note: str = "") -> dict[str, Any] | None:
        return self._set_status(skill_id, "rejected", note=note, source="manual_reject")

    def deprecate(self, skill_id: str, note: str = "") -> dict[str, Any] | None:
        return self._set_status(skill_id, "deprecated", note=note, source="manual_deprecate")

    def suppress(self, skill_id: str, reason: str = "") -> dict[str, Any] | None:
        return self._set_status(skill_id, "suppressed", note=reason, source="runtime_suppress", extra={"suppressed_reason": reason})

    def stats(self) -> dict[str, Any]:
        skills = self.list(status=None, limit=1000)
        by_status: dict[str, int] = {status: 0 for status in ("candidate", "active", "suppressed", "deprecated", "rejected")}
        needs_review = []
        for skill in skills:
            status = str(skill.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            metadata = skill.get("metadata_json") or {}
            failure_count = int(metadata.get("failure_count") or 0)
            success_count = int(metadata.get("success_count") or 0)
            if metadata.get("review_required") or (failure_count >= 3 and failure_count > success_count):
                needs_review.append(skill)
        return {
            "count": len(skills),
            "by_status": by_status,
            "top_by_reuse": _top(skills, "reuse_count"),
            "top_by_success": _top(skills, "success_count"),
            "top_by_failure": _top(skills, "failure_count"),
            "needs_review": needs_review[:20],
            "recent_candidates": _recent([skill for skill in skills if skill.get("status") == "candidate"]),
            "recent_active": _recent([skill for skill in skills if skill.get("status") == "active"]),
        }

    def _set_status(self, skill_id: str, status: str, note: str, source: str, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        record = self.get(skill_id)
        if not record:
            return None
        now = _utc_now()
        patch = {
            "review_note": note,
            "reviewed_at": now,
            "last_reviewed_at": now,
            "review_source": source,
            "last_status_change_at": now,
        }
        current = record.get("metadata_json") or {}
        patch["skill_version"] = int(current.get("skill_version") or current.get("version") or 1) + (1 if status == "active" else 0)
        patch.update(extra or {})
        return self.ledger.set_cognitive_record_status(skill_id, status, patch)


def relevant_durable_skills(ledger: Any, prompt: str, cwd: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
    tokens = set(tokenize(prompt))
    if not tokens:
        return []
    project_key = project_key_for_cwd(cwd) if cwd else None
    candidates = []
    for record in ledger.list_cognitive_records(layer="skill", status="active", limit=1000):
        if not is_durable_skill_eligible(record, project_key=project_key):
            continue
        metadata = record.get("metadata_json") or {}
        haystack = " ".join(
            [
                str(metadata.get("title") or ""),
                " ".join(str(item) for item in metadata.get("trigger") or []),
                " ".join(str(item) for item in metadata.get("procedure") or []),
                " ".join(str(item) for item in metadata.get("verification") or []),
                str(record.get("content") or "")[:2000],
            ]
        )
        overlap = len(tokens.intersection(set(tokenize(haystack))))
        if overlap <= 0:
            continue
        success_count = int(metadata.get("success_count") or 0)
        failure_count = int(metadata.get("failure_count") or 0)
        feedback_score = success_count - (failure_count * 1.5)
        candidates.append((overlap, feedback_score, float(record.get("strength") or 1), float(record.get("importance") or 0), record))
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    return [item[4] for item in candidates[:limit]]


def is_durable_skill_eligible(record: dict[str, Any], project_key: str | None = None) -> bool:
    if record.get("status") != "active" or record.get("record_type") != "dynamic_skill":
        return False
    metadata = record.get("metadata_json") or {}
    if metadata.get("trust_state") in {"suppressed", "disabled"}:
        return False
    if int(metadata.get("failure_count") or 0) >= 3 and int(metadata.get("failure_count") or 0) > int(metadata.get("success_count") or 0):
        return False
    if project_key and record.get("project_key") not in {None, project_key}:
        return False
    return True


def durable_skill_basis_summary(skills: list[dict[str, Any]]) -> str:
    if not skills:
        return "No active durable skills matched this task."
    parts = []
    for skill in skills[:3]:
        metadata = skill.get("metadata_json") or {}
        title = metadata.get("title") or skill.get("content") or "dynamic skill"
        procedure = " ".join(str(item) for item in (metadata.get("procedure") or [])[:2])
        parts.append(f"{title}: {procedure}"[:220])
    return " | ".join(parts)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _top(skills: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    return sorted(skills, key=lambda item: int((item.get("metadata_json") or {}).get(field) or 0), reverse=True)[:10]


def _recent(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(skills, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[:10]
