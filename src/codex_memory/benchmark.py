from __future__ import annotations

import json
from pathlib import Path

from .feedback_classifier import RuntimeSkillFeedbackClassifier
from .skill_need import SkillNeedClassifier


DEFAULT_BENCHMARK_FIXTURE = Path(__file__).resolve().parents[2] / "benchmarks" / "runtime_skill" / "tasks.jsonl"


DEFAULT_THRESHOLDS = {
    "direct_answer_skip_accuracy": 0.95,
    "skill_trigger_precision": 0.90,
    "skill_trigger_recall": 0.90,
    "intent_accuracy": 0.85,
    "domain_accuracy": 0.85,
    "clarification_accuracy": 0.85,
    "feedback_attribution_accuracy": 0.85,
    "feedback_dimension_accuracy": 0.85,
    "seed_adjustment_accuracy": 0.90,
    "durable_adjustment_accuracy": 0.90,
}


def run_runtime_skill_benchmark(fixture_path: str | None = None, synthetic: bool = False, fail_under_defaults: bool = False) -> dict[str, object]:
    classifier = SkillNeedClassifier(model=None)
    feedback = RuntimeSkillFeedbackClassifier()
    loaded = None if synthetic else _load_fixture(Path(fixture_path).expanduser() if fixture_path else DEFAULT_BENCHMARK_FIXTURE)
    tasks = loaded["tasks"] if loaded else _benchmark_tasks()
    feedback_cases = loaded["feedback"] if loaded else _feedback_cases()
    trigger_total = 0
    trigger_correct = 0
    trigger_false_positive = 0
    trigger_false_negative = 0
    direct_total = 0
    direct_correct = 0
    intent_total = 0
    intent_correct = 0
    domain_total = 0
    domain_correct = 0
    clarification_total = 0
    clarification_correct = 0
    error_samples = []
    for item in tasks:
        decision = classifier.classify(item["prompt"])
        expected = bool(item["skill_needed"])
        if "expected_intent" in item:
            intent_total += 1
            intent_correct += 1 if decision.intent == item["expected_intent"] else 0
        if "expected_domain" in item:
            domain_total += 1
            domain_correct += 1 if decision.domain == item["expected_domain"] else 0
        if "requires_clarification" in item:
            clarification_total += 1
            clarification_correct += 1 if bool(decision.requires_clarification) == bool(item["requires_clarification"]) else 0
        if expected:
            trigger_total += 1
            if decision.skill_needed:
                trigger_correct += 1
            else:
                trigger_false_negative += 1
                error_samples.append({"kind": "false_negative", "prompt": item["prompt"], "expected": expected, "actual": decision.skill_needed})
        else:
            direct_total += 1
            if not decision.skill_needed:
                direct_correct += 1
            else:
                trigger_false_positive += 1
                error_samples.append({"kind": "false_positive", "prompt": item["prompt"], "expected": expected, "actual": decision.skill_needed})
    feedback_correct = 0
    dimension_total = 0
    dimension_correct = 0
    seed_adjust_total = 0
    seed_adjust_correct = 0
    durable_adjust_total = 0
    durable_adjust_correct = 0
    feedback_errors = []
    for item in feedback_cases:
        decision = feedback.classify(item["prompt"])
        if decision and decision.feedback_target == item["target"]:
            feedback_correct += 1
        else:
            feedback_errors.append({"prompt": item["prompt"], "expected": item["target"], "actual": decision.feedback_target if decision else None})
        expected_dimensions = item.get("expected_dimensions") or {}
        for key, value in expected_dimensions.items():
            dimension_total += 1
            dimension_correct += 1 if decision and decision.dimensions.get(key) == value else 0
        if "adjust_seed_skill_strength" in item:
            seed_adjust_total += 1
            seed_adjust_correct += 1 if decision and bool(decision.adjust_seed_skill_strength) == bool(item["adjust_seed_skill_strength"]) else 0
        if "adjust_durable_skill_strength" in item:
            durable_adjust_total += 1
            durable_adjust_correct += 1 if decision and bool(decision.adjust_durable_skill_strength) == bool(item["adjust_durable_skill_strength"]) else 0
    metrics = {
        "skill_trigger_recall": _ratio(trigger_correct, trigger_total),
        "skill_trigger_precision": _ratio(trigger_correct, trigger_correct + trigger_false_positive),
        "direct_answer_skip_accuracy": _ratio(direct_correct, direct_total),
        "intent_accuracy": _ratio(intent_correct, intent_total),
        "domain_accuracy": _ratio(domain_correct, domain_total),
        "clarification_accuracy": _ratio(clarification_correct, clarification_total),
        "feedback_attribution_accuracy": _ratio(feedback_correct, len(feedback_cases)),
        "feedback_dimension_accuracy": _ratio(dimension_correct, dimension_total),
        "seed_adjustment_accuracy": _ratio(seed_adjust_correct, seed_adjust_total),
        "durable_adjustment_accuracy": _ratio(durable_adjust_correct, durable_adjust_total),
    }
    metric_denominators = {
        "skill_trigger_recall": trigger_total,
        "skill_trigger_precision": trigger_correct + trigger_false_positive,
        "direct_answer_skip_accuracy": direct_total,
        "intent_accuracy": intent_total,
        "domain_accuracy": domain_total,
        "clarification_accuracy": clarification_total,
        "feedback_attribution_accuracy": len(feedback_cases),
        "feedback_dimension_accuracy": dimension_total,
        "seed_adjustment_accuracy": seed_adjust_total,
        "durable_adjustment_accuracy": durable_adjust_total,
    }
    threshold_failures = {
        key: {"actual": metrics.get(key, 0.0), "threshold": threshold}
        for key, threshold in DEFAULT_THRESHOLDS.items()
        if metric_denominators.get(key, 0) > 0 and metrics.get(key, 0.0) < threshold
    }
    return {
        "task_count": len(tasks),
        "feedback_count": len(feedback_cases),
        "source": "synthetic" if synthetic or not loaded else str(loaded["path"]),
        **metrics,
        "passed_thresholds": not threshold_failures,
        "threshold_failures": threshold_failures,
        "counts": {
            "trigger_total": trigger_total,
            "trigger_correct": trigger_correct,
            "trigger_false_positive": trigger_false_positive,
            "trigger_false_negative": trigger_false_negative,
            "direct_total": direct_total,
            "direct_correct": direct_correct,
            "feedback_correct": feedback_correct,
            "intent_total": intent_total,
            "intent_correct": intent_correct,
            "domain_total": domain_total,
            "domain_correct": domain_correct,
            "clarification_total": clarification_total,
            "clarification_correct": clarification_correct,
            "dimension_total": dimension_total,
            "dimension_correct": dimension_correct,
            "seed_adjust_total": seed_adjust_total,
            "seed_adjust_correct": seed_adjust_correct,
            "durable_adjust_total": durable_adjust_total,
            "durable_adjust_correct": durable_adjust_correct,
        },
        "error_samples": error_samples[:20],
        "feedback_error_samples": feedback_errors[:20],
        "categories": loaded["categories"] if loaded else {
            "direct_answer": 100,
            "creative_design": 100,
            "planning_business": 100,
            "engineering": 100,
            "feedback": 100,
            "ambiguous": 50,
        },
    }


def _load_fixture(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    tasks: list[dict[str, object]] = []
    feedback: list[dict[str, str]] = []
    categories: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = json.loads(line)
        repeat = max(1, int(item.get("repeat") or 1))
        kind = str(item.get("kind") or "task")
        category = str(item.get("category") or kind)
        categories[category] = categories.get(category, 0) + repeat
        for index in range(repeat):
            prompt = str(item["prompt"]).replace("{n}", str(index))
            if kind == "feedback":
                feedback.append(
                    {
                        "prompt": prompt,
                        "target": str(item["target"]),
                        "expected_dimensions": item.get("expected_dimensions") or {},
                        "adjust_seed_skill_strength": item.get("adjust_seed_skill_strength"),
                        "adjust_durable_skill_strength": item.get("adjust_durable_skill_strength"),
                    }
                )
            else:
                tasks.append(
                    {
                        "prompt": prompt,
                        "skill_needed": bool(item["skill_needed"]),
                        **{key: item[key] for key in ("expected_intent", "expected_domain", "requires_clarification") if key in item},
                    }
                )
    return {"path": path, "tasks": tasks, "feedback": feedback, "categories": categories}


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
