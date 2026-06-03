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
from .schema import Evidence, MemoryCandidate
from .service import MemoryService
from .timeutil import local_now_iso


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JSON_REPORT = Path("/private/tmp/codex-cognitive-runtime-real-full-chain-report.json")
DEFAULT_MARKDOWN_REPORT = Path("/private/tmp/codex-cognitive-runtime-real-full-chain-report.md")


def run_real_full_chain_proof(
    *,
    state_dir: str | Path | None = None,
    json_report: str | Path | None = None,
    markdown_report: str | Path | None = None,
    clear_before: bool = False,
    sample_filter: str = "",
    max_samples: int | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    previous_fake_model = os.environ.pop("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL", None)
    state_path = Path(state_dir) if state_dir else Path(tempfile.mkdtemp(prefix="codex-cognitive-runtime-real-full-chain-"))
    if clear_before and state_path.exists():
        shutil.rmtree(state_path)
    state_path.mkdir(parents=True, exist_ok=True)
    json_path = Path(json_report) if json_report else DEFAULT_JSON_REPORT
    markdown_path = Path(markdown_report) if markdown_report else DEFAULT_MARKDOWN_REPORT
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)

    service = MemoryService(_config(state_path))
    try:
        scenario_specs = _scenario_specs()
        if sample_filter:
            needle = sample_filter.lower()
            scenario_specs = [
                spec
                for spec in scenario_specs
                if needle in spec.get("name", "").lower()
                or needle in spec.get("type", "").lower()
                or needle in str(spec.get("industry") or "").lower()
                or needle in str(spec.get("depth_axis") or "").lower()
            ]
        if max_samples is not None and max_samples > 0:
            scenario_specs = scenario_specs[:max_samples]
        scenarios: list[dict[str, Any]] = []
        for index, spec in enumerate(scenario_specs, 1):
            _progress(progress, f"[{index}/{len(scenario_specs)}] start {spec['name']} ({spec['type']})")
            scenario = _run_scenario_spec(service, spec)
            scenarios.append(scenario)
            _progress(progress, f"[{index}/{len(scenario_specs)}] done {spec['name']} passed={scenario.get('passed')} class={(scenario.get('failure_class') or {}).get('class')}")
        report = {
            "schema_version": 1,
            "generated_at": local_now_iso(),
            "objective": "real_full_chain_breadth_depth_proof",
            "fake_model_env": False,
            "state_dir": str(state_path),
            "sample_count": len(scenarios),
            "requested_sample_count": len(scenario_specs),
            "sample_filter": sample_filter,
            "max_samples": max_samples,
            "coverage_dimensions": {
                "industries": sorted({item for scenario in scenarios for item in ([scenario.get("industry")] if scenario.get("industry") else [])}),
                "depth_axes": sorted({item for scenario in scenarios for item in ([scenario.get("depth_axis")] if scenario.get("depth_axis") else [])}),
                "domains": sorted({item for scenario in scenarios for item in scenario.get("domains", [])}),
                "surfaces": sorted({item for scenario in scenarios for item in scenario.get("surfaces", [])}),
                "loop_segments": sorted({item for scenario in scenarios for item in scenario.get("loop_segments", [])}),
            },
            "scenarios": scenarios,
            "summary": _summary(scenarios),
            "failure_classification": _failure_classification(scenarios),
            "artifacts": {"json_report": str(json_path), "markdown_report": str(markdown_path)},
        }
        report["passed"] = all(scenario.get("passed") for scenario in scenarios)
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path.write_text(_markdown_report(report), encoding="utf-8")
        return report
    finally:
        service.close()
        if previous_fake_model is not None:
            os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = previous_fake_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run real-mode full chain proof for Codex Cognitive Runtime.")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--json-report", default=str(DEFAULT_JSON_REPORT))
    parser.add_argument("--markdown-report", default=str(DEFAULT_MARKDOWN_REPORT))
    parser.add_argument("--clear-before", action="store_true")
    parser.add_argument("--sample-filter", default="", help="Run only samples whose name/type/industry/depth contains this text.")
    parser.add_argument("--max-samples", type=int, default=0, help="Run at most this many matching samples.")
    parser.add_argument("--progress", action="store_true", help="Print one progress line before and after every sample.")
    args = parser.parse_args(argv)
    report = run_real_full_chain_proof(
        state_dir=args.state_dir or None,
        json_report=args.json_report,
        markdown_report=args.markdown_report,
        clear_before=args.clear_before,
        sample_filter=args.sample_filter,
        max_samples=args.max_samples or None,
        progress=args.progress,
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


def _scenario_specs() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    specs.extend(_selection_specs())
    specs.extend(_industry_depth_specs())
    specs.extend(_workflow_specs())
    specs.append({"name": "real_feedback_seed_calibration", "type": "feedback_calibration"})
    specs.append({"name": "partition_split_ledger_closure", "type": "partition"})
    return specs


def _run_scenario_spec(service: MemoryService, spec: dict[str, Any]) -> dict[str, Any]:
    scenario_type = spec.get("type")
    if scenario_type in {"selection", "industry_depth"}:
        return _selection_scenario(service, spec)
    if scenario_type == "workflow":
        return _workflow_scenario(service, **spec["workflow"])
    if scenario_type == "feedback_calibration":
        return _feedback_calibration_scenario(service)
    if scenario_type == "partition":
        return _partition_split_ledger_scenario(service)
    raise ValueError(f"unknown scenario type: {scenario_type}")


def _selection_specs() -> list[dict[str, Any]]:
    cases = [
        {
            "name": "brand_design_logo_guardian",
            "type": "selection",
            "prompt": "帮我设计一个品牌 logo，先给方向和约束，不要直接乱画",
            "expected": {"skill_needed": True, "top_contains": "Brand Guardian", "selected_any": ["Brand Guardian"]},
            "domains": ["brand_design"],
            "surfaces": ["design"],
        },
        {
            "name": "wechat_mini_program_ui",
            "type": "selection",
            "prompt": "优化微信小程序订单页 UI 布局，保留小程序交互习惯",
            "expected": {"skill_needed": True, "top_contains": "WeChat Mini Program Developer", "selected_any": ["WeChat Mini Program Developer"]},
            "domains": ["software_engineering"],
            "surfaces": ["frontend", "ui", "wechat"],
        },
        {
            "name": "generic_frontend_ui_excludes_cross_domain",
            "type": "selection",
            "prompt": "调整重置按钮大小，增加下拉 select placeholder，普通 Web 前端页面",
            "expected": {
                "skill_needed": True,
                "top_contains": "UI Designer",
                "selected_any": ["UI Designer"],
                "forbidden_selected": ["WeChat Mini Program Developer", "Feishu Integration Developer", "Roblox Systems Scripter"],
            },
            "domains": ["software_engineering"],
            "surfaces": ["frontend", "ui", "ux"],
        },
        {
            "name": "backend_api_contract",
            "type": "selection",
            "prompt": "修复后端分页 API 的 total 字段和过滤参数语义，并跑后端测试",
            "expected": {"skill_needed": True, "required_surfaces_any": ["backend"]},
            "domains": ["software_engineering"],
            "surfaces": ["backend", "testing"],
        },
        {
            "name": "privacy_secret_redaction_task",
            "type": "selection",
            "prompt": "检查日志里是否会暴露 api_key token secret，并补隐私脱敏验证",
            "expected": {"skill_needed": True, "required_surfaces_any": ["privacy"]},
            "domains": ["software_engineering"],
            "surfaces": ["privacy", "testing"],
        },
        {
            "name": "direct_answer_life_question",
            "type": "selection",
            "prompt": "家里灯不亮怎么办？只需要给排查建议",
            "expected": {"skill_needed": False, "no_seed_scores": True},
            "domains": ["general"],
            "surfaces": ["direct_answer"],
        },
        {
            "name": "memory_statement_no_runtime_skill",
            "type": "selection",
            "prompt": "经验：工程任务必须先 inspect，再最小修改，最后跑 unittest。",
            "expected": {"skill_needed": False, "no_seed_scores": True},
            "domains": ["memory"],
            "surfaces": ["memory_statement"],
        },
    ]
    return cases


def _industry_depth_specs() -> list[dict[str, Any]]:
    industries = [
        {
            "industry": "healthcare",
            "domain": "healthcare_operations",
            "samples": [
                ("patient_portal_ui", "医疗患者门户：优化预约、报告查看、复诊提醒的 UI 信息层级，要求隐私提示清晰", ["frontend", "ui", "privacy"]),
                ("clinical_data_api", "医疗数据平台：修复检查报告 API 的分页、患者筛选和权限字段，并验证接口契约", ["backend", "privacy", "testing"]),
                ("triage_audit_workflow", "医疗分诊系统：设计 triage 规则变更的审计链路，覆盖误分诊风险和回滚验证", ["governance", "privacy", "testing"]),
            ],
        },
        {
            "industry": "finance",
            "domain": "financial_services",
            "samples": [
                ("risk_dashboard_ui", "金融风控看板：重构逾期率、敞口、授信状态的仪表盘 UI，支持筛选和空状态", ["frontend", "ui", "testing"]),
                ("ledger_reconciliation_api", "金融对账服务：修复 ledger reconciliation API 的金额精度、币种过滤和异常返回", ["backend", "testing"]),
                ("compliance_evidence", "金融合规审计：为模型输出和人工复核建立证据链，要求可追溯、可解释、可验证", ["governance", "privacy", "testing"]),
            ],
        },
        {
            "industry": "education",
            "domain": "education",
            "samples": [
                ("learning_path_ui", "在线教育：优化学习路径页面，区分课程进度、作业状态和错题复习入口", ["frontend", "ui"]),
                ("assessment_scoring_api", "教育测评：修复评分 API 的 rubrics、重交策略和教师批注字段", ["backend", "testing"]),
                ("student_privacy_policy", "教育平台：检查学生隐私数据在日志、反馈和报告中的脱敏闭环", ["privacy", "governance", "testing"]),
            ],
        },
        {
            "industry": "ecommerce",
            "domain": "commerce",
            "samples": [
                ("checkout_ui", "电商结算页：优化优惠券、库存、地址、支付错误提示的 UI 和交互", ["frontend", "ui"]),
                ("inventory_order_api", "电商库存订单：修复库存锁定、订单状态和退款回调 API 的一致性", ["backend", "testing"]),
                ("growth_not_cross_domain", "电商增长活动：规划活动配置后台，但不要把营销 seed 错注入到普通 UI 任务", ["frontend", "governance"]),
            ],
        },
        {
            "industry": "manufacturing",
            "domain": "manufacturing",
            "samples": [
                ("quality_dashboard", "制造质检看板：展示缺陷率、批次、工站和追溯入口，要求异常优先级清晰", ["frontend", "ui"]),
                ("mes_integration_api", "制造 MES 集成：修复工单、设备状态、批次追踪 API，并验证边界参数", ["backend", "testing"]),
                ("safety_workflow", "制造安全：设计设备停机告警的执行守卫和验收覆盖，避免只口头完成", ["governance", "testing"]),
            ],
        },
        {
            "industry": "logistics",
            "domain": "logistics",
            "samples": [
                ("dispatch_ui", "物流调度台：优化司机、车辆、路线、异常包裹的 UI 扫描效率", ["frontend", "ui"]),
                ("tracking_api", "物流追踪：修复运单轨迹 API 的时间线排序、签收状态和异常码", ["backend", "testing"]),
                ("route_incident_loop", "物流异常处理：建立延误、改派、客户通知的闭环归因和验收标准", ["governance", "testing"]),
            ],
        },
        {
            "industry": "legal",
            "domain": "legal_tech",
            "samples": [
                ("contract_review_ui", "法律合同审查：优化条款风险、引用证据、修改建议的 UI 呈现", ["frontend", "ui", "privacy"]),
                ("case_search_api", "法律检索：修复案例搜索 API 的管辖区、日期、关键词过滤和高亮字段", ["backend", "testing"]),
                ("confidentiality_guard", "法律业务：检查客户机密信息在记忆、日志、报告中的脱敏和不外泄规则", ["privacy", "governance"]),
            ],
        },
        {
            "industry": "hr",
            "domain": "human_resources",
            "samples": [
                ("candidate_pipeline_ui", "HR 招聘漏斗：优化候选人阶段、面试反馈、offer 风险的 UI 信息架构", ["frontend", "ui"]),
                ("payroll_api", "HR 薪酬：修复 payroll API 的周期、税前税后、异常扣款字段", ["backend", "privacy", "testing"]),
                ("bias_audit", "HR 合规：建立招聘建议的偏见审计、人工复核和反馈校准闭环", ["governance", "privacy", "testing"]),
            ],
        },
        {
            "industry": "real_estate",
            "domain": "real_estate",
            "samples": [
                ("listing_ui", "房产列表页：优化户型、价格、位置、贷款估算和筛选项的 UI", ["frontend", "ui"]),
                ("property_api", "房产系统：修复房源 API 的上下架、价格历史和经纪人权限字段", ["backend", "testing"]),
                ("deal_workflow", "房产交易：建立带看、报价、合同、放款节点的执行守卫和验收标准", ["governance", "testing"]),
            ],
        },
        {
            "industry": "gaming",
            "domain": "game_platform",
            "samples": [
                ("game_admin_ui", "游戏平台管理后台：优化玩家封禁、道具发放、活动配置的 UI 扫描效率", ["frontend", "ui"]),
                ("matchmaking_api", "游戏匹配服务：修复 matchmaking API 的段位、延迟、队列状态和错误返回", ["backend", "testing"]),
                ("roblox_cross_domain_guard", "普通 Web UI 任务中不要误选 Roblox 脚本类 seed，除非任务明确是 Roblox", ["frontend", "governance"]),
            ],
        },
        {
            "industry": "data_ml",
            "domain": "data_ml",
            "samples": [
                ("model_monitoring_ui", "机器学习监控：优化漂移、召回率、错误样本、告警阈值的 UI 看板", ["frontend", "ui", "testing"]),
                ("feature_store_api", "特征平台：修复 feature store API 的实体键、时间窗和离线在线一致性", ["backend", "testing"]),
                ("model_feedback_loop", "AI 模型反馈：建立错误样本归因、人工反馈、校准和回归证明闭环", ["governance", "testing"]),
            ],
        },
        {
            "industry": "developer_tools",
            "domain": "developer_tools",
            "samples": [
                ("plugin_settings_ui", "开发者工具：优化插件设置页的权限、开关、状态提示和错误恢复 UI", ["frontend", "ui", "privacy"]),
                ("cli_api_contract", "开发者工具：修复 CLI/API schema 的参数校验、错误码和输出 JSON 契约", ["backend", "testing"]),
                ("runtime_observability", "开发者工具：把 runtime trace、feedback、attribution 暴露给 API/MCP 并可回归验证", ["governance", "testing"]),
            ],
        },
    ]
    specs = []
    for industry in industries:
        for sample_name, prompt, surfaces in industry["samples"]:
            specs.append(
                {
                    "name": f"industry_{industry['industry']}_{sample_name}",
                    "type": "industry_depth",
                    "prompt": prompt,
                    "expected": {
                        "skill_needed": True,
                        "required_surfaces_any": surfaces,
                        "forbidden_selected": _industry_forbidden_selected(prompt),
                    },
                    "domains": [industry["domain"]],
                    "surfaces": surfaces,
                    "industry": industry["industry"],
                    "depth_axis": _depth_axis(surfaces),
                }
            )
    return specs


def _selection_scenario(service: MemoryService, case: dict[str, Any]) -> dict[str, Any]:
    session_id = "real-" + str(case["name"])
    context = service.prompt_context(case["prompt"], cwd=str(PLUGIN_ROOT), session_id=session_id, turn_id="selection")
    trace = service.list_traces(session_id=session_id, turn_id="selection")[0]
    events = service.trace_events(str(trace["id"]))
    by_name = _by_name(events)
    task = (by_name.get("task_understanding_completed", {}).get("metadata_json") or {})
    skill = (by_name.get("skill_need_decision", {}).get("metadata_json") or {})
    basis = (by_name.get("basis_retrieved", {}).get("metadata_json") or {})
    scores = _score_rows(basis.get("seed_skill_selection_scores") or [])
    selected = {item["name"] for item in scores if item.get("selected")}
    top = scores[0]["name"] if scores else ""
    attribution = service.trace_attribution(str(trace["id"])) or {}
    expected = case["expected"]
    required_surfaces = set(expected.get("required_surfaces_any") or [])
    observed_surfaces = set(task.get("surfaces") or [])
    if "ui" in required_surfaces and "frontend" in observed_surfaces:
        observed_surfaces.add("ui")
    assertions = {
        "skill_needed_matches": bool(skill.get("skill_needed")) == bool(expected.get("skill_needed")),
        "top_contains": not expected.get("top_contains") or expected["top_contains"] in top,
        "selected_any": not expected.get("selected_any") or any(item in selected for item in expected["selected_any"]),
        "forbidden_selected_absent": not (set(expected.get("forbidden_selected") or []) & selected),
        "no_seed_scores": not expected.get("no_seed_scores") or not scores,
        "required_surface_present": not required_surfaces or bool(required_surfaces & observed_surfaces),
        "trace_attribution_available": bool((attribution.get("layers") or [])),
    }
    return {
        "name": case["name"],
        "type": "selection",
        "passed": all(assertions.values()),
        "prompt": case["prompt"],
        "domains": case["domains"],
        "surfaces": case["surfaces"],
        "industry": case.get("industry"),
        "depth_axis": case.get("depth_axis"),
        "loop_segments": ["task_understanding", "recall", "seed_scoring", "fragment_selection", "final_context", "outcome_attribution"],
        "trace_id": str(trace["id"]),
        "task_understanding_source": task.get("source"),
        "skill_needed": bool(skill.get("skill_needed")),
        "task_type": task.get("task_type"),
        "task_surfaces": task.get("surfaces") or [],
        "top_scores": scores[:5],
        "context_has_task_rules": "任务规则：" in context,
        "attribution_layers": [layer.get("layer") for layer in attribution.get("layers") or []],
        "assertions": assertions,
        "failure_class": _classify_failure(assertions, attribution=attribution, scores=scores, coverage=None),
    }


def _workflow_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "engineering_verified_success",
            "type": "workflow",
            "workflow": {
                "name": "engineering_verified_success",
                "prompt": "实现一个后端 API 修复并验证",
                "observations": [
                    {"tool_name": "functions.exec_command", "cmd": "rg api src tests", "stdout": "src/service.py", "exit_code": 0},
                    {"tool_name": "functions.apply_patch"},
                    {"tool_name": "functions.exec_command", "cmd": "python3 -m unittest tests.test_service", "stdout": "OK", "exit_code": 0},
                ],
                "stop_message": "已完成，测试通过",
                "expect_completed": True,
                "expected_missing": False,
                "domains": ["software_engineering"],
                "surfaces": ["backend", "testing"],
            },
        },
        {
            "name": "engineering_missing_verification_detected",
            "type": "workflow",
            "workflow": {
                "name": "engineering_missing_verification_detected",
                "prompt": "修改一个后端接口但先不要跑测试",
                "observations": [
                    {"tool_name": "functions.exec_command", "cmd": "rg api src", "stdout": "src/service.py", "exit_code": 0},
                    {"tool_name": "functions.apply_patch"},
                ],
                "stop_message": "已完成",
                "expect_completed": False,
                "expected_missing": True,
                "domains": ["software_engineering"],
                "surfaces": ["backend", "testing"],
            },
        },
        {
            "name": "frontend_ui_acceptance_missing_browser",
            "type": "workflow",
            "workflow": {
                "name": "frontend_ui_acceptance_missing_browser",
                "prompt": "调整页面 UI 布局，需要浏览器验证视觉和交互",
                "observations": [
                    {"tool_name": "functions.exec_command", "cmd": "rg page src", "stdout": "src/App.vue", "exit_code": 0},
                    {"tool_name": "functions.apply_patch"},
                    {"tool_name": "functions.exec_command", "cmd": "npm run typecheck", "stdout": "passed", "exit_code": 0},
                ],
                "stop_message": "已完成，typecheck 通过",
                "expect_completed": False,
                "expected_missing": True,
                "domains": ["software_engineering"],
                "surfaces": ["frontend", "ui", "testing"],
            },
        },
    ]


