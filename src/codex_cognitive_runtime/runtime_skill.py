from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .model_client import ModelError
from .security import SECRET_PATTERNS, redact_secrets
from .skill_need import SkillNeedDecision

RUNTIME_SKILL_MODEL_TIMEOUT_SECONDS = 12


@dataclass(frozen=True)
class RuntimeSkill:
    name: str
    applies_to: str
    goal: str
    memory_basis_ids: list[str]
    memory_basis_summary: str
    strategy: list[str]
    first_action: dict[str, Any]
    durable_skill_ids: list[str] = field(default_factory=list)
    durable_skill_basis_summary: str = ""
    seed_skill_ids: list[str] = field(default_factory=list)
    seed_skill_basis_summary: str = ""
    avoid: list[str] = field(default_factory=list)
    confidence: float = 0.0
    intent: str = ""
    domain: str = ""
    task_profile: dict[str, Any] = field(default_factory=dict)
    source_skill_ids: list[str] = field(default_factory=list)
    distilled_from: list[dict[str, Any]] = field(default_factory=list)
    selected_fragments: list[dict[str, Any]] = field(default_factory=list)
    fragment_rule_mappings: list[dict[str, Any]] = field(default_factory=list)
    workflow_required_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_type": "runtime",
            "name": self.name,
            "applies_to": self.applies_to,
            "goal": self.goal,
            "memory_basis_ids": self.memory_basis_ids,
            "memory_basis_summary": self.memory_basis_summary,
            "durable_skill_ids": self.durable_skill_ids,
            "durable_skill_basis_summary": self.durable_skill_basis_summary,
            "seed_skill_ids": self.seed_skill_ids,
            "seed_skill_basis_summary": self.seed_skill_basis_summary,
            "strategy": self.strategy,
            "first_action": self.first_action,
            "avoid": self.avoid,
            "confidence": self.confidence,
            "intent": self.intent,
            "domain": self.domain,
            "task_profile": self.task_profile,
            "source_skill_ids": self.source_skill_ids,
            "distilled_from": self.distilled_from,
            "selected_fragments": self.selected_fragments,
            "fragment_rule_mappings": self.fragment_rule_mappings,
            "workflow_required_steps": self.workflow_required_steps,
        }


