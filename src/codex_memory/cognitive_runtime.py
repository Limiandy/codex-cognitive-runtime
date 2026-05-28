from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from .ontology import cognitive_layer_for_memory, ontology_snapshot
from .observation import normalize_tool_observation
from .recall import MemoryRecall
from .reasoning_policy import ReasoningPolicyEngine
from .security import redact_secrets, summarize_payload
from .skill_synthesizer import SkillSynthesizer
from .state_machine import RuntimeStateMachine
from .taxonomy import classify, near_duplicate_text, tokenize
from .workflow_dag import WorkflowDAG, WorkflowExecutor, WorkflowStep, build_dag


class CognitiveRuntime:
    def __init__(self, ledger: Any, store_observation_previews: bool = False, strict_privacy: bool = False):
        self.ledger = ledger
        self.store_observation_previews = store_observation_previews
        self.strict_privacy = strict_privacy
        self.state = RuntimeStateMachine()
        self.reasoning = ReasoningPolicyEngine()

    def begin_event(self, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.transition("event", event_id, "received", event_id=event_id, metadata={"event_type": event_type})
        self.transition("event", event_id, "processing", event_id=event_id, metadata={"event_type": event_type})
        payload_summary = summarize_payload(payload)
        self.ledger.record_cognitive_record(
            "audit",
            "event",
            event_id,
            f"{event_type}: payload_summary={payload_summary}",
            "active",
            "session",
            metadata={"event_type": event_type, "payload_summary": payload_summary},
            source_kind="event",
        )

    def finish_event(self, event_id: str, result: dict[str, Any]) -> None:
        self.transition("event", event_id, "processed", event_id=event_id, metadata={"result": result})

    def fail_event(self, event_id: str, error: str) -> None:
        self.transition("event", event_id, "failed", event_id=event_id, metadata={"error": error[:500]})

    def transition(
        self,
        subject_type: str,
        subject_id: str,
        next_state: str,
        event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = self.ledger.latest_state_for(subject_type, subject_id)
        verdict = self.state.validate(subject_type, previous, next_state)
        payload = {**(metadata or {}), "state_machine": verdict.__dict__}
        if not verdict.allowed:
            self.ledger.record_cognitive_record(
                "audit",
                "invalid_state_transition",
                f"{subject_type}:{subject_id}:{previous}:{next_state}",
                f"{subject_type} {subject_id}: {previous} -> {next_state}",
                "active",
                "session",
                importance=0.9,
                metadata=payload,
                source_kind="state_machine",
            )
            raise ValueError(f"invalid {subject_type} transition: {previous} -> {next_state}")
        transition_id = self.ledger.record_state_transition(subject_type, subject_id, next_state, previous, event_id, payload)
        return {"transition_id": transition_id, "previous_state": previous, "state": next_state}

    def sync_memory(self, memory_id: str) -> dict[str, Any] | None:
        memory = self.ledger.get_memory(memory_id)
        if not memory:
            return None
        layer = cognitive_layer_for_memory(memory)
        record = self.ledger.record_cognitive_record(
            layer,
            str(memory.get("memory_type") or "memory"),
            memory_id,
            str(memory.get("content") or ""),
            str(memory.get("status") or "candidate"),
            str(memory.get("scope") or "session"),
            domain=memory.get("domain"),
            category=memory.get("category"),
            subcategory=memory.get("subcategory"),
            confidence=float(memory.get("confidence") or 0),
            importance=float(memory.get("importance") or 0),
            strength=float(memory.get("strength") or 1),
            project_key=memory.get("project_key"),
            session_id=memory.get("source_session_id"),
            metadata={
                "ttl": memory.get("ttl"),
                "triggers": memory.get("triggers_json") or [],
                "review": memory.get("review_json") or {},
            },
            source_kind="memory",
        )
        self.transition("memory", memory_id, str(memory.get("status") or "candidate"), metadata={"layer": layer})
        self._sync_memory_edges(memory)
        if layer == "skill":
            self._materialize_skill(memory)
        if layer == "knowledge":
            self._materialize_knowledge(memory)
        return record

    def sync_all_active(self) -> dict[str, Any]:
        synced = []
        for memory in self.ledger.list_memories(status="active", limit=200):
            record = self.sync_memory(str(memory["id"]))
            if record:
                synced.append(record["id"])
        return {"synced_count": len(synced), "record_ids": synced}

    def start_task_from_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt") or payload.get("text") or "")
        session_id = _optional_str(payload.get("session_id"))
        turn_id = _optional_str(payload.get("turn_id"))
        cwd = _optional_str(payload.get("cwd"))
        if not _is_engineering_task(prompt):
            return {"started": False, "reason": "not_engineering_task"}
        active = self.active_workflow_for_session(session_id=session_id, turn_id=turn_id, cwd=cwd)
        if active:
            return {"started": False, "workflow_id": active["id"], "reason": "active_workflow_exists"}
        steps = _observed_workflow_steps()
        metadata = {
            "runtime_kind": "observed_workflow",
            "user_goal": prompt,
            "session_id": session_id,
            "turn_id": turn_id,
            "cwd": cwd,
            "project_key": _project_key(cwd),
            "task_type": "software_engineering",
            "risk_level": "medium",
            "steps": steps,
            "observations": [],
            "completed_steps": ["read_context", "recall_memory"],
            "changed": False,
            "verified": False,
            "test_failed": False,
        }
        workflow = self.ledger.record_cognitive_record(
            "workflow",
            "observed_workflow",
            None,
            "Observed workflow: " + " -> ".join(step["name"] for step in steps),
            "active",
            "session",
            domain="software_engineering",
            category="workflow",
            subcategory="observed_runtime",
            confidence=0.9,
            importance=0.86,
            session_id=session_id,
            project_key=_project_key(cwd),
            metadata=metadata,
            source_kind="runtime_observer",
        )
        workflow_id = str(workflow["id"])
        self.transition("workflow", workflow_id, "planned", metadata={"runtime_kind": "observed_workflow", "step_count": len(steps)})
        self.transition("workflow", workflow_id, "running", metadata={"runtime_kind": "observed_workflow"})
        return {"started": True, "workflow_id": workflow_id, "steps": steps}

    def active_workflow_for_session(
        self,
        session_id: str | None = None,
        turn_id: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any] | None:
        project_key = _project_key(cwd)
        fallback = None
        for workflow in self.ledger.list_cognitive_records(layer="workflow", status="active", limit=100):
            if workflow.get("record_type") != "observed_workflow":
                continue
            metadata = workflow.get("metadata_json") or {}
            state = self.ledger.latest_state_for("workflow", str(workflow["id"]))
            if state in {"completed", "cancelled", "failed"}:
                continue
            if session_id and turn_id and metadata.get("session_id") == session_id and metadata.get("turn_id") == turn_id:
                return workflow
            if not turn_id and session_id and metadata.get("session_id") == session_id:
                return workflow
            if not turn_id and project_key and metadata.get("project_key") == project_key and fallback is None:
                fallback = workflow
        return fallback

    def observe_tool_use(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow = self.active_workflow_for_session(
            session_id=_optional_str(payload.get("session_id")),
            turn_id=_optional_str(payload.get("turn_id")),
            cwd=_optional_str(payload.get("cwd")),
        )
        if not workflow:
            return {"observed": False, "reason": "no_active_workflow", "hook_output": {}}
        observation = self.match_observation_to_step(workflow, payload)
        if not observation.get("matched_step_id"):
            return {"observed": True, "workflow_id": workflow["id"], "matched": False, "hook_output": {}}
        updated = self._apply_observation(workflow, observation)
        return {"observed": True, "workflow_id": workflow["id"], "matched": True, "observation": observation, "workflow": updated, "hook_output": {}}

    def observe_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow = self.active_workflow_for_session(
            session_id=_optional_str(payload.get("session_id")),
            turn_id=_optional_str(payload.get("turn_id")),
            cwd=_optional_str(payload.get("cwd")),
        )
        if not workflow:
            return {"observed": False, "reason": "no_active_workflow", "hook_output": {}}
        metadata = dict(workflow.get("metadata_json") or {})
        assistant_message = str(payload.get("last_assistant_message") or payload.get("assistant_message") or "")
        if assistant_message.strip():
            observation = {"matched_step_id": "audit_outcome", "tool_name": "Stop", "summary": _summarize_observation(payload)}
            workflow = self._apply_observation(workflow, observation)
            metadata = dict(workflow.get("metadata_json") or {})
        violations = self._detect_workflow_violations(str(workflow["id"]), metadata, assistant_message)
        if not violations and _step_completed(metadata, "audit_outcome"):
            self.transition("workflow", str(workflow["id"]), "completed", metadata={"runtime_kind": "observed_workflow"})
            self.ledger.set_cognitive_record_status(str(workflow["id"]), "completed", {"workflow_state": "completed"})
            learned = self._learn_from_successful_workflow(workflow)
        else:
            learned = {}
        return {"observed": True, "workflow_id": workflow["id"], "violations": violations, "learned": learned, "hook_output": {}}

    def match_observation_to_step(self, workflow: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_tool_observation(payload)
        matched_step_id = None
        if normalized.tool_kind == "inspect":
            matched_step_id = "inspect_repository"
        if normalized.tool_kind == "edit":
            matched_step_id = "execute_change"
        if normalized.tool_kind == "verify":
            matched_step_id = "execute_and_verify"
        summary = redact_secrets(normalized.to_dict())
        if not self.store_observation_previews:
            summary = _redact_observation_previews(summary)
        command = str(redact_secrets(normalized.command))[:300]
        if self.strict_privacy:
            command = _hash_text(command)
            summary = _strict_observation_summary(summary)
        return {
            "matched_step_id": matched_step_id,
            "tool_name": str(redact_secrets(normalized.tool_name)),
            "tool_kind": normalized.tool_kind,
            "command": command,
            "summary": summary,
            "test_failed": bool(normalized.evidence_summary.get("failed")) if matched_step_id == "execute_and_verify" else False,
        }

    def runtime_status(self, cwd: str | None = None, session_id: str | None = None, turn_id: str | None = None) -> dict[str, Any]:
        workflow = self.active_workflow_for_session(session_id=session_id, turn_id=turn_id, cwd=cwd)
        recipes = [
            item
            for item in self.ledger.list_cognitive_records(layer="skill", status="active", limit=50)
            if item.get("record_type") == "verification_recipe"
        ]
        if not workflow:
            return {"active_workflow": None, "open_violations": self.ledger.list_open_workflow_violations(limit=20), "learned_recipes": recipes[:10]}
        metadata = workflow.get("metadata_json") or {}
        return {
            "active_workflow": {
                "id": workflow["id"],
                "state": self.ledger.latest_state_for("workflow", str(workflow["id"])),
                "current_task": metadata.get("user_goal"),
                "session_id": metadata.get("session_id"),
                "turn_id": metadata.get("turn_id"),
                "completed_steps": metadata.get("completed_steps") or [],
                "pending_required_step": _next_pending_step(metadata),
                "changed": bool(metadata.get("changed")),
                "verified": bool(metadata.get("verified")),
                "test_failed": bool(metadata.get("test_failed")),
            },
            "open_violations": self.ledger.list_open_workflow_violations(workflow_id=str(workflow["id"]), limit=20),
            "learned_recipes": recipes[:10],
            "recommended_recipe_ids": metadata.get("recommended_recipe_ids") or [],
            "recent_observations": (metadata.get("observations") or [])[-5:],
        }

    def plan_workflow(
        self,
        prompt: str,
        limit: int = 6,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        memories = self.ledger.list_recallable_memories(cwd=cwd, session_id=session_id, limit=200)
        edges = self.ledger.list_edges([str(item["id"]) for item in memories if item.get("id")])
        recalled = MemoryRecall(memories, edges=edges).recall(prompt, limit=limit)
        records = self.ledger.list_cognitive_records(status="active", limit=300)
        route = classify(prompt)
        selected_skills = _rank_records(records, "skill", prompt, route, limit=4)
        selected_knowledge = _rank_records(records, "knowledge", prompt, route, limit=5)
        policy = self.reasoning.decide(
            prompt,
            route,
            recalled.memories,
            selected_knowledge,
            selected_skills,
            injection_pressure=_recent_injection_pressure(self.ledger),
            policies=self.ledger.list_governance_policies(active=True),
        )
        reasoning = self.ledger.record_cognitive_record(
            "reasoning",
            "reasoning_policy",
            None,
            _reasoning_policy_content(prompt, route, recalled.memories, selected_skills, selected_knowledge, policy.to_dict()),
            "active",
            "session",
            domain=route["domain"],
            category=route["category"],
            subcategory=route["subcategory"],
            confidence=0.84,
            importance=0.74,
            session_id=session_id,
            metadata={
                "prompt": prompt,
                "route": route,
                "memory_count": len(recalled.memories),
                "skill_count": len(selected_skills),
                "knowledge_count": len(selected_knowledge),
                "policy": policy.to_dict(),
            },
        )
        steps = _workflow_steps(prompt, route, recalled.memories, selected_skills, selected_knowledge, policy.to_dict())
        workflow = self.ledger.record_cognitive_record(
            "workflow",
            "dynamic_workflow",
            None,
            " -> ".join(step["name"] for step in steps),
            "active",
            "session",
            domain=route["domain"],
            category=route["category"],
            subcategory=route["subcategory"],
            confidence=0.86,
            importance=0.78,
            session_id=session_id,
            metadata={
                "prompt": prompt,
                "steps": steps,
                "memory_ids": [item["id"] for item in recalled.memories],
                "skill_ids": [item["id"] for item in selected_skills],
                "knowledge_ids": [item["id"] for item in selected_knowledge],
                "reasoning_id": reasoning["id"],
                "route": route,
                "policy": policy.to_dict(),
            },
        )
        self.transition("workflow", str(workflow["id"]), "planned", metadata={"prompt": prompt, "step_count": len(steps)})
        for memory in recalled.memories:
            self.ledger.upsert_cognitive_edge(str(workflow["id"]), str(memory["id"]), "uses_knowledge", 0.75, {"source": "workflow_memory"})
        for skill in selected_skills:
            self.ledger.upsert_cognitive_edge(str(workflow["id"]), str(skill["id"]), "uses_skill", 0.85, {"source": "workflow_skill"})
        for knowledge in selected_knowledge:
            self.ledger.upsert_cognitive_edge(str(workflow["id"]), str(knowledge["id"]), "uses_knowledge", 0.8, {"source": "workflow_knowledge"})
        self.ledger.upsert_cognitive_edge(str(workflow["id"]), str(reasoning["id"]), "supports", 0.82, {"source": "workflow_reasoning_policy"})
        return {
            "workflow_id": workflow["id"],
            "reasoning_id": reasoning["id"],
            "route": route,
            "steps": steps,
            "memories": recalled.memories,
            "skills": selected_skills,
            "knowledge": selected_knowledge,
            "policy": policy.to_dict(),
            "dag": build_dag(str(workflow["id"]), steps, policy.to_dict()).to_dict(),
        }

    def execute_workflow(
        self,
        prompt: str,
        limit: int = 6,
        cwd: str | None = None,
        session_id: str | None = None,
        fail_step: str | None = None,
    ) -> dict[str, Any]:
        plan = self.plan_workflow(prompt, limit=limit, cwd=cwd, session_id=session_id)
        workflow_id = str(plan["workflow_id"])
        dag = build_dag(workflow_id, plan["steps"], plan["policy"])
        for index, step in enumerate(dag.steps):
            self.ledger.record_cognitive_record(
                "workflow",
                "workflow_step",
                step.id,
                f"{step.name}: {step.inputs.get('reason', '')}",
                "active",
                "session",
                domain=plan["route"]["domain"],
                category=plan["route"]["category"],
                subcategory=plan["route"]["subcategory"],
                confidence=0.86,
                importance=0.7,
                session_id=session_id,
                metadata={"workflow_id": workflow_id, "step": step.to_dict(), "index": index},
                source_kind="workflow",
            )
            self.transition("workflow_step", step.id, "pending", metadata={"workflow_id": workflow_id})
            self.ledger.upsert_cognitive_edge(workflow_id, step.id, "instantiates", 0.8, {"index": index})
        executed = WorkflowExecutor(self).execute(dag, fail_step=fail_step)
        success = executed["workflow_state"] == "completed"
        for skill in plan["skills"]:
            metadata = skill.get("metadata_json") or {}
            metadata["reuse_count"] = int(metadata.get("reuse_count") or 0) + 1
            metadata["success_count"] = int(metadata.get("success_count") or 0) + (1 if success else 0)
            metadata["failure_count"] = int(metadata.get("failure_count") or 0) + (0 if success else 1)
            metadata["last_workflow_id"] = workflow_id
            self.ledger.adjust_cognitive_record_strength(str(skill["id"]), 0.12 if success else -0.3, metadata)
        self.ledger.patch_cognitive_record_metadata(workflow_id, {"dag": executed["dag"], "workflow_state": executed["workflow_state"]})
        return {
            **plan,
            **executed,
            "mode": "legacy_simulation",
            "warning": "workflow-execute is a legacy simulation. The runtime product observes Codex tool use through hooks and does not execute tools.",
        }

    def resume_workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow = self.ledger.get_cognitive_record(workflow_id)
        if not workflow:
            raise ValueError(f"workflow not found: {workflow_id}")
        metadata = workflow.get("metadata_json") or {}
        dag = _dag_from_metadata(workflow_id, metadata)
        executed = WorkflowExecutor(self).resume(dag)
        self.ledger.patch_cognitive_record_metadata(workflow_id, {"dag": executed["dag"], "workflow_state": executed["workflow_state"]})
        return {"workflow_id": workflow_id, **executed}

    def cancel_workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow = self.ledger.get_cognitive_record(workflow_id)
        if not workflow:
            raise ValueError(f"workflow not found: {workflow_id}")
        metadata = workflow.get("metadata_json") or {}
        dag = _dag_from_metadata(workflow_id, metadata)
        result = WorkflowExecutor(self).cancel(dag)
        self.ledger.patch_cognitive_record_metadata(workflow_id, {"dag": result["dag"], "workflow_state": result["workflow_state"]})
        return {"workflow_id": workflow_id, **result}

    def audit_workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow = self.ledger.get_cognitive_record(workflow_id)
        if not workflow:
            raise ValueError(f"workflow not found: {workflow_id}")
        states = [
            item
            for item in self.ledger.latest_state_transitions(limit=1000)
            if item.get("subject_id") == workflow_id or str(item.get("subject_id") or "").startswith(f"{workflow_id}:step:")
        ]
        return {"workflow": workflow, "states": states, "state": self.ledger.latest_state_for("workflow", workflow_id)}

    def injection_context(
        self,
        prompt: str,
        limit: int = 6,
        cwd: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> str:
        active = self.active_workflow_for_session(session_id=session_id, turn_id=turn_id, cwd=cwd)
        if active:
            control = self._active_workflow_control(active)
            if control:
                return control
        plan = self.plan_workflow(prompt, limit=limit, cwd=cwd, session_id=session_id)
        if (
            "short_prompt_low_cognitive_need" in (plan.get("policy") or {}).get("reasons", [])
            and not plan["memories"]
        ):
            return ""
        if not plan["memories"] and not plan["skills"] and not plan["knowledge"]:
            return ""
        lines = ["Codex Cognitive Runtime context:"]
        if plan.get("reasoning_id"):
            reasoning = self.ledger.list_cognitive_records(layer="reasoning", limit=1)
            if reasoning:
                lines.append(f"reasoning_policy: {reasoning[0]['content']}")
        if plan["skills"]:
            lines.append("skill_strategy: " + " | ".join(str(item.get("content") or "")[:120] for item in plan["skills"][:3]))
        if plan["knowledge"]:
            lines.append("organizational_knowledge: " + " | ".join(str(item.get("content") or "")[:120] for item in plan["knowledge"][:3]))
        lines.append("policy_gate: " + str(plan["policy"]))
        lines.append("workflow: " + " -> ".join(step["name"] for step in plan["steps"]))
        return "\n".join(lines)

    def _apply_observation(self, workflow: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(workflow.get("metadata_json") or {})
        step_id = str(observation["matched_step_id"])
        summary = observation.get("summary") or {}
        confidence = float(summary.get("confidence") or 0)
        if confidence < 0.8 and step_id != "audit_outcome":
            observations = [*(metadata.get("observations") or []), {**observation, "soft_evidence": True}]
            self.ledger.record_runtime_observation(str(workflow["id"]), observation, soft_evidence=True)
            return self.ledger.patch_cognitive_record_metadata(str(workflow["id"]), {"observations": observations[-50:]}) or workflow
        steps = [dict(step) for step in metadata.get("steps") or []]
        for step in steps:
            if step.get("id") == step_id or step.get("name") == step_id:
                step["state"] = "completed"
                step["completed_by"] = observation.get("tool_name")
        completed = sorted({*(metadata.get("completed_steps") or []), step_id}, key=_observed_step_order)
        observations = [*(metadata.get("observations") or []), observation]
        patch = {
            "steps": steps,
            "completed_steps": completed,
            "observations": observations[-50:],
            "changed": bool(metadata.get("changed")) or step_id == "execute_change",
            "verified": bool(metadata.get("verified")) or step_id == "execute_and_verify",
            "test_failed": bool(observation.get("test_failed")) if step_id == "execute_and_verify" else bool(metadata.get("test_failed")),
        }
        self.ledger.record_runtime_observation(str(workflow["id"]), observation, soft_evidence=False)
        self._complete_observed_step(str(workflow["id"]), step_id, observation)
        updated = self.ledger.patch_cognitive_record_metadata(str(workflow["id"]), patch) or workflow
        if step_id == "execute_and_verify":
            self._record_recipe_reuse(updated, observation)
        self._resolve_observed_violations(str(workflow["id"]), step_id, observation)
        return updated

    def _complete_observed_step(self, workflow_id: str, step_id: str, observation: dict[str, Any]) -> None:
        subject_id = f"{workflow_id}:{step_id}"
        state = self.ledger.latest_state_for("workflow_step", subject_id)
        if state is None:
            self.transition("workflow_step", subject_id, "pending", metadata={"workflow_id": workflow_id})
            self.transition("workflow_step", subject_id, "running", metadata={"workflow_id": workflow_id})
        elif state == "pending":
            self.transition("workflow_step", subject_id, "running", metadata={"workflow_id": workflow_id})
        elif state in {"completed", "rolled_back"}:
            return
        self.transition("workflow_step", subject_id, "completed", metadata={"workflow_id": workflow_id, "observation": observation})

    def _detect_workflow_violations(self, workflow_id: str, metadata: dict[str, Any], assistant_message: str) -> list[dict[str, Any]]:
        completed = set(metadata.get("completed_steps") or [])
        violations = []
        if "inspect_repository" not in completed:
            violations.append(self.ledger.record_runtime_violation(workflow_id, "answered_without_inspection", "high", {"completed_steps": sorted(completed)}))
        if metadata.get("changed") and not metadata.get("verified"):
            violations.append(self.ledger.record_runtime_violation(workflow_id, "changed_without_verification", "high", {"completed_steps": sorted(completed)}))
        if metadata.get("test_failed") and _claims_completion(assistant_message):
            violations.append(self.ledger.record_runtime_violation(workflow_id, "verification_failed_but_claimed_success", "high", {"assistant_preview": assistant_message[:240]}))
        return violations

    def _resolve_observed_violations(self, workflow_id: str, step_id: str, observation: dict[str, Any]) -> None:
        if step_id == "inspect_repository":
            self._resolve_violation_type(workflow_id, "answered_without_inspection")
        if step_id == "execute_and_verify" and not observation.get("test_failed"):
            self._resolve_violation_type(workflow_id, "changed_without_verification")
            self._resolve_violation_type(workflow_id, "verification_failed_but_claimed_success")

    def _resolve_violation_type(self, workflow_id: str, violation_type: str) -> None:
        for violation in self.ledger.list_open_workflow_violations(workflow_id=workflow_id, limit=20):
            metadata = violation.get("metadata_json") or {}
            if metadata.get("violation_type") == violation_type:
                self.ledger.resolve_runtime_violation(str(violation["id"]))

    def _active_workflow_control(self, workflow: dict[str, Any]) -> str:
        metadata = workflow.get("metadata_json") or {}
        completed = metadata.get("completed_steps") or []
        pending = _next_pending_step(metadata)
        violations = self.ledger.list_open_workflow_violations(workflow_id=str(workflow["id"]), limit=5)
        lines = [
            "Runtime control:",
            f"- current_task: {str(metadata.get('user_goal') or '')[:180]}",
            "- completed_steps: " + (", ".join(completed) if completed else "none"),
        ]
        if pending:
            lines.append(f"- pending_required_step: {pending}")
        lines.append("- violation_guard: do not claim completion without repository inspection and verification evidence.")
        lines.append("- evidence_required: test/build/lint output, or an explicit reason verification is impossible.")
        if violations:
            lines.append("Previous workflow violation:")
            for violation in violations:
                meta = violation.get("metadata_json") or {}
                lines.append(f"- {meta.get('violation_type')}: {meta.get('severity')}")
            lines.append("Required next action: resolve the violation before final answer.")
        dynamic_skills = self._recommended_dynamic_skills(metadata, limit=1)
        if dynamic_skills:
            self.ledger.patch_cognitive_record_metadata(str(workflow["id"]), {"recommended_dynamic_skill_ids": [str(skill["id"]) for skill in dynamic_skills]})
            lines.append("Recommended dynamic skill:")
            for skill in dynamic_skills:
                meta = skill.get("metadata_json") or {}
                lines.append(f"- {meta.get('title') or skill.get('content')} (source: {(meta.get('source_workflow_ids') or [''])[0]})")
                procedure = [str(item) for item in meta.get("procedure") or [] if item]
                if procedure:
                    lines.append("  procedure: " + " | ".join(procedure[:3]))
                anti_patterns = [str(item) for item in meta.get("anti_patterns") or [] if item]
                if anti_patterns:
                    lines.append("  avoid: " + " | ".join(anti_patterns[:2]))
        recipes = self._recommended_verification_recipes(metadata, limit=2)
        if recipes:
            self.ledger.record_recipe_recommendation(str(workflow["id"]), [str(recipe["id"]) for recipe in recipes])
            lines.append("Recommended verification recipe:")
            for recipe in recipes:
                meta = recipe.get("metadata_json") or {}
                commands = [str(item) for item in meta.get("recipe") or [] if item]
                if commands:
                    lines.append(f"- {commands[0]} (source: {meta.get('source_workflow_id')})")
        return "\n".join(lines)

    def _recommended_verification_recipes(self, workflow_metadata: dict[str, Any], limit: int = 2) -> list[dict[str, Any]]:
        project_key = workflow_metadata.get("project_key")
        recipes = []
        for record in self.ledger.list_cognitive_records(layer="skill", status="active", limit=100):
            if record.get("record_type") != "verification_recipe":
                continue
            metadata = record.get("metadata_json") or {}
            if project_key and record.get("project_key") not in {None, project_key}:
                continue
            if not metadata.get("recipe"):
                continue
            recipes.append(record)
        recipes.sort(key=lambda item: (float(item.get("strength") or 1), float(item.get("importance") or 0)), reverse=True)
        return recipes[:limit]

    def _recommended_dynamic_skills(self, workflow_metadata: dict[str, Any], limit: int = 1) -> list[dict[str, Any]]:
        project_key = workflow_metadata.get("project_key")
        skills = []
        for record in self.ledger.list_cognitive_records(layer="skill", status="active", limit=100):
            if record.get("record_type") != "dynamic_skill":
                continue
            if project_key and record.get("project_key") not in {None, project_key}:
                continue
            metadata = record.get("metadata_json") or {}
            if not metadata.get("procedure"):
                continue
            skills.append(record)
        skills.sort(key=lambda item: (float(item.get("strength") or 1), float(item.get("importance") or 0)), reverse=True)
        return skills[:limit]

    def _learn_from_successful_workflow(self, workflow: dict[str, Any]) -> dict[str, Any]:
        metadata = workflow.get("metadata_json") or {}
        observations = metadata.get("observations") or []
        verify_observations = [item for item in observations if item.get("matched_step_id") == "execute_and_verify" and item.get("command")]
        recipe = [item.get("command") for item in verify_observations]
        if not recipe:
            return {}
        latest_verify = verify_observations[-1]
        latest_summary = latest_verify.get("summary") or {}
        files_changed = sorted(
            {
                str(path)
                for item in observations
                for path in ((item.get("summary") or {}).get("files_changed") or [])
                if path
            }
        )
        recipe_record = self.ledger.record_cognitive_record(
            "skill",
            "verification_recipe",
            f"verification_recipe:{workflow['id']}",
            "Verification recipe: " + " && ".join(recipe[:3]),
            "active",
            "project" if metadata.get("project_key") else "session",
            domain="software_engineering",
            category="verification",
            subcategory="recipe",
            confidence=0.82,
            importance=0.74,
            project_key=metadata.get("project_key"),
            session_id=metadata.get("session_id"),
            metadata={
                "skill_type": "verification_recipe",
                "source_workflow_id": workflow["id"],
                "success_count": 1,
                "failure_count": 0,
                "reuse_count": 0,
                "recipe": recipe[:5],
                "verification_stdout_preview": str(latest_summary.get("stdout") or "")[:300],
                "exit_code": latest_summary.get("exit_code"),
                "files_changed": files_changed[:50],
                "task_type": metadata.get("task_type"),
                "project_key": metadata.get("project_key"),
                "created_from_observations": [item.get("summary") for item in verify_observations[-3:]],
                "last_used_at": None,
            },
            source_kind="workflow_learning",
        )
        dynamic_skill = SkillSynthesizer(self.ledger).synthesize_from_workflow(workflow)
        return {"verification_recipe": recipe_record, "dynamic_skill": dynamic_skill}

    def _record_recipe_reuse(self, workflow: dict[str, Any], observation: dict[str, Any]) -> None:
        metadata = workflow.get("metadata_json") or {}
        recipe_ids = [str(item) for item in metadata.get("recommended_recipe_ids") or [] if item]
        if not recipe_ids:
            return
        command = str(observation.get("command") or "")
        if not command:
            return
        summary = observation.get("summary") or {}
        if float(summary.get("confidence") or 0) < 0.8:
            return
        if (summary.get("source_fields") or {}).get("command") is None:
            return
        succeeded = not bool(observation.get("test_failed"))
        for recipe_id in recipe_ids:
            match = self._match_recommended_recipe(recipe_id, command)
            if not match:
                continue
            if succeeded:
                self._update_recipe_success(match, workflow, observation)
            else:
                self._update_recipe_failure(match, workflow, observation)

    def _match_recommended_recipe(self, recipe_id: str, command: str) -> dict[str, Any] | None:
        recipe = self.ledger.get_cognitive_record(recipe_id)
        if not recipe:
            return None
        metadata = dict(recipe.get("metadata_json") or {})
        commands = [str(item) for item in metadata.get("recipe") or [] if item]
        for recipe_command in commands:
            if _same_command(command, recipe_command):
                return {"recipe": recipe, "metadata": metadata, "matched_command": recipe_command}
        return None

    def _update_recipe_success(self, match: dict[str, Any], workflow: dict[str, Any], observation: dict[str, Any]) -> None:
        metadata = self._recipe_reuse_metadata(match, workflow, observation)
        metadata["success_count"] = int(metadata.get("success_count") or 0) + 1
        self.ledger.record_recipe_reuse(str(match["recipe"]["id"]), str(workflow["id"]), observation, True, metadata, 0.1)

    def _update_recipe_failure(self, match: dict[str, Any], workflow: dict[str, Any], observation: dict[str, Any]) -> None:
        metadata = self._recipe_reuse_metadata(match, workflow, observation)
        metadata["failure_count"] = int(metadata.get("failure_count") or 0) + 1
        self.ledger.record_recipe_reuse(str(match["recipe"]["id"]), str(workflow["id"]), observation, False, metadata, -0.2)

    def _recipe_reuse_metadata(self, match: dict[str, Any], workflow: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(match.get("metadata") or {})
        summary = observation.get("summary") or {}
        metadata["reuse_count"] = int(metadata.get("reuse_count") or 0) + 1
        metadata["last_used_at"] = _utc_now()
        metadata["last_reuse_workflow_id"] = workflow["id"]
        metadata["last_reuse_command"] = str(observation.get("command") or "")[:300]
        metadata["last_reuse_matched_command"] = str(match.get("matched_command") or "")[:300]
        metadata["last_reuse_command_source"] = (summary.get("source_fields") or {}).get("command")
        metadata["last_reuse_observation_confidence"] = summary.get("confidence")
        metadata["last_reuse_exit_code"] = summary.get("exit_code")
        metadata["last_reuse_succeeded"] = not bool(observation.get("test_failed"))
        return metadata

    def snapshot(self) -> dict[str, Any]:
        self.sync_governance_policies()
        by_layer = Counter()
        by_status = Counter()
        for record in self.ledger.list_cognitive_records(limit=1000):
            by_layer[str(record.get("layer"))] += 1
            by_status[str(record.get("status"))] += 1
        edges = self.ledger.list_cognitive_edges(limit=1000)
        by_relation = Counter(str(edge.get("relation")) for edge in edges)
        return {
            "ontology": ontology_snapshot(),
            "records": {"by_layer": dict(by_layer), "by_status": dict(by_status)},
            "edges": {"count": len(edges), "by_relation": dict(by_relation)},
            "state": self.ledger.latest_state_transitions(limit=20),
        }

    def sync_governance_policies(self) -> dict[str, Any]:
        synced = []
        for policy in self.ledger.list_governance_policies(active=True):
            record = self.ledger.record_cognitive_record(
                "policy",
                str(policy.get("policy_type") or "governance_policy"),
                str(policy["id"]),
                f"{policy.get('action')}: {policy.get('reason')}",
                "active" if policy.get("active") else "inactive",
                "global",
                confidence=0.86,
                importance=0.82,
                strength=1.0 + min(1.0, float(policy.get("hit_count") or 0) / 10.0),
                metadata={
                    "matcher": policy.get("matcher_json") or {},
                    "action": policy.get("action"),
                    "reason": policy.get("reason"),
                    "hit_count": policy.get("hit_count"),
                    "source_memory_id": policy.get("source_memory_id"),
                    "expires_at": policy.get("expires_at"),
                },
                source_kind="governance_policy",
            )
            synced.append(record["id"])
            if policy.get("source_memory_id"):
                self.ledger.upsert_cognitive_edge(str(record["id"]), str(policy["source_memory_id"]), "governed_by", 0.78, {"policy_id": policy["id"]})
        return {"synced_count": len(synced), "record_ids": synced}

    def _sync_memory_edges(self, memory: dict[str, Any]) -> None:
        memory_id = str(memory["id"])
        active = [item for item in self.ledger.list_memories(status="active", limit=200) if item.get("id") != memory_id]
        for other in active:
            relation = _cognitive_relation(memory, other)
            if relation:
                name, weight, evidence = relation
                self.ledger.upsert_cognitive_edge(memory_id, str(other["id"]), name, weight, evidence)

    def _materialize_skill(self, memory: dict[str, Any]) -> None:
        if str(memory.get("memory_type")) != "experience":
            return
        skill = self.ledger.record_cognitive_record(
            "skill",
            "execution_strategy",
            f"skill:{memory['id']}",
            str(memory.get("content") or ""),
            str(memory.get("status") or "candidate"),
            str(memory.get("scope") or "global"),
            domain=memory.get("domain"),
            category=memory.get("category"),
            subcategory=memory.get("subcategory"),
            confidence=float(memory.get("confidence") or 0),
            importance=float(memory.get("importance") or 0),
            strength=float(memory.get("strength") or 1),
            project_key=memory.get("project_key"),
            session_id=memory.get("source_session_id"),
            metadata={"source_memory_id": memory["id"], "reasoning_policy": "reuse_as_execution_pattern"},
            source_kind="memory",
        )
        self.ledger.upsert_cognitive_edge(str(skill["id"]), str(memory["id"]), "derived_from", 0.95, {"kind": "skill_materialization"})

    def _materialize_knowledge(self, memory: dict[str, Any]) -> None:
        knowledge = self.ledger.record_cognitive_record(
            "knowledge",
            "organizational_knowledge",
            f"knowledge:{memory['id']}",
            str(memory.get("content") or ""),
            str(memory.get("status") or "candidate"),
            str(memory.get("scope") or "project"),
            domain=memory.get("domain"),
            category=memory.get("category"),
            subcategory=memory.get("subcategory"),
            confidence=float(memory.get("confidence") or 0),
            importance=float(memory.get("importance") or 0),
            strength=float(memory.get("strength") or 1),
            project_key=memory.get("project_key"),
            session_id=memory.get("source_session_id"),
            metadata={"source_memory_id": memory["id"], "knowledge_policy": "source_anchored"},
            source_kind="memory",
        )
        self.ledger.upsert_cognitive_edge(str(knowledge["id"]), str(memory["id"]), "derived_from", 0.95, {"kind": "knowledge_materialization"})


def _cognitive_relation(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, float, dict[str, Any]] | None:
    left_text = str(left.get("content") or "")
    right_text = str(right.get("content") or "")
    if _contradicts(left_text, right_text):
        return "contradicts", 0.9, {"left": left_text[:160], "right": right_text[:160]}
    if near_duplicate_text(left_text, right_text):
        return "merged_into", 0.8, {"reason": "near_duplicate"}
    left_tokens = tokenize(" ".join([left_text, *(left.get("triggers_json") or [])]))
    right_tokens = tokenize(" ".join([right_text, *(right.get("triggers_json") or [])]))
    shared = sorted(left_tokens & right_tokens)
    if len(shared) >= 3:
        return "supports", 0.62, {"shared_terms": shared[:8]}
    return None


def _contradicts(left: str, right: str) -> bool:
    if _polarity(left) == 0 or _polarity(right) == 0 or _polarity(left) == _polarity(right):
        return False
    return len(tokenize(left) & tokenize(right)) >= 2


def _polarity(text: str) -> int:
    lowered = text.lower()
    negative = ("不能", "不要", "不应该", "不重叠", "分离", "禁用", "disable", "never", "not ")
    positive = ("必须", "应该", "需要", "统一", "混合", "启用", "enable", "always")
    if any(item in lowered for item in negative):
        return -1
    if any(item in lowered for item in positive):
        return 1
    return 0


def _rank_records(records: list[dict[str, Any]], layer: str, prompt: str, route: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    prompt_tokens = tokenize(prompt)
    scored = []
    for record in records:
        if record.get("layer") != layer:
            continue
        content = str(record.get("content") or "")
        metadata = record.get("metadata_json") or {}
        triggers = [str(item) for item in metadata.get("triggers") or []]
        tokens = tokenize(" ".join([content, *triggers]))
        score = len(prompt_tokens & tokens) * 4
        if record.get("domain") == route.get("domain"):
            score += 6
        if record.get("category") == route.get("category"):
            score += 4
        score += float(record.get("importance") or 0) * 3
        score += float(record.get("strength") or 1) * 2
        if score >= 6:
            item = dict(record)
            item["runtime_score"] = round(score, 3)
            scored.append(item)
    scored.sort(key=lambda item: item["runtime_score"], reverse=True)
    return scored[:limit]


def _workflow_steps(
    prompt: str,
    route: dict[str, Any],
    memories: list[dict[str, Any]],
    skills: list[dict[str, Any]],
    knowledge: list[dict[str, Any]],
    policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    policy = policy or {}
    steps = [
        {"name": "read_context", "kind": "inspection", "reason": "读取当前任务与运行约束"},
        {"name": "recall_memory", "kind": "inspection", "reason": f"召回 {len(memories)} 条相关长期经验"},
    ]
    if knowledge:
        steps.append({"name": "apply_knowledge", "kind": "planning", "reason": f"应用 {len(knowledge)} 条组织知识"})
    if skills:
        steps.append({"name": "select_skill", "kind": "planning", "reason": f"选择 {len(skills)} 条可复用执行策略"})
    if policy.get("tool_strategy") in {"inspect_first", "verify_required"}:
        steps.append({"name": "inspect_repository", "kind": "inspection", "reason": "先让代码库事实约束方案"})
    if route.get("domain") == "software_engineering" or any(term in prompt.lower() for term in ("代码", "测试", "实现", "工程")):
        steps.extend(
            [
                {"name": "execute_change", "kind": "execution", "reason": "按认知上下文执行任务"},
                {"name": "execute_and_verify", "kind": "verification", "reason": "实施后用测试或命令验证"},
            ]
        )
    else:
        steps.append({"name": "reason_and_answer", "kind": "reasoning", "reason": "结合上下文直接推理回答"})
    if policy.get("verification_required") and not any(step["name"] == "execute_and_verify" for step in steps):
        steps.append({"name": "execute_and_verify", "kind": "verification", "reason": "reasoning policy 要求验证"})
    steps.append({"name": "audit_outcome", "kind": "audit", "reason": "记录采用情况与治理反馈"})
    return steps


def _reasoning_policy_content(
    prompt: str,
    route: dict[str, Any],
    memories: list[dict[str, Any]],
    skills: list[dict[str, Any]],
    knowledge: list[dict[str, Any]],
    policy: dict[str, Any] | None = None,
) -> str:
    policy = policy or {}
    constraints = []
    if memories:
        constraints.append("先采用已召回的长期经验，避免重复犯错")
    if knowledge:
        constraints.append("组织知识优先于临时推测")
    if skills:
        constraints.append("用已抽象技能影响工具选择和执行顺序")
    if route.get("domain") == "software_engineering":
        constraints.append("工程任务必须读代码并验证")
    if policy:
        constraints.append(
            "policy gate: "
            f"depth={policy.get('reasoning_depth')} tool={policy.get('tool_strategy')} workflow={policy.get('workflow_mode')}"
        )
    if not constraints:
        constraints.append("低记忆相关任务保持轻量推理")
    return f"Reasoning policy for {route['domain']}/{route['category']}: " + "；".join(constraints) + f"。Prompt: {prompt[:160]}"


def _recent_injection_pressure(ledger: Any) -> float:
    recalls = ledger.list_recall_events(limit=100)
    if not recalls:
        return 0.0
    total = sum(len(item.get("memory_ids_json") or []) for item in recalls)
    return total / len(recalls)


def _dag_from_metadata(workflow_id: str, metadata: dict[str, Any]) -> WorkflowDAG:
    dag_data = metadata.get("dag") or {}
    steps = []
    for item in dag_data.get("steps") or []:
        steps.append(
            WorkflowStep(
                id=str(item.get("id")),
                name=str(item.get("name")),
                kind=str(item.get("kind") or "execution"),
                depends_on=[str(dep) for dep in item.get("depends_on") or []],
                state=str(item.get("state") or "pending"),
                inputs=dict(item.get("inputs") or {}),
                outputs=dict(item.get("outputs") or {}),
                rollback=item.get("rollback"),
                policy=dict(item.get("policy") or {}),
            )
        )
    if not steps:
        steps = build_dag(workflow_id, metadata.get("steps") or [], metadata.get("policy") or {}).steps
    return WorkflowDAG(workflow_id, steps)


def _observed_workflow_steps() -> list[dict[str, Any]]:
    return [
        {"id": "read_context", "name": "read_context", "kind": "inspection", "state": "completed"},
        {"id": "recall_memory", "name": "recall_memory", "kind": "inspection", "state": "completed"},
        {"id": "inspect_repository", "name": "inspect_repository", "kind": "inspection", "state": "pending"},
        {"id": "execute_change", "name": "execute_change", "kind": "execution", "state": "pending"},
        {"id": "execute_and_verify", "name": "execute_and_verify", "kind": "verification", "state": "pending"},
        {"id": "audit_outcome", "name": "audit_outcome", "kind": "audit", "state": "pending"},
    ]


def _is_engineering_task(prompt: str) -> bool:
    lowered = prompt.lower()
    if _looks_like_non_action_statement(lowered):
        return False
    action_signals = (
        "修复",
        "实现",
        "修改",
        "改一下",
        "调整",
        "重构",
        "排查",
        "解决",
        "运行",
        "执行",
        "添加",
        "删除",
        "update",
        "edit",
        "implement",
        "fix",
        "debug",
        "refactor",
    )
    verification_signals = (
        "跑测试",
        "运行测试",
        "执行测试",
        "测试失败",
        "单测失败",
        "pytest",
        "unittest",
        "npm test",
        "lint",
    )
    issue_signals = ("bug", "报错", "失败")
    request_signals = ("帮我", "请", "看一下", "查一下", "定位", "分析")
    return (
        any(signal in lowered for signal in action_signals)
        or any(signal in lowered for signal in verification_signals)
        or (any(signal in lowered for signal in issue_signals) and any(signal in lowered for signal in request_signals))
    )


def _looks_like_non_action_statement(lowered: str) -> bool:
    stripped = _strip_leading_label(lowered.strip())
    prefixes = (
        "经验：",
        "经验:",
        "偏好：",
        "偏好:",
        "规则：",
        "规则:",
        "原则：",
        "原则:",
        "记住：",
        "记住:",
        "请记住",
        "临时测试：",
        "临时测试:",
    )
    return stripped.startswith(prefixes)


def _strip_leading_label(text: str) -> str:
    if text.startswith("[") and "]" in text[:120]:
        return text.split("]", 1)[1].strip()
    return text


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _project_key(cwd: str | None) -> str | None:
    if not cwd:
        return None
    try:
        return str(Path(cwd).expanduser().resolve()).lower()
    except OSError:
        return str(Path(cwd).expanduser()).lower()


def _summarize_observation(payload: dict[str, Any]) -> dict[str, Any]:
    return _redact_observation_previews(normalize_tool_observation(payload).to_dict())


def _redact_observation_previews(summary: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(summary)
    for field in ("stdout", "stderr"):
        value = str(redacted.get(field) or "")
        redacted[f"{field}_chars"] = len(value)
        redacted[f"{field}_sha256"] = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest() if value else None
        redacted[field] = ""
    redacted["preview_storage"] = "redacted"
    return redacted


def _strict_observation_summary(summary: dict[str, Any]) -> dict[str, Any]:
    strict = dict(summary)
    command = str(strict.get("command") or "")
    strict["command_sha256"] = hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest() if command else None
    strict["command"] = ""
    files = [str(item) for item in strict.get("files_changed") or [] if item]
    strict["files_changed_sha256"] = [_hash_text(path) for path in files]
    strict["files_changed"] = []
    strict["strict_privacy"] = True
    return strict


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest() if text else ""


def _step_completed(metadata: dict[str, Any], step_id: str) -> bool:
    return step_id in set(metadata.get("completed_steps") or [])


def _observed_step_order(step_id: str) -> int:
    order = {step["id"]: index for index, step in enumerate(_observed_workflow_steps())}
    return order.get(step_id, 999)


def _next_pending_step(metadata: dict[str, Any]) -> str | None:
    completed = set(metadata.get("completed_steps") or [])
    for step in _observed_workflow_steps():
        step_id = str(step["id"])
        if step_id not in completed:
            return step_id
    return None


def _claims_completion(message: str) -> bool:
    lowered = message.lower()
    negative_signals = (
        "not done",
        "not completed",
        "not fixed",
        "未完成",
        "没有完成",
        "尚未完成",
        "未修复",
        "没有修复",
        "未通过",
        "没有通过",
        "测试失败",
        "验证失败",
        "failed",
        "failure",
    )
    if any(signal in lowered for signal in negative_signals):
        return False
    return any(signal in lowered for signal in ("done", "completed", "fixed", "已完成", "完成", "修复完成", "通过"))


def _same_command(left: str, right: str) -> bool:
    return " ".join(left.split()) == " ".join(right.split())


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