def _workflow_scenario(
    service: MemoryService,
    *,
    name: str,
    prompt: str,
    observations: list[dict[str, Any]],
    stop_message: str,
    expect_completed: bool,
    expected_missing: bool,
    domains: list[str],
    surfaces: list[str],
) -> dict[str, Any]:
    session_id = "real-" + name
    turn_id = "workflow"
    service.prompt_context(prompt, cwd=str(PLUGIN_ROOT), session_id=session_id, turn_id=turn_id)
    started = service.start_task_from_prompt({"prompt": prompt, "cwd": str(PLUGIN_ROOT), "session_id": session_id, "turn_id": turn_id})
    for observation in observations:
        service.observe_tool_use({**observation, "session_id": session_id, "turn_id": turn_id, "cwd": str(PLUGIN_ROOT)})
    stop = service.observe_stop({"session_id": session_id, "turn_id": turn_id, "cwd": str(PLUGIN_ROOT), "last_assistant_message": stop_message})
    trace = service.list_traces(session_id=session_id, turn_id=turn_id)[0]
    summary = service.trace_summary(str(trace["id"])) or {}
    coverage = stop.get("acceptance_coverage") or ((summary.get("workflow") or {}).get("acceptance_coverage") or {})
    coverage_summary = coverage.get("summary") or {}
    violation_types = [(item.get("metadata_json") or {}).get("violation_type") for item in stop.get("violations") or []]
    attribution = service.trace_attribution(str(trace["id"])) or {}
    assertions = {
        "workflow_started": bool(started.get("started")),
        "completion_matches": bool(coverage_summary.get("complete")) == bool(expect_completed),
        "missing_matches": (int(coverage_summary.get("missing") or 0) > 0 or "changed_without_verification" in violation_types or "acceptance_missing" in violation_types) == bool(expected_missing),
        "trace_attribution_available": bool(attribution.get("layers")),
        "stop_result_has_coverage": bool(coverage_summary),
    }
    return {
        "name": name,
        "type": "workflow",
        "passed": all(assertions.values()),
        "prompt": prompt,
        "domains": domains,
        "surfaces": surfaces,
        "loop_segments": ["task_understanding", "runtime_workflow", "tool_observation", "acceptance_coverage", "execution_guard", "outcome_attribution"],
        "trace_id": str(trace["id"]),
        "workflow_id": started.get("workflow_id"),
        "coverage_summary": coverage_summary,
        "violation_types": violation_types,
        "attribution_layers": [layer.get("layer") for layer in attribution.get("layers") or []],
        "assertions": assertions,
        "failure_class": _classify_failure(assertions, attribution=attribution, scores=[], coverage=coverage),
    }


