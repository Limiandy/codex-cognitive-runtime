from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReasoningPolicy:
    reasoning_depth: str
    tool_strategy: str
    workflow_mode: str
    memory_budget: int
    knowledge_budget: int
    skill_budget: int
    verification_required: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reasoning_depth": self.reasoning_depth,
            "tool_strategy": self.tool_strategy,
            "workflow_mode": self.workflow_mode,
            "memory_budget": self.memory_budget,
            "knowledge_budget": self.knowledge_budget,
            "skill_budget": self.skill_budget,
            "verification_required": self.verification_required,
            "reasons": self.reasons,
        }


class ReasoningPolicyEngine:
    def decide(
        self,
        prompt: str,
        route: dict[str, Any],
        memories: list[dict[str, Any]],
        knowledge: list[dict[str, Any]],
        skills: list[dict[str, Any]],
        injection_pressure: float = 0.0,
        policies: list[dict[str, Any]] | None = None,
    ) -> ReasoningPolicy:
        text = prompt.lower()
        reasons = []
        is_engineering = route.get("domain") in {"software_engineering", "memory_system"} or any(
            term in text for term in ("代码", "实现", "测试", "修复", "工程", "hook", "mcp", "cli", "workflow")
        )
        high_risk = is_engineering and any(term in text for term in ("删除", "迁移", "发布", "权限", "安全", "密钥", "git", "数据库"))
        memory_budget = 6
        knowledge_budget = 5
        skill_budget = 4
        if len(prompt.strip()) < 18 and not any(term in text for term in ("之前", "上次", "经验", "架构", "实现", "测试")):
            memory_budget = 2
            knowledge_budget = 1
            skill_budget = 1
            reasons.append("short_prompt_low_cognitive_need")
        if injection_pressure > 4.0:
            memory_budget = min(memory_budget, 3)
            knowledge_budget = min(knowledge_budget, 2)
            skill_budget = min(skill_budget, 2)
            reasons.append("high_injection_pressure")
        if is_engineering:
            reasons.append("engineering_task")
        if high_risk:
            reasons.append("high_risk_task")
        if skills:
            reasons.append("skill_available")
        if knowledge:
            reasons.append("knowledge_available")
        if policies:
            reasons.append("governance_policy_available")

        verification_required = is_engineering or high_risk
        if high_risk:
            depth = "high"
            tool_strategy = "verify_required"
        elif is_engineering or knowledge or skills:
            depth = "medium"
            tool_strategy = "inspect_first" if is_engineering else "execute_after_plan"
        else:
            depth = "low"
            tool_strategy = "avoid"

        workflow_mode = "dag" if is_engineering or skills or knowledge else "none"
        return ReasoningPolicy(
            reasoning_depth=depth,
            tool_strategy=tool_strategy,
            workflow_mode=workflow_mode,
            memory_budget=memory_budget,
            knowledge_budget=knowledge_budget,
            skill_budget=skill_budget,
            verification_required=verification_required,
            reasons=reasons or ["default_low_risk"],
        )
