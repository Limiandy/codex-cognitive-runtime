from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


MEMORY_TYPES = {
    "user_preference",
    "project_context",
    "experience",
    "fact",
    "task_state",
    "relationship",
    "temporary",
}

SCOPES = {"global", "project", "session"}
TTLS = {"short", "session", "long"}
ACTIONS = {"store", "update", "merge", "skip", "forget"}
STATUSES = {"candidate", "active", "quarantined", "rejected", "superseded", "deprecated", "suppressed", "deleted"}


@dataclass
class Evidence:
    source: str
    quote: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(source=str(data.get("source", "")).strip(), quote=str(data.get("quote", "")).strip())


@dataclass
class MemoryCandidate:
    content: str
    memory_type: str
    proposed_action: str
    confidence: float
    importance: float
    ttl: str
    scope: str = "session"
    domain: str | None = None
    category: str | None = None
    subcategory: str | None = None
    abstraction_level: str | None = None
    triggers: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    related_memory_ids: list[str] = field(default_factory=list)
    reason: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryCandidate":
        evidence = [Evidence.from_dict(item) for item in data.get("evidence", []) if isinstance(item, dict)]
        related = [str(item) for item in data.get("related_memory_ids", []) if item]
        triggers = [str(item).strip() for item in data.get("triggers", []) if str(item).strip()]
        return cls(
            content=str(data.get("content", "")).strip(),
            memory_type=str(data.get("type") or data.get("memory_type") or "temporary").strip(),
            proposed_action=str(data.get("proposed_action") or data.get("action") or "skip").strip(),
            confidence=_float(data.get("confidence"), 0.0),
            importance=_float(data.get("importance"), 0.0),
            ttl=str(data.get("ttl") or "session").strip(),
            scope=str(data.get("scope") or "session").strip(),
            domain=_optional_str(data.get("domain")),
            category=_optional_str(data.get("category")),
            subcategory=_optional_str(data.get("subcategory")),
            abstraction_level=_optional_str(data.get("abstraction_level")),
            triggers=triggers,
            evidence=evidence,
            related_memory_ids=related,
            reason=str(data.get("reason", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "type": self.memory_type,
            "proposed_action": self.proposed_action,
            "confidence": self.confidence,
            "importance": self.importance,
            "ttl": self.ttl,
            "scope": self.scope,
            "domain": self.domain,
            "category": self.category,
            "subcategory": self.subcategory,
            "abstraction_level": self.abstraction_level,
            "triggers": self.triggers,
            "evidence": [e.__dict__ for e in self.evidence],
            "related_memory_ids": self.related_memory_ids,
            "reason": self.reason,
        }


def _float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