def _feedback_calibration_scenario(service: MemoryService) -> dict[str, Any]:
    prompt = "调整 zetaReset alphaSelect sentinelPanel 的重置按钮和 select placeholder，普通 Web 前端页面"
    profile = {"project_type": "unknown", "task_type": "frontend_ui_redesign", "domain": "software_engineering", "surfaces": ["frontend", "ui", "ux"], "confidence": 0.9}
    good = _seed_record(
        service,
        "real:good-ui-template",
        "Correct Control Specialist",
        "zetaReset alphaSelect sentinelPanel reset button select placeholder",
        "zetaReset alphaSelect sentinelPanel compact control layout with verification.",
        importance=0.98,
    )
    bad = _seed_record(
        service,
        "real:bad-ui-template",
        "Bad Generic Frontend Template",
        "zetaReset alphaSelect sentinelPanel frontend ui ux reset button select placeholder",
        "zetaReset alphaSelect sentinelPanel frontend UI template for placeholder and reset button tasks; ignores real constraints.",
        importance=0.99,
    )
    before = _score_rows(CleanMemoryRetriever(service.ledger).retrieve(prompt, cwd=str(PLUGIN_ROOT), task_profile=profile, limit=4).get("seed_skill_selection_scores") or [])
    injection = service.ledger.record_runtime_skill_injection(
        prompt,
        {
            "skill_type": "runtime",
            "name": "real_bad_ui_template",
            "domain": "software_engineering",
            "intent": "frontend_ui_redesign",
            "confidence": 0.8,
            "seed_skill_ids": [str(bad["id"])],
            "source_skill_ids": [str(bad["id"])],
            "task_profile": profile,
        },
        session_id="real-feedback-calibration",
        turn_id="feedback",
        cwd=str(PLUGIN_ROOT),
    )
    feedback = service.apply_natural_feedback("这个模板不适合，不要用这个模板", session_id="real-feedback-calibration", turn_id="feedback")
    after = _score_rows(CleanMemoryRetriever(service.ledger).retrieve(prompt, cwd=str(PLUGIN_ROOT), task_profile=profile, limit=4).get("seed_skill_selection_scores") or [])
    bad_record = service.ledger.get_cognitive_record(str(bad["id"])) or {}
    delta = _seed_profile_weight_delta(bad_record.get("metadata_json") or {})
    assertions = {
        "before_bad_first": bool(before and before[0]["id"] == str(bad["id"])),
        "feedback_recorded": bool(feedback.get("runtime_skill_feedback")),
        "profile_penalty_recorded": delta < 0,
        "bad_demoted": not after or after[0]["id"] != str(bad["id"]),
        "good_available": any(item["id"] == str(good["id"]) for item in after),
    }
    return {
        "name": "real_feedback_seed_calibration",
        "type": "feedback_calibration",
        "passed": all(assertions.values()),
        "prompt": prompt,
        "domains": ["software_engineering"],
        "surfaces": ["frontend", "ui", "ux"],
        "loop_segments": ["seed_scoring", "runtime_feedback", "profile_calibration", "rerank_after_feedback"],
        "injection_id": str(injection["id"]),
        "before_scores": before[:5],
        "after_scores": after[:5],
        "bad_profile_weight_delta": delta,
        "assertions": assertions,
        "failure_class": _classify_failure(assertions, attribution={}, scores=after, coverage=None),
    }