class RuntimeSkillSynthesizer:
    def __init__(self, model: Any | None = None):
        self.model = model

    def synthesize(self, prompt: str, decision: SkillNeedDecision, memory_basis: dict[str, Any]) -> RuntimeSkill | None:
        if not decision.skill_needed:
            return None
        memories = memory_basis.get("memories") or []
        durable_skills = memory_basis.get("durable_skills") or []
        seed_skills = memory_basis.get("seed_skills") or []
        basis_ids = [str(item.get("id")) for item in memories if item.get("id")]
        basis_summary = str(memory_basis.get("memory_basis_summary") or "")
        durable_skill_ids = [str(item.get("id")) for item in durable_skills if item.get("id")]
        durable_summary = str(memory_basis.get("durable_skill_basis_summary") or "")
        seed_skill_ids = [str(item.get("id")) for item in seed_skills if item.get("id")]
        seed_summary = str(memory_basis.get("seed_skill_basis_summary") or "")
        task_profile = memory_basis.get("task_profile") if isinstance(memory_basis.get("task_profile"), dict) else {}
        distillation = memory_basis.get("skill_distillation") if isinstance(memory_basis.get("skill_distillation"), dict) else {}
        if self.model is not None:
            modeled = self._model_synthesize(
                prompt,
                decision,
                memories,
                basis_ids,
                basis_summary,
                durable_skills,
                durable_skill_ids,
                durable_summary,
                seed_skills,
                seed_skill_ids,
                seed_summary,
                task_profile,
                distillation,
            )
            if modeled:
                if durable_skill_ids and not modeled.durable_skill_ids:
                    modeled = replace(modeled, durable_skill_ids=durable_skill_ids, durable_skill_basis_summary=durable_summary)
                modeled = _attach_distillation(modeled, task_profile, distillation)
                return modeled
        fallback = self._fallback_synthesize(decision, basis_ids, basis_summary, memories, durable_skill_ids, durable_summary, seed_skill_ids, seed_summary, task_profile, distillation)
        return _attach_distillation(fallback, task_profile, distillation)

    def _fallback_synthesize(
        self,
        decision: SkillNeedDecision,
        basis_ids: list[str],
        basis_summary: str,
        memories: list[dict[str, Any]],
        durable_skill_ids: list[str],
        durable_summary: str,
        seed_skill_ids: list[str],
        seed_summary: str,
        task_profile: dict[str, Any] | None = None,
        distillation: dict[str, Any] | None = None,
    ) -> RuntimeSkill:
        task_profile = task_profile or {}
        distillation = distillation or {}
        distilled_steps = [str(item) for item in distillation.get("workflow_steps") or [] if item]
        distilled_verification = [str(item) for item in distillation.get("verification") or [] if item]
        distilled_avoid = [str(item) for item in distillation.get("avoid") or [] if item]
        if decision.intent == "brand_logo_design":
            return RuntimeSkill(
                name="brand_logo_design_intake",
                applies_to="brand logo and visual identity design requests",
                goal="Use clean long-term preferences to narrow the design brief before any image generation.",
                memory_basis_ids=basis_ids,
                memory_basis_summary=basis_summary,
                durable_skill_ids=durable_skill_ids,
                durable_skill_basis_summary=durable_summary,
                seed_skill_ids=seed_skill_ids,
                seed_skill_basis_summary=seed_summary,
                strategy=[
                    "Do not generate the logo immediately.",
                    "First ask for brand name, industry or product, target audience, logo type, color or style constraints, and forbidden elements.",
                    "After clarification, offer 2-3 visual directions grounded in the memory basis.",
                ],
                first_action={
                    "type": "ask_clarifying_questions",
                    "questions": [
                        "品牌名称是什么？",
                        "面向什么行业或产品？",
                        "目标客户是谁？",
                        "希望文字标、图形标还是组合标？",
                        "有没有颜色、风格或禁忌元素？",
                    ],
                },
                avoid=[
                    "Do not invent organization positioning that is not present in memory basis.",
                    "Do not use complex gradients, noisy symbols, or cartoon-like style unless the user asks for them.",
                    *distilled_avoid,
                ][:5],
                confidence=0.86 if memories else 0.72,
                intent=decision.intent,
                domain=decision.domain,
            )
        if decision.domain == "software_engineering":
            strategy = [
                "Inspect the relevant repository context before editing.",
                *distilled_steps,
                *distilled_verification,
                "Report verification evidence honestly before claiming completion.",
            ]
            return RuntimeSkill(
                name=_profile_runtime_skill_name(task_profile),
                applies_to=_profile_applies_to(task_profile),
                goal="Make code changes through inspected context, minimal edits, and explicit verification evidence.",
                memory_basis_ids=basis_ids,
                memory_basis_summary=basis_summary,
                durable_skill_ids=durable_skill_ids,
                durable_skill_basis_summary=durable_summary,
                seed_skill_ids=seed_skill_ids,
                seed_skill_basis_summary=seed_summary,
                strategy=_dedupe(strategy)[:6],
                first_action={"type": "inspect_repository"},
                avoid=[
                    "Do not edit before inspecting relevant files.",
                    "Do not claim completion without verification evidence.",
                    "Do not describe failed verification as success.",
                    *distilled_avoid,
                ][:5],
                confidence=0.84,
                intent=decision.intent,
                domain=decision.domain,
            )
        return RuntimeSkill(
            name="memory_grounded_task_strategy",
            applies_to="multi-step task requiring remembered preferences or project context",
            goal="Use clean long-term memory to choose a task-specific strategy before acting.",
            memory_basis_ids=basis_ids,
            memory_basis_summary=basis_summary,
            durable_skill_ids=durable_skill_ids,
            durable_skill_basis_summary=durable_summary,
            seed_skill_ids=seed_skill_ids,
            seed_skill_basis_summary=seed_summary,
            strategy=[
                "Use only the memory basis listed below; do not invent missing user or organization facts.",
                "Ask for clarification when required information is missing.",
                "Keep the first response aligned with the task goal and known preferences.",
            ],
            first_action={"type": "proceed_or_clarify"},
            avoid=["Do not overfit unrelated memories.", "Do not expose raw memory internals."],
            confidence=0.78 if memories else 0.62,
            intent=decision.intent,
            domain=decision.domain,
        )

    def _model_synthesize(
        self,
        prompt: str,
        decision: SkillNeedDecision,
        memories: list[dict[str, Any]],
        basis_ids: list[str],
        basis_summary: str,
        durable_skills: list[dict[str, Any]],
        durable_skill_ids: list[str],
        durable_summary: str,
        seed_skills: list[dict[str, Any]],
        seed_skill_ids: list[str],
        seed_summary: str,
        task_profile: dict[str, Any],
        distillation: dict[str, Any],
    ) -> RuntimeSkill | None:
        prompt_text = (
            "Generate a Runtime Skill for the current Codex request. "
            "A Runtime Skill is temporary, task-specific guidance for this turn. "
            "Use only the supplied clean memory basis, durable skills, and seed skills. Do not invent user preferences, organization facts, or project constraints. "
            "Use seed and dynamic skills as source material only; do not copy full skill/persona text. "
            "Return one distilled runtime skill for this request, not a concatenation of multiple skills. "
            "Priority: current user request, clean memory, active durable skills, then seed skills as general fallback. "
            "If key information is missing, make first_action ask clarifying questions. "
            "Return concise JSON only.\n\n"
            f"User request:\n{redact_secrets(prompt)[:1200]}\n\n"
            f"Skill need decision:\n{_skill_need_decision_for_model(decision)}\n\n"
            f"Task profile:\n{task_profile}\n\n"
            f"Allowed memory basis:\n{_memory_basis_for_model(memories)}\n\n"
            f"Allowed durable skills:\n{_durable_skills_for_model(durable_skills)}\n\n"
            f"Distilled skill material:\n{_distillation_for_model(distillation)}\n\n"
            f"Allowed seed skills:\n{_seed_skills_for_model(seed_skills)}"
        )
        schema = {
            "name": "short_snake_case_name",
            "applies_to": "what current tasks this skill applies to",
            "goal": "one sentence",
            "memory_basis_ids": ["ids from allowed memory basis only"],
            "durable_skill_ids": ["ids from allowed durable skills only"],
            "seed_skill_ids": ["ids from allowed seed skills only"],
            "strategy": ["3-5 concise execution steps"],
            "first_action": {"type": "ask_clarifying_questions|inspect_repository|proceed_or_clarify", "questions": ["optional"]},
            "workflow_required_steps": ["inspect_repository|execute_change|backend_test|frontend_typecheck|browser_verify|execute_and_verify"],
            "avoid": ["2-5 concise anti-patterns"],
            "confidence": 0.0,
        }
        try:
            result = self.model.complete_json(prompt_text, schema, timeout_seconds=RUNTIME_SKILL_MODEL_TIMEOUT_SECONDS)
        except TypeError:
            try:
                result = self.model.complete_json(prompt_text, schema)
            except (ModelError, ValueError, TypeError):
                return None
        except (ModelError, ValueError, TypeError):
            return None
        if not isinstance(result, dict):
            return None
        skill = _skill_from_model(result, decision, basis_ids, basis_summary, durable_skill_ids, durable_summary, seed_skill_ids, seed_summary)
        return _attach_distillation(skill, task_profile, distillation) if skill else None


