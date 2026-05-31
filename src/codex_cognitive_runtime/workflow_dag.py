from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkflowStep:
    id: str
    name: str
    kind: str
    depends_on: list[str] = field(default_factory=list)
    state: str = "pending"
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    rollback: str | None = None
    policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "depends_on": self.depends_on,
            "state": self.state,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "rollback": self.rollback,
            "policy": self.policy,
        }


@dataclass
class WorkflowDAG:
    id: str
    steps: list[WorkflowStep]

    def ready_steps(self, completed: set[str]) -> list[WorkflowStep]:
        return [
            step
            for step in self.steps
            if step.state == "pending" and all(dep in completed for dep in step.depends_on)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "steps": [step.to_dict() for step in self.steps]}


class WorkflowExecutor:
    def __init__(self, runtime: Any):
        self.runtime = runtime

    def execute(self, dag: WorkflowDAG, fail_step: str | None = None) -> dict[str, Any]:
        completed: set[str] = {step.id for step in dag.steps if step.state == "completed"}
        executed = []
        self.runtime.transition("workflow", dag.id, "running", metadata={"step_count": len(dag.steps)})
        while True:
            ready = dag.ready_steps(completed)
            if not ready:
                break
            for step in ready:
                self._run_step(dag.id, step, fail_step=fail_step)
                if step.state == "completed":
                    completed.add(step.id)
                executed.append(step.to_dict())
                if step.state == "failed":
                    self._skip_dependents(dag, step.id)
                    self.runtime.transition("workflow", dag.id, "failed", metadata={"failed_step_id": step.id})
                    return {"workflow_state": "failed", "executed_steps": executed, "dag": dag.to_dict()}
        if len(completed) == len(dag.steps):
            self.runtime.transition("workflow", dag.id, "completed", metadata={"step_count": len(executed)})
            return {"workflow_state": "completed", "executed_steps": executed, "dag": dag.to_dict()}
        self.runtime.transition("workflow", dag.id, "failed", metadata={"reason": "no_ready_steps", "completed": sorted(completed)})
        return {"workflow_state": "failed", "executed_steps": executed, "dag": dag.to_dict()}

    def resume(self, dag: WorkflowDAG) -> dict[str, Any]:
        for step in dag.steps:
            if step.state in {"failed", "skipped"}:
                step.state = "pending"
        return self.execute(dag)

    def cancel(self, dag: WorkflowDAG) -> dict[str, Any]:
        self.runtime.transition("workflow", dag.id, "cancelled", metadata={"reason": "manual_cancel"})
        return {"workflow_state": "cancelled", "dag": dag.to_dict()}

    def _run_step(self, workflow_id: str, step: WorkflowStep, fail_step: str | None = None) -> None:
        self.runtime.transition("workflow_step", step.id, "ready", metadata={"workflow_id": workflow_id})
        self.runtime.transition("workflow_step", step.id, "running", metadata={"workflow_id": workflow_id})
        if fail_step and step.name == fail_step:
            step.state = "failed"
            step.outputs = {"error": "simulated_step_failure"}
            self.runtime.transition("workflow_step", step.id, "failed", metadata={"workflow_id": workflow_id, "error": "simulated_step_failure"})
            return
        step.state = "completed"
        step.outputs = {"ok": True}
        self.runtime.transition("workflow_step", step.id, "completed", metadata={"workflow_id": workflow_id})

    def _skip_dependents(self, dag: WorkflowDAG, failed_step_id: str) -> None:
        for step in dag.steps:
            if failed_step_id in step.depends_on and step.state == "pending":
                step.state = "skipped"
                self.runtime.transition("workflow_step", step.id, "skipped", metadata={"workflow_id": dag.id, "failed_dependency": failed_step_id})


def build_dag(workflow_id: str, steps: list[dict[str, Any]], policy: dict[str, Any]) -> WorkflowDAG:
    dag_steps = []
    previous_id = None
    for index, step in enumerate(steps):
        step_id = f"{workflow_id}:step:{index}:{step['name']}"
        depends_on = [] if previous_id is None else [previous_id]
        dag_steps.append(
            WorkflowStep(
                id=step_id,
                name=str(step["name"]),
                kind=str(step.get("kind") or _kind_for(str(step["name"]))),
                depends_on=depends_on,
                inputs={"reason": step.get("reason")},
                rollback=_rollback_for(str(step["name"])),
                policy=policy,
            )
        )
        previous_id = step_id
    return WorkflowDAG(id=workflow_id, steps=dag_steps)


def _kind_for(name: str) -> str:
    if "verify" in name or "test" in name:
        return "verification"
    if "inspect" in name or "read" in name:
        return "inspection"
    if "apply" in name or "select" in name:
        return "planning"
    return "execution"


def _rollback_for(name: str) -> str | None:
    if "execute" in name:
        return "audit_and_mark_failed"
    return None
