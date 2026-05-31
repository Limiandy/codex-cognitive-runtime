from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from . import __version__, plugin_manager
from .benchmark import DEFAULT_BENCHMARK_FIXTURE
from .config import Config, ensure_state_dir
from .ledger import Ledger
from .model_client import CodexMiniClient
from .runtime_monitor import RuntimeMonitor


def run_doctor(config: Config, model_check: bool = False, privacy: bool = False) -> dict[str, Any]:
    root = plugin_root()
    checks = {
        "plugin_root": _check_plugin_root(root),
        "python_version": _check_python_version(),
        "sqlite_version": _check_sqlite_version(),
        "state_dir": _check_state_dir(config),
        "sqlite_ledger": _check_sqlite_ledger(config),
        "schema_migrations": _check_schema_migrations(config),
        "runtime_skill_governance": _check_runtime_skill_governance(config),
        "runtime_trace": _check_runtime_trace(config),
        "codex_cli": _check_codex_cli(),
        "installed_plugin": _check_installed_plugin(),
        "raw_event_storage": _check_raw_event_storage(config),
        "runtime_observer": _check_runtime_observer(config),
        "mcp_config_portable": _check_config_portable(root / ".mcp.json"),
        "hooks_config": _check_hooks_config(root / "hooks.json"),
        "mcp_server": _check_mcp_server(config),
        "model_smoke": _check_model(config) if model_check else _skipped("pass --model-check to run a model smoke test"),
    }
    result = {
        "version": __version__,
        "ok": all(item.get("ok") is not False for item in checks.values() if item.get("level") == "fatal"),
        "summary": _summary(checks),
        "checks": checks,
    }
    if privacy:
        result["privacy"] = _privacy_report(config)
    return result


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def config_text_is_portable(text: str) -> bool:
    if ("hook" + "-probe") in text:
        return False
    absolute_path = re.compile(r"(?<![$A-Za-z0-9_])/(Users|home|var|tmp|opt|Applications)/")
    return absolute_path.search(text) is None


def _check_plugin_root(root: Path) -> dict[str, Any]:
    manifest = root / ".codex-plugin" / "plugin.json"
    return _result(
        "fatal",
        root.is_dir() and manifest.is_file(),
        path=str(root),
        manifest=str(manifest),
        fix_hint="Run doctor from a valid codex-cognitive-runtime checkout or reinstall the plugin.",
    )


def _check_state_dir(config: Config) -> dict[str, Any]:
    try:
        ensure_state_dir(config)
        probe = config.state_dir / f".doctor-write-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        probe.write_text("ok", encoding="utf-8")
        try:
            probe.unlink()
        except OSError:
            pass
        mode = oct(config.state_dir.stat().st_mode & 0o777)
        return _result("fatal", True, path=str(config.state_dir), mode=mode)
    except OSError as exc:
        return _result("fatal", False, path=str(config.state_dir), error=str(exc), fix_hint="Create the state directory or fix local filesystem permissions.")


def _check_sqlite_ledger(config: Config) -> dict[str, Any]:
    try:
        ledger = Ledger(config.ledger_path)
        try:
            stats = ledger.stats()
        finally:
            ledger.close()
        return _result("fatal", True, path=str(config.ledger_path), stats=stats)
    except Exception as exc:
        return _result("fatal", False, path=str(config.ledger_path), error=str(exc), fix_hint="Check SQLite file permissions or move CODEX_COGNITIVE_RUNTIME_STATE_DIR to a writable path.")


def _check_schema_migrations(config: Config) -> dict[str, Any]:
    try:
        ledger = Ledger(config.ledger_path)
        try:
            status = ledger.runtime_skill_governance_migration_status()
        finally:
            ledger.close()
        ok = bool(status.pop("ok", False))
        return _result(
            "warn",
            ok,
            **status,
            fix_hint="Open the Ledger with the current codex-cognitive-runtime version or rerun doctor to apply idempotent runtime skill governance migrations.",
        )
    except Exception as exc:
        return _result("warn", False, error=str(exc), fix_hint="Check Ledger permissions and schema_migrations integrity.")