class RuntimeSkillReviewer:
    def review(self, skill: RuntimeSkill | None, decision: SkillNeedDecision, memory_basis: dict[str, Any]) -> dict[str, Any]:
        if not skill:
            return {"status": "dropped", "reasons": ["missing_skill"], "risk_flags": [], "skill": None}
        reasons = []
        risk_flags = []
        allowed_memory_ids = {str(item.get("id")) for item in memory_basis.get("memories") or [] if item.get("id")}
        allowed_durable_ids = {str(item.get("id")) for item in memory_basis.get("durable_skills") or [] if item.get("id")}
        allowed_seed_ids = {str(item.get("id")) for item in memory_basis.get("seed_skills") or [] if item.get("id")}
        memory_ids = [item for item in skill.memory_basis_ids if item in allowed_memory_ids]
        durable_ids = [item for item in skill.durable_skill_ids if item in allowed_durable_ids]
        seed_ids = [item for item in skill.seed_skill_ids if item in allowed_seed_ids]
        if len(memory_ids) != len(skill.memory_basis_ids):
            reasons.append("filtered_unknown_memory_basis")
        if len(durable_ids) != len(skill.durable_skill_ids):
            reasons.append("filtered_unknown_durable_skill")
        if len(seed_ids) != len(skill.seed_skill_ids):
            reasons.append("filtered_unknown_seed_skill")
        if seed_ids and _basis_conflict(skill.memory_basis_summary + " " + skill.durable_skill_basis_summary, skill.seed_skill_basis_summary):
            seed_ids = []
            reasons.append("seed_skill_conflicts_with_higher_priority_basis")
        if skill.confidence < 0.55:
            return {"status": "dropped", "reasons": [*reasons, "low_confidence"], "risk_flags": risk_flags, "skill": None}
        if _has_secret_like_text(skill):
            return {"status": "dropped", "reasons": [*reasons, "secret_like_runtime_skill"], "risk_flags": ["secret_like_runtime_skill"], "skill": None}
        strategy = [_clean_text(item, 220) for item in skill.strategy if _clean_text(item, 220)][:5]
        avoid = [_clean_text(item, 180) for item in skill.avoid if _clean_text(item, 180)][:5]
        if len(strategy) < 2:
            return {"status": "dropped", "reasons": [*reasons, "insufficient_strategy"], "risk_flags": risk_flags, "skill": None}
        first_action = dict(skill.first_action or {})
        goal = skill.goal
        status = "approved"
        if decision.requires_clarification and first_action.get("type") != "ask_clarifying_questions":
            first_action = {"type": "ask_clarifying_questions", "questions": _default_questions(decision)}
            reasons.append("first_action_corrected_for_clarification")
            status = "fallback"
        if not memory_ids and _claims_user_or_org_facts(skill):
            goal = "Clarify missing task facts before applying any user-specific preference or organization assumption."
            strategy = [
                "Ask clarifying questions before assuming user preferences or organization facts.",
                "Use seed skills only as general guidance until user-specific facts are provided.",
                "Proceed only after the missing task facts are clear.",
            ]
            first_action = {"type": "ask_clarifying_questions", "questions": _default_questions(decision)}
            avoid = ["Do not claim user preferences or organization positioning without memory basis."]
            reasons.append("removed_unbacked_user_or_org_claims")
            status = "fallback"
        reviewed = replace(
            skill,
            goal=goal,
            memory_basis_ids=memory_ids,
            durable_skill_ids=durable_ids,
            seed_skill_ids=seed_ids,
            strategy=strategy,
            first_action=first_action,
            avoid=avoid,
            confidence=max(0.0, min(1.0, skill.confidence)),
        )
        return {
            "status": status,
            "reasons": reasons or ["passed_runtime_skill_review"],
            "risk_flags": risk_flags,
            "skill": reviewed,
            "basis_precedence": "memory_over_durable_over_seed",
        }


