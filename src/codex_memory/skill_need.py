from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillNeedDecision:
    skill_needed: bool
    mode: str
    intent: str
    domain: str
    complexity: str
    requires_memory: bool
    requires_clarification: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_needed": self.skill_needed,
            "mode": self.mode,
            "intent": self.intent,
            "domain": self.domain,
            "complexity": self.complexity,
            "requires_memory": self.requires_memory,
            "requires_clarification": self.requires_clarification,
            "reason": self.reason,
        }


class SkillNeedClassifier:
    def classify(self, prompt: str) -> SkillNeedDecision:
        text = " ".join(str(prompt or "").split())
        lowered = text.lower()
        if not text:
            return _direct("empty", "empty prompt")
        if lowered in _AMBIGUOUS_SHORT_SIGNALS:
            return _direct("ambiguous_short_prompt", "short ambiguous prompt should not trigger a runtime skill")
        if _matches(lowered, _DIRECT_FACT_SIGNALS) and not _matches(lowered, _COMPLEX_TASK_SIGNALS):
            return _direct("simple_query", "simple realtime/factual request")
        if _matches(lowered, _BRAND_DESIGN_SIGNALS):
            return SkillNeedDecision(
                True,
                "generate_runtime_skill",
                "brand_logo_design",
                "brand_design",
                "medium",
                True,
                True,
                "creative brand task requiring user and organization preferences",
            )
        if _matches(lowered, _ENGINEERING_SIGNALS):
            return SkillNeedDecision(
                True,
                "generate_runtime_skill",
                "software_engineering_change",
                "software_engineering",
                "medium",
                True,
                False,
                "engineering task benefits from inspected context, verification strategy, and workflow guardrails",
            )
        if _matches(lowered, _COMPLEX_TASK_SIGNALS):
            return SkillNeedDecision(
                True,
                "generate_runtime_skill",
                "complex_task",
                "general",
                "medium",
                True,
                False,
                "multi-step task benefits from memory-grounded execution strategy",
            )
        return _direct("direct_answer", "request is simple enough to answer without a runtime skill")


def _direct(intent: str, reason: str) -> SkillNeedDecision:
    return SkillNeedDecision(False, "direct_answer", intent, "general", "low", False, False, reason)


def _matches(text: str, signals: tuple[str, ...]) -> bool:
    return any(signal in text for signal in signals)


_DIRECT_FACT_SIGNALS = (
    "天气",
    "几点",
    "现在时间",
    "汇率",
    "翻译",
    "解释这个词",
    "什么意思",
    "weather",
    "time",
    "exchange rate",
    "translate",
    "define",
)

_AMBIGUOUS_SHORT_SIGNALS = {
    "测试",
    "修复",
    "代码",
    "test",
    "fix",
    "debug",
    "lint",
}

_BRAND_DESIGN_SIGNALS = (
    "logo",
    "标志",
    "品牌",
    "视觉识别",
    "brand identity",
    "brand logo",
)

_ENGINEERING_SIGNALS = (
    "修复",
    "实现",
    "重构",
    "代码",
    "测试",
    "bug",
    "报错",
    "fix",
    "implement",
    "debug",
    "refactor",
    "test",
    "lint",
)

_COMPLEX_TASK_SIGNALS = (
    "方案",
    "计划",
    "策略",
    "设计",
    "商业",
    "产品定位",
    "营销",
    "pitch",
    "strategy",
    "plan",
    "design",
    "proposal",
)
