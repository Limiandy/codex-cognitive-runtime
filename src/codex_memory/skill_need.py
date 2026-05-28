from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model_client import ModelError
from .security import redact_secrets

RUNTIME_SKILL_MODEL_TIMEOUT_SECONDS = 12


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
    def __init__(self, model: Any | None = None):
        self.model = model

    def classify(self, prompt: str) -> SkillNeedDecision:
        text = " ".join(str(prompt or "").split())
        lowered = text.lower()
        if not text:
            return _direct("empty", "empty prompt")
        if lowered in _AMBIGUOUS_SHORT_SIGNALS:
            return _direct("ambiguous_short_prompt", "short ambiguous prompt should not trigger a runtime skill")
        if _matches(lowered, _DIRECT_FACT_SIGNALS) and not _matches(lowered, _COMPLEX_TASK_SIGNALS):
            return _direct("simple_query", "simple realtime/factual request")
        if self.model is not None and _matches(lowered, _MODEL_SKILL_CANDIDATE_SIGNALS):
            modeled = self._model_classify(text)
            if modeled:
                return modeled
        return self._fallback_classify(text)

    def _fallback_classify(self, text: str) -> SkillNeedDecision:
        lowered = text.lower()
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
        template = _template_intent(lowered)
        if template:
            intent, domain, clarify = template
            return SkillNeedDecision(
                True,
                "generate_runtime_skill",
                intent,
                domain,
                "medium",
                True,
                clarify,
                "templated task benefits from memory-grounded runtime skill",
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

    def _model_classify(self, prompt: str) -> SkillNeedDecision | None:
        request = (
            "Classify runtime skill need for the current user request. "
            "A runtime skill is a short, task-specific execution strategy grounded in clean long-term memory. "
            "Return skill_needed=false for simple factual, realtime, translation, calculation, or one-off direct-answer requests. "
            "Return skill_needed=true for multi-step creative, planning, product, design, writing, or software engineering tasks. "
            "Do not generate the skill itself.\n\n"
            f"User request:\n{redact_secrets(prompt)[:1200]}"
        )
        schema = {
            "skill_needed": True,
            "mode": "generate_runtime_skill|direct_answer",
            "intent": "short intent label",
            "domain": "brand_design|software_engineering|general|other",
            "complexity": "low|medium|high",
            "requires_memory": True,
            "requires_clarification": False,
            "reason": "short reason",
        }
        try:
            result = self.model.complete_json(request, schema, timeout_seconds=RUNTIME_SKILL_MODEL_TIMEOUT_SECONDS)
        except TypeError:
            try:
                result = self.model.complete_json(request, schema)
            except (ModelError, ValueError, TypeError):
                return None
        except (ModelError, ValueError, TypeError):
            return None
        if not isinstance(result, dict):
            return None
        return _decision_from_model(result)


def _direct(intent: str, reason: str) -> SkillNeedDecision:
    return SkillNeedDecision(False, "direct_answer", intent, "general", "low", False, False, reason)


def _decision_from_model(result: dict[str, Any]) -> SkillNeedDecision | None:
    mode = str(result.get("mode") or "").strip() or ("generate_runtime_skill" if result.get("skill_needed") else "direct_answer")
    skill_needed = bool(result.get("skill_needed")) and mode != "direct_answer"
    complexity = str(result.get("complexity") or "medium").strip().lower()
    if complexity not in {"low", "medium", "high"}:
        complexity = "medium"
    intent = str(result.get("intent") or ("complex_task" if skill_needed else "direct_answer")).strip()[:80]
    domain = str(result.get("domain") or "general").strip()[:80]
    if not intent or not domain:
        return None
    return SkillNeedDecision(
        skill_needed,
        "generate_runtime_skill" if skill_needed else "direct_answer",
        intent,
        domain,
        complexity,
        bool(result.get("requires_memory")) if skill_needed else False,
        bool(result.get("requires_clarification")),
        str(result.get("reason") or "model classified runtime skill need")[:240],
    )


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

_MODEL_SKILL_CANDIDATE_SIGNALS = _BRAND_DESIGN_SIGNALS + _ENGINEERING_SIGNALS + _COMPLEX_TASK_SIGNALS


def _template_intent(text: str) -> tuple[str, str, bool] | None:
    templates = (
        (("品牌定位", "positioning"), "brand_positioning", "brand_strategy", True),
        (("营销", "marketing"), "marketing_strategy", "marketing", False),
        (("写作", "文案", "writing"), "writing_style", "writing", False),
        (("产品分析", "product analysis"), "product_analysis", "product", False),
        (("商业计划", "business plan"), "business_plan", "business", False),
        (("pitch", "融资", "路演"), "pitch_deck", "business", True),
        (("代码审查", "code review"), "code_review", "software_engineering", False),
        (("架构", "architecture"), "architecture_design", "software_engineering", False),
        (("研究计划", "research plan"), "research_plan", "research", False),
    )
    for signals, intent, domain, clarify in templates:
        if any(signal in text for signal in signals):
            return intent, domain, clarify
    return None
