from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .skill_need import SkillNeedDecision


@dataclass(frozen=True)
class RuntimeSkill:
    name: str
    applies_to: str
    goal: str
    memory_basis_ids: list[str]
    memory_basis_summary: str
    strategy: list[str]
    first_action: dict[str, Any]
    avoid: list[str] = field(default_factory=list)
    confidence: float = 0.0
    intent: str = ""
    domain: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_type": "runtime",
            "name": self.name,
            "applies_to": self.applies_to,
            "goal": self.goal,
            "memory_basis_ids": self.memory_basis_ids,
            "memory_basis_summary": self.memory_basis_summary,
            "strategy": self.strategy,
            "first_action": self.first_action,
            "avoid": self.avoid,
            "confidence": self.confidence,
            "intent": self.intent,
            "domain": self.domain,
        }


class RuntimeSkillSynthesizer:
    def synthesize(self, prompt: str, decision: SkillNeedDecision, memory_basis: dict[str, Any]) -> RuntimeSkill | None:
        if not decision.skill_needed:
            return None
        memories = memory_basis.get("memories") or []
        basis_ids = [str(item.get("id")) for item in memories if item.get("id")]
        basis_summary = str(memory_basis.get("memory_basis_summary") or "")
        if decision.intent == "brand_logo_design":
            return RuntimeSkill(
                name="brand_logo_design_intake",
                applies_to="brand logo and visual identity design requests",
                goal="Use clean long-term preferences to narrow the design brief before any image generation.",
                memory_basis_ids=basis_ids,
                memory_basis_summary=basis_summary,
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
                ],
                confidence=0.86 if memories else 0.72,
                intent=decision.intent,
                domain=decision.domain,
            )
        if decision.domain == "software_engineering":
            return RuntimeSkill(
                name="software_change_guarded_workflow",
                applies_to="bug fixes, code changes, refactors, and implementation tasks",
                goal="Make code changes through inspected context, minimal edits, and explicit verification evidence.",
                memory_basis_ids=basis_ids,
                memory_basis_summary=basis_summary,
                strategy=[
                    "Inspect the relevant repository context before editing.",
                    "Make the smallest focused change that satisfies the task.",
                    "Run the most relevant test, build, or lint command and report the result honestly.",
                ],
                first_action={"type": "inspect_repository"},
                avoid=[
                    "Do not edit before inspecting relevant files.",
                    "Do not claim completion without verification evidence.",
                    "Do not describe failed verification as success.",
                ],
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
        lines.append("Execution:")
        for index, step in enumerate(skill.strategy, 1):
            lines.append(f"{index}. {step}")
        if skill.first_action:
            lines.append("First action: " + _first_action_text(skill.first_action))
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
