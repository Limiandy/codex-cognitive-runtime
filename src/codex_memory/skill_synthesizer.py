from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .taxonomy import tokenize


class SkillSynthesizer:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def synthesize_from_workflow(self, workflow: dict[str, Any]) -> dict[str, Any] | None:
        metadata = dict(workflow.get("metadata_json") or {})
        observations = [dict(item) for item in metadata.get("observations") or []]
        verify_commands = _commands_for_step(observations, "execute_and_verify")
        if not verify_commands:
            return None
        if "execute_and_verify" not in set(metadata.get("completed_steps") or []):
            return None
        workflow_id = str(workflow.get("id") or "")
        if not workflow_id:
            return None
        source_memory_ids = _related_experience_memory_ids(
            self.ledger,
            cwd=metadata.get("cwd"),
            session_id=metadata.get("session_id"),
        )
        inspect_commands = _commands_for_step(observations, "inspect_repository")
        changed_files = _changed_files(observations)
        user_goal = str(metadata.get("user_goal") or "")
        procedure = _procedure(inspect_commands, changed_files, verify_commands)
        title = _title(user_goal, verify_commands[-1])
        skill_metadata = {
            "version": 1,
            "skill_type": "dynamic_skill",
            "title": title,
            "trigger": _triggers(user_goal, verify_commands),
            "preconditions": _preconditions(metadata, inspect_commands),
            "procedure": procedure,
            "verification": verify_commands[:5],
            "anti_patterns": [
                "Do not claim completion before inspecting repository context.",
                "Do not claim completion after code changes without verification evidence.",
                "Do not treat failed verification output as success.",
            ],
            "source_workflow_ids": [workflow_id],
            "source_memory_ids": source_memory_ids,
            "source_recipe_ids": [],
            "task_type": metadata.get("task_type"),
            "project_key": metadata.get("project_key"),
            "files_changed": changed_files[:50],
            "success_count": 1,
            "failure_count": 0,
            "reuse_count": 0,
            "last_used_at": None,
            "review_required": True,
            "created_at": _utc_now(),
        }
        content = _content(title, procedure, verify_commands)
        record = self.ledger.record_cognitive_record(
            "skill",
            "dynamic_skill",
            f"dynamic_skill:{workflow_id}",
            content,
            "candidate",
            "project" if metadata.get("project_key") else "session",
            domain="software_engineering",
            category="workflow",
            subcategory="dynamic_skill",
            confidence=0.8,
            importance=0.78,
            strength=1.02,
            project_key=metadata.get("project_key"),
            session_id=metadata.get("session_id"),
            metadata=skill_metadata,
            source_kind="skill_synthesizer",
        )
        self.ledger.upsert_cognitive_edge(str(record["id"]), workflow_id, "derived_from", 0.9, {"source": "skill_synthesizer"})
        for memory_id in source_memory_ids:
            self.ledger.upsert_cognitive_edge(str(record["id"]), memory_id, "uses_experience", 0.72, {"source": "skill_synthesizer"})
        return record


def _commands_for_step(observations: list[dict[str, Any]], step_id: str) -> list[str]:
    commands = []
    seen = set()
    for observation in observations:
        if observation.get("matched_step_id") != step_id:
            continue
        command = str(observation.get("command") or "").strip()
        if not command or command in seen:
            continue
        seen.add(command)
        commands.append(command)
    return commands


def _changed_files(observations: list[dict[str, Any]]) -> list[str]:
    files = []
    seen = set()
    for observation in observations:
        summary = observation.get("summary") or {}
        for path in summary.get("files_changed") or []:
            text = str(path or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            files.append(text)
    return files


def _related_experience_memory_ids(ledger: Any, cwd: str | None, session_id: str | None) -> list[str]:
    ids = []
    for memory in ledger.list_recallable_memories(cwd=cwd, session_id=session_id, limit=100):
        if memory.get("memory_type") != "experience":
            continue
        memory_id = str(memory.get("id") or "")
        if memory_id:
            ids.append(memory_id)
    return ids[:20]


def _procedure(inspect_commands: list[str], changed_files: list[str], verify_commands: list[str]) -> list[str]:
    steps = []
    if inspect_commands:
        steps.append(f"Inspect repository context with `{inspect_commands[0]}`.")
    else:
        steps.append("Inspect repository context before changing code.")
    if changed_files:
        steps.append("Make a minimal code change and keep the touched files focused: " + ", ".join(changed_files[:5]) + ".")
    else:
        steps.append("Make the smallest code change that satisfies the task.")
    steps.append(f"Verify the result with `{verify_commands[-1]}`.")
    steps.append("Report verification evidence or state exactly why verification could not be completed.")
    return steps


def _preconditions(metadata: dict[str, Any], inspect_commands: list[str]) -> list[str]:
    preconditions = ["Task involves a software engineering change or debugging workflow."]
    if metadata.get("project_key"):
        preconditions.append("Workflow is scoped to the same local project.")
    if inspect_commands:
        preconditions.append("Repository context can be inspected before edits.")
    return preconditions


def _triggers(user_goal: str, verify_commands: list[str]) -> list[str]:
    tokens = [token for token in tokenize(user_goal) if len(token) >= 3]
    command_tokens = [token for command in verify_commands for token in tokenize(command) if len(token) >= 3]
    ordered = []
    for token in [*tokens, *command_tokens]:
        if token not in ordered:
            ordered.append(token)
    return ordered[:12]


def _title(user_goal: str, verify_command: str) -> str:
    if "unittest" in verify_command:
        return "Python unittest change workflow"
    if "pytest" in verify_command:
        return "Pytest change workflow"
    if "npm" in verify_command or "pnpm" in verify_command or "yarn" in verify_command:
        return "JavaScript verification workflow"
    goal = " ".join(user_goal.split())[:60]
    return f"Verified software change workflow: {goal}" if goal else "Verified software change workflow"


def _content(title: str, procedure: list[str], verify_commands: list[str]) -> str:
    return (
        f"Dynamic skill: {title}. "
        f"Procedure: {' '.join(procedure[:3])} "
        f"Verification: {' && '.join(verify_commands[:3])}."
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