def _partition_split_ledger_scenario(service: MemoryService) -> dict[str, Any]:
    sentinel = "PRIVATE_USER_SENTINEL_9f3a"
    prompt = "帮我设计一个品牌 logo，先用品牌守护视角评估，不要直接乱画"
    service.ledger.add_candidate(
        MemoryCandidate(
            content=f"用户私有偏好：{sentinel}",
            memory_type="user_preference",
            proposed_action="store",
            confidence=0.95,
            importance=0.9,
            ttl="long",
            scope="global",
            evidence=[Evidence(source="real_full_chain", quote=sentinel)],
            reason="partition leak guard",
        ),
        "active",
        {"status": "active"},
    )
    context = service.prompt_context(prompt, cwd=str(PLUGIN_ROOT), session_id="real-partition", turn_id="partition")
    seed_id = "agency-agents:design/design-brand-guardian.md"
    baseline_seed = service.baseline_ledger.get_cognitive_record(seed_id) or {}
    user_overlay = service.ledger.get_cognitive_record(seed_id) or {}
    before_baseline_metadata = dict(baseline_seed.get("metadata_json") or {})
    for _ in range(2):
        service.apply_natural_feedback("这个模板不适合，不要用这个模板", session_id="real-partition", turn_id="partition")
    updated_overlay = service.ledger.get_cognitive_record(seed_id) or {}
    updated_baseline = service.baseline_ledger.get_cognitive_record(seed_id) or {}
    baseline_export = service.export_data(target="baseline")
    user_export = service.export_data(target="user")
    baseline_blob = json.dumps(baseline_export, ensure_ascii=False)
    user_blob = json.dumps(user_export, ensure_ascii=False)
    assertions = {
        "baseline_seed_available": bool(baseline_seed),
        "user_overlay_created": bool(user_overlay) and (user_overlay.get("metadata_json") or {}).get("overlay_source_layer") == "baseline",
        "feedback_calibrates_user_overlay": "seed_scoring_calibration" in (updated_overlay.get("metadata_json") or {}),
        "baseline_not_calibrated_by_user_feedback": "seed_scoring_calibration" not in (updated_baseline.get("metadata_json") or {}),
        "baseline_failure_count_unchanged": (updated_baseline.get("metadata_json") or {}).get("failure_count") == before_baseline_metadata.get("failure_count"),
        "baseline_export_github_safe": bool(baseline_export.get("github_safe")),
        "baseline_export_no_user_sentinel": sentinel not in baseline_blob,
        "user_export_keeps_user_sentinel": sentinel in user_blob,
        "context_uses_runtime_rules": "任务规则：" in context,
    }
    return {
        "name": "partition_split_ledger_closure",
        "type": "partition",
        "passed": all(assertions.values()),
        "prompt": prompt,
        "domains": ["runtime_storage", "brand_design"],
        "surfaces": ["baseline_distribution", "user_personalization", "feedback_calibration", "export_safety"],
        "loop_segments": ["baseline_seed_import", "layered_recall", "user_overlay", "runtime_feedback", "baseline_export_guard"],
        "ledger_layers": service.ledger_view.stats(),
        "seed_id": seed_id,
        "assertions": assertions,
        "failure_class": _classify_failure(assertions, attribution={}, scores=[], coverage=None),
    }


