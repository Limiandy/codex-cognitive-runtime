from __future__ import annotations

from dataclasses import dataclass, field


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
    def classify(self, feedback_text: str) -> RuntimeSkillFeedbackDecision | None:
        text = " ".join(str(feedback_text or "").strip().lower().split())
        if not text:
            return None
        positive = _has_positive(text)
        negative = _has_any(text, _NEGATIVE)
        if not positive and not negative:
            return None
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
        return RuntimeSkillFeedbackDecision(
            outcome=outcome,
            feedback_target=target,
            dimensions=dimensions,
            adjust_seed_skill_strength=adjust_seed,
            adjust_durable_skill_strength=adjust_durable,
            reason=f"rule_target:{target}",
        )


def _target(text: str) -> str:
    if _has_any(text, ("模板", "agent", "seed", "种子")):
        return "seed_skill"
    if _has_any(text, ("durable", "dynamic", "长期技能", "持久技能")):
        return "durable_skill"
    if _has_any(text, ("偏好", "记忆", "memory", "不是我的偏好")):
        return "memory_basis"
    if _has_any(text, ("提问", "问题", "question", "clarify", "澄清")):
        return "first_action"
    if _has_any(text, ("方向", "策略", "方法", "流程", "workflow", "strategy")):
        return "skill_strategy"
    if _has_any(text, ("执行", "验证", "测试", "verification", "test")):
        return "execution"
    return "final_result"


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
    return "对" in text and "不对" not in text and "方向对" in text


_POSITIVE = ("很好", "不错", "可以", "对", "正是", "有用", "useful", "good", "great", "right", "correct")
_NEGATIVE = ("不对", "不是", "不要这样", "没用", "wrong", "bad", "not useful", "不适合", "太多", "不好")
