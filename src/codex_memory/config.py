from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MODEL = "gpt-5.4-mini"


@dataclass(frozen=True)
class Config:
    model: str
    state_dir: Path
    ledger_path: Path
    min_active_confidence: float
    min_quarantine_confidence: float
    duplicate_threshold: float
    max_evidence_quote_chars: int
    primary_store: str = "ledger"
    enable_dangerous_mcp_tools: bool = False
    enable_mcp_write_tools: bool = False
    enable_mcp_review_tools: bool = False
    enable_mcp_admin_tools: bool = False
    store_raw_events: bool = False
    enable_experimental_cli: bool = False
    enable_runtime_observer: bool = True
    store_runtime_observation_previews: bool = False
    strict_privacy: bool = False


def _default_state_dir() -> Path:
    return Path(os.environ.get("CODEX_MEMORY_STATE_DIR", "~/.codex-memory")).expanduser()


def load_config() -> Config:
    state_dir = _default_state_dir()
    config_file = state_dir / "config.json"
    data: dict = {}
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

    model = os.environ.get("CODEX_MEMORY_MODEL") or data.get("model") or DEFAULT_MODEL
    ledger_path = Path(data.get("ledger_path") or state_dir / "ledger.sqlite3").expanduser()
    enable_dangerous_mcp_tools = _bool(
        os.environ.get("CODEX_MEMORY_ENABLE_DANGEROUS_MCP_TOOLS"),
        bool(data.get("enable_dangerous_mcp_tools", False)),
    )
    enable_mcp_write_tools = _bool(
        os.environ.get("CODEX_MEMORY_ENABLE_MCP_WRITE_TOOLS"),
        bool(data.get("enable_mcp_write_tools", False)),
    )
    enable_mcp_review_tools = _bool(
        os.environ.get("CODEX_MEMORY_ENABLE_MCP_REVIEW_TOOLS"),
        bool(data.get("enable_mcp_review_tools", False)),
    )
    enable_mcp_admin_tools = _bool(
        os.environ.get("CODEX_MEMORY_ENABLE_MCP_ADMIN_TOOLS"),
        bool(data.get("enable_mcp_admin_tools", False)),
    )
    store_raw_events = _bool(
        os.environ.get("CODEX_MEMORY_STORE_RAW_EVENTS"),
        bool(data.get("store_raw_events", False)),
    )
    enable_experimental_cli = _bool(
        os.environ.get("CODEX_MEMORY_ENABLE_EXPERIMENTAL_CLI"),
        bool(data.get("enable_experimental_cli", False)),
    )
    enable_runtime_observer = _bool(
        os.environ.get("CODEX_MEMORY_ENABLE_RUNTIME_OBSERVER"),
        bool(data.get("enable_runtime_observer", True)),
    )
    store_runtime_observation_previews = _bool(
        os.environ.get("CODEX_MEMORY_STORE_RUNTIME_OBSERVATION_PREVIEWS"),
        bool(data.get("store_runtime_observation_previews", False)),
    )
    strict_privacy = _bool(
        os.environ.get("CODEX_MEMORY_STRICT_PRIVACY"),
        bool(data.get("strict_privacy", False)),
    )

    return Config(
        model=str(model),
        state_dir=state_dir,
        ledger_path=ledger_path,
        min_active_confidence=float(data.get("min_active_confidence", 0.82)),
        min_quarantine_confidence=float(data.get("min_quarantine_confidence", 0.62)),
        duplicate_threshold=float(data.get("duplicate_threshold", 0.9)),
        max_evidence_quote_chars=int(data.get("max_evidence_quote_chars", 500)),
        primary_store="ledger",
        enable_dangerous_mcp_tools=enable_dangerous_mcp_tools,
        enable_mcp_write_tools=enable_dangerous_mcp_tools or enable_mcp_write_tools,
        enable_mcp_review_tools=enable_dangerous_mcp_tools or enable_mcp_review_tools,
        enable_mcp_admin_tools=enable_dangerous_mcp_tools or enable_mcp_admin_tools,
        store_raw_events=store_raw_events,
        enable_experimental_cli=enable_experimental_cli,
        enable_runtime_observer=enable_runtime_observer,
        store_runtime_observation_previews=False if strict_privacy else store_runtime_observation_previews,
        strict_privacy=strict_privacy,
    )


def ensure_state_dir(config: Config) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        config.state_dir.chmod(0o700)
    except OSError:
        pass


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