def _seed_record(service: MemoryService, source_id: str, name: str, description: str, content: str, *, importance: float = 0.9) -> dict[str, Any]:
    return service.ledger.record_cognitive_record(
        "skill",
        "seed_skill",
        source_id,
        content,
        "active",
        "global",
        domain="design",
        category="seed_skill",
        subcategory="real_full_chain",
        confidence=0.8,
        importance=importance,
        strength=0.95,
        metadata={"skill_type": "seed_skill", "name": name, "description": description, "category": "design", "trust_level": "external_seed", "trust_state": "trusted", "source_verified": True},
        source_kind="real_full_chain_seed",
    )


def _by_name(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(event.get("name") or ""): event for event in events}


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
                "calibration": calibration,
            }
        )
    return rows


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
        "by_type": {kind: sum(1 for item in scenarios if item.get("type") == kind) for kind in sorted({item.get("type") for item in scenarios})},
        "by_industry": {
            industry: {
                "total": sum(1 for item in scenarios if item.get("industry") == industry),
                "passed": sum(1 for item in scenarios if item.get("industry") == industry and item.get("passed")),
            }
            for industry in sorted({item.get("industry") for item in scenarios if item.get("industry")})
        },
        "by_depth_axis": {
            axis: {
                "total": sum(1 for item in scenarios if item.get("depth_axis") == axis),
                "passed": sum(1 for item in scenarios if item.get("depth_axis") == axis and item.get("passed")),
            }
            for axis in sorted({item.get("depth_axis") for item in scenarios if item.get("depth_axis")})
        },
    }


