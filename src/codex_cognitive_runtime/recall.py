from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .taxonomy import classify, normalize_text, tokenize


@dataclass
class RecallResult:
    context: str
    memories: list[dict[str, Any]]
    route: dict[str, Any]


class MemoryRecall:
    def __init__(self, memories: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None):
        self.memories = memories
        self.edges = edges or []

    def recall(self, prompt: str, limit: int = 6) -> RecallResult:
        route = classify(prompt)
        prompt_tokens = tokenize(prompt)
        scored: list[tuple[float, list[str], dict[str, Any]]] = []
        for memory in self.memories:
            score, reasons = self._score(prompt, prompt_tokens, route, memory)
            if _has_direct_signal(reasons):
                reasons.append("direct_signal")
            if self._passes_gate(score, reasons, route, memory):
                scored.append((score, reasons, memory))
        scored = self._apply_association_boost(scored)
        scored.sort(key=lambda item: item[0], reverse=True)

        selected = []
        seen = set()
        for score, reasons, memory in scored:
            key = _dedupe_key(memory)
            if key in seen:
                continue
            seen.add(key)
            item = dict(memory)
            item["recall_score"] = round(score, 3)
            item["recall_reasons"] = reasons
            selected.append(item)
            if len(selected) >= max(1, limit):
                break

        context = self._format_context(selected)
        return RecallResult(context=context, memories=selected, route=route)

    def _score(
        self,
        prompt: str,
        prompt_tokens: set[str],
        route: dict[str, Any],
        memory: dict[str, Any],
    ) -> tuple[float, list[str]]:
        content = str(memory.get("content") or "")
        memory_route = classify(content, str(memory.get("memory_type") or ""))
        triggers = [str(item).lower() for item in memory.get("triggers_json") or [] if str(item).strip()]
        if not triggers:
            triggers = [str(item).lower() for item in memory_route["triggers"]]
        memory_tokens = tokenize(" ".join([content, *triggers]))
        overlap = prompt_tokens & memory_tokens

        score = 0.0
        reasons = []
        if overlap:
            score += min(len(overlap), 6) * 4.0
            reasons.append("token_overlap")

        trigger_hits = [trigger for trigger in triggers if trigger and trigger in prompt.lower()]
        if trigger_hits:
            score += min(len(trigger_hits), 4) * 6.0
            reasons.append("trigger_hit")

        memory_domain = memory.get("domain") or memory_route["domain"]
        memory_category = memory.get("category") or memory_route["category"]
        memory_subcategory = memory.get("subcategory") or memory_route["subcategory"]

        if memory_domain == route["domain"]:
            score += 9.0
            reasons.append("domain_match")
        if memory_category == route["category"]:
            score += 6.0
            reasons.append("category_match")
        if memory_subcategory == route["subcategory"]:
            score += 9.0
            reasons.append("subcategory_match")

        if _contains_phrase(prompt, content):
            score += 10.0
            reasons.append("phrase_match")

        score += float(memory.get("importance") or 0) * 4.0
        score += float(memory.get("confidence") or 0) * 2.0

        memory_type = memory.get("memory_type")
        if memory_type == "user_preference" and _asks_preference(prompt, route):
            score += 12.0
            reasons.append("preference_requested")
        elif memory_type == "user_preference":
            score -= 4.0
        if memory_type == "task_state":
            score -= 12.0
            reasons.append("task_state_penalty")
        score += max(0.1, min(3.0, float(memory.get("strength") or 1.0))) * 2.5

        return score, reasons

    def _apply_association_boost(
        self,
        scored: list[tuple[float, list[str], dict[str, Any]]],
    ) -> list[tuple[float, list[str], dict[str, Any]]]:
        if not scored or not self.edges:
            return scored
        direct_ids = {
            str(memory.get("id"))
            for _score, reasons, memory in scored
            if memory.get("id") and _has_direct_signal(reasons)
        }
        if not direct_ids:
            return scored
        boost_by_id: dict[str, float] = {}
        for edge in self.edges:
            source_id = str(edge.get("source_id") or "")
            target_id = str(edge.get("target_id") or "")
            if source_id in direct_ids and target_id:
                boost_by_id[target_id] = max(boost_by_id.get(target_id, 0.0), float(edge.get("weight") or 0) * 8.0)
        boosted = []
        for score, reasons, memory in scored:
            boost = boost_by_id.get(str(memory.get("id") or ""), 0.0)
            if boost > 0:
                boosted.append((score + boost, [*reasons, "association_edge"], memory))
            else:
                boosted.append((score, reasons, memory))
        return boosted

    def _passes_gate(
        self,
        score: float,
        reasons: list[str],
        route: dict[str, Any],
        memory: dict[str, Any],
    ) -> bool:
        if memory.get("status") != "active":
            return False
        if "task_state_penalty" in reasons and score < 30:
            return False
        if "phrase_match" in reasons or "trigger_hit" in reasons:
            return score >= 12
        memory_route = classify(str(memory.get("content") or ""), str(memory.get("memory_type") or ""))
        memory_domain = memory.get("domain") or memory_route["domain"]
        if route["domain"] == memory_domain and {"token_overlap", "category_match"} & set(reasons):
            return score >= 15
        if memory.get("memory_type") == "user_preference" and "preference_requested" in reasons:
            return score >= 14
        return score >= 22

    def _format_context(self, memories: list[dict[str, Any]]) -> str:
        return format_memory_context(memories)


def format_memory_context(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""
    lines = ["Codex Cognitive Runtime context:"]
    for item in memories:
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        inferred = classify(content, str(item.get("memory_type") or ""))
        path = "/".join(
            part
            for part in [
                str(item.get("domain") or inferred["domain"]),
                str(item.get("category") or inferred["category"]),
                str(item.get("subcategory") or inferred["subcategory"]),
            ]
            if part
        )
        memory_type = item.get("memory_type") or "memory"
        scope = item.get("scope") or "unknown"
        lines.append(f"- [{path} {memory_type}/{scope}] {content}")
    return "\n".join(lines)


def _dedupe_key(memory: dict[str, Any]) -> str:
    content = normalize_text(str(memory.get("content") or ""))
    return "|".join(
        [
            str(memory.get("memory_type") or ""),
            str(memory.get("scope") or ""),
            str(memory.get("domain") or ""),
            content,
        ]
    )


def _contains_phrase(prompt: str, content: str) -> bool:
    prompt_norm = normalize_text(prompt)
    content_norm = normalize_text(content)
    if not prompt_norm or not content_norm:
        return False
    return prompt_norm in content_norm or content_norm in prompt_norm


def _asks_preference(prompt: str, route: dict[str, Any]) -> bool:
    lowered = prompt.lower()
    return route["category"] == "preference" or any(term in lowered for term in ("偏好", "默认", "希望", "我喜欢"))


def _has_direct_signal(reasons: list[str]) -> bool:
    return bool({"trigger_hit", "phrase_match", "token_overlap"} & set(reasons))
