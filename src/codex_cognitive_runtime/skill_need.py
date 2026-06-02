from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    interpreted_request: str = ""
    decision_chain: dict[str, Any] = field(default_factory=dict)

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
            "interpreted_request": self.interpreted_request,
            "decision_chain": self.decision_chain,
        }


class SkillNeedClassifier:
    def __init__(self, model: Any | None = None):
        self.model = model

    def classify(self, prompt: str) -> SkillNeedDecision:
        text = " ".join(str(prompt or "").split())
        lowered = text.lower()
        chain: dict[str, Any] = {
            "prompt_chars": len(text),
            "stages": [],
        }
        if not text:
            decision = _direct("empty", "empty prompt")
            _add_chain_stage(chain, "hard_exclusion", "empty_prompt", None, decision)
            return _with_chain(decision, chain, text)
        if _looks_like_non_action_statement(lowered):
            decision = _direct("memory_statement", "statement should be reviewed as memory, not executed as a runtime skill")
            _add_chain_stage(chain, "hard_exclusion", "memory_statement", None, decision)
            return _with_chain(decision, chain, text)
        if lowered in _AMBIGUOUS_SHORT_SIGNALS:
            decision = _direct("ambiguous_short_prompt", "short ambiguous prompt should not trigger a runtime skill")
            _add_chain_stage(chain, "hard_exclusion", "ambiguous_short_prompt", None, decision)
            return _with_chain(decision, chain, text)
        direct = _hard_direct_decision(lowered)
        if direct:
            _add_chain_stage(chain, "hard_direct_rule", direct.reason, None, direct)
            return _with_chain(direct, chain, text)
        if self.model is not None:
            model_output = self._model_classify(text)
            if isinstance(model_output, tuple):
                modeled, model_audit = model_output
            else:
                modeled = model_output
                model_audit = {"reason": "model_result_accepted"}
            _add_chain_stage(chain, "model_classification", model_audit.get("reason") or "model_classification", model_audit, modeled)
            if modeled:
                validated = _validate_decision(text, modeled)
                _add_chain_stage(chain, "rule_validation", _validation_reason(modeled, validated), None, validated)
                return _with_chain(validated, chain, text)
        fallback = self._fallback_classify(text)
        _add_chain_stage(chain, "fallback_heuristic", fallback.reason, None, fallback)
        validated = _validate_decision(text, fallback)
        _add_chain_stage(chain, "rule_validation", _validation_reason(fallback, validated), None, validated)
        return _with_chain(validated, chain, text)

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
        if _matches(lowered, _ENGINEERING_ACTION_SIGNALS):
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

    def _model_classify(self, prompt: str) -> tuple[SkillNeedDecision | None, dict[str, Any]]:
        request = (
            "Classify runtime skill need for the current user request. "
            "A runtime skill is a short, task-specific execution strategy grounded in clean long-term memory. "
            "Return skill_needed=false for simple factual, realtime, translation, calculation, or one-off direct-answer requests. "
            "Return skill_needed=true for multi-step creative, planning, product, design, writing, or software engineering tasks. "
            "Short UI or engineering change requests are not direct answers: page, layout, tabs, editable controls, model-powered optimization, "
            "scrollbar, scroll behavior, buttons, selects, dropdowns, tables, filters, API, tests, architecture, and bug-fix requests usually need a runtime skill even when phrased briefly. "
            "Memory statements such as preferences, rules, facts, or lessons should not be executed as runtime skills. "
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
            "interpreted_request": "one sentence normalized user request",
            "reason": "short reason",
        }
        audit: dict[str, Any] = {
            "model_available": True,
            "timeout_seconds": RUNTIME_SKILL_MODEL_TIMEOUT_SECONDS,
            "rule_prompt_preview": request.split("User request:", 1)[0].strip()[:2000],
            "user_request_chars": len(prompt),
            "schema": schema,
        }
        try:
            result = self.model.complete_json(request, schema, timeout_seconds=RUNTIME_SKILL_MODEL_TIMEOUT_SECONDS)
        except TypeError:
            try:
                result = self.model.complete_json(request, schema)
            except (ModelError, ValueError, TypeError) as exc:
                audit["error"] = type(exc).__name__
                audit["reason"] = "model_unavailable_or_invalid"
                return None, audit
        except (ModelError, ValueError, TypeError) as exc:
            audit["error"] = type(exc).__name__
            audit["reason"] = "model_unavailable_or_invalid"
            return None, audit
        if not isinstance(result, dict):
            audit["raw_result_type"] = type(result).__name__
            audit["reason"] = "model_returned_non_object"
            return None, audit
        audit["raw_result"] = redact_secrets(result)
        decision = _decision_from_model(result)
        audit["normalized_result"] = _decision_snapshot(decision) if decision else None
        audit["reason"] = "model_result_accepted" if decision else "model_result_invalid"
        return decision, audit


