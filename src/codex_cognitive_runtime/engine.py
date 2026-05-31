from __future__ import annotations

from typing import Any

from .config import Config
from .model_client import CodexMiniClient, ModelError
from .schema import MemoryCandidate
from .security import sanitize_payload, sanitize_model_result


class MemoryEngine:
    def __init__(self, config: Config, model: CodexMiniClient):
        self.config = config
        self.model = model

    def extract(self, event_type: str, payload: dict[str, Any]) -> list[MemoryCandidate]:
        safe_payload = sanitize_payload(payload)
        prompt = (
            "Extract durable memory candidates from this Codex event. "
            "Prefer explicit user preferences, project context, decisions, solved problems, stable facts, "
            "and important task state. Skip routine chatter, transient commands, and anything unsupported by evidence.\n\n"
            f"Event type: {event_type}\n"
            f"Payload: {safe_payload}\n\n"
            "Use concise Chinese content when the evidence is Chinese."
        )
        schema = {
            "candidates": [
                {
                    "content": "string",
                    "type": "user_preference|project_context|experience|fact|task_state|relationship|temporary",
                    "proposed_action": "store|update|merge|skip|forget",
                    "confidence": 0.0,
                    "importance": 0.0,
                    "ttl": "short|session|long",
                    "scope": "global|project|session",
                    "domain": "optional stable domain, e.g. memory_system|software_engineering|life|water_engineering|user_profile|general",
                    "category": "optional category, e.g. preference|architecture|troubleshooting|lesson|fact|workflow|quality",
                    "subcategory": "optional narrow topic, e.g. hook|mcp|logging|recall|review|lighting",
                    "abstraction_level": "concrete|pattern|principle",
                    "triggers": ["short recall trigger phrases"],
                    "evidence": [{"source": "string", "quote": "string"}],
                    "related_memory_ids": ["string"],
                    "reason": "string",
                }
            ]
        }
        try:
            result = self.model.complete_json(prompt, schema)
        except ModelError:
            return []
        result = sanitize_model_result(result)
        raw_candidates = result.get("candidates", [])
        if not isinstance(raw_candidates, list):
            return []
        return [MemoryCandidate.from_dict(item) for item in raw_candidates if isinstance(item, dict)]

    def search_intent(self, user_message: str) -> dict[str, Any]:
        prompt = (
            "Search intent: decide whether Codex should search long-term memory before answering. "
            "Return queries that would retrieve relevant memories. Search for project names, preferences, previous decisions, "
            "or phrases like remember/之前/上次/偏好.\n\n"
            f"User message:\n{user_message}"
        )
        schema = {"should_search": True, "queries": ["string"]}
        try:
            result = self.model.complete_json(prompt, schema)
        except ModelError:
            return {"should_search": False, "queries": []}
        queries = result.get("queries")
        if not isinstance(queries, list):
            queries = []
        return {
            "should_search": bool(result.get("should_search")),
            "queries": [str(q) for q in queries[:3] if str(q).strip()],
        }

    def rank_memories(self, query: str, memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not memories:
            return []
        prompt = (
            "Rank memories for relevance. Return only IDs in best order. Penalize stale, weak, or unrelated memories.\n\n"
            f"Query: {query}\n"
            f"Memories: {memories[:10]}"
        )
        schema = {"ranked_ids": ["string"], "reason": "string"}
        try:
            result = self.model.complete_json(prompt, schema)
        except ModelError:
            return memories
        ranked_ids = [str(item) for item in result.get("ranked_ids", [])]
        by_id = {str(item.get("id")): item for item in memories}
        ranked = [by_id[mid] for mid in ranked_ids if mid in by_id]
        rest = [item for item in memories if item not in ranked]
        return ranked + rest