def _failure_classification(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [scenario for scenario in scenarios if not scenario.get("passed")]
    by_class: dict[str, list[str]] = {}
    for scenario in failed:
        failure_class = str((scenario.get("failure_class") or {}).get("class") or "unknown")
        by_class.setdefault(failure_class, []).append(str(scenario.get("name") or ""))
    return {
        "failed_count": len(failed),
        "by_class": by_class,
        "policy": {
            "code_failure": "Fix code only when deterministic contract or runtime behavior is broken.",
            "audit_degradation": "Do not blindly fix code; inspect attribution and audit severity first.",
            "scoring_mismatch": "Tune scoring/calibration only if the expected ranking is valid for the task profile.",
            "coverage_gap": "Distinguish missing test evidence from implementation failure.",
            "model_variance": "Prefer deterministic validation guards over model-specific patches.",
            "test_fixture_issue": "Fix the fixture/report expectation rather than production logic.",
        },
    }


def _classify_failure(
    assertions: dict[str, bool],
    *,
    attribution: dict[str, Any],
    scores: list[dict[str, Any]],
    coverage: dict[str, Any] | None,
) -> dict[str, Any]:
    failed = [name for name, ok in assertions.items() if not ok]
    if not failed:
        return {"class": "passed", "failed_assertions": []}
    layers = {str(layer.get("layer")): layer for layer in attribution.get("layers") or []}
    negative_layers = [
        name
        for name, layer in layers.items()
        if layer.get("outcome") == "failure" or layer.get("contribution") == "negative"
    ]
    coverage_summary = (coverage or {}).get("summary") if isinstance((coverage or {}).get("summary"), dict) else {}
    if any(name in failed for name in ("trace_attribution_available", "stop_result_has_coverage", "workflow_started")):
        failure_class = "code_failure"
    elif any(name in failed for name in ("completion_matches", "missing_matches")) or int(coverage_summary.get("missing") or 0) or int(coverage_summary.get("failed") or 0):
        failure_class = "coverage_gap"
    elif any(name in failed for name in ("top_contains", "selected_any", "forbidden_selected_absent", "before_bad_first", "bad_demoted", "good_available")):
        failure_class = "scoring_mismatch"
    elif any(name in failed for name in ("skill_needed_matches", "required_surface_present")):
        failure_class = "model_variance"
    elif negative_layers:
        failure_class = "audit_degradation"
    elif scores:
        failure_class = "scoring_mismatch"
    else:
        failure_class = "test_fixture_issue"
    return {
        "class": failure_class,
        "failed_assertions": failed,
        "negative_attribution_layers": negative_layers,
        "coverage_summary": coverage_summary,
    }


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Real Full Chain Proof Report",
        "",
        f"Generated: {report.get('generated_at')}",
        "",
        f"Passed: `{str(report.get('passed')).lower()}`",
        f"Fake model env: `{str(report.get('fake_model_env')).lower()}`",
        "",
        "## Coverage",
        "",
        f"- Samples: `{report.get('sample_count')}`",
        f"- Industries: `{', '.join((report.get('coverage_dimensions') or {}).get('industries') or [])}`",
        f"- Depth axes: `{', '.join((report.get('coverage_dimensions') or {}).get('depth_axes') or [])}`",
        f"- Domains: `{', '.join((report.get('coverage_dimensions') or {}).get('domains') or [])}`",
        f"- Surfaces: `{', '.join((report.get('coverage_dimensions') or {}).get('surfaces') or [])}`",
        f"- Loop segments: `{', '.join((report.get('coverage_dimensions') or {}).get('loop_segments') or [])}`",
        "",
        "## Scenario Summary",
        "",
        "| Scenario | Type | Industry | Depth | Passed | Trace/Signal |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for scenario in report.get("scenarios") or []:
        signal = scenario.get("trace_id") or scenario.get("injection_id") or ""
        lines.append(f"| {scenario.get('name')} | {scenario.get('type')} | {scenario.get('industry') or ''} | {scenario.get('depth_axis') or ''} | {str(bool(scenario.get('passed'))).lower()} | {signal} |")
    failed = [scenario for scenario in report.get("scenarios") or [] if not scenario.get("passed")]
    lines.extend(["", "## Failed Assertions", ""])
    if not failed:
        lines.append("- None")
    else:
        for scenario in failed:
            misses = [key for key, ok in (scenario.get("assertions") or {}).items() if not ok]
            failure_class = (scenario.get("failure_class") or {}).get("class")
            lines.append(f"- {scenario.get('name')} [{failure_class}]: {', '.join(misses)}")
    lines.extend(["", "## Failure Classification Policy", ""])
    policy = (report.get("failure_classification") or {}).get("policy") or {}
    for key, value in policy.items():
        lines.append(f"- `{key}`: {value}")
    return "\n".join(lines) + "\n"


def _progress(enabled: bool, message: str) -> None:
    if enabled:
        print(message, flush=True)


def _depth_axis(surfaces: list[str]) -> str:
    values = set(surfaces)
    if values & {"privacy", "governance"}:
        return "governance_privacy_compliance"
    if "backend" in values:
        return "backend_api_data"
    if values & {"frontend", "ui", "ux"}:
        return "product_ui_experience"
    if "testing" in values:
        return "verification_quality"
    return "general"


def _industry_forbidden_selected(prompt: str) -> list[str]:
    lowered = prompt.lower()
    forbidden = []
    if "roblox" not in lowered:
        forbidden.append("Roblox Systems Scripter")
    if "小程序" not in lowered and "wechat" not in lowered and "微信" not in lowered:
        forbidden.append("WeChat Mini Program Developer")
    if "营销" not in lowered and "growth" not in lowered and "marketing" not in lowered:
        forbidden.append("Private Domain Growth Operator")
    return forbidden


if __name__ == "__main__":
    raise SystemExit(main())