def _direct(intent: str, reason: str) -> SkillNeedDecision:
    return SkillNeedDecision(False, "direct_answer", intent, "general", "low", False, False, reason)


def _with_chain(decision: SkillNeedDecision, chain: dict[str, Any], prompt: str = "") -> SkillNeedDecision:
    interpreted = decision.interpreted_request or " ".join(str(prompt or "").split())
    updated = replace(decision, interpreted_request=interpreted[:300])
    final = _decision_snapshot(updated)
    return replace(updated, decision_chain={**chain, "final": final})


def _add_chain_stage(
    chain: dict[str, Any],
    stage: str,
    reason: str,
    details: dict[str, Any] | None,
    decision: SkillNeedDecision | None,
) -> None:
    item: dict[str, Any] = {
        "stage": stage,
        "reason": str(reason or "")[:240],
    }
    if details:
        item["details"] = details
    if decision:
        item["decision"] = _decision_snapshot(decision)
    chain.setdefault("stages", []).append(item)


def _decision_snapshot(decision: SkillNeedDecision | None) -> dict[str, Any] | None:
    if not decision:
        return None
    return {
        "skill_needed": decision.skill_needed,
        "mode": decision.mode,
        "intent": decision.intent,
        "domain": decision.domain,
        "complexity": decision.complexity,
        "requires_memory": decision.requires_memory,
        "requires_clarification": decision.requires_clarification,
        "reason": decision.reason,
        "interpreted_request": decision.interpreted_request,
    }


def _validation_reason(before: SkillNeedDecision, after: SkillNeedDecision) -> str:
    if _decision_snapshot(before) == _decision_snapshot(after):
        return "model_or_fallback_result_confirmed"
    if after.skill_needed and after.domain == "software_engineering":
        return "engineering_or_ui_rule_correction"
    if not after.skill_needed:
        return "hard_direct_rule_correction"
    return "decision_normalized"


def _decision_from_model(result: dict[str, Any]) -> SkillNeedDecision | None:
    mode = str(result.get("mode") or "").strip() or ("generate_runtime_skill" if result.get("skill_needed") else "direct_answer")
    skill_needed = bool(result.get("skill_needed"))
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
        str(result.get("interpreted_request") or "")[:300],
    )


def _validate_decision(prompt: str, decision: SkillNeedDecision) -> SkillNeedDecision:
    lowered = " ".join(str(prompt or "").split()).lower()
    direct = _hard_direct_decision(lowered)
    if direct:
        return direct
    if _looks_like_engineering_action(lowered):
        return SkillNeedDecision(
            True,
            "generate_runtime_skill",
            "software_engineering_change" if decision.intent in {"direct_answer", "simple_query"} else _clean_label(decision.intent, "software_engineering_change"),
            "software_engineering",
            _clean_complexity(decision.complexity, "medium"),
            True,
            bool(decision.requires_clarification),
            _append_rule_reason(decision.reason, "engineering or UI change request requires runtime strategy and workflow guardrails"),
            decision.interpreted_request,
        )
    skill_needed = bool(decision.skill_needed)
    intent_default = "complex_task" if skill_needed else "direct_answer"
    return SkillNeedDecision(
        skill_needed,
        "generate_runtime_skill" if skill_needed else "direct_answer",
        _clean_label(decision.intent, intent_default),
        _clean_label(decision.domain, "general"),
        _clean_complexity(decision.complexity, "medium" if skill_needed else "low"),
        bool(decision.requires_memory) if skill_needed else False,
        bool(decision.requires_clarification),
        str(decision.reason or "rule-validated runtime skill need decision")[:240],
        decision.interpreted_request,
    )


def _hard_direct_decision(text: str) -> SkillNeedDecision | None:
    if _matches(text, _DIRECT_FACT_SIGNALS) and not _looks_like_engineering_action(text):
        return _direct("simple_query", "simple realtime/factual request")
    if _matches(text, _DIRECT_QUESTION_SIGNALS) and not _has_explicit_execution_action(text):
        return _direct("direct_answer", "question asks for explanation rather than execution")
    return None


