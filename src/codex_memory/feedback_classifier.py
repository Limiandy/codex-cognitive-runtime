from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model_client import ModelError


FEEDBACK_MODEL_TIMEOUT_SECONDS = 12


@dataclass(frozen=True)
class RuntimeSkillFeedbackDecision:
    outcome: str
    feedback_target: str
    dimensions: dict[str, str] = field(default_factory=dict)
    adjust_seed_skill_strength: bool = False
    adjust_durable_skill_strength: bool = False
    reason: str = ""

    def to_evidence(self) -> dict[str, object]:
        return {
            "feedback_target": self.feedback_target,
            "adjust_seed_skill_strength": self.adjust_seed_skill_strength,
            "adjust_durable_skill_strength": self.adjust_durable_skill_strength,
            "classifier_dimensions": self.dimensions,
            "classifier_reason": self.reason,
        }


class RuntimeSkillFeedbackClassifier:
    def __init__(self, model: Any | None = None, enable_model: bool = True):
        self.model = model
        self.enable_model = enable_model

    def classify(self, feedback_text: str) -> RuntimeSkillFeedbackDecision | None:
        text = " ".join(str(feedback_text or "").strip().lower().split())
        if not text:
            return None
        positive = _has_positive(text)
        negative = _has_any(text, _NEGATIVE)
        if not positive and not negative:
            return self._model_classify(text, None) if self._should_call_model(text, None) else None
        outcome = "mixed" if positive and negative else "positive" if positive else "negative"
        target = _target(text)
        dimensions = _dimensions(outcome, target)
        adjust_seed = target in {"seed_skill", "skill_strategy", "first_action"} and outcome in {"positive", "negative"}
        adjust_durable = target in {"durable_skill", "skill_strategy", "first_action", "execution"} and outcome in {"positive", "negative"}
        if target == "memory_basis":
            adjust_seed = False
            adjust_durable = False
        if target == "final_result":
            adjust_seed = False
            adjust_durable = False
        if outcome == "mixed":
            adjust_seed = False
            adjust_durable = False
        decision = RuntimeSkillFeedbackDecision(
            outcome=outcome,
            feedback_target=target,
            dimensions=dimensions,
            adjust_seed_skill_strength=adjust_seed,
            adjust_durable_skill_strength=adjust_durable,
            reason=f"rule_target:{target}",
        )
        if self._should_call_model(text, decision):
            return self._model_classify(text, decision) or decision
        return decision

    def _should_call_model(self, text: str, decision: RuntimeSkillFeedbackDecision | None) -> bool:
        if self.model is None or not self.enable_model:
            return False
        if decision and decision.outcome == "mixed":
            return True
        return _target_signal_count(text) > 1

    def _model_classify(self, text: str, fallback: RuntimeSkillFeedbackDecision | None) -> RuntimeSkillFeedbackDecision | None:
        prompt = (
            "Classify Runtime Skill feedback. Return JSON only. "
            "Do not change durable or seed strength for generic final-result praise. "
            "Use mixed when the user praises one aspect and criticizes another.\n\n"
            f"Feedback:\n{text[:1200]}"
        )
        schema = {
            "outcome": "positive|negative|mixed|unknown",
            "feedback_target": "first_action|skill_strategy|memory_basis|seed_skill|durable_skill|final_result|execution",
            "confidence": 0.0,
            "reason": "short reason",
        }
        try:
            result = self.model.complete_json(prompt, schema, timeout_seconds=FEEDBACK_MODEL_TIMEOUT_SECONDS)
        except TypeError:
            try:
                result = self.model.complete_json(prompt, schema)
            except (ModelError, ValueError, TypeError):
                return fallback
        except (ModelError, ValueError, TypeError):
            return fallback
        if not isinstance(result, dict):
            return fallback
        return _decision_from_model(result, fallback)


def _target(text: str) -> str:
    if _has_any(text, ("模板", "agent", "seed", "种子")):
        return "seed_skill"
    if _has_any(text, ("durable", "dynamic", "长期技能", "持久技能", "持久")):
        return "durable_skill"
    if _has_any(text, ("偏好", "记忆", "memory", "不是我的偏好", "组织定位")):
        return "memory_basis"
    if _has_any(text, ("提问", "问题", "question", "clarify", "澄清", "first action", "先问")):
        return "first_action"
    if _has_any(text, ("方向", "策略", "方法", "流程", "workflow", "strategy")):
        return "skill_strategy"
    if _has_any(text, ("执行", "验证", "测试", "verification", "test")):
        return "execution"
    return "final_result"