class RuntimeSkillInjector:
    def format(self, skill: RuntimeSkill | None) -> str:
        if not skill:
            return ""
        lines = [
            f"Runtime Skill: {skill.name}",
            "Use this skill for the current request.",
            f"Applies to: {skill.applies_to}",
            f"Goal: {skill.goal}",
            "Memory basis:",
        ]
        lines.append("- " + skill.memory_basis_summary if skill.memory_basis_ids else "- No clean long-term memory matched; ask before assuming missing facts.")
        if skill.durable_skill_ids:
            lines.append("Durable skill basis:")
            lines.append("- " + skill.durable_skill_basis_summary)
        if skill.seed_skill_ids:
            lines.append("Seed skill basis:")
            lines.append("- " + skill.seed_skill_basis_summary)
        lines.append("Execution:")
        for index, step in enumerate(skill.strategy, 1):
            lines.append(f"{index}. {step}")
        if skill.first_action:
            lines.append("First action: " + _first_action_text(skill.first_action))
        if skill.workflow_required_steps:
            lines.append("Workflow checks:")
            for item in skill.workflow_required_steps[:8]:
                lines.append(f"- {item}")
        if skill.avoid:
            lines.append("Avoid:")
            for item in skill.avoid[:5]:
                lines.append(f"- {item}")
        return "\n".join(lines)


def _first_action_text(action: dict[str, Any]) -> str:
    action_type = str(action.get("type") or "proceed")
    questions = [str(item) for item in action.get("questions") or [] if item]
    if not questions:
        return action_type
    return action_type + " -> " + " | ".join(questions[:6])


