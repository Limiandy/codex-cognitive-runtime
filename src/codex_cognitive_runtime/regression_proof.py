from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .memory_retriever import CleanMemoryRetriever
from .service import MemoryService
from .timeutil import local_now_iso


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REPORT = PLUGIN_ROOT / "docs" / "scoring-mechanism-effectiveness-report.md"
DEFAULT_JSON_REPORT = Path("/private/tmp/codex-cognitive-runtime-regression-proof-report.json")
DEFAULT_MARKDOWN_REPORT = Path("/private/tmp/codex-cognitive-runtime-regression-proof-report.md")


def run_regression_proof(
    *,
    state_dir: str | Path | None = None,
    json_report: str | Path | None = None,
    markdown_report: str | Path | None = None,
    clear_before: bool = False,
) -> dict[str, Any]:
    previous_fake_model = os.environ.get("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL")
    os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = "1"
    state_path = Path(state_dir) if state_dir else Path(tempfile.mkdtemp(prefix="codex-cognitive-runtime-regression-proof-"))
    if clear_before and state_path.exists():
        shutil.rmtree(state_path)
    state_path.mkdir(parents=True, exist_ok=True)
    json_path = Path(json_report) if json_report else DEFAULT_JSON_REPORT
    markdown_path = Path(markdown_report) if markdown_report else DEFAULT_MARKDOWN_REPORT
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)

    service = MemoryService(_config(state_path))
    try:
        scenarios = [
            _runtime_selection_scenario(
                service,
                name="brand_logo_task",
                prompt="帮我画一个品牌 logo",
                expected_top="Brand Guardian",
                required_selected={"Brand Guardian", "UI Designer"},
                forbidden_selected=set(),
                expected_skill_needed=True,
                expected_task_type="brand_logo_design",
            ),
            _runtime_selection_scenario(
                service,
                name="wechat_mini_program_ui",
                prompt="优化微信小程序订单页 UI 布局",
                expected_top="WeChat Mini Program Developer",
                required_selected={"WeChat Mini Program Developer", "UI Designer"},
                forbidden_selected=set(),
                expected_skill_needed=True,
                expected_task_type="frontend_ui_redesign",
            ),
            _runtime_selection_scenario(
                service,
                name="generic_frontend_ui",
                prompt="调整重置按钮大小，增加下拉 select placeholder",
                expected_top="UI Designer",
                required_selected={"UI Designer", "Mobile App Builder", "UX Architect"},
                forbidden_selected={"WeChat Mini Program Developer", "Feishu Integration Developer", "Roblox Systems Scripter"},
                expected_skill_needed=True,
                expected_task_type="frontend_ui_redesign",
            ),
            _runtime_selection_scenario(
                service,
                name="memory_statement",
                prompt="[trace-rerun-0098/project_exp] 经验：工程任务必须先 inspect，再最小修改，最后跑 unittest。",
                expected_top="",
                required_selected=set(),
                forbidden_selected=set(),
                expected_skill_needed=False,
                expected_task_type="software_engineering_change",
            ),
            _feedback_calibration_scenario(service),
        ]
        report = {
            "schema_version": 1,
            "generated_at": local_now_iso(),
            "objective": "closed_loop_regression_proof",
            "fake_model": True,
            "external_model_calls": False,
            "source_report": str(SOURCE_REPORT),
            "source_report_present": SOURCE_REPORT.exists(),
            "commands": {
                "json": f"PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m codex_cognitive_runtime.regression_proof --json-report {json_path} --markdown-report {markdown_path}",
                "unittest": "PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest tests.test_regression_proof",
            },
            "state_dir": str(state_path),
            "scenarios": scenarios,
            "summary": _summary(scenarios),
            "artifacts": {
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
            },
        }
        report["passed"] = bool(report["source_report_present"] and all(item.get("passed") for item in scenarios))
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path.write_text(_markdown_report(report), encoding="utf-8")
        return report
    finally:
        service.close()
        if previous_fake_model is None:
            os.environ.pop("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL", None)
        else:
            os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = previous_fake_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Codex Cognitive Runtime regression proof harness.")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--json-report", default=str(DEFAULT_JSON_REPORT))
    parser.add_argument("--markdown-report", default=str(DEFAULT_MARKDOWN_REPORT))
    parser.add_argument("--clear-before", action="store_true")
    args = parser.parse_args(argv)
    report = run_regression_proof(
        state_dir=args.state_dir or None,
        json_report=args.json_report,
        markdown_report=args.markdown_report,
        clear_before=args.clear_before,
    )
    print(json.dumps({"passed": report["passed"], "summary": report["summary"], "artifacts": report["artifacts"]}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def _config(state_dir: Path) -> Config:
    return Config(
        model="gpt-5.4-mini",
        state_dir=state_dir,
        ledger_path=state_dir / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
        development_audit=True,
        enable_feedback_model=True,
        store_raw_events=True,
        store_runtime_observation_previews=True,
    )


def _runtime_selection_scenario(
    service: MemoryService,
    *,
    name: str,
    prompt: str,
    expected_top: str,
    required_selected: set[str],
    forbidden_selected: set[str],
    expected_skill_needed: bool,
    expected_task_type: str,
) -> dict[str, Any]:
    session_id = f"proof-{name}"
    turn_id = "before"
    context = service.prompt_context(prompt, cwd=str(PLUGIN_ROOT), session_id=session_id, turn_id=turn_id)
    trace = service.list_traces(session_id=session_id, turn_id=turn_id)[0]
    events = service.trace_events(str(trace["id"]))
    by_name = {event["name"]: event for event in events}
    decision = by_name.get("task_understanding_completed", by_name.get("skill_need_decision", {})).get("metadata_json") or {}
    skill_decision = by_name.get("skill_need_decision", {}).get("metadata_json") or {}
    basis = by_name.get("basis_retrieved", {}).get("metadata_json") or {}
    scores = _score_rows(basis.get("seed_skill_selection_scores") or [])
    selected_names = {item["name"] for item in scores if item.get("selected")}
    top_name = scores[0]["name"] if scores else ""
    skill_needed = bool(skill_decision.get("skill_needed"))
    task_type = str(decision.get("task_type") or skill_decision.get("intent") or "")
    assertions = {
        "skill_need_matches": skill_needed == expected_skill_needed,
        "task_type_matches": (not expected_task_type) or task_type == expected_task_type,
        "top_skill_matches": (not expected_top) or top_name == expected_top,
        "required_selected_present": required_selected.issubset(selected_names),
        "forbidden_selected_absent": not (forbidden_selected & selected_names),
        "memory_statement_has_no_scores": expected_skill_needed or not scores,
        "context_shape_matches": ("任务规则：" in context) == expected_skill_needed,
    }
    return {
        "name": name,
        "prompt": prompt,
        "passed": all(assertions.values()),
        "before": {
            "skill_needed": skill_needed,
            "task_type": task_type,
            "context_has_task_rules": "任务规则：" in context,
            "trace_id": str(trace["id"]),
            "top_selected": scores[:4],
            "selected_names": sorted(selected_names),
        },
        "feedback": {
            "applied": False,
            "reason": "selection_path_regression_snapshot",
        },
        "after": {
            "expected_top": expected_top,
            "expected_skill_needed": expected_skill_needed,
            "improvement_claim": "runtime selection matches the scored behavior documented in scoring-mechanism-effectiveness-report.md",
        },
        "assertions": assertions,
    }


def _feedback_calibration_scenario(service: MemoryService) -> dict[str, Any]:
    prompt = "调整重置按钮大小，增加下拉 select placeholder"
    task_profile = {
        "project_type": "unknown",
        "task_type": "frontend_ui_redesign",
        "domain": "software_engineering",
        "surfaces": ["frontend", "ui", "ux"],
        "confidence": 0.9,
    }
    bad = _seed_record(
        service,
        "proof:bad-front-end-template",
        "Bad Generic Frontend Template",
        "reset button select placeholder frontend ui ux interface css react vue layout",
        "Bad template for reset button size, select placeholder, dropdown, frontend UI, UX, interface, CSS, React, Vue, and layout tasks. Force huge card-heavy layout and ignore the current requirement.",
    )
    good_records = [
        _seed_record(
            service,
            f"proof:correct-ui-{index}",
            name,
            "frontend ui designer",
            content,
        )
        for index, (name, content) in enumerate(
            [
                ("Correct UI Designer", "Design compact frontend UI controls with accessibility and browser verification."),
                ("Focused Control Stylist", "Tune reset buttons, selects, placeholders, and spacing with minimal UI changes."),
                ("Frontend Interaction Verifier", "Verify changed frontend controls in browser and keep state behavior visible."),
                ("Accessible Form Designer", "Keep form controls accessible, aligned, and easy to scan across responsive layouts."),
            ],
            start=1,
        )
    ]
    before_basis = CleanMemoryRetriever(service.ledger).retrieve(prompt, cwd=str(PLUGIN_ROOT), task_profile=task_profile, limit=4)
    before_scores = _score_rows(before_basis.get("seed_skill_selection_scores") or [])

    injection = service.ledger.record_runtime_skill_injection(
        prompt,
        {
            "skill_type": "runtime",
            "name": "proof_wrong_frontend_template",
            "domain": "software_engineering",
            "intent": "frontend_ui_redesign",
            "confidence": 0.8,
            "memory_basis_ids": [],
            "durable_skill_ids": [],
            "seed_skill_ids": [str(bad["id"])],
            "source_skill_ids": [str(bad["id"])],
            "task_profile": task_profile,
            "workflow_required_steps": ["inspect_repository", "execute_change", "frontend_typecheck"],
        },
        session_id="proof-feedback-calibration",
        turn_id="feedback",
        cwd=str(PLUGIN_ROOT),
    )
    feedback = service.apply_natural_feedback("这个模板不适合，不要用这个模板", session_id="proof-feedback-calibration", turn_id="feedback")
    feedback_record = feedback.get("runtime_skill_feedback") or {}
    feedback_metadata = feedback_record.get("metadata_json") or {}
    bad_after = service.ledger.get_cognitive_record(str(bad["id"])) or {}
    after_basis = CleanMemoryRetriever(service.ledger).retrieve(prompt, cwd=str(PLUGIN_ROOT), task_profile=task_profile, limit=4)
    after_scores = _score_rows(after_basis.get("seed_skill_selection_scores") or [])
    after_selected_names = {item["name"] for item in after_scores if item.get("selected")}
    bad_after_score = next((item for item in after_scores if item["id"] == str(bad["id"])), {})
    bad_profile_delta = _seed_profile_weight_delta(bad_after.get("metadata_json") or {})
    assertions = {
        "before_bad_ranked_first": before_scores and before_scores[0]["id"] == str(bad["id"]),
        "feedback_attributed_to_seed_skill": (feedback_metadata.get("evidence") or {}).get("feedback_target") == "seed_skill",
        "feedback_adjusts_seed_strength": bool((feedback_metadata.get("evidence") or {}).get("adjust_seed_skill_strength")),
        "calibration_recorded": bool((bad_after.get("metadata_json") or {}).get("seed_scoring_calibration")),
        "after_bad_has_penalty": bad_profile_delta < 0,
        "after_correct_skill_promoted": after_scores and after_scores[0]["id"] != str(bad["id"]),
        "after_bad_not_selected": "Bad Generic Frontend Template" not in after_selected_names,
        "good_skills_remain_selected": {item["metadata_json"]["name"] for item in good_records[:3]}.issubset(after_selected_names),
    }
    return {
        "name": "wrong_sort_feedback_calibration",
        "prompt": prompt,
        "passed": all(assertions.values()),
        "before": {
            "top_selected": before_scores[:5],
            "bad_seed_id": str(bad["id"]),
            "bad_rank": _rank_of(before_scores, str(bad["id"])),
        },
        "feedback": {
            "applied": True,
            "injection_id": str(injection["id"]),
            "feedback_record_id": str(feedback_record.get("id") or ""),
            "outcome": feedback_metadata.get("outcome"),
            "attribution": feedback_metadata.get("evidence") or {},
            "dimensions": feedback_metadata.get("dimensions") or {},
            "bad_seed_after_feedback": {
                "status": bad_after.get("status"),
                "metadata": _seed_feedback_metadata(bad_after.get("metadata_json") or {}),
            },
        },
        "after": {
            "top_selected": after_scores[:5],
            "bad_rank": _rank_of(after_scores, str(bad["id"])),
            "bad_score_after": bad_after_score,
            "bad_profile_weight_delta": bad_profile_delta,
            "improvement_claim": "negative seed-skill feedback created a profile penalty, moving the incorrect template below correct frontend UI skills",
        },
        "assertions": assertions,
    }


def _seed_record(service: MemoryService, source_id: str, name: str, description: str, content: str) -> dict[str, Any]:
    return service.ledger.record_cognitive_record(
        "skill",
        "seed_skill",
        source_id,
        content,
        "active",
        "global",
        domain="design",
        category="seed_skill",
        subcategory="proof",
        confidence=0.8,
        importance=0.9,
        strength=0.95,
        metadata={
            "skill_type": "seed_skill",
            "name": name,
            "description": description,
            "category": "design",
            "trust_level": "external_seed",
            "trust_state": "trusted",
            "source_verified": True,
            "success_count": 0,
            "failure_count": 0,
            "reuse_count": 0,
        },
        source_kind="regression_proof_seed",
    )


def _score_rows(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in scores:
        calibration = item.get("calibration") if isinstance(item.get("calibration"), dict) else {}
        rows.append(
            {
                "rank": int(item.get("rank") or 0),
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "score": float(item.get("score") or 0.0),
                "base_score": float(item.get("base_score") if item.get("base_score") is not None else item.get("score") or 0.0),
                "selected": bool(item.get("selected")),
                "target_surfaces": [str(value) for value in item.get("target_surfaces") or []],
                "target_domains": [str(value) for value in item.get("target_domains") or []],
                "calibration": calibration,
            }
        )
    return rows


def _rank_of(scores: list[dict[str, Any]], skill_id: str) -> int | None:
    for item in scores:
        if item.get("id") == skill_id:
            return int(item.get("rank") or 0)
    return None


def _seed_feedback_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "trust_state": metadata.get("trust_state"),
        "reuse_count": metadata.get("reuse_count"),
        "success_count": metadata.get("success_count"),
        "failure_count": metadata.get("failure_count"),
        "seed_scoring_calibration": metadata.get("seed_scoring_calibration"),
    }


def _seed_profile_weight_delta(metadata: dict[str, Any]) -> float:
    calibration = metadata.get("seed_scoring_calibration") if isinstance(metadata.get("seed_scoring_calibration"), dict) else {}
    profiles = calibration.get("profiles") if isinstance(calibration.get("profiles"), dict) else {}
    deltas = []
    for entry in profiles.values():
        if isinstance(entry, dict):
            try:
                deltas.append(float(entry.get("weight_delta") or 0.0))
            except (TypeError, ValueError):
                pass
    return min(deltas) if deltas else 0.0


def _summary(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scenario_count": len(scenarios),
        "passed_count": sum(1 for item in scenarios if item.get("passed")),
        "failed": [item.get("name") for item in scenarios if not item.get("passed")],
        "coverage": [item.get("name") for item in scenarios],
    }


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Regression Proof Harness Report",
        "",
        f"Generated: {report.get('generated_at')}",
        "",
        f"Passed: `{str(report.get('passed')).lower()}`",
        "",
        f"Source scoring report: `{report.get('source_report')}`",
        "",
        "## Scenario Summary",
        "",
        "| Scenario | Passed | Before | Feedback | After |",
        "| --- | --- | --- | --- | --- |",
    ]
    for scenario in report.get("scenarios") or []:
        before = _markdown_before(scenario)
        feedback = "applied" if (scenario.get("feedback") or {}).get("applied") else "snapshot"
        after = (scenario.get("after") or {}).get("improvement_claim") or "checked"
        lines.append(f"| {scenario.get('name')} | {str(bool(scenario.get('passed'))).lower()} | {before} | {feedback} | {after} |")
    lines.extend(["", "## Failed Assertions", ""])
    failed = False
    for scenario in report.get("scenarios") or []:
        assertions = scenario.get("assertions") or {}
        misses = [name for name, ok in assertions.items() if not ok]
        if misses:
            failed = True
            lines.append(f"- {scenario.get('name')}: {', '.join(misses)}")
    if not failed:
        lines.append("- None")
    lines.extend(["", "## Artifacts", "", f"- JSON: `{(report.get('artifacts') or {}).get('json_report')}`", f"- Markdown: `{(report.get('artifacts') or {}).get('markdown_report')}`"])
    return "\n".join(lines) + "\n"


def _markdown_before(scenario: dict[str, Any]) -> str:
    scores = (scenario.get("before") or {}).get("top_selected") or []
    if not scores:
        return "no seed scores"
    first = scores[0]
    return f"rank1={first.get('name')} score={first.get('score')}"


if __name__ == "__main__":
    raise SystemExit(main())