def _looks_like_engineering_action(text: str) -> bool:
    return _matches(text, _ENGINEERING_ACTION_SIGNALS) and (_has_action_signal(text) or not _matches(text, _DIRECT_QUESTION_SIGNALS))


def _has_action_signal(text: str) -> bool:
    return _matches(text, _ACTION_SIGNALS)


def _has_explicit_execution_action(text: str) -> bool:
    return _matches(text, _EXPLICIT_EXECUTION_ACTION_SIGNALS)


def _clean_label(value: str, fallback: str) -> str:
    cleaned = str(value or "").strip()[:80]
    return cleaned or fallback


def _clean_complexity(value: str, fallback: str) -> str:
    cleaned = str(value or "").strip().lower()
    return cleaned if cleaned in {"low", "medium", "high"} else fallback


def _append_rule_reason(reason: str, suffix: str) -> str:
    base = str(reason or "").strip()
    if not base:
        return suffix[:240]
    if suffix in base:
        return base[:240]
    return f"{base}; rule correction: {suffix}"[:240]


def _matches(text: str, signals: tuple[str, ...]) -> bool:
    return any(signal in text for signal in signals)


def _looks_like_non_action_statement(text: str) -> bool:
    stripped = _strip_leading_label(text.strip())
    prefixes = (
        "经验：",
        "经验:",
        "治理规则",
        "水利工程经验",
        "生活经验",
        "项目经验",
        "前端通用经验",
        "偏好：",
        "偏好:",
        "默认使用",
        "默认用",
        "规则：",
        "规则:",
        "原则：",
        "原则:",
        "记住：",
        "记住:",
        "请记住",
        "事实：",
        "事实:",
        "项目架构决策",
        "项目架构变更",
        "项目架构上",
        "项目架构要求",
        "临时测试：",
        "临时测试:",
        "这次只需要临时",
    )
    if stripped.startswith(prefixes):
        return True
    return any(marker in stripped[:40] for marker in ("经验：", "经验:"))


def _strip_leading_label(text: str) -> str:
    if text.startswith("[") and "]" in text[:120]:
        return text.split("]", 1)[1].strip()
    return text


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

_DIRECT_QUESTION_SIGNALS = (
    "这是什么",
    "这个是什么",
    "是什么页面",
    "是不是",
    "是否",
    "对吧",
    "指的是",
    "区别",
    "为什么",
    "原因",
    "解释一下",
    "说明一下",
    "what is",
    "why",
    "explain",
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

_ACTION_SIGNALS = (
    "修复",
    "实现",
    "重构",
    "调整",
    "增加",
    "新增",
    "修改",
    "改成",
    "改为",
    "优化",
    "处理",
    "验证",
    "应该",
    "需要",
    "不对",
    "太大",
    "太小",
    "fix",
    "implement",
    "debug",
    "refactor",
    "adjust",
    "add",
    "change",
    "update",
    "should",
    "need",
    "wrong",
)

_EXPLICIT_EXECUTION_ACTION_SIGNALS = (
    "修复",
    "实现",
    "重构",
    "调整",
    "增加",
    "新增",
    "修改",
    "改成",
    "改为",
    "优化",
    "处理",
    "验证",
    "不对",
    "太大",
    "太小",
    "fix",
    "implement",
    "debug",
    "refactor",
    "adjust",
    "add",
    "change",
    "update",
    "wrong",
)

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

_UI_ENGINEERING_SIGNALS = (
    "页面",
    "滚动条",
    "滚动",
    "布局",
    "样式",
    "按钮",
    "下拉",
    "控件",
    "弹窗",
    "面板",
    "列表",
    "表格",
    "筛选",
    "placeholder",
    "ui",
    "layout",
    "scrollbar",
    "scroll",
    "button",
    "select",
    "dropdown",
    "panel",
    "table",
    "filter",
)

_API_ENGINEERING_SIGNALS = (
    "api",
    "接口",
    "后端",
    "前端",
    "架构",
    "workflow",
    "runtime",
    "skill",
)

_ENGINEERING_ACTION_SIGNALS = _ENGINEERING_SIGNALS + _UI_ENGINEERING_SIGNALS + _API_ENGINEERING_SIGNALS

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

_MODEL_SKILL_CANDIDATE_SIGNALS = _BRAND_DESIGN_SIGNALS + _ENGINEERING_ACTION_SIGNALS + _COMPLEX_TASK_SIGNALS


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
