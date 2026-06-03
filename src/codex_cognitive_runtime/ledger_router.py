from __future__ import annotations

from dataclasses import dataclass
from typing import Any


LAYER_PRECEDENCE = {"user": 3, "team": 2, "baseline": 1}


@dataclass(frozen=True)
class LedgerEntry:
    name: str
    ledger: Any
    writable: bool = False


class LayeredLedgerView:
    """Read-through view over user/team/baseline ledgers.

    The user ledger remains the only default write target. This view exists so
    retrieval and cold-start skill selection can see shared baseline records
    without giving those records a writable path.
    """

    def __init__(self, user: Any, *, baseline: Any | None = None, team: Any | None = None):
        self.user = user
        self.baseline = baseline
        self.team = team

    @property
    def entries(self) -> list[LedgerEntry]:
        entries = [LedgerEntry("user", self.user, writable=True)]
        if self.team is not None:
            entries.append(LedgerEntry("team", self.team, writable=False))
        if self.baseline is not None:
            entries.append(LedgerEntry("baseline", self.baseline, writable=False))
        return entries

    def close(self) -> None:
        seen = set()
        for entry in self.entries:
            key = id(entry.ledger)
            if key in seen:
                continue
            seen.add(key)
            entry.ledger.close()

    def list_recallable_memories(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen = set()
        for entry in self.entries:
            for memory in entry.ledger.list_recallable_memories(*args, **kwargs):
                tagged = _tag_record(memory, entry.name)
                key = _memory_key(tagged)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(tagged)
        merged.sort(key=lambda item: (_source_rank(item), float(item.get("strength") or 1), float(item.get("importance") or 0)), reverse=True)
        return merged[: int(kwargs.get("limit") or 200)]

    def list_edges(self, ids: list[str]) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        for entry in self.entries:
            try:
                edges.extend(_tag_record(edge, entry.name) for edge in entry.ledger.list_edges(ids))
            except AttributeError:
                continue
        return edges

    def list_cognitive_records(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for entry in self.entries:
            for record in entry.ledger.list_cognitive_records(*args, **kwargs):
                records.append(_tag_record(record, entry.name))
        return _dedupe_records(records, int(kwargs.get("limit") or 200))

    def get_cognitive_record(self, record_id: str) -> dict[str, Any] | None:
        for entry in self.entries:
            record = entry.ledger.get_cognitive_record(record_id)
            if record:
                return _tag_record(record, entry.name)
        return None

    def get_user_cognitive_record(self, record_id: str) -> dict[str, Any] | None:
        return self.user.get_cognitive_record(record_id)

    def stats(self) -> dict[str, Any]:
        return {
            entry.name: {
                **entry.ledger.stats(),
                "writable": entry.writable,
                "path": str(getattr(entry.ledger, "path", "")),
            }
            for entry in self.entries
        }


def clone_cognitive_record_to_user(user_ledger: Any, source_record: dict[str, Any], *, overlay_reason: str) -> dict[str, Any] | None:
    if not source_record or not source_record.get("id"):
        return None
    existing = user_ledger.get_cognitive_record(str(source_record["id"]))
    if existing:
        return existing
    metadata = dict(source_record.get("metadata_json") or {})
    metadata.update(
        {
            "overlay_source_layer": source_record.get("_ledger_layer") or "baseline",
            "overlay_reason": overlay_reason,
            "overlay_source_id": source_record.get("id"),
        }
    )
    return user_ledger.record_cognitive_record(
        str(source_record.get("layer") or "skill"),
        str(source_record.get("record_type") or ""),
        str(source_record.get("id")),
        str(source_record.get("content") or ""),
        str(source_record.get("status") or "active"),
        str(source_record.get("scope") or "global"),
        domain=source_record.get("domain"),
        category=source_record.get("category"),
        subcategory=source_record.get("subcategory"),
        confidence=float(source_record.get("confidence") or 0.0),
        importance=float(source_record.get("importance") or 0.0),
        strength=float(source_record.get("strength") or 1.0),
        project_key=source_record.get("project_key"),
        session_id=source_record.get("session_id"),
        metadata=metadata,
        source_kind=str(source_record.get("source_kind") or "ledger_overlay"),
    )


def _tag_record(record: dict[str, Any], layer: str) -> dict[str, Any]:
    tagged = dict(record)
    metadata = dict(tagged.get("metadata_json") or {})
    metadata.setdefault("ledger_layer", layer)
    tagged["metadata_json"] = metadata
    tagged["_ledger_layer"] = layer
    return tagged


def _dedupe_records(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get("id") or "")
        if not key:
            continue
        current = best.get(key)
        if current is None or _source_rank(record) > _source_rank(current):
            best[key] = record
    values = list(best.values())
    values.sort(
        key=lambda item: (
            _source_rank(item),
            float(item.get("importance") or 0),
            float(item.get("strength") or 1),
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return values[: max(1, min(limit, 5000))]


def _source_rank(record: dict[str, Any]) -> int:
    return LAYER_PRECEDENCE.get(str(record.get("_ledger_layer") or ""), 0)


def _memory_key(memory: dict[str, Any]) -> str:
    return "|".join(
        [
            str(memory.get("memory_type") or ""),
            str(memory.get("scope") or ""),
            str(memory.get("domain") or ""),
            " ".join(str(memory.get("content") or "").split()).lower(),
        ]
    )