def _check_runtime_skill_governance(config: Config) -> dict[str, Any]:
    try:
        ledger = Ledger(config.ledger_path)
        try:
            migration = ledger.runtime_skill_governance_migration_status()
            records = ledger.list_cognitive_records(limit=5000)
        finally:
            ledger.close()
        runtime_injection_count = 0
        runtime_feedback_count = 0
        seed_status: dict[str, int] = {}
        dynamic_status: dict[str, int] = {}
        for record in records:
            if record.get("layer") == "runtime_skill" and record.get("record_type") == "injection":
                runtime_injection_count += 1
            if record.get("layer") == "runtime_skill" and record.get("record_type") == "feedback":
                runtime_feedback_count += 1
            if record.get("record_type") == "seed_skill":
                status = str(record.get("status") or "unknown")
                seed_status[status] = seed_status.get(status, 0) + 1
            if record.get("record_type") == "dynamic_skill":
                status = str(record.get("status") or "unknown")
                dynamic_status[status] = dynamic_status.get(status, 0) + 1
        benchmark_available = DEFAULT_BENCHMARK_FIXTURE.is_file()
        ok = bool(migration.get("ok")) and benchmark_available
        return _result(
            "warn",
            ok,
            migration=migration,
            legacy_runtime_skill_records=migration.get("legacy_runtime_skill_records"),
            seed_status_trust_conflicts=migration.get("seed_status_trust_conflicts"),
            runtime_skill_records={"injection_count": runtime_injection_count, "feedback_count": runtime_feedback_count},
            seed_skill_status=seed_status,
            dynamic_skill_status=dynamic_status,
            benchmark={"available": benchmark_available, "fixture_path": str(DEFAULT_BENCHMARK_FIXTURE), "can_run": benchmark_available},
            strict_privacy=config.strict_privacy,
            fix_hint="Open the Ledger with the current codex-cognitive-runtime version and ensure benchmarks/runtime_skill/tasks.jsonl is present.",
        )
    except Exception as exc:
        return _result("warn", False, error=str(exc), fix_hint="Check Ledger permissions and runtime skill governance records.")


