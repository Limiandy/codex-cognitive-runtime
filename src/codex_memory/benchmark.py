from __future__ import annotations

from .feedback_classifier import RuntimeSkillFeedbackClassifier
from .skill_need import SkillNeedClassifier


def run_runtime_skill_benchmark() -> dict[str, object]:
    classifier = SkillNeedClassifier(model=None)
    feedback = RuntimeSkillFeedbackClassifier()
    tasks = _benchmark_tasks()
    trigger_total = 0
    trigger_correct = 0
    direct_total = 0
    direct_correct = 0
    for item in tasks:
        decision = classifier.classify(item["prompt"])
        expected = bool(item["skill_needed"])
        if expected:
            trigger_total += 1
            trigger_correct += 1 if decision.skill_needed else 0
        else:
            direct_total += 1
            direct_correct += 1 if not decision.skill_needed else 0
    feedback_cases = _feedback_cases()
    feedback_correct = 0
    for item in feedback_cases:
        decision = feedback.classify(item["prompt"])
        feedback_correct += 1 if decision and decision.feedback_target == item["target"] else 0
    return {
        "task_count": len(tasks),
        "feedback_count": len(feedback_cases),
        "skill_trigger_recall": _ratio(trigger_correct, trigger_total),
        "direct_answer_skip_accuracy": _ratio(direct_correct, direct_total),
        "feedback_attribution_accuracy": _ratio(feedback_correct, len(feedback_cases)),
        "categories": {
            "direct_answer": 100,
            "creative_design": 100,
            "planning_business": 100,
            "engineering": 100,
            "feedback": 100,
            "ambiguous": 50,
        },
    }


def _benchmark_tasks() -> list[dict[str, object]]:
    direct = [{"prompt": f"现在天气怎么样？ #{idx}", "skill_needed": False} for idx in range(100)]
    creative = [{"prompt": f"帮我设计一个品牌 logo 方向 #{idx}", "skill_needed": True} for idx in range(100)]
    planning = [{"prompt": f"帮我制定一个产品营销策略 #{idx}", "skill_needed": True} for idx in range(100)]
    engineering = [{"prompt": f"帮我修复这个 bug 并运行测试 #{idx}", "skill_needed": True} for idx in range(100)]
    ambiguous_signals = ["测试", "修复", "代码", "test", "fix"] * 10
    ambiguous = [{"prompt": prompt, "skill_needed": False} for prompt in ambiguous_signals[:50]]
    return [*direct, *creative, *planning, *engineering, *ambiguous]


def _feedback_cases() -> list[dict[str, str]]:
    base = [
        ("很好", "final_result"),
        ("这个方向很好", "skill_strategy"),
        ("这个提问方式很好", "first_action"),
        ("不是我的偏好", "memory_basis"),
        ("这个模板不适合", "seed_skill"),
    ]
    return [{"prompt": text + f" #{idx}", "target": target} for idx in range(20) for text, target in base]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0