def _memory_basis_for_model(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    basis = []
    for memory in memories[:8]:
        basis.append(
            {
                "id": memory.get("id"),
                "type": memory.get("memory_type"),
                "content": str(redact_secrets(memory.get("content") or ""))[:240],
                "confidence": memory.get("confidence"),
                "importance": memory.get("importance"),
            }
        )
    return basis


def _seed_skills_for_model(seed_skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    basis = []
    for skill in seed_skills[:5]:
        metadata = skill.get("metadata_json") or {}
        basis.append(
            {
                "id": skill.get("id"),
                "name": metadata.get("name"),
                "description": metadata.get("description"),
                "category": metadata.get("category"),
                "source_path": metadata.get("source_path"),
            }
        )
    return basis


def _durable_skills_for_model(durable_skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    basis = []
    for skill in durable_skills[:5]:
        metadata = skill.get("metadata_json") or {}
        basis.append(
            {
                "id": skill.get("id"),
                "title": metadata.get("title"),
                "procedure": (metadata.get("procedure") or [])[:4],
                "verification": (metadata.get("verification") or [])[:3],
                "success_count": metadata.get("success_count"),
                "failure_count": metadata.get("failure_count"),
            }
        )
    return basis


def _skill_need_decision_for_model(decision: SkillNeedDecision) -> dict[str, Any]:
    return {
        "skill_needed": decision.skill_needed,
        "mode": decision.mode,
        "intent": decision.intent,
        "domain": decision.domain,
        "complexity": decision.complexity,
        "requires_memory": decision.requires_memory,
        "requires_clarification": decision.requires_clarification,
        "reason": decision.reason,
    }


def _skill_from_model(
    result: dict[str, Any],
    decision: SkillNeedDecision,
    allowed_basis_ids: list[str],
    basis_summary: str,
    allowed_durable_skill_ids: list[str],
    durable_summary: str,
    allowed_seed_skill_ids: list[str],
    seed_summary: str,
) -> RuntimeSkill | None:
    name = _safe_identifier(str(result.get("name") or "memory_grounded_runtime_skill"))
    applies_to = _clean_text(result.get("applies_to"), 180)
    goal = _clean_text(result.get("goal"), 220)
    strategy = [_clean_text(item, 220) for item in result.get("strategy") or [] if _clean_text(item, 220)]
    avoid = [_clean_text(item, 180) for item in result.get("avoid") or [] if _clean_text(item, 180)]
    first_action = result.get("first_action") if isinstance(result.get("first_action"), dict) else {}
    memory_basis_ids = [str(item) for item in result.get("memory_basis_ids") or [] if str(item) in set(allowed_basis_ids)]
    durable_skill_ids = [str(item) for item in result.get("durable_skill_ids") or [] if str(item) in set(allowed_durable_skill_ids)]
    seed_skill_ids = [str(item) for item in result.get("seed_skill_ids") or [] if str(item) in set(allowed_seed_skill_ids)]
    if not applies_to or not goal or len(strategy) < 2:
        return None
    if not first_action.get("type"):
        first_action = {"type": "proceed_or_clarify"}
    first_action = {
        "type": _clean_text(first_action.get("type"), 80) or "proceed_or_clarify",
        "questions": [_clean_text(item, 120) for item in first_action.get("questions") or [] if _clean_text(item, 120)][:6],
    }
    try:
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.72
    return RuntimeSkill(
        name=name,
        applies_to=applies_to,
        goal=goal,
        memory_basis_ids=memory_basis_ids,
        memory_basis_summary=basis_summary,
        durable_skill_ids=durable_skill_ids,
        durable_skill_basis_summary=durable_summary,
        strategy=strategy[:5],
        first_action=first_action,
        seed_skill_ids=seed_skill_ids,
        seed_skill_basis_summary=seed_summary,
        avoid=avoid[:5],
        confidence=max(0.0, min(1.0, confidence)),
        intent=decision.intent,
        domain=decision.domain,
    )


def _attach_distillation(skill: RuntimeSkill | None, task_profile: dict[str, Any], distillation: dict[str, Any]) -> RuntimeSkill | None:
    if not skill:
        return None
    source_skill_ids = [str(item) for item in distillation.get("source_skill_ids") or [] if item]
    workflow_steps = [str(item) for item in distillation.get("workflow_required_steps") or [] if item]
    distilled_from = [dict(item) for item in distillation.get("distilled_from") or [] if isinstance(item, dict)]
    selected_fragments = [dict(item) for item in distillation.get("selected_fragments") or [] if isinstance(item, dict)]
    fragment_rule_mappings = [dict(item) for item in distillation.get("fragment_rule_mappings") or [] if isinstance(item, dict)]
    return replace(
        skill,
        task_profile=dict(task_profile or {}),
        source_skill_ids=source_skill_ids,
        distilled_from=distilled_from,
        selected_fragments=selected_fragments,
        fragment_rule_mappings=fragment_rule_mappings,
        workflow_required_steps=workflow_steps,
    )


def _distillation_for_model(distillation: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_skill_ids": distillation.get("source_skill_ids") or [],
        "principles": distillation.get("principles") or [],
        "workflow_steps": distillation.get("workflow_steps") or [],
        "verification": distillation.get("verification") or [],
        "avoid": distillation.get("avoid") or [],
        "selected_fragments": distillation.get("selected_fragments") or [],
        "fragment_rule_mappings": distillation.get("fragment_rule_mappings") or [],
        "workflow_required_steps": distillation.get("workflow_required_steps") or [],
    }


def _profile_runtime_skill_name(task_profile: dict[str, Any]) -> str:
    task_type = str(task_profile.get("task_type") or "")
    if task_type == "fullstack_integration_change":
        return "fullstack_integration_change_strategy"
    if task_type == "frontend_change":
        return "frontend_change_strategy"
    if task_type == "backend_api_change":
        return "backend_api_change_strategy"
    return "software_change_guarded_workflow"


def _profile_applies_to(task_profile: dict[str, Any]) -> str:
    surfaces = ", ".join(str(item) for item in task_profile.get("surfaces") or [])
    return f"software engineering tasks touching {surfaces or 'repository'} surfaces"


def _dedupe(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        clean = " ".join(str(value).split())
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
    return result


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(redact_secrets(value or "")).split())[:limit]


def _safe_identifier(value: str) -> str:
    text = "".join(ch if ch.isalnum() else "_" for ch in value.strip().lower())
    text = "_".join(part for part in text.split("_") if part)
    return (text or "runtime_skill")[:80]


def _has_secret_like_text(skill: RuntimeSkill) -> bool:
    text = " ".join(
        [
            skill.name,
            skill.applies_to,
            skill.goal,
            skill.memory_basis_summary,
            skill.durable_skill_basis_summary,
            skill.seed_skill_basis_summary,
            *skill.strategy,
            *skill.avoid,
            str(skill.first_action),
        ]
    )
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _claims_user_or_org_facts(skill: RuntimeSkill) -> bool:
    text = " ".join([skill.goal, skill.memory_basis_summary, *skill.strategy, *skill.avoid]).lower()
    signals = (
        "根据你的偏好",
        "你的偏好",
        "你的组织",
        "你的公司",
        "your preference",
        "your preferences",
        "according to your preferences",
        "your organization",
        "your company",
    )
    return any(signal in text for signal in signals)


def _basis_conflict(higher_priority_text: str, seed_text: str) -> bool:
    high = higher_priority_text.lower()
    seed = seed_text.lower()
    conflict_pairs = (
        (("不喜欢复杂渐变", "避免复杂渐变", "no gradient", "avoid gradient"), ("gradient", "渐变")),
        (("不要问太多", "少问问题", "avoid too many questions"), ("ask many", "many questions", "问很多", "大量问题")),
        (("极简", "minimal", "克制"), ("complex", "复杂", "noisy", "繁复")),
    )
    return any(any(item in high for item in negatives) and any(item in seed for item in positives) for negatives, positives in conflict_pairs)


def _default_questions(decision: SkillNeedDecision) -> list[str]:
    if decision.intent == "brand_logo_design":
        return ["品牌名称是什么？", "面向什么行业或产品？", "目标客户是谁？", "有没有颜色、风格或禁忌元素？"]
    if decision.domain == "software_engineering":
        return ["要解决的具体错误或目标是什么？", "期望我优先运行哪类验证命令？"]
    return ["当前任务的目标是什么？", "有哪些必须遵守的偏好、约束或禁忌？"]