def _check_runtime_trace(config: Config) -> dict[str, Any]:
    try:
        ledger = Ledger(config.ledger_path)
        try:
            migration = ledger.runtime_trace_migration_status()
            traces = ledger.list_traces(limit=5000)
            audit = RuntimeMonitor(ledger, strict_privacy=config.strict_privacy, live_log=False).trace_audit()
        finally:
            ledger.close()
        by_status: dict[str, int] = {}
        for trace in traces:
            status = str(trace.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        return _result(
            "info",
            bool(migration.get("ok")),
            migration=migration,
            tables_present=bool(migration.get("ok")),
            trace_count=len(traces),
            by_status=by_status,
            open_count=audit.get("open_count", 0),
            failed_count=audit.get("failed_count", 0),
            stale_open_count=len(audit.get("stale_open_traces") or []),
            live_log_enabled=config.trace_live_log,
            strict_privacy=config.strict_privacy,
        )
    except Exception as exc:
        return _result("warn", False, error=str(exc), fix_hint="Open the Ledger with the current version to create runtime trace tables.")


def _check_codex_cli() -> dict[str, Any]:
    path = shutil.which("codex")
    version = _codex_version(path) if path else None
    return _result(
        "fatal",
        bool(path),
        path=path,
        version=version,
        impact="required for model-backed memory extraction",
        fix_hint="Install and log in to the Codex CLI, then rerun doctor.",
    )


def _check_python_version() -> dict[str, Any]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info >= (3, 9)
    return _result("fatal", ok, version=version, minimum="3.9", fix_hint="Use Python 3.9 or newer.")


def _check_sqlite_version() -> dict[str, Any]:
    return _result("fatal", True, version=sqlite3.sqlite_version)


def _check_installed_plugin() -> dict[str, Any]:
    state = plugin_manager.status()
    installed = state.get("status") in {"on", "published"}
    return _result(
        "warn",
        installed,
        status=state.get("status"),
        install_path=state.get("install_path"),
        codex_plugin_enabled=state.get("codex_plugin_enabled"),
        fix_hint="Run ./scripts/codex-cognitive-runtime plugin install --source \"$PWD\" if this plugin is not installed.",
    )


def _check_raw_event_storage(config: Config) -> dict[str, Any]:
    if config.store_raw_events:
        return _result(
            "warn",
            False,
            enabled=True,
            impact="raw event payloads are stored in the local Ledger",
            fix_hint="Unset CODEX_COGNITIVE_RUNTIME_STORE_RAW_EVENTS or set store_raw_events=false.",
        )
    return _result("info", True, enabled=False)


def _check_runtime_observer(config: Config) -> dict[str, Any]:
    if config.store_runtime_observation_previews:
        return _result(
            "warn",
            False,
            enabled=config.enable_runtime_observer,
            observation_previews="stored",
            impact="runtime observations store stdout/stderr previews in the local Ledger",
            fix_hint="Unset CODEX_COGNITIVE_RUNTIME_STORE_RUNTIME_OBSERVATION_PREVIEWS unless local debugging requires output previews.",
        )
    return _result(
        "info",
        True,
        enabled=config.enable_runtime_observer,
        observation_previews="redacted",
        strict_privacy=config.strict_privacy,
        impact="runtime observations store command, file paths, exit code, output hashes, lengths, and failure flags",
    )


def _check_config_portable(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _result("fatal", False, path=str(path), error=str(exc), fix_hint="Reinstall the plugin to regenerate .mcp.json.")
    return _result(
        "fatal",
        config_text_is_portable(text),
        path=str(path),
        fix_hint="Ensure .mcp.json uses CODEX_PLUGIN_ROOT or $HOME instead of machine-specific absolute paths.",
    )


def _check_hooks_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result("fatal", False, path=str(path), error=str(exc), fix_hint="Reinstall the plugin to regenerate hooks.json.")
    hooks = data.get("hooks") if isinstance(data, dict) else {}
    required = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact"}
    present = set(hooks or {})
    text = path.read_text(encoding="utf-8")
    return _result(
        "fatal",
        required.issubset(present) and config_text_is_portable(text),
        path=str(path),
        missing=sorted(required - present),
        fix_hint="Reinstall the plugin or update hooks.json so SessionStart, UserPromptSubmit, PostToolUse, Stop, and PreCompact are present.",
    )


def _check_mcp_server(config: Config) -> dict[str, Any]:
    env = os.environ.copy()
    env["CODEX_COGNITIVE_RUNTIME_STATE_DIR"] = str(config.state_dir)
    proc = subprocess.Popen(
        [sys.executable, "-m", "codex_cognitive_runtime.mcp_server"],
        cwd=str(plugin_root()),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
        proc.stdin.flush()
        first = _read_json_line(proc)
        second = _read_json_line(proc)
        names = [tool.get("name") for tool in second.get("result", {}).get("tools", [])]
        ok = first.get("result", {}).get("serverInfo", {}).get("name") == "codex-cognitive-runtime" and "codex_cognitive_runtime_search" in names
        return _result("fatal", ok, tool_count=len(names), fix_hint="Check scripts/codex-cognitive-runtime-mcp and run plugin install again.")
    except Exception as exc:
        return _result("fatal", False, error=str(exc), fix_hint="Run PYTHONPATH=src python3 -m codex_cognitive_runtime.mcp_server to inspect MCP startup errors.")
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _check_model(config: Config) -> dict[str, Any]:
    try:
        result = CodexMiniClient(config).complete_json(
            "Return {\"ok\": true} as JSON.",
            {"ok": "boolean"},
        )
        return _result("fatal", bool(result), result_keys=sorted(result.keys()), fix_hint="Check Codex CLI login, model availability, and CODEX_COGNITIVE_RUNTIME_MODEL.")
    except Exception as exc:
        return _result("fatal", False, error=str(exc), fix_hint="Check Codex CLI login, model availability, and CODEX_COGNITIVE_RUNTIME_MODEL.")


def _codex_version(path: str | None) -> str | None:
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except Exception:
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0][:120] if text else None


def _privacy_report(config: Config) -> dict[str, Any]:
    ledger = Ledger(config.ledger_path)
    try:
        events = ledger.list_events(limit=5)
        return {
            "ledger_path": str(config.ledger_path),
            "store_raw_events": config.store_raw_events,
            "event_storage": "raw" if config.store_raw_events else "sanitized",
            "runtime_observer_enabled": config.enable_runtime_observer,
            "runtime_observation_previews": "stored" if config.store_runtime_observation_previews else "redacted",
            "strict_privacy": config.strict_privacy,
            "trace_live_log": config.trace_live_log,
            "runtime_observation_storage": "commands, file paths, exit code, source fields, output hashes/lengths, and failure flags; stdout/stderr previews only when explicitly enabled",
            "retention_policy": "manual; prune-events only removes events; prune-runtime removes runtime audit records and embedded workflow observation copies; wipe removes the full local Ledger",
            "recent_event_count": len(events),
            "recent_events": [
                {
                    "id": item.get("id"),
                    "event_type": item.get("event_type"),
                    "created_at": item.get("created_at"),
                    "raw_payload_stored": bool((item.get("payload_json") or {}).get("_raw_payload_stored")),
                    "payload_keys": sorted(str(key) for key in (item.get("payload_json") or {}).keys()),
                }
                for item in events
            ],
        }
    finally:
        ledger.close()


def _read_json_line(proc: subprocess.Popen[str]) -> dict[str, Any]:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(stderr.strip() or "mcp server returned no response")
    return json.loads(line)


def _skipped(reason: str) -> dict[str, Any]:
    return _result("info", None, skipped=True, reason=reason)


def _result(level: str, ok: bool | None, **fields: Any) -> dict[str, Any]:
    return {"level": level, "ok": ok, **fields}


def _summary(checks: dict[str, dict[str, Any]]) -> dict[str, int]:
    summary = {"fatal_failed": 0, "warn_failed": 0, "skipped": 0}
    for check in checks.values():
        if check.get("ok") is False and check.get("level") == "fatal":
            summary["fatal_failed"] += 1
        if check.get("ok") is False and check.get("level") == "warn":
            summary["warn_failed"] += 1
        if check.get("skipped"):
            summary["skipped"] += 1
    return summary
