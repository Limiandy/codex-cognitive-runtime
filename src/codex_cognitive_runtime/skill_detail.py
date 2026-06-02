from __future__ import annotations

from typing import Any

from .security import redact_secrets


def public_skill_record(record: dict[str, Any]) -> dict[str, Any]:
    detail = public_skill_detail(record)
    sanitized = {**record, "project_key": None, "public_detail": detail}
    metadata = _public_metadata(record)
    if metadata is not None:
        sanitized["metadata_json"] = metadata
    return sanitized


def _public_metadata(record: dict[str, Any]) -> dict[str, Any] | None:
    metadata = dict(record.get("metadata_json") or {})
    if record.get("layer") == "runtime_skill" and record.get("record_type") == "injection":
        metadata.pop("prompt_preview", None)
        metadata.pop("cwd", None)
        metadata.pop("project_key", None)
        return metadata
    if record.get("record_type") == "dynamic_skill":
        for key in ("project_key", "files_changed", "source_memory_ids"):
            metadata.pop(key, None)
        return metadata
    if record.get("record_type") == "verification_recipe":
        for key in ("project_key", "files_changed", "verification_stdout_preview", "created_from_observations"):
            metadata.pop(key, None)
        return metadata
    return metadata if metadata else None


def public_skill_detail(record: dict[str, Any]) -> dict[str, Any]:
    record_type = str(record.get("record_type") or "")
    if record_type == "seed_skill":
        return _seed_skill_detail(record)
    if record_type == "dynamic_skill":
        return _dynamic_skill_detail(record)
    if record_type == "verification_recipe":
        return _verification_recipe_detail(record)
    if record.get("layer") == "runtime_skill" and record_type == "injection":
        return _runtime_skill_detail(record)
    return _generic_detail(record)


