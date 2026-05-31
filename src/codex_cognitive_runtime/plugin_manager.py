from __future__ import annotations

import difflib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HOME = Path.home()
MARKETPLACE_ROOT = HOME
MARKETPLACE_NAME = "personal"
MARKETPLACE_PATH = HOME / ".agents" / "plugins" / "marketplace.json"
PLUGIN_NAME = "codex-cognitive-runtime"
LEGACY_PLUGIN_NAMES = ("codex-memory",)
PLUGIN_CONFIG_KEY = f'{PLUGIN_NAME}@{MARKETPLACE_NAME}'
PLUGIN_INSTALL_PATH = HOME / "plugins" / PLUGIN_NAME
CODEX_CONFIG = HOME / ".codex" / "config.toml"


@dataclass
class PluginState:
    marketplace_present: bool
    marketplace_policy: str | None
    installed_path_exists: bool
    codex_marketplace_enabled: bool
    codex_plugin_enabled: bool | None

    def to_dict(self) -> dict:
        status = "not_installed"
        if self.marketplace_present and self.installed_path_exists:
            if self.marketplace_policy == "NOT_AVAILABLE":
                status = "disabled_by_marketplace"
            elif self.codex_plugin_enabled is False:
                status = "off"
            elif self.codex_plugin_enabled is True:
                status = "on"
            else:
                status = "published"
        return {
            "status": status,
            "marketplace": MARKETPLACE_NAME,
            "marketplace_path": str(MARKETPLACE_PATH),
            "plugin_key": PLUGIN_CONFIG_KEY,
            "install_path": str(PLUGIN_INSTALL_PATH),
            "marketplace_present": self.marketplace_present,
            "marketplace_policy": self.marketplace_policy,
            "installed_path_exists": self.installed_path_exists,
            "codex_marketplace_enabled": self.codex_marketplace_enabled,
            "codex_plugin_enabled": self.codex_plugin_enabled,
        }


def status() -> dict:
    market = _read_marketplace()
    entry = _find_entry(market)
    config = _read_config()
    return PluginState(
        marketplace_present=entry is not None,
        marketplace_policy=(entry or {}).get("policy", {}).get("installation"),
        installed_path_exists=PLUGIN_INSTALL_PATH.is_dir(),
        codex_marketplace_enabled=f"[marketplaces.{MARKETPLACE_NAME}]" in config,
        codex_plugin_enabled=_plugin_enabled(config),
    ).to_dict()


def install(source_path: Path, dry_run: bool = False, show_diff: bool = False) -> dict:
    source_path = source_path.expanduser().resolve()
    if not (source_path / ".codex-plugin" / "plugin.json").is_file():
        raise RuntimeError(f"not a Codex plugin path: {source_path}")
    plan = _install_plan(source_path, enabled=True, show_diff=show_diff)
    if dry_run:
        return plan
    same_path = _same_path(source_path, PLUGIN_INSTALL_PATH)
    PLUGIN_INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_backup: Path | None = None
    marketplace_backup: dict | None = _read_marketplace()
    installed_backup = None if same_path else _backup_installed_plugin()
    if PLUGIN_INSTALL_PATH.exists() and not same_path:
        shutil.rmtree(PLUGIN_INSTALL_PATH)
    try:
        if not same_path:
            ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store")
            shutil.copytree(source_path, PLUGIN_INSTALL_PATH, ignore=ignore)
        _write_marketplace("AVAILABLE")
        config_backup = _upsert_config(enabled=True)
        result = status()
        result["backup_path"] = str(config_backup) if config_backup else None
        return result
    except Exception:
        _restore_after_failed_install(config_backup, marketplace_backup, installed_backup, remove_install_path=not same_path)
        raise
    finally:
        if installed_backup and installed_backup.exists():
            shutil.rmtree(installed_backup, ignore_errors=True)


def enable() -> dict:
    _write_marketplace("AVAILABLE")
    _upsert_config(enabled=True)
    return status()


