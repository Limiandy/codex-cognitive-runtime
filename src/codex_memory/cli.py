from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from . import plugin_manager
from .service import MemoryService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex Memory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

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
    promote.add_argument("--no-file", action="store_true")

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

    sub.add_parser("expire")
    sub.add_parser("reconcile")
    sub.add_parser("audit")
    sub.add_parser("consolidate")
    sub.add_parser("cognitive-snapshot")
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
    govern = sub.add_parser("govern")
    govern.add_argument("--apply", action="store_true")
    govern_cognitive = sub.add_parser("govern-cognitive")
    govern_cognitive.add_argument("--apply", action="store_true")
    periodic = sub.add_parser("govern-periodic")
    periodic.add_argument("--interval-minutes", type=int, default=60)
    reconcile_mp = sub.add_parser("reconcile-mempalace")
    reconcile_mp.add_argument("--apply", action="store_true")

    plugin = sub.add_parser("plugin")
    plugin_sub = plugin.add_subparsers(dest="plugin_cmd", required=True)
    install = plugin_sub.add_parser("install")
    install.add_argument("--source", default=str(Path(__file__).resolve().parents[2]))
    plugin_sub.add_parser("status")
    plugin_sub.add_parser("enable")
    plugin_sub.add_parser("disable")
    plugin_sub.add_parser("block")
    uninstall = plugin_sub.add_parser("uninstall")
    uninstall.add_argument("--delete-files", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "plugin":
        if args.plugin_cmd == "install":
            return _print(plugin_manager.install(Path(args.source)))
        if args.plugin_cmd == "status":
            return _print(plugin_manager.status())
        if args.plugin_cmd == "enable":
            return _print(plugin_manager.enable())
        if args.plugin_cmd == "disable":
            return _print(plugin_manager.disable())
        if args.plugin_cmd == "block":
            return _print(plugin_manager.block())
        if args.plugin_cmd == "uninstall":
            return _print(plugin_manager.uninstall(delete_files=args.delete_files))

    service = MemoryService(load_config())
    try:
        if args.cmd == "status":
            return _print(service.status())
        if args.cmd == "ingest":
            return _print(service.ingest_event(args.event_type, {"text": args.text}))
        if args.cmd == "search":
            return _print(service.search_context(args.query, limit=args.limit, cwd=args.cwd, session_id=args.session_id))
        if args.cmd == "queue":
            return _print(service.list_memories(status=args.status, limit=args.limit))
        if args.cmd == "promote":
            return _print(service.promote_memory(args.memory_id, note=args.note, file_to_mempalace=not args.no_file))
        if args.cmd == "reject":
            return _print(service.reject_memory(args.memory_id, note=args.note))
        if args.cmd == "delete":
            return _print(service.delete_memory(args.memory_id, note=args.note))
        if args.cmd == "recall-feedback":
            return _print(service.recall_feedback(args.memory_id, args.outcome, note=args.note))
        if args.cmd == "expire":
            return _print(service.expire_due_memories())
        if args.cmd == "reconcile":
            return _print(service.reconcile())
        if args.cmd == "consolidate":
            return _print(service.consolidate_memories())
        if args.cmd == "cognitive-snapshot":
            return _print(service.cognitive_snapshot())
        if args.cmd == "workflow-plan":
            return _print(service.workflow_plan(args.prompt, limit=args.limit, cwd=args.cwd, session_id=args.session_id))
        if args.cmd == "workflow-execute":
            return _print(service.workflow_execute(args.prompt, limit=args.limit, cwd=args.cwd, session_id=args.session_id))
        if args.cmd == "govern":
            return _print(service.govern_memories(apply=args.apply))
        if args.cmd == "govern-cognitive":
            return _print(service.govern_cognitive(apply=args.apply))
        if args.cmd == "govern-periodic":
            return _print(service.periodic_governance(interval_minutes=args.interval_minutes))
        if args.cmd == "reconcile-mempalace":
            return _print(service.reconcile_mempalace(apply=args.apply))
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


if __name__ == "__main__":
    raise SystemExit(main())