def _seed_skill_detail(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata_json") or {}
    name = str(metadata.get("name") or record.get("content") or "Seed Skill")
    description = str(metadata.get("description") or "")
    content = str(record.get("content") or "")
    markdown = content if content.lstrip().startswith("---") or content.lstrip().startswith("#") else _markdown(
        name,
        [
            ("Purpose", [description or "General reusable seed skill guidance."]),
            ("Source", [str(metadata.get("source_path") or "external seed skill")]),
            ("Usage", ["Use as general guidance only; current user request and reviewed memory take priority."]),
        ],
    )
    return {
        "kind": "seed_skill",
        "title": name,
        "summary": description or _first_sentence(content) or name,
        "markdown": _safe(markdown, 12000),
        "privacy": _privacy_note("Public seed skill content; no local prompt or session data is included."),
    }


def _dynamic_skill_detail(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata_json") or {}
    title = str(metadata.get("title") or record.get("content") or "Dynamic Skill")
    triggers = _string_list(metadata.get("trigger"))
    preconditions = _string_list(metadata.get("preconditions"))
    procedure = _string_list(metadata.get("procedure"))
    verification = _string_list(metadata.get("verification"))
    anti_patterns = _string_list(metadata.get("anti_patterns"))
    markdown = _markdown(
        title,
        [
            ("When To Use", triggers or ["Use when the current task matches this learned workflow pattern."]),
            ("Preconditions", preconditions or ["Apply only when the task context matches the learned workflow scope."]),
            ("Workflow", procedure or [_safe(record.get("content"), 500)]),
            ("Verification", verification or ["Run the relevant verification before claiming completion."]),
            ("Avoid", anti_patterns or ["Do not apply this skill when the task context does not match."]),
            ("Review State", [f"status={record.get('status')}", f"success={metadata.get('success_count') or 0}", f"failure={metadata.get('failure_count') or 0}"]),
        ],
    )
    return {
        "kind": "dynamic_skill",
        "title": title,
        "summary": _safe(" ".join(procedure[:2]) or record.get("content") or title, 260),
        "markdown": markdown,
        "privacy": _privacy_note("Generated from reviewed workflow structure; raw prompt, cwd, file paths, and stdout previews are not included."),
    }


def _runtime_skill_detail(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata_json") or {}
    skill = metadata.get("skill") if isinstance(metadata.get("skill"), dict) else {}
    title = str(skill.get("name") or record.get("content") or "Runtime Skill")
    strategy = _string_list(skill.get("strategy"))
    avoid = _string_list(skill.get("avoid"))
    first_action = skill.get("first_action") if isinstance(skill.get("first_action"), dict) else {}
    basis = []
    if skill.get("memory_basis_ids"):
        basis.append("Reviewed memory basis was used.")
    if skill.get("durable_skill_ids"):
        basis.append("Active durable skill basis was used.")
    if skill.get("seed_skill_ids"):
        basis.append("Public seed skill basis was used as fallback guidance.")
    markdown = _markdown(
        title,
        [
            ("Applies To", [str(skill.get("applies_to") or "Current task-specific runtime guidance.")]),
            ("Goal", [str(skill.get("goal") or "Guide the current request with reviewed context.")]),
            ("Strategy", strategy or ["Use the reviewed runtime guidance for this turn."]),
            ("First Action", [_first_action(first_action)]),
            ("Basis", basis or ["No private basis details are exposed in this public view."]),
            ("Avoid", avoid or ["Do not invent missing facts."]),
        ],
    )
    return {
        "kind": "runtime_skill",
        "title": title,
        "summary": _safe(str(skill.get("goal") or record.get("content") or title), 260),
        "markdown": markdown,
        "privacy": _privacy_note("Runtime prompt preview, cwd, and project key are stripped from this view."),
    }


def _verification_recipe_detail(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata_json") or {}
    recipe = _string_list(metadata.get("recipe"))
    title = str(record.get("content") or "Verification Recipe")
    markdown = _markdown(
        title,
        [
            ("When To Use", ["Use after code changes in a matching project or task type."]),
            ("Commands", recipe or ["No command captured."]),
            ("Success Criteria", ["Command exits successfully and output does not indicate failure."]),
            ("Avoid", ["Do not treat this recipe as proof for unrelated projects or tasks."]),
        ],
    )
    return {
        "kind": "verification_recipe",
        "title": title,
        "summary": _safe(" && ".join(recipe[:3]) or title, 260),
        "markdown": markdown,
        "privacy": _privacy_note("Commands are shown, but stdout previews and touched file lists are omitted."),
    }


def _generic_detail(record: dict[str, Any]) -> dict[str, Any]:
    title = str(record.get("content") or record.get("id") or "Record")
    return {"kind": str(record.get("record_type") or "record"), "title": title, "summary": _safe(title, 260), "markdown": _markdown(title, [("Status", [str(record.get("status") or "unknown")])]), "privacy": _privacy_note("No raw runtime payload is included.")}


def _markdown(title: str, sections: list[tuple[str, list[str]]]) -> str:
    lines = [f"# {_safe(title, 160)}", ""]
    for heading, items in sections:
        clean_items = [_safe(item, 500) for item in items if _safe(item, 500)]
        if not clean_items:
            continue
        lines.extend([f"## {heading}", ""])
        lines.extend(f"- {item}" for item in clean_items)
        lines.append("")
    return "\n".join(lines).strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_safe(item, 500) for item in value if _safe(item, 500)]


def _safe(value: Any, limit: int) -> str:
    return str(redact_secrets(value or "")).strip()[:limit]


def _first_sentence(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        return ""
    for sep in (". ", "。", "\n"):
        if sep in text:
            return text.split(sep, 1)[0].strip()
    return text[:220]


def _first_action(action: dict[str, Any]) -> str:
    action_type = str(action.get("type") or "proceed_or_clarify")
    questions = _string_list(action.get("questions"))
    return action_type + (": " + " | ".join(questions[:6]) if questions else "")


def _privacy_note(text: str) -> dict[str, Any]:
    return {"safe_for_ui": True, "note": text}
