from __future__ import annotations

from dataclasses import dataclass


EVENT_TRANSITIONS = {
    None: {"received"},
    "received": {"processing", "processed", "failed"},
    "processing": {"processed", "failed"},
    "failed": {"processing"},
    "processed": set(),
}

MEMORY_TRANSITIONS = {
    None: {"candidate", "active", "quarantined", "rejected", "superseded", "deleted"},
    "candidate": {"active", "quarantined", "rejected", "superseded", "deleted"},
    "active": {"quarantined", "superseded", "deleted"},
    "quarantined": {"active", "rejected", "deleted"},
    "rejected": {"active", "deleted"},
    "superseded": {"active", "deleted"},
    "deleted": set(),
}

WORKFLOW_TRANSITIONS = {
    None: {"planned"},
    "planned": {"running", "cancelled", "paused"},
    "running": {"completed", "failed", "cancelled", "paused"},
    "paused": {"running", "cancelled"},
    "failed": {"running", "cancelled", "paused"},
    "completed": set(),
    "cancelled": set(),
}

STEP_TRANSITIONS = {
    None: {"pending"},
    "pending": {"ready", "running", "skipped"},
    "ready": {"running", "skipped"},
    "running": {"completed", "failed", "rolled_back"},
    "failed": {"ready", "running", "skipped", "rolled_back"},
    "completed": set(),
    "skipped": {"ready", "running"},
    "rolled_back": set(),
}


TRANSITIONS = {
    "event": EVENT_TRANSITIONS,
    "memory": MEMORY_TRANSITIONS,
    "workflow": WORKFLOW_TRANSITIONS,
    "workflow_step": STEP_TRANSITIONS,
}


@dataclass(frozen=True)
class TransitionResult:
    allowed: bool
    subject_type: str
    previous_state: str | None
    next_state: str
    reason: str


class RuntimeStateMachine:
    def validate(self, subject_type: str, previous_state: str | None, next_state: str) -> TransitionResult:
        if previous_state == next_state:
            return TransitionResult(True, subject_type, previous_state, next_state, "idempotent_refresh")
        transitions = TRANSITIONS.get(subject_type)
        if not transitions:
            return TransitionResult(True, subject_type, previous_state, next_state, "unmanaged_subject_type")
        allowed = next_state in transitions.get(previous_state, set())
        return TransitionResult(
            allowed,
            subject_type,
            previous_state,
            next_state,
            "allowed" if allowed else "invalid_runtime_transition",
        )
