from __future__ import annotations

from typing import Any

from .durable_skills import durable_skill_basis_summary, relevant_durable_skills
from .recall import MemoryRecall
from .seed_skills import relevant_seed_skills, seed_skill_basis_summary


class CleanMemoryRetriever:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def retrieve(
        self,
        prompt: str,
        cwd: str | None = None,
        session_id: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        candidates = [
            memory
            for memory in self.ledger.list_recallable_memories(cwd=cwd, session_id=session_id, limit=200)
            if _is_clean_memory(memory)
        ]
        edges = self.ledger.list_edges([str(item["id"]) for item in candidates if item.get("id")])
        recalled = MemoryRecall(candidates, edges=edges).recall(prompt, limit=limit)
        memories = _merge_memory_lists(recalled.memories, _stable_preferences(candidates), limit)
        durable_skills = relevant_durable_skills(self.ledger, prompt, cwd=cwd, limit=3)
        seed_skills = relevant_seed_skills(self.ledger, prompt, limit=4)
        return {
            "route": recalled.route,
            "memories": memories,
            "durable_skills": durable_skills,
            "seed_skills": seed_skills,
            "memory_basis_summary": _basis_summary(memories),
            "durable_skill_basis_summary": durable_skill_basis_summary(durable_skills),
            "seed_skill_basis_summary": seed_skill_basis_summary(seed_skills),
        }


def _is_clean_memory(memory: dict[str, Any]) -> bool:
    if memory.get("status") != "active":
        return False
    if float(memory.get("confidence") or 0) < 0.82:
        return False
    review = memory.get("review_json") or {}
    if review.get("status") not in {None, "active"}:
        return False
    if review.get("risk_flags"):
        return False
    return True


def _stable_preferences(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stable = []
    for memory in memories:
        if memory.get("memory_type") not in {"user_preference", "project_context", "experience"}:
            continue
        if memory.get("scope") not in {"global", "project"}:
            continue
        stable.append(memory)
    stable.sort(key=lambda item: (float(item.get("importance") or 0), float(item.get("confidence") or 0)), reverse=True)
    return stable[:5]


def _merge_memory_lists(primary: list[dict[str, Any]], secondary: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged = []
    seen = set()
    for memory in [*primary, *secondary]:
        memory_id = str(memory.get("id") or "")
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        merged.append(memory)
        if len(merged) >= limit:
            break
    return merged


def _basis_summary(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "No clean long-term memories matched this task."
    parts = []
    for memory in memories[:5]:
        label = str(memory.get("memory_type") or "memory")
        content = " ".join(str(memory.get("content") or "").split())[:120]
        parts.append(f"{label}: {content}")
    return " | ".join(parts)