def disable() -> dict:
    _upsert_config(enabled=False)
    return status()


def block() -> dict:
    _write_marketplace("NOT_AVAILABLE")
    _upsert_config(enabled=False)
    return status()


def uninstall(delete_files: bool = False, dry_run: bool = False, show_diff: bool = False) -> dict:
    if dry_run:
        return _uninstall_plan(delete_files=delete_files, show_diff=show_diff)
    market = _read_marketplace()
    market["plugins"] = [
        entry for entry in market.get("plugins", []) if entry.get("name") not in {PLUGIN_NAME, *LEGACY_PLUGIN_NAMES}
    ]
    _save_marketplace(market)
    _remove_plugin_config()
    if delete_files and PLUGIN_INSTALL_PATH.exists():
        shutil.rmtree(PLUGIN_INSTALL_PATH)
    return status()


def _read_marketplace() -> dict:
    if MARKETPLACE_PATH.is_file():
        try:
            return json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"name": MARKETPLACE_NAME, "interface": {"displayName": "Personal"}, "plugins": []}


def _save_marketplace(data: dict) -> None:
    MARKETPLACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKETPLACE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_marketplace(policy: str) -> None:
    market = _read_marketplace()
    market["name"] = MARKETPLACE_NAME
    market.setdefault("interface", {}).setdefault("displayName", "Personal")
    plugins = [entry for entry in market.get("plugins", []) if entry.get("name") not in {PLUGIN_NAME, *LEGACY_PLUGIN_NAMES}]
    plugins.append(
        {
            "name": PLUGIN_NAME,
            "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
            "policy": {"installation": policy, "authentication": "ON_INSTALL"},
            "category": "Productivity",
        }
    )
    market["plugins"] = plugins
    _save_marketplace(market)


def _find_entry(market: dict) -> dict | None:
    for entry in market.get("plugins", []):
        if entry.get("name") == PLUGIN_NAME:
            return entry
    return None


def _read_config() -> str:
    if not CODEX_CONFIG.is_file():
        return ""
    return CODEX_CONFIG.read_text(encoding="utf-8")


def _write_config(text: str) -> Path | None:
    _validate_toml(text)
    CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if CODEX_CONFIG.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = CODEX_CONFIG.with_suffix(f".toml.codex-cognitive-runtime-{stamp}.bak")
        shutil.copy2(CODEX_CONFIG, backup)
    CODEX_CONFIG.write_text(text, encoding="utf-8")
    return backup


def _upsert_config(enabled: bool) -> Path | None:
    text = _read_config()
    new_text = _build_config_text(text, enabled=enabled)
    return _write_config(new_text)


def _build_config_text(text: str, enabled: bool) -> str:
    _validate_toml(text)
    text = _remove_section(text, f"marketplaces.{MARKETPLACE_NAME}")
    text = _remove_section(text, f'plugins."{PLUGIN_CONFIG_KEY}"')
    for legacy_name in LEGACY_PLUGIN_NAMES:
        text = _remove_section(text, f'plugins."{legacy_name}@{MARKETPLACE_NAME}"')
    block_text = (
        f'\n[marketplaces.{MARKETPLACE_NAME}]\n'
        f'last_updated = "{datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}"\n'
        f'source_type = "local"\n'
        f'source = {json.dumps(str(MARKETPLACE_ROOT))}\n'
        f'\n[plugins."{PLUGIN_CONFIG_KEY}"]\n'
        f"enabled = {str(enabled).lower()}\n"
    )
    new_text = text.rstrip() + "\n" + block_text
    _validate_toml(new_text)
    return new_text


def _remove_plugin_config() -> None:
    text = _read_config()
    _validate_toml(text)
    text = _remove_section(text, f'plugins."{PLUGIN_CONFIG_KEY}"')
    for legacy_name in LEGACY_PLUGIN_NAMES:
        text = _remove_section(text, f'plugins."{legacy_name}@{MARKETPLACE_NAME}"')
    _write_config(text.rstrip() + "\n")