def _target_signal_count(text: str) -> int:
    targets = set()
    for target, signals in (
        ("seed_skill", ("模板", "agent", "seed", "种子")),
        ("durable_skill", ("durable", "dynamic", "长期技能", "持久技能", "持久")),
        ("memory_basis", ("偏好", "记忆", "memory", "不是我的偏好", "组织定位")),
        ("first_action", ("提问", "问题", "question", "clarify", "澄清", "first action", "先问")),
        ("skill_strategy", ("方向", "策略", "方法", "流程", "workflow", "strategy")),
        ("execution", ("执行", "验证", "测试", "verification", "test")),
    ):
        if _has_any(text, signals):
            targets.add(target)
    return len(targets)


def _decision_from_model(result: dict[str, Any], fallback: RuntimeSkillFeedbackDecision | None) -> RuntimeSkillFeedbackDecision | None:
    outcome = str(result.get("outcome") or "").strip().lower()
    target = str(result.get("feedback_target") or "").strip().lower()
    if outcome not in {"positive", "negative", "mixed", "unknown"}:
        return fallback
    if target not in {"first_action", "skill_strategy", "memory_basis", "seed_skill", "durable_skill", "final_result", "execution"}:
        return fallback
    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if outcome == "mixed" or confidence < 0.72:
        adjust_seed = False
        adjust_durable = False
    else:
        adjust_seed = target in {"seed_skill", "skill_strategy", "first_action"}
        adjust_durable = target in {"durable_skill", "skill_strategy", "first_action", "execution"}
    if target in {"memory_basis", "final_result"}:
        adjust_seed = False
        adjust_durable = False
    return RuntimeSkillFeedbackDecision(
        outcome=outcome,
        feedback_target=target,
        dimensions=_dimensions(outcome, target),
        adjust_seed_skill_strength=adjust_seed,
        adjust_durable_skill_strength=adjust_durable,
        reason=f"model_target:{target}:{str(result.get('reason') or '')[:120]}",
    )


def _dimensions(outcome: str, target: str) -> dict[str, str]:
    unknown = "unknown"
    dims = {
        "skill_relevance": unknown,
        "first_action_quality": unknown,
        "memory_basis_quality": unknown,
        "seed_skill_quality": unknown,
        "durable_skill_quality": unknown,
        "execution_compliance": unknown,
        "final_result_quality": unknown,
    }
    value = outcome if outcome in {"positive", "negative", "mixed"} else unknown
    if target == "first_action":
        dims["first_action_quality"] = value
    elif target == "memory_basis":
        dims["memory_basis_quality"] = value
    elif target == "seed_skill":
        dims["seed_skill_quality"] = value
    elif target == "durable_skill":
        dims["durable_skill_quality"] = value
    elif target == "execution":
        dims["execution_compliance"] = "passed" if outcome == "positive" else "failed" if outcome == "negative" else "mixed"
    elif target == "skill_strategy":
        dims["skill_relevance"] = value
    if target == "final_result" or outcome in {"positive", "negative", "mixed"}:
        dims["final_result_quality"] = value if target == "final_result" else dims["final_result_quality"]
    return dims


def _has_any(text: str, signals: tuple[str, ...]) -> bool:
    return any(signal in text for signal in signals)


def _has_positive(text: str) -> bool:
    if any(signal in text for signal in ("很好", "不错", "可以", "正是", "有用", "useful", "good", "great", "right", "correct")):
        return True
    return "对" in text and "不对" not in text and _target_signal_count(text) > 0


_POSITIVE = ("很好", "不错", "可以", "对", "正是", "有用", "useful", "good", "great", "right", "correct")
_NEGATIVE = ("不对", "不是", "不要这样", "不要用", "没用", "wrong", "bad", "not useful", "不适合", "太多", "太泛", "不好", "错误", "错了", "不符合", "过时")
