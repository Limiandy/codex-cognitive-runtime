from __future__ import annotations

from typing import Any

from .ledger import Ledger


class LocalCognitiveStore:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def status(self) -> dict[str, Any]:
        stats = self.ledger.stats()
        records = self.ledger.list_cognitive_records(limit=1000)
        edges = self.ledger.list_cognitive_edges(limit=1000)
        return {
            "primary": True,
            "engine": "sqlite-ledger",
            "ledger": stats,
            "cognitive_records": len(records),
            "cognitive_edges": len(edges),
        }

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        memories = self.ledger.list_recallable_memories(limit=500)
        terms = _terms(query)
        scored = []
        for memory in memories:
            content = str(memory.get("content") or "")
            tokens = _terms(content)
            score = len(terms & tokens)
            if score:
                item = dict(memory)
                item["local_score"] = score
                scored.append(item)
        scored.sort(key=lambda item: (item["local_score"], float(item.get("importance") or 0)), reverse=True)
        return scored[: max(1, min(limit, 100))]


def _terms(text: str) -> set[str]:
    import re

    return {item.lower() for item in re.findall(r"[A-Za-z0-9_./-]+|[\u4e00-\u9fff]{2,}", text or "")}
