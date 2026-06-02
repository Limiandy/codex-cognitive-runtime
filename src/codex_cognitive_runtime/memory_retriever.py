from __future__ import annotations

from typing import Any

from .durable_skills import durable_skill_basis_summary, relevant_durable_skills
from .recall import MemoryRecall
from .seed_calibration import seed_scoring_adjustment
from .seed_skills import relevant_seed_skills, seed_skill_basis_summary
from .skill_distillation import distill_skill_basis


class CleanMemoryRetriever:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def retrieve(
        self,
        prompt: str,
        cwd: str | None = None,
        session_id: str | None = None,
        limit: int = 8,
        task_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidates = [
            memory
            for memory in self.ledger.list_recallable_memories(cwd=cwd, session_id=session_id, limit=200)
            if _is_clean_memory(memory)
        ]
        edges = self.ledger.list_edges([str(item["id"]) for item in candidates if item.get("id")])
        recalled = MemoryRecall(candidates, edges=edges).recall(prompt, limit=limit)
        memories = _merge_memory_lists(recalled.memories, _stable_preferences(candidates), limit)
        durable_skills = relevant_durable_skills(self.ledger, prompt, cwd=cwd, limit=3, task_profile=task_profile)
        seed_skills, seed_skill_scores = _rank_seed_skills_for_task(
            relevant_seed_skills(self.ledger, prompt, limit=12, task_profile=task_profile),
            prompt,
            task_profile or {},
            limit=4,
        )
        distillation = distill_skill_basis(seed_skills, durable_skills, task_profile or {}, limit=3)
        return {
            "route": recalled.route,
            "memories": memories,
            "durable_skills": durable_skills,
            "seed_skills": seed_skills,
            "seed_skill_selection_scores": seed_skill_scores,
            "task_profile": task_profile or {},
            "skill_distillation": distillation,
            "memory_basis_summary": _basis_summary(memories),
            "durable_skill_basis_summary": durable_skill_basis_summary(durable_skills),
            "seed_skill_basis_summary": distillation.get("summary") or seed_skill_basis_summary(seed_skills),
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
        if memory.get("memory_type") not in {"user_preference", "project_context"}:
            continue
        if memory.get("category") == "collaboration_rules":
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
        key = _memory_dedupe_key(memory)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(memory)
        if len(merged) >= limit:
            break
    return merged


def _memory_dedupe_key(memory: dict[str, Any]) -> str:
    content = " ".join(str(memory.get("content") or "").split()).lower()
    return "|".join(
        [
            str(memory.get("memory_type") or ""),
            str(memory.get("scope") or ""),
            str(memory.get("domain") or ""),
            content,
        ]
    )


def _basis_summary(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "No clean long-term memories matched this task."
    parts = []
    for memory in memories[:5]:
        label = str(memory.get("memory_type") or "memory")
        content = " ".join(str(memory.get("content") or "").split())[:120]
        parts.append(f"{label}: {content}")
    return " | ".join(parts)


def _rank_seed_skills_for_task(seed_skills: list[dict[str, Any]], prompt: str, task_profile: dict[str, Any], limit: int = 4) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not seed_skills:
        return [], []
    target = _target_terms(prompt, task_profile)
    ranked = []
    for index, skill in enumerate(seed_skills):
        haystack = _seed_haystack(skill)
        metadata = skill.get("metadata_json") or {}
        if _seed_has_internal_pollution(haystack):
            ranked.append(
                (
                    -999,
                    -index,
                    skill,
                    {
                        "id": str(skill.get("id") or ""),
                        "name": str(metadata.get("name") or ""),
                        "score": -999,
                        "selected": False,
                        "reason": "excluded_internal_runtime_fragment",
                    },
                )
            )
            continue
        base_score = _seed_compatibility_score(haystack, target, metadata)
        calibration = seed_scoring_adjustment(metadata, task_profile=task_profile, domain=_profile_domain(task_profile))
        calibration_delta = float(calibration.get("score_delta") or 0.0) if calibration.get("applied") else 0.0
        score = base_score + calibration_delta
        ranked.append(
            (
                score,
                -index,
                skill,
                {
                    "id": str(skill.get("id") or ""),
                    "name": str(metadata.get("name") or ""),
                    "score": score,
                    "base_score": base_score,
                    "calibration": calibration,
                    "selected": False,
                    "target_surfaces": sorted(target.get("surfaces") or []),
                    "target_domains": sorted(target.get("domains") or []),
                    "profile_domain": _profile_domain(task_profile),
                },
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected_ids = {str(item[2].get("id") or "") for item in ranked[:limit] if item[0] > -999}
    scores = []
    selected = []
    for rank, item in enumerate(ranked, start=1):
        score_record = dict(item[3])
        score_record["rank"] = rank
        score_record["selected"] = score_record["id"] in selected_ids
        scores.append(score_record)
        if score_record["selected"]:
            selected.append(item[2])
        if len(selected) >= limit:
            break
    return selected, scores


def _target_terms(prompt: str, task_profile: dict[str, Any]) -> dict[str, set[str]]:
    text = " ".join([str(prompt or ""), str((task_profile.get("evidence") or {}).get("validated_task") or "")]).lower()
    surfaces = {str(item).lower() for item in task_profile.get("surfaces") or []}
    domains = set()
    if any(term in text for term in ("brand", "logo", "品牌", "标志", "视觉识别")):
        domains.add("brand")
        surfaces.add("design")
    if any(term in text for term in ("wechat", "mini program", "小程序", "微信", "wxml", "wxss")):
        domains.add("wechat")
        surfaces.add("frontend")
    if _has_positive_domain_signal(text, ("roblox", "unity", "unreal", "godot", "game", "游戏")):
        domains.add("game")
    if _has_positive_domain_signal(text, ("sales", "marketing", "营销", "销售", "campaign")):
        domains.add("growth")
    return {"surfaces": surfaces, "domains": domains}


def _profile_domain(task_profile: dict[str, Any]) -> str:
    return str(task_profile.get("domain") or task_profile.get("task_domain") or "")


def _seed_haystack(skill: dict[str, Any]) -> str:
    metadata = skill.get("metadata_json") or {}
    return " ".join(
        [
            str(skill.get("id") or ""),
            str(skill.get("content") or "")[:2400],
            str(metadata.get("name") or ""),
            str(metadata.get("description") or ""),
            str(metadata.get("category") or ""),
        ]
    ).lower()


def _seed_compatibility_score(haystack: str, target: dict[str, set[str]], metadata: dict[str, Any] | None = None) -> int:
    metadata = metadata or {}
    surfaces = target.get("surfaces") or set()
    domains = target.get("domains") or set()
    score = 0
    surface_hints = {
        "design": ("brand", "logo", "visual identity", "design", "designer", "ui", "ux"),
        "frontend": ("frontend", "ui", "ux", "interface", "vue", "react", "css", "wxml", "wxss", "mini program", "wechat", "小程序"),
        "backend": ("backend", "api", "server", "database", "architect"),
        "testing": ("testing", "qa", "verification", "tester", "test"),
        "governance": ("workflow", "runtime", "governance", "reviewer", "architect"),
        "privacy": ("security", "privacy", "threat", "risk"),
    }
    domain_hints = {
        "brand": ("brand", "logo", "visual identity"),
        "game": ("roblox", "unity", "unreal", "godot", "game"),
        "growth": ("sales", "marketing", "campaign", "增长", "营销", "销售"),
    }
    for surface in surfaces:
        if any(term in haystack for term in surface_hints.get(surface, ())):
            score += 4
    for domain in domains:
        if domain == "wechat":
            if _matches_wechat_mini_program(haystack):
                score += 10
            elif "wechat" in haystack or "微信" in haystack:
                score += 2
            elif any(term in haystack for term in ("mini program", "小程序", "wxml", "wxss")):
                score -= 2
            continue
        if any(term in haystack for term in domain_hints.get(domain, ())):
            score += 5
    if domains and not any(any(term in haystack for term in hints) for hints in domain_hints.values() if hints):
        score -= 1
    metadata_text = " ".join(str(metadata.get(key) or "") for key in ("name", "description", "category")).lower()
    if "growth" not in domains and any(term in metadata_text for term in ("sales", "marketing", "private domain", "livestream", "official account", "营销", "销售", "公众号", "私域", "直播")):
        score -= 14
    if "game" not in domains and any(term in metadata_text for term in ("roblox", "unity", "unreal", "godot", "game")):
        score -= 8
    if "xr" not in domains and any(term in metadata_text for term in ("xr", "spatial", "visionos")):
        score -= 6
    if "feishu" not in domains and "feishu" in metadata_text:
        score -= 8
    if "orchestration" not in domains and any(term in metadata_text for term in ("orchestrator", "autonomous optimization", "optimization architect")):
        score -= 6
    if "wechat" not in domains and any(term in metadata_text for term in ("wechat mini program", "mini program", "小程序", "wxml", "wxss")):
        score -= 8
    if not domains and any(term in metadata_text for term in ("feishu", "roblox", "xr", "spatial")):
        score -= 2
    if "frontend" in surfaces and any(term in haystack for term in ("backend", "database", "server")):
        score -= 1
    return score


def _matches_wechat_mini_program(haystack: str) -> bool:
    return (
        "wechat mini program" in haystack
        or "微信小程序" in haystack
        or "小程序" in haystack
        or (("wechat" in haystack or "微信" in haystack) and any(term in haystack for term in ("mini program", "wxml", "wxss")))
    )


def _has_positive_domain_signal(text: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            window = text[max(0, index - 18) : index + len(term) + 18]
            if not _is_negated_domain_mention(window):
                return True
            start = index + len(term)
    return False


def _is_negated_domain_mention(window: str) -> bool:
    return any(
        signal in window
        for signal in (
            "不要",
            "不能",
            "不得",
            "避免",
            "排除",
            "除非",
            "误选",
            "错注入",
            "not ",
            "don't",
            "do not",
            "unless",
            "exclude",
            "avoid",
        )
    )


def _seed_has_internal_pollution(haystack: str) -> bool:
    return any(term in haystack for term in ("runtime skill:", "runtime control:", "workflow checks:", "source guidance:"))
