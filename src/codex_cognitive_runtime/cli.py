from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .benchmark import run_runtime_skill_benchmark
from .config import load_config
from .doctor import run_doctor
from . import plugin_manager
from .service import MemoryService


EXPERIMENTAL_COMMANDS = {
    "cognitive-snapshot",
    "knowledge-build",
    "knowledge-search",
    "knowledge-audit",
    "skill-build",
    "skill-list",
    "skill-audit",
    "skill-promote",
    "skill-deprecate",
    "workflow-plan",
    "workflow-execute",
    "workflow-simulate",
    "workflow-resume",
    "workflow-cancel",
    "workflow-audit",
    "govern-cognitive",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config = load_config()
    if argv and argv[0] in EXPERIMENTAL_COMMANDS and not config.enable_experimental_cli:
        return _print_error(
            {
                "error": "experimental_cli_disabled",
                "command": argv[0],
                "hint": "Set CODEX_COGNITIVE_RUNTIME_ENABLE_EXPERIMENTAL_CLI=1 to enable experimental commands.",
            },
            code=2,
        )

    parser = argparse.ArgumentParser(description="Codex Cognitive Runtime")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    runtime_benchmark = sub.add_parser("runtime-benchmark")
    runtime_benchmark.add_argument("--fixture", default=None)
    runtime_benchmark.add_argument("--synthetic", action="store_true")
    runtime_benchmark.add_argument("--fail-under-defaults", action="store_true")
    runtime_status = sub.add_parser("runtime-status")
    runtime_status.add_argument("--cwd", default=None)
    runtime_status.add_argument("--session-id", default=None)
    runtime_status.add_argument("--turn-id", default=None)
    runtime_status.add_argument("--pretty", action="store_true")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--model-check", action="store_true")
    doctor.add_argument("--privacy", action="store_true")

    ingest = sub.add_parser("ingest")
    ingest.add_argument("text")
    ingest.add_argument("--event-type", default="manual")

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--cwd", default=None)
    search.add_argument("--session-id", default=None)

    queue = sub.add_parser("queue")
    queue.add_argument("--status", default=None)
    queue.add_argument("--limit", type=int, default=20)

    promote = sub.add_parser("promote")
    promote.add_argument("memory_id")
    promote.add_argument("--note", default="")

    reject = sub.add_parser("reject")
    reject.add_argument("memory_id")
    reject.add_argument("--note", default="")

    delete = sub.add_parser("delete")
    delete.add_argument("memory_id")
    delete.add_argument("--note", default="")

    feedback = sub.add_parser("recall-feedback")
    feedback.add_argument("memory_id")
    feedback.add_argument("outcome", choices=["positive", "negative"])
    feedback.add_argument("--note", default="")

    export = sub.add_parser("export")
    export.add_argument("--output", default=None)
    export.add_argument("--limit", type=int, default=5000)

    wipe = sub.add_parser("wipe")
    wipe.add_argument("--yes", action="store_true")

    prune = sub.add_parser("prune-events")
    prune.add_argument("--older-than-days", type=int, default=None)
    prune_runtime = sub.add_parser("prune-runtime")
    prune_runtime.add_argument("--older-than-days", type=int, default=None)
    prune_runtime.add_argument("--include-recipes", action="store_true")
    prune_runtime.add_argument("--include-skills", action="store_true")
    seed_skills = sub.add_parser("seed-skills")
    seed_skills.add_argument("--source", default=None)
    seed_skills.add_argument("--repo-url", default="https://github.com/msitarzewski/agency-agents.git")
    seed_skills.add_argument("--limit", type=int, default=None)
    seed_skills.add_argument("--category", default=None)
    seed_skills.add_argument("--dry-run", action="store_true")
    seed_skills.add_argument("--activate", action="store_true")
    seed_skills.add_argument("seed_cmd", nargs="?", choices=["list", "show", "disable", "restore", "suppress", "stats"])
    seed_skills.add_argument("seed_id", nargs="?")

    runtime_skills = sub.add_parser("runtime-skills")
    runtime_skills.add_argument("runtime_cmd", choices=["list", "show", "feedback", "audit"])
    runtime_skills.add_argument("runtime_id", nargs="?")
    runtime_skills.add_argument("--outcome", choices=["positive", "negative", "mixed", "unknown"], default="positive")
    runtime_skills.add_argument(
        "--target",
        choices=["first_action", "skill_strategy", "memory_basis", "seed_skill", "durable_skill", "final_result", "execution"],
        default="final_result",
    )
    runtime_skills.add_argument("--note", default="")
    runtime_skills.add_argument("--limit", type=int, default=50)

    dynamic_skills = sub.add_parser("dynamic-skills")
    dynamic_skills.add_argument("dynamic_cmd", choices=["list", "show", "promote", "reject", "deprecate", "suppress", "stats"])
    dynamic_skills.add_argument("dynamic_id", nargs="?")
    dynamic_skills.add_argument("--status", default=None)
    dynamic_skills.add_argument("--note", default="")
    dynamic_skills.add_argument("--reason", default="")
    dynamic_skills.add_argument("--limit", type=int, default=50)

    traces = sub.add_parser("traces")
    traces.add_argument("trace_cmd", choices=["list", "show", "events", "summary", "audit", "export", "prune"])
    traces.add_argument("trace_id", nargs="?")
    traces.add_argument("--session-id", default=None)
    traces.add_argument("--turn-id", default=None)
    traces.add_argument("--limit", type=int, default=50)
    traces.add_argument("--older-than-days", type=int, default=None)

    sub.add_parser("expire")
    sub.add_parser("reconcile")
    sub.add_parser("audit")
    sub.add_parser("consolidate")
    if config.enable_experimental_cli:
        sub.add_parser("cognitive-snapshot")
        knowledge_build = sub.add_parser("knowledge-build")
        knowledge_build.add_argument("--source", choices=["repo", "git", "all"], default="all")
        knowledge_search = sub.add_parser("knowledge-search")
        knowledge_search.add_argument("query")
        knowledge_search.add_argument("--limit", type=int, default=10)
        sub.add_parser("knowledge-audit")
        sub.add_parser("skill-build")
        skill_list = sub.add_parser("skill-list")
        skill_list.add_argument("--limit", type=int, default=50)
        sub.add_parser("skill-audit")
        skill_promote = sub.add_parser("skill-promote")
        skill_promote.add_argument("skill_id")
        skill_deprecate = sub.add_parser("skill-deprecate")
        skill_deprecate.add_argument("skill_id")
        workflow = sub.add_parser("workflow-plan")
        workflow.add_argument("prompt")
        workflow.add_argument("--limit", type=int, default=6)
        workflow.add_argument("--cwd", default=None)
        workflow.add_argument("--session-id", default=None)
        execute_workflow = sub.add_parser("workflow-execute")
        execute_workflow.add_argument("prompt")
        execute_workflow.add_argument("--limit", type=int, default=6)
        execute_workflow.add_argument("--cwd", default=None)
        execute_workflow.add_argument("--session-id", default=None)
        simulate_workflow = sub.add_parser("workflow-simulate")
        simulate_workflow.add_argument("prompt")
        simulate_workflow.add_argument("--limit", type=int, default=6)
        simulate_workflow.add_argument("--cwd", default=None)
        simulate_workflow.add_argument("--session-id", default=None)
        workflow_resume = sub.add_parser("workflow-resume")
        workflow_resume.add_argument("workflow_id")
        workflow_cancel = sub.add_parser("workflow-cancel")
        workflow_cancel.add_argument("workflow_id")
        workflow_audit = sub.add_parser("workflow-audit")
        workflow_audit.add_argument("workflow_id")
    govern = sub.add_parser("govern")
    govern.add_argument("--apply", action="store_true")
    periodic = sub.add_parser("govern-periodic")
    periodic.add_argument("--interval-minutes", type=int, default=60)
    if config.enable_experimental_cli:
        govern_cognitive = sub.add_parser("govern-cognitive")
        govern_cognitive.add_argument("--apply", action="store_true")
        govern_cognitive.add_argument("--full", action="store_true")

    plugin = sub.add_parser("plugin")
    plugin_sub = plugin.add_subparsers(dest="plugin_cmd", required=True)
    install = plugin_sub.add_parser("install")
    install.add_argument("--source", default=str(Path(__file__).resolve().parents[2]))
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--diff", action="store_true")
    plugin_sub.add_parser("status")
    plugin_sub.add_parser("enable")
    plugin_sub.add_parser("disable")
    plugin_sub.add_parser("block")
    uninstall = plugin_sub.add_parser("uninstall")
    uninstall.add_argument("--delete-files", action="store_true")
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument("--diff", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "plugin":
        if args.plugin_cmd == "install":
            return _print(plugin_manager.install(Path(args.source), dry_run=args.dry_run, show_diff=args.diff))
        if args.plugin_cmd == "status":
            return _print(plugin_manager.status())
        if args.plugin_cmd == "enable":
            return _print(plugin_manager.enable())
        if args.plugin_cmd == "disable":
            return _print(plugin_manager.disable())
        if args.plugin_cmd == "block":
            return _print(plugin_manager.block())
        if args.plugin_cmd == "uninstall":
            return _print(plugin_manager.uninstall(delete_files=args.delete_files, dry_run=args.dry_run, show_diff=args.diff))
    if args.cmd == "doctor":
        return _print(run_doctor(config, model_check=args.model_check, privacy=args.privacy))

    service = MemoryService(config)
    try:
        if args.cmd == "status":
            return _print(service.status())
        if args.cmd == "runtime-benchmark":
            result = run_runtime_skill_benchmark(fixture_path=args.fixture, synthetic=args.synthetic, fail_under_defaults=args.fail_under_defaults)
            if args.fail_under_defaults and not result.get("passed_thresholds"):
                return _print_error(result, code=1)
            return _print(result)
        if args.cmd == "runtime-status":
            status = service.runtime_status(cwd=args.cwd, session_id=args.session_id, turn_id=args.turn_id)
            if args.pretty:
                return _print_text(_format_runtime_status(status))
            return _print(status)
        if args.cmd == "ingest":
            return _print(service.ingest_event(args.event_type, {"text": args.text}))
        if args.cmd == "search":
            return _print(service.search_context(args.query, limit=args.limit, cwd=args.cwd, session_id=args.session_id))
        if args.cmd == "queue":
            return _print(service.list_memories(status=args.status, limit=args.limit))
        if args.cmd == "promote":
            return _print(service.promote_memory(args.memory_id, note=args.note))
        if args.cmd == "reject":
            return _print(service.reject_memory(args.memory_id, note=args.note))
        if args.cmd == "delete":
            return _print(service.delete_memory(args.memory_id, note=args.note))
        if args.cmd == "recall-feedback":
            return _print(service.recall_feedback(args.memory_id, args.outcome, note=args.note))
        if args.cmd == "export":
            data = service.export_data(limit=args.limit)
            if args.output:
                output_path = Path(args.output).expanduser()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return _print({"exported": True, "path": str(output_path), "stats": data.get("stats")})
            return _print(data)
        if args.cmd == "wipe":
            if not args.yes:
                return _print_error({"error": "confirmation_required", "hint": "Pass --yes to wipe the local Codex Cognitive Runtime Ledger."}, code=2)
            return _print(service.wipe_data())
        if args.cmd == "prune-events":
            return _print(service.prune_events(older_than_days=args.older_than_days))
        if args.cmd == "prune-runtime":
            return _print(service.prune_runtime(older_than_days=args.older_than_days, include_recipes=args.include_recipes, include_skills=args.include_skills))
        if args.cmd == "seed-skills":
            if args.seed_cmd == "list":
                return _print(service.list_seed_skills(limit=args.limit or 50))
            if args.seed_cmd == "show":
                return _print(_require_result(service.get_seed_skill(args.seed_id or ""), "seed_skill_not_found"))
            if args.seed_cmd == "disable":
                return _print(_require_result(service.set_seed_skill_trust_state(args.seed_id or "", "disabled"), "seed_skill_not_found"))
            if args.seed_cmd == "restore":
                seed = service.get_seed_skill(args.seed_id or "")
                if not seed:
                    return _print_error({"error": "seed_skill_not_found", "id": args.seed_id}, code=2)
                restored = "trusted" if (seed.get("metadata_json") or {}).get("source_verified") else "unverified"
                return _print(service.set_seed_skill_trust_state(args.seed_id or "", restored))
            if args.seed_cmd == "suppress":
                return _print(_require_result(service.set_seed_skill_trust_state(args.seed_id or "", "suppressed"), "seed_skill_not_found"))
            if args.seed_cmd == "stats":
                return _print(service.seed_skill_stats())
            return _print(service.seed_skills(source=args.source, repo_url=args.repo_url, limit=args.limit, category=args.category, dry_run=args.dry_run, activate=args.activate))
        if args.cmd == "runtime-skills":
            if args.runtime_cmd == "list":
                return _print(service.list_runtime_skills(limit=args.limit))
            if args.runtime_cmd == "show":
                return _print(_require_result(service.get_runtime_skill(args.runtime_id or ""), "runtime_skill_not_found"))
            if args.runtime_cmd == "feedback":
                return _print(_require_result(service.runtime_skill_feedback(args.runtime_id or "", args.outcome, target=args.target, note=args.note), "runtime_skill_not_found"))
            if args.runtime_cmd == "audit":
                return _print(service.runtime_skill_audit())
        if args.cmd == "dynamic-skills":
            if args.dynamic_cmd == "list":
                return _print(service.list_dynamic_skills(status=args.status, limit=args.limit))
            if args.dynamic_cmd == "show":
                return _print(_require_result(service.get_dynamic_skill(args.dynamic_id or ""), "dynamic_skill_not_found"))
            if args.dynamic_cmd == "promote":
                return _print(_require_result(service.promote_dynamic_skill(args.dynamic_id or "", note=args.note), "dynamic_skill_not_found"))
            if args.dynamic_cmd == "reject":
                return _print(_require_result(service.reject_dynamic_skill(args.dynamic_id or "", note=args.note), "dynamic_skill_not_found"))
            if args.dynamic_cmd == "deprecate":
                return _print(_require_result(service.deprecate_dynamic_skill(args.dynamic_id or "", note=args.note), "dynamic_skill_not_found"))
            if args.dynamic_cmd == "suppress":
                return _print(_require_result(service.suppress_dynamic_skill(args.dynamic_id or "", reason=args.reason), "dynamic_skill_not_found"))
            if args.dynamic_cmd == "stats":
                return _print(service.dynamic_skill_stats())
        if args.cmd == "traces":
            if args.trace_cmd == "list":
                return _print(service.list_traces(session_id=args.session_id, turn_id=args.turn_id, limit=args.limit))
            if args.trace_cmd == "show":
                return _print(_require_result(service.get_trace(args.trace_id or ""), "trace_not_found"))
            if args.trace_cmd == "events":
                return _print(service.trace_events(args.trace_id or "", limit=args.limit))
            if args.trace_cmd == "summary":
                return _print(_require_result(service.trace_summary(args.trace_id or ""), "trace_not_found"))
            if args.trace_cmd == "audit":
                return _print(service.trace_audit())
            if args.trace_cmd == "export":
                return _print(_require_result(service.export_trace(args.trace_id or ""), "trace_not_found"))
            if args.trace_cmd == "prune":
                return _print(service.prune_traces(older_than_days=args.older_than_days))
        if args.cmd == "expire":
            return _print(service.expire_due_memories())
        if args.cmd == "reconcile":
            return _print(service.reconcile())
        if args.cmd == "consolidate":
            return _print(service.consolidate_memories())
        if args.cmd == "cognitive-snapshot":
            return _print(service.cognitive_snapshot())
        if args.cmd == "knowledge-build":
            return _print(service.knowledge_build(source=args.source))
        if args.cmd == "knowledge-search":
            return _print(service.knowledge_search(args.query, limit=args.limit))
        if args.cmd == "knowledge-audit":
            return _print(service.knowledge_audit())
        if args.cmd == "skill-build":
            return _print(service.skill_build())
        if args.cmd == "skill-list":
            return _print(service.skill_list(limit=args.limit))
        if args.cmd == "skill-audit":
            return _print(service.skill_audit())
        if args.cmd == "skill-promote":
            return _print(service.skill_promote(args.skill_id))
        if args.cmd == "skill-deprecate":
            return _print(service.skill_deprecate(args.skill_id))
        if args.cmd == "workflow-plan":
            return _print(service.workflow_plan(args.prompt, limit=args.limit, cwd=args.cwd, session_id=args.session_id))
        if args.cmd == "workflow-execute":
            result = service.workflow_execute(args.prompt, limit=args.limit, cwd=args.cwd, session_id=args.session_id)
            result["deprecated_command"] = "workflow-execute"
            result["replacement_command"] = "workflow-simulate"
            return _print(result)
        if args.cmd == "workflow-simulate":
            return _print(service.workflow_simulate(args.prompt, limit=args.limit, cwd=args.cwd, session_id=args.session_id))
        if args.cmd == "workflow-resume":
            return _print(service.workflow_resume(args.workflow_id))
        if args.cmd == "workflow-cancel":
            return _print(service.workflow_cancel(args.workflow_id))
        if args.cmd == "workflow-audit":
            return _print(service.workflow_audit(args.workflow_id))
        if args.cmd == "govern":
            return _print(service.govern_memories(apply=args.apply))
        if args.cmd == "govern-cognitive":
            return _print(service.govern_cognitive(apply=args.apply, full=args.full))
        if args.cmd == "govern-periodic":
            return _print(service.periodic_governance(interval_minutes=args.interval_minutes))
        if args.cmd == "audit":
            return _print(
                {
                    "stats": service.ledger.stats(),
                    "quarantine_sample": service.list_memories(status="quarantined", limit=10),
                    "rejected_sample": service.list_memories(status="rejected", limit=10),
                }
            )
    finally:
        service.close()
    return 1


def _print(data) -> int:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return 0


def _print_text(text: str) -> int:
    sys.stdout.write(text.rstrip() + "\n")
    return 0


def _print_error(data, code: int) -> int:
    sys.stderr.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return code


def _require_result(result, error: str):
    if result is None:
        return {"error": error}
    return result


def _format_runtime_status(status: dict) -> str:
    workflow = status.get("active_workflow")
    lines = ["Codex Cognitive Runtime Runtime Status"]
    if workflow:
        lines.append(f"Active workflow: {workflow.get('id')}")
        lines.append(f"Task: {workflow.get('current_task') or ''}")
        lines.append(f"Turn: {workflow.get('session_id') or '-'} / {workflow.get('turn_id') or '-'}")
        completed = workflow.get("completed_steps") or []
        lines.append("Completed steps: " + (", ".join(completed) if completed else "none"))
        lines.append(f"Pending required step: {workflow.get('pending_required_step') or 'none'}")
        lines.append(f"Changed: {bool(workflow.get('changed'))}")
        lines.append(f"Verified: {bool(workflow.get('verified'))}")
        lines.append(f"Test failed: {bool(workflow.get('test_failed'))}")
    else:
        lines.append("Active workflow: none")

    violations = status.get("open_violations") or []
    lines.append(f"Open violations: {len(violations)}")
    for violation in violations[:5]:
        metadata = violation.get("metadata_json") or {}
        lines.append(f"- {metadata.get('violation_type')}: {metadata.get('severity')}")

    recipes = status.get("learned_recipes") or []
    lines.append(f"Learned recipes: {len(recipes)}")
    for recipe in recipes[:5]:
        metadata = recipe.get("metadata_json") or {}
        commands = [str(item) for item in metadata.get("recipe") or [] if item]
        command = _compact_line(commands[0] if commands else str(recipe.get("content") or ""))
        lines.append(
            "- "
            + command[:120]
            + f" | reuse={int(metadata.get('reuse_count') or 0)}"
            + f" success={int(metadata.get('success_count') or 0)}"
            + f" failure={int(metadata.get('failure_count') or 0)}"
        )
    observations = status.get("recent_observations") or []
    lines.append(f"Recent observations: {len(observations)}")
    for observation in observations[-5:]:
        summary = observation.get("summary") or {}
        tool_name = _compact_line(str(observation.get("tool_name") or summary.get("tool_name") or "-"))
        lines.append(
            f"- {observation.get('matched_step_id') or 'unmatched'}"
            + f" via {tool_name}"
            + f" confidence={summary.get('confidence') if summary else '-'}"
        )
    return "\n".join(lines)


def _compact_line(value: str) -> str:
    return " ".join(str(value).split())


if __name__ == "__main__":
    raise SystemExit(main())