def _plugin_enabled(config: str) -> bool | None:
    section = _extract_section(config, f'plugins."{PLUGIN_CONFIG_KEY}"')
    if section is None:
        return None
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("enabled"):
            return stripped.split("=", 1)[1].strip().lower() == "true"
    return None


def _remove_section(text: str, name: str) -> str:
    lines = text.splitlines()
    out = []
    i = 0
    header = f"[{name}]"
    while i < len(lines):
        if lines[i].strip() == header:
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("["):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).rstrip() + ("\n" if out else "")


def _extract_section(text: str, name: str) -> str | None:
    lines = text.splitlines()
    header = f"[{name}]"
    for idx, line in enumerate(lines):
        if line.strip() == header:
            section = [line]
            for next_line in lines[idx + 1 :]:
                if next_line.lstrip().startswith("["):
                    break
                section.append(next_line)
            return "\n".join(section)
    return None


def _install_plan(source_path: Path, enabled: bool, show_diff: bool) -> dict:
    before = _read_config()
    after = _build_config_text(before, enabled=enabled)
    plan = {
        "dry_run": True,
        "action": "install",
        "source_path": str(source_path),
        "install_path": str(PLUGIN_INSTALL_PATH),
        "marketplace_path": str(MARKETPLACE_PATH),
        "codex_config": str(CODEX_CONFIG),
        "will_copy_files": not _same_path(source_path, PLUGIN_INSTALL_PATH),
        "will_write_marketplace": True,
        "will_write_codex_config": before != after,
        "toml_valid": True,
    }
    if show_diff:
        plan["config_diff"] = _diff(before, after)
    return plan


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return left.expanduser().absolute() == right.expanduser().absolute()


def _uninstall_plan(delete_files: bool, show_diff: bool) -> dict:
    before = _read_config()
    _validate_toml(before)
    after = _remove_section(before, f'plugins."{PLUGIN_CONFIG_KEY}"').rstrip() + "\n"
    _validate_toml(after)
    plan = {
        "dry_run": True,
        "action": "uninstall",
        "install_path": str(PLUGIN_INSTALL_PATH),
        "marketplace_path": str(MARKETPLACE_PATH),
        "codex_config": str(CODEX_CONFIG),
        "will_delete_files": bool(delete_files and PLUGIN_INSTALL_PATH.exists()),
        "will_write_marketplace": True,
        "will_write_codex_config": before != after,
        "toml_valid": True,
    }
    if show_diff:
        plan["config_diff"] = _diff(before, after)
    return plan


def _diff(before: str, after: str) -> list[str]:
    return list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=str(CODEX_CONFIG) + ".before",
            tofile=str(CODEX_CONFIG) + ".after",
            lineterm="",
        )
    )


def _validate_toml(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        import tomllib  # type: ignore
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("TOML parser unavailable; install tomli or use Python 3.11+.") from exc
    try:
        return tomllib.loads(text)
    except Exception as exc:
        raise RuntimeError(f"invalid TOML config: {exc}") from exc


def _backup_installed_plugin() -> Path | None:
    if not PLUGIN_INSTALL_PATH.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = PLUGIN_INSTALL_PATH.with_name(f"{PLUGIN_INSTALL_PATH.name}.codex-cognitive-runtime-{stamp}.bak")
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(PLUGIN_INSTALL_PATH, backup)
    return backup


def _restore_after_failed_install(
    config_backup: Path | None,
    marketplace_backup: dict | None,
    installed_backup: Path | None,
    remove_install_path: bool,
) -> None:
    if config_backup and config_backup.exists():
        CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config_backup, CODEX_CONFIG)
    if marketplace_backup is not None:
        _save_marketplace(marketplace_backup)
    if remove_install_path and PLUGIN_INSTALL_PATH.exists():
        shutil.rmtree(PLUGIN_INSTALL_PATH, ignore_errors=True)
    if installed_backup and installed_backup.exists():
        shutil.copytree(installed_backup, PLUGIN_INSTALL_PATH)
