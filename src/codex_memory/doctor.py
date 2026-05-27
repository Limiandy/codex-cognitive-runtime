from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .config import Config, ensure_state_dir
from .ledger import Ledger
from .model_client import CodexMiniClient


def run_doctor(config: Config, model_check: bool = False) -> dict[str, Any]:
    root = plugin_root()
    checks = {
        "plugin_root": _check_plugin_root(root),
        "state_dir": _check_state_dir(config),
        "sqlite_ledger": _check_sqlite_ledger(config),
        "codex_cli": _check_codex_cli(),
        "raw_event_storage": _check_raw_event_storage(config),
        "mcp_config_portable": _check_config_portable(root / ".mcp.json"),
        "hooks_config": _check_hooks_config(root / "hooks.json"),
        "mcp_server": _check_mcp_server(config),
        "model_smoke": _check_model(config) if model_check else _skipped("pass --model-check to run a model smoke test"),
    }
    return {
        "ok": all(item.get("ok") is not False for item in checks.values() if item.get("level") == "fatal"),
        "summary": _summary(checks),
        "checks": checks,
    }


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
        fix_hint="Run doctor from a valid codex-memory checkout or reinstall the plugin.",
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
        return _result("fatal", False, path=str(config.ledger_path), error=str(exc), fix_hint="Check SQLite file permissions or move CODEX_MEMORY_STATE_DIR to a writable path.")


def _check_codex_cli() -> dict[str, Any]:
    path = shutil.which("codex")
    return _result(
        "fatal",
        bool(path),
        path=path,
        impact="required for model-backed memory extraction",
        fix_hint="Install and log in to the Codex CLI, then rerun doctor.",
    )


def _check_raw_event_storage(config: Config) -> dict[str, Any]:
    if config.store_raw_events:
        return _result(
            "warn",
            False,
            enabled=True,
            impact="raw event payloads are stored in the local Ledger",
            fix_hint="Unset CODEX_MEMORY_STORE_RAW_EVENTS or set store_raw_events=false.",
        )
    return _result("info", True, enabled=False)


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
    env["CODEX_MEMORY_STATE_DIR"] = str(config.state_dir)
    proc = subprocess.Popen(
        [sys.executable, "-m", "codex_memory.mcp_server"],
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
        ok = first.get("result", {}).get("serverInfo", {}).get("name") == "codex-memory" and "codex_memory_search" in names
        return _result("fatal", ok, tool_count=len(names), fix_hint="Check scripts/codex-memory-mcp and run plugin install again.")
    except Exception as exc:
        return _result("fatal", False, error=str(exc), fix_hint="Run PYTHONPATH=src python3 -m codex_memory.mcp_server to inspect MCP startup errors.")
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
        return _result("fatal", bool(result), result_keys=sorted(result.keys()), fix_hint="Check Codex CLI login, model availability, and CODEX_MEMORY_MODEL.")
    except Exception as exc:
        return _result("fatal", False, error=str(exc), fix_hint="Check Codex CLI login, model availability, and CODEX_MEMORY_MODEL.")


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
