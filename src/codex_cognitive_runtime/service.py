from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from typing import Any

from .consolidation import MemoryConsolidator
from .config import Config, ensure_state_dir
from .cognitive_governance import CognitiveGovernance
from .cognitive_runtime import CognitiveRuntime
from .default_memories import ensure_default_memories
from .doctor import run_doctor
from .durable_skills import DurableSkillManager
from .engine import MemoryEngine
from .feedback_classifier import RuntimeSkillFeedbackClassifier
from .governance import MemoryGovernance
from .knowledge import KnowledgeBuilder
from .ledger import Ledger, project_key_for_cwd
from .ledger_router import LayeredLedgerView, clone_cognitive_record_to_user
from . import logger
from .local_store import LocalCognitiveStore
from .model_client import CodexMiniClient, ModelError
from .memory_retriever import CleanMemoryRetriever, _merge_memory_lists, _stable_preferences
from .outcome_attribution import OutcomeAttributionEngine, build_acceptance_coverage, build_outcome_attribution
from .recall import MemoryRecall, format_memory_context
from .review import MemoryReviewer
from .runtime_skill import RuntimeSkillInjector, RuntimeSkillReviewer, RuntimeSkillSynthesizer
from .runtime_monitor import RuntimeMonitor, TraceContext
from .security import redact_secrets, sanitize_payload, summarize_payload, summarize_candidate
from .seed_skills import AgencySkillSeeder, default_seed_source_available
from .skill_detail import public_skill_record
from .skills import SkillEngine
from .task_profile import infer_task_profile
from .task_understanding import TaskUnderstandingEngine, classify_memory_for_task
from .timeutil import local_now_iso

USER_PREFERENCE_MEMORY_TYPE = "user_preference"
LEDGER_MEMORY_EXCLUDED_TYPES = (USER_PREFERENCE_MEMORY_TYPE,)
MEMORY_EXCLUDED_STATUSES = ("deleted",)
USER_PREFERENCE_EXCLUDED_STATUSES = ("deleted",)
MEMORY_OPTIMIZE_MODEL_TIMEOUT_SECONDS = 12


class MemoryService:
    def __init__(self, config: Config):
        ensure_state_dir(config)
        self.config = config
        self.ledger = Ledger(config.ledger_path)
        baseline_ledger_path = config.baseline_ledger_path or (config.state_dir / "baseline-ledger.sqlite3")
        self.baseline_ledger = Ledger(baseline_ledger_path)
        self.team_ledger = Ledger(config.team_ledger_path) if config.team_ledger_path else None
        self.ledger_view = LayeredLedgerView(self.ledger, baseline=self.baseline_ledger, team=self.team_ledger)
        self.model = CodexMiniClient(config)
        self.engine = MemoryEngine(config, self.model)
        self.reviewer = MemoryReviewer(config, self.model)
        self.runtime = CognitiveRuntime(
            self.ledger,
            store_observation_previews=config.store_runtime_observation_previews,
            strict_privacy=config.strict_privacy,
        )
        self.store = LocalCognitiveStore(self.ledger)
        self.monitor = RuntimeMonitor(self.ledger, strict_privacy=config.strict_privacy, live_log=config.trace_live_log)
        self.outcome_attribution = OutcomeAttributionEngine(self.ledger)
        self._runtime_skill_cache: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._default_seed_skills_checked = False
        self.default_memories_status = ensure_default_memories(self.baseline_ledger or self.ledger)
        self.user_preferences_activation_count = self.ledger.activate_user_preferences()

    def close(self) -> None:
        self.ledger_view.close()

    def __enter__(self) -> "MemoryService":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def ingest_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = self.ledger.add_event(event_type, self._stored_event_payload(payload))
        logger.info("ingest event created", event_id=event_id, event_type=event_type, payload_summary=summarize_payload(payload))
        return self.process_event(event_id, event_type, payload)

    def process_event_id(self, event_id: str) -> dict[str, Any]:
        event = self.ledger.get_event(event_id)
        if event is None:
            raise ValueError(f"event not found: {event_id}")
        if event.get("processed_at"):
            logger.debug("process event skipped", event_id=event_id, reason="already_processed")
            return {"event_id": event_id, "candidate_count": 0, "results": [], "skipped": "already_processed"}
        return self.process_event(event_id, str(event["event_type"]), dict(event["payload_json"]))

    def process_event(self, event_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        logger.info("process event started", event_id=event_id, event_type=event_type, payload_summary=summarize_payload(payload))
        self.runtime.begin_event(event_id, event_type, payload)
        trace = self._existing_trace_for_payload(payload)
        extraction_span = None
        if trace:
            extraction_span = self.monitor.start_span(trace, "memory_extraction", metadata={"event_id": event_id, "event_type": event_type})
            self.monitor.event(trace, "memory_extraction_started", span_id=str(extraction_span["id"]), subject_type="event", subject_id=event_id, metadata={"event_type": event_type})
        if event_type == "user_message":
            prompt = str(payload.get("prompt") or payload.get("text") or "")
            if _memory_storage_opt_out(prompt):
                result = {"event_id": event_id, "candidate_count": 0, "results": [], "skipped": "memory_storage_opt_out"}
                self.ledger.mark_event_processed(event_id)
                self.runtime.finish_event(event_id, result)
                if trace:
                    self.monitor.event(trace, "memory_extraction_skipped", span_id=str(extraction_span["id"]) if extraction_span else None, metadata={"reason": "memory_storage_opt_out"})
                    self.monitor.end_span(str(extraction_span["id"]) if extraction_span else None, status="skipped")
                logger.info("memory extraction skipped by user opt-out", event_id=event_id)
                return result
            feedback = self.apply_natural_feedback(
                prompt,
                str(payload.get("session_id") or "") or None,
                str(payload.get("turn_id") or "") or None,
            )
            if feedback.get("updated"):
                logger.info("natural memory feedback applied", event_id=event_id, feedback=feedback)
        candidates = self.engine.extract(event_type, payload)
        logger.debug("memory candidates extracted", event_id=event_id, candidate_count=len(candidates), candidates=[summarize_candidate(candidate) for candidate in candidates])
        if trace:
            self.monitor.event(
                trace,
                "memory_candidates_extracted",
                span_id=str(extraction_span["id"]) if extraction_span else None,
                metadata={"candidate_count": len(candidates), "candidate_types": [candidate.memory_type for candidate in candidates]},
            )
        results = []
        active_memory_ids = []
        project_key = project_key_for_cwd(str(payload.get("cwd") or "")) if payload.get("cwd") else None
        session_id = str(payload.get("session_id") or "") or None
        with self.ledger.transaction():
            for candidate in candidates:
                local_duplicates = self.ledger.find_active_duplicates(
                    candidate.content,
                    candidate.memory_type,
                    candidate.scope,
                    project_key=project_key,
                    session_id=session_id,
                )
                if local_duplicates:
                    review = {
                        "status": "superseded",
                        "reasons": ["merged_exact_duplicate"],
                        "duplicates": [{"id": item["id"], "content_preview": str(item["content"])[:160]} for item in local_duplicates[:3]],
                    }
                    memory_id = self.ledger.add_candidate(candidate, "superseded", review, project_key=project_key, session_id=session_id)
                    self.runtime.sync_memory(memory_id)
                    self.ledger.add_review_feedback(str(local_duplicates[0]["id"]), "merge_duplicate", f"merged {memory_id}")
                    if trace:
                        self.monitor.event(
                            trace,
                            "memory_candidate_reviewed",
                            span_id=str(extraction_span["id"]) if extraction_span else None,
                            metadata={"status": "superseded", "memory_type": candidate.memory_type, "reasons": review.get("reasons") or []},
                        )
                        self.monitor.event(
                            trace,
                            "memory_candidate_stored",
                            span_id=str(extraction_span["id"]) if extraction_span else None,
                            subject_type="memory",
                            subject_id=memory_id,
                            metadata={"status": "superseded", "memory_type": candidate.memory_type, "scope": candidate.scope},
                        )
                        self.monitor.link(trace, "memory", memory_id, "created")
                        self.monitor.link(trace, "memory", str(local_duplicates[0]["id"]), "used", {"relation": "duplicate_source"})
                    results.append(
                        {
                            "id": memory_id,
                            "status": "superseded",
                            "candidate": summarize_candidate(candidate),
                            "storage": "ledger_only",
                        }
                    )
                    logger.debug("duplicate candidate merged", event_id=event_id, memory_id=memory_id, duplicate_id=local_duplicates[0].get("id"))
                    continue

                conflicts = self.ledger.find_active_conflicts(
                    candidate.content,
                    candidate.memory_type,
                    candidate.scope,
                    project_key=project_key,
                    session_id=session_id,
                )
                duplicates = [{"source": "local", "id": item["id"], "content": item["content"]} for item in local_duplicates]
                logger.debug("duplicate check completed", event_id=event_id, candidate=summarize_candidate(candidate), duplicate_count=len(duplicates))
                review = self.reviewer.review(candidate, duplicates)
                policy_decision = self.ledger.candidate_policy_decision(candidate)
                if policy_decision:
                    policy_status = {
                        "quarantine": "quarantined",
                        "reject": "rejected",
                        "supersede": "superseded",
                    }.get(str(policy_decision["action"]), "rejected")
                    review = {
                        **review,
                        "status": policy_status,
                        "reasons": [*review.get("reasons", []), "governance_policy_matched"],
                        "governance_policy": policy_decision,
                    }
                if conflicts and review["status"] == "active":
                    review = {
                        **review,
                        "status": "quarantined",
                        "reasons": [*review.get("reasons", []), "possible_conflict_with_active_memory"],
                        "risk_flags": [*review.get("risk_flags", []), "memory_conflict"],
                        "conflicts": [{"id": item["id"], "content_preview": str(item["content"])[:160]} for item in conflicts[:3]],
                    }
                status = review["status"]
                logger.debug("review completed", event_id=event_id, candidate=summarize_candidate(candidate), review_status=status, reasons=review.get("reasons", []))
                if trace:
                    self.monitor.event(
                        trace,
                        "memory_candidate_reviewed",
                        span_id=str(extraction_span["id"]) if extraction_span else None,
                        metadata={"status": status, "memory_type": candidate.memory_type, "reasons": review.get("reasons") or [], "risk_flags": review.get("risk_flags") or []},
                    )
                memory_id = self.ledger.add_candidate(candidate, status, review, project_key=project_key, session_id=session_id)
                self.runtime.sync_memory(memory_id)
                if trace:
                    self.monitor.event(
                        trace,
                        "memory_candidate_stored",
                        span_id=str(extraction_span["id"]) if extraction_span else None,
                        subject_type="memory",
                        subject_id=memory_id,
                        metadata={"status": status, "memory_type": candidate.memory_type, "scope": candidate.scope},
                    )
                    self.monitor.link(trace, "memory", memory_id, "created")
                if status == "active":
                    self.ledger.set_status(memory_id, "active", {**review, "storage": "ledger_only"})
                    self.runtime.sync_memory(memory_id)
                    linked = self.ledger.link_related_active_memories(memory_id)
                    active_memory_ids.append(memory_id)
                    logger.debug("memory association edges updated", event_id=event_id, memory_id=memory_id, edge_updates=linked)
                results.append({"id": memory_id, "status": status, "candidate": summarize_candidate(candidate), "storage": "ledger_only"})
            self.ledger.mark_event_processed(event_id)
            result = {"event_id": event_id, "candidate_count": len(candidates), "results": results}
            self.runtime.finish_event(event_id, result)
        if active_memory_ids:
            consolidated = self.consolidate_memories()
            if consolidated.get("created_count"):
                logger.info("memory consolidation completed", event_id=event_id, result=consolidated)
            if trace:
                self.monitor.event(
                    trace,
                    "memory_consolidation_completed",
                    span_id=str(extraction_span["id"]) if extraction_span else None,
                    metadata={"created_count": consolidated.get("created_count", 0), "created": consolidated.get("created") or []},
                )
                for item in consolidated.get("created") or []:
                    self.monitor.link(trace, "memory", str(item.get("id")), "created", {"kind": item.get("kind"), "source_ids": item.get("source_ids") or []})
        if trace:
            self.monitor.end_span(str(extraction_span["id"]) if extraction_span else None, metadata={"candidate_count": len(candidates), "result_count": len(results)})
        logger.info("process event finished", event_id=event_id, candidate_count=len(candidates), result_count=len(results))
        return result

    def promote_memory(self, memory_id: str, note: str = "") -> dict[str, Any]:
        memory = self.ledger.promote(memory_id, note)
        self.runtime.sync_memory(memory_id)
        return {"memory": self.ledger.get_memory(memory_id), "storage": "ledger_only"}

    def reject_memory(self, memory_id: str, note: str = "") -> dict[str, Any]:
        return self.ledger.reject(memory_id, note)

    def delete_memory(self, memory_id: str, note: str = "") -> dict[str, Any]:
        return self.ledger.delete(memory_id, note)

    def expire_due_memories(self) -> dict[str, Any]:
        expired = self.ledger.expire_due()
        return {"expired_count": len(expired), "expired": expired}

    def reconcile(self) -> dict[str, Any]:
        return {"audit_events_processed": self.ledger.reconcile_audit_events(), "stats": self.ledger.stats()}

    def record_event(self, event_type: str, payload: dict[str, Any], processed: bool = False) -> str:
        event_id = self.ledger.add_event(event_type, self._stored_event_payload(payload))
        if processed:
            self.ledger.mark_event_processed(event_id)
        logger.debug("event recorded", event_id=event_id, event_type=event_type, processed=processed, payload_summary=summarize_payload(payload))
        return event_id

    def _existing_trace_for_payload(self, payload: dict[str, Any]) -> TraceContext | None:
        trace_id = str(payload.get("_codex_cognitive_runtime_trace_id") or "") or None
        existing = self.monitor.get_trace(trace_id) if trace_id else None
        if not existing:
            existing = self.ledger.latest_trace(
                session_id=str(payload.get("session_id") or "") or None,
                turn_id=str(payload.get("turn_id") or "") or None,
            )
        if not existing:
            return None
        return TraceContext(
            str(existing["id"]),
            str(existing.get("session_id") or "") or None,
            str(existing.get("turn_id") or "") or None,
            str(existing.get("cwd") or payload.get("cwd") or "") or None,
            str(existing.get("project_key") or "") or None,
        )

    def start_trace_from_payload(self, payload: dict[str, Any], event_id: str | None = None) -> TraceContext:
        prompt = str(payload.get("prompt") or payload.get("text") or "")
        context = self.monitor.start_trace(
            prompt,
            session_id=str(payload.get("session_id") or "") or None,
            turn_id=str(payload.get("turn_id") or "") or None,
            cwd=str(payload.get("cwd") or "") or None,
            root_event_id=event_id,
            metadata={
                "hook": payload.get("hook_event_name") or "UserPromptSubmit",
                "permission_mode": payload.get("permission_mode"),
                "model": payload.get("model"),
            },
        )
        self.monitor.event(context, "user_prompt_received", metadata={"prompt_chars": len(prompt)})
        self._trace_development_audit(
            context,
            "development_audit_user_prompt",
            {
                "event_id": event_id,
                "prompt": prompt,
                "payload": payload,
            },
        )
        return context

    def start_task_from_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.enable_runtime_observer:
            return {"started": False, "reason": "runtime_observer_disabled"}
        prompt = str(payload.get("prompt") or payload.get("text") or "")
        session_id = str(payload.get("session_id") or "") or None
        turn_id = str(payload.get("turn_id") or "") or None
        cwd = str(payload.get("cwd") or "") or None
        trace = self.monitor.get_or_start_trace(prompt, session_id=session_id, turn_id=turn_id, cwd=cwd)
        active_workflow = self.runtime.active_workflow_for_session(session_id=session_id, turn_id=turn_id, cwd=cwd)
        task_state = self._understand_task_for_prompt(
            prompt,
            trace=trace,
            limit=6,
            cwd=cwd,
            session_id=session_id,
            turn_id=turn_id,
            active_workflow=active_workflow,
        )
        skill_decision = task_state["skill_decision"]
        validated_task = task_state["validated_task"]
        self._trace_skill_need_audit(trace, "start_task_from_prompt", skill_decision)
        if not skill_decision.skill_needed or skill_decision.domain != "software_engineering":
            return {
                "started": False,
                "reason": "runtime_skill_not_needed",
                "skill_decision": skill_decision.to_dict(),
                "validated_task": validated_task.to_dict(),
            }
        return self.runtime.start_task_from_prompt(
            {
                **payload,
                "prompt": validated_task.interpreted_request,
                "_runtime_skill_needed": True,
                "_skill_need_decision": skill_decision.to_dict(),
                "_validated_task": validated_task.to_dict(),
            }
        )

    def observe_tool_use(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = self.record_event("after_tool_call", payload, processed=True)
        trace = self.monitor.get_or_start_trace(
            "",
            session_id=str(payload.get("session_id") or "") or None,
            turn_id=str(payload.get("turn_id") or "") or None,
            cwd=str(payload.get("cwd") or "") or None,
            root_event_id=event_id,
        )
        if not self.config.enable_runtime_observer:
            return {"observed": False, "reason": "runtime_observer_disabled", "event_id": event_id, "hook_output": {}}
        result = self.runtime.observe_tool_use(payload)
        result["event_id"] = event_id
        self._trace_tool_observation(trace, result)
        self.outcome_attribution.refresh(trace.trace_id)
        return result

    def observe_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = self.record_event("session_end", payload, processed=True)
        trace = self.monitor.get_or_start_trace(
            "",
            session_id=str(payload.get("session_id") or "") or None,
            turn_id=str(payload.get("turn_id") or "") or None,
            cwd=str(payload.get("cwd") or "") or None,
            root_event_id=event_id,
        )
        if not self.config.enable_runtime_observer:
            return {"observed": False, "reason": "runtime_observer_disabled", "event_id": event_id, "hook_output": {}}
        result = self.runtime.observe_stop(payload)
        result["event_id"] = event_id
        self._trace_stop(trace, result)
        feedback = self._record_runtime_skill_workflow_feedback(payload, result)
        if feedback:
            result["runtime_skill_feedback"] = feedback
            self._trace_feedback(trace, feedback, source="workflow_stop")
        closed_loop = self._trace_closed_loop(trace)
        if closed_loop:
            result["acceptance_coverage"] = closed_loop.get("acceptance_coverage")
            result["outcome_attribution"] = closed_loop.get("outcome_attribution")
        return result

    def _stored_event_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.config.store_raw_events:
            return {**payload, "_raw_payload_stored": True}
        sanitized = sanitize_payload(payload)
        sanitized["_raw_payload_stored"] = False
        return sanitized

    def _understand_task_for_prompt(
        self,
        prompt: str,
        *,
        trace: TraceContext,
        limit: int,
        cwd: str | None,
        session_id: str | None,
        turn_id: str | None,
        active_workflow: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context_packet = _build_context_packet(
            self,
            prompt,
            cwd=cwd,
            session_id=session_id,
            turn_id=turn_id,
            active_workflow=active_workflow,
        )
        self.monitor.event(trace, "context_packet_built", metadata=context_packet)
        self._trace_development_audit(trace, "development_audit_context_packet_built", context_packet)

        recall_started = time.perf_counter()
        recall_span = self.monitor.start_span(trace, "memory_recall")
        memories = self.ledger_view.list_recallable_memories(cwd=cwd, session_id=session_id, limit=200)
        edges = self.ledger_view.list_edges([str(item["id"]) for item in memories if item.get("id")])
        recall_query = " ".join(
            str(item or "")
            for item in [
                prompt,
                (context_packet.get("candidate_parent_task") or {}).get("interpreted_request"),
                (context_packet.get("candidate_parent_task") or {}).get("prompt_preview"),
            ]
        )
        result = MemoryRecall(memories, edges=edges).recall(recall_query, limit=limit)
        result.memories = _merge_memory_lists(result.memories, _stable_preferences(memories), limit)
        result.context = format_memory_context(result.memories)
        memory_classes = classify_memory_for_task(result.memories)
        recall_id = self.ledger.record_recall(prompt, result.route, result.memories, cwd=cwd, session_id=session_id, turn_id=turn_id)
        recalled_ids = [str(item.get("id")) for item in result.memories if item.get("id")]
        self.monitor.event(trace, "memory_recall_completed", span_id=str(recall_span["id"]), subject_type="recall", subject_id=recall_id, metadata={"route": result.route, "memory_count": len(recalled_ids), "memory_ids": recalled_ids, "recall_id": recall_id})
        self.monitor.event(trace, "memory_recall_classified", span_id=str(recall_span["id"]), metadata={key: [str(item.get("id")) for item in value] for key, value in memory_classes.items()})
        self._trace_development_audit(
            trace,
            "development_audit_memory_recall",
            {
                "recall_id": recall_id,
                "route": result.route,
                "memories": _records_for_development_audit(result.memories),
                "memory_classes": {key: _records_for_development_audit(value) for key, value in memory_classes.items()},
                "memory_context": result.context,
            },
        )
        if recall_id:
            self.monitor.link(trace, "recall", recall_id, "created")
        for memory_id in recalled_ids:
            self.monitor.link(trace, "memory", memory_id, "used")
        self.monitor.end_span(str(recall_span["id"]), metadata={"duration_ms": _elapsed_ms(recall_started)})

        understanding_started = time.perf_counter()
        understanding_span = self.monitor.start_span(trace, "task_understanding")
        self.monitor.event(trace, "task_understanding_started", span_id=str(understanding_span["id"]), metadata={"prompt_chars": len(prompt)})
        validated_task = TaskUnderstandingEngine(self.model).understand(
            prompt,
            cwd=cwd,
            context_packet=context_packet,
            recalled_memory={
                "user_preferences": _records_for_development_audit(memory_classes.get("user_preferences") or []),
                "project_rules": _records_for_development_audit(memory_classes.get("project_rules") or []),
                "task_memories": _records_for_development_audit(memory_classes.get("task_memories") or []),
            },
        )
        skill_decision = validated_task.to_skill_decision()
        self.monitor.event(trace, "task_understanding_completed", span_id=str(understanding_span["id"]), metadata=validated_task.to_dict())
        self.monitor.event(trace, "task_understanding_validated", span_id=str(understanding_span["id"]), metadata={"validated_task": validated_task.to_dict(), "corrections": validated_task.corrections, "violations": validated_task.violations}, severity="warn" if validated_task.violations else "info")
        self.monitor.event(trace, "role_profile_selected", span_id=str(understanding_span["id"]), metadata=validated_task.role_profile.to_dict())
        self.monitor.event(trace, "skill_need_decision", span_id=str(understanding_span["id"]), metadata=skill_decision.to_dict())
        self._trace_development_audit(
            trace,
            "development_audit_task_understanding",
            {
                "context_packet": context_packet,
                "memory_classes": {key: _records_for_development_audit(value) for key, value in memory_classes.items()},
                "validated_task": validated_task.to_dict(),
                "skill_need_decision": skill_decision.to_dict(),
            },
        )
        self.monitor.end_span(str(understanding_span["id"]), metadata={"duration_ms": _elapsed_ms(understanding_started)})
        return {
            "context_packet": context_packet,
            "recall_result": result,
            "recall_id": recall_id,
            "memory_classes": memory_classes,
            "validated_task": validated_task,
            "skill_decision": skill_decision,
        }

    def prompt_context(
        self,
        prompt: str,
        limit: int = 6,
        cwd: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> str:
        trace = self.monitor.get_or_start_trace(prompt, session_id=session_id, turn_id=turn_id, cwd=cwd)
        self.monitor.event(trace, "prompt_context_started")
        self._trace_development_audit(
            trace,
            "development_audit_prompt_context_started",
            {
                "prompt": prompt,
                "cwd": cwd,
                "session_id": session_id,
                "turn_id": turn_id,
                "requested_limit": limit,
            },
        )
        total_started = time.perf_counter()
        budget = MemoryGovernance(self.ledger).injection_budget(prompt, limit)
        limit = int(budget["limit"])
        active_workflow = self.runtime.active_workflow_for_session(session_id=session_id, turn_id=turn_id, cwd=cwd) if self.config.enable_runtime_observer else None
        stale_workflow = None
        if active_workflow and not _active_workflow_matches_prompt_context(active_workflow, prompt, turn_id):
            stale_workflow = active_workflow
            active_workflow = None
        understanding_total_started = time.perf_counter()
        task_state = self._understand_task_for_prompt(
            prompt,
            trace=trace,
            limit=limit,
            cwd=cwd,
            session_id=session_id,
            turn_id=turn_id,
            active_workflow=active_workflow,
        )
        skill_decision = task_state["skill_decision"]
        validated_task = task_state["validated_task"]
        result = task_state["recall_result"]
        recall_id = task_state["recall_id"]
        memory_retrieval_latency_ms = _elapsed_ms(understanding_total_started)
        task_understanding_latency_ms = memory_retrieval_latency_ms
        self._trace_skill_need_audit(trace, "prompt_context", skill_decision)
        runtime_skill_context = ""
        runtime_skill = None
        cache_hit = False
        fallback_count = 0
        task_profile: dict[str, Any] = {}
        memory_basis: dict[str, Any] = {}
        distillation: dict[str, Any] = {}
        final_context = ""
        final_context_sha256: str | None = None
        fragment_rule_mappings: list[dict[str, Any]] = []
        if skill_decision.skill_needed:
            self._ensure_default_seed_skills()
            basis_started = time.perf_counter()
            basis_span = self.monitor.start_span(trace, "basis_retrieval")
            active_metadata = active_workflow.get("metadata_json") if active_workflow else {}
            task_profile = _task_profile_from_validated_task(validated_task, cwd=cwd, recent_observations=(active_metadata or {}).get("observations") or [])
            task_profile["domain"] = skill_decision.domain
            self.monitor.event(trace, "task_profile_inferred", span_id=str(basis_span["id"]), metadata=task_profile)
            memory_basis = CleanMemoryRetriever(self.ledger_view).retrieve(validated_task.interpreted_request, cwd=cwd, session_id=session_id, limit=limit, task_profile=task_profile)
            memory_retrieval_latency_ms += _elapsed_ms(basis_started)
            memory_basis_ids = [str(item.get("id")) for item in memory_basis.get("memories") or [] if item.get("id")]
            durable_skill_ids = [str(item.get("id")) for item in memory_basis.get("durable_skills") or [] if item.get("id")]
            seed_skill_ids = [str(item.get("id")) for item in memory_basis.get("seed_skills") or [] if item.get("id")]
            distillation = memory_basis.get("skill_distillation") or {}
            basis_metadata = {
                "memory_basis_ids": memory_basis_ids,
                "durable_skill_ids": durable_skill_ids,
                "seed_skill_ids": seed_skill_ids,
                "source_skill_ids": distillation.get("source_skill_ids") or [],
                "selected_fragments": distillation.get("selected_fragments") or [],
                "workflow_required_steps": distillation.get("workflow_required_steps") or [],
                "task_profile": task_profile,
                "seed_skill_selection_scores": memory_basis.get("seed_skill_selection_scores") or [],
                "memory_basis_count": len(memory_basis_ids),
                "durable_skill_count": len(durable_skill_ids),
                "seed_skill_count": len(seed_skill_ids),
            }
            self.monitor.event(trace, "basis_retrieved", span_id=str(basis_span["id"]), metadata=basis_metadata)
            self._trace_development_audit(
                trace,
                "development_audit_skill_basis",
                {
                    "task_profile": task_profile,
                    "memory_basis": {
                        "memories": _records_for_development_audit(memory_basis.get("memories") or []),
                        "durable_skills": _records_for_development_audit(memory_basis.get("durable_skills") or []),
                        "seed_skills": _records_for_development_audit(memory_basis.get("seed_skills") or []),
                        "memory_basis_summary": memory_basis.get("memory_basis_summary"),
                        "durable_skill_basis_summary": memory_basis.get("durable_skill_basis_summary"),
                        "seed_skill_basis_summary": memory_basis.get("seed_skill_basis_summary"),
                        "seed_skill_selection_scores": memory_basis.get("seed_skill_selection_scores") or [],
                        "skill_distillation": memory_basis.get("skill_distillation") or {},
                    },
                },
            )
            for memory_id in memory_basis_ids:
                self.monitor.link(trace, "memory", memory_id, "used")
            for skill_id in durable_skill_ids:
                self.monitor.link(trace, "durable_skill", skill_id, "used")
            for seed_id in seed_skill_ids:
                self.monitor.link(trace, "seed_skill", seed_id, "used")
            self.monitor.end_span(str(basis_span["id"]))
            self._ensure_user_seed_overlays(seed_skill_ids, reason="runtime_skill_selected")
            task_prompt = validated_task.interpreted_request
            cache_key = _runtime_skill_cache_key(task_prompt, memory_basis, model=self.config.model, strict_privacy=self.config.strict_privacy)
            cached = self._runtime_skill_cache.get(cache_key)
            if cached:
                runtime_skill, review = cached
                cache_hit = True
                self.monitor.event(trace, "runtime_skill_cache_hit", metadata={"cache_hit": True})
            else:
                synthesis_started = time.perf_counter()
                synthesis_span = self.monitor.start_span(trace, "runtime_skill_synthesis")
                runtime_skill = RuntimeSkillSynthesizer(self.model).synthesize(task_prompt, skill_decision, memory_basis)
                skill_synthesis_latency_ms = _elapsed_ms(synthesis_started)
                self.monitor.event(
                    trace,
                    "runtime_skill_synthesized",
                    span_id=str(synthesis_span["id"]),
                    metadata={
                        "skill_name": getattr(runtime_skill, "name", None),
                        "intent": getattr(runtime_skill, "intent", None),
                        "domain": getattr(runtime_skill, "domain", None),
                        "confidence": getattr(runtime_skill, "confidence", None),
                        "cache_hit": False,
                    },
                )
                self._trace_development_audit(
                    trace,
                    "development_audit_runtime_skill_synthesized",
                    {
                        "runtime_skill": runtime_skill.to_dict() if runtime_skill else None,
                    },
                )
                self.monitor.end_span(str(synthesis_span["id"]), metadata={"duration_ms": skill_synthesis_latency_ms})
                review_started = time.perf_counter()
                review_span = self.monitor.start_span(trace, "runtime_skill_review")
                review = RuntimeSkillReviewer().review(runtime_skill, skill_decision, memory_basis)
                review_latency_ms = _elapsed_ms(review_started)
                self.monitor.event(
                    trace,
                    "runtime_skill_reviewed",
                    span_id=str(review_span["id"]),
                    metadata={
                        "review_status": review.get("status"),
                        "reasons": review.get("reasons") or [],
                        "risk_flags": review.get("risk_flags") or [],
                        "basis_precedence": review.get("basis_precedence"),
                    },
                    severity="warn" if review.get("status") in {"fallback", "dropped"} else "info",
                )
                self._trace_development_audit(
                    trace,
                    "development_audit_runtime_skill_reviewed",
                    {
                        "review": {
                            key: (value.to_dict() if key == "skill" and value else value)
                            for key, value in (review or {}).items()
                        },
                    },
                )
                if review.get("status") == "dropped":
                    self.monitor.event(trace, "runtime_skill_dropped", span_id=str(review_span["id"]), severity="warn", metadata={"reasons": review.get("reasons") or []})
                self.monitor.end_span(str(review_span["id"]), metadata={"duration_ms": review_latency_ms})
                runtime_skill = review.get("skill")
                if runtime_skill and review.get("status") in {"approved", "fallback"}:
                    self._runtime_skill_cache[cache_key] = (runtime_skill, review)
                if review.get("status") == "fallback":
                    fallback_count += 1
            if cached:
                skill_synthesis_latency_ms = 0
                review_latency_ms = 0
            runtime_skill = review.get("skill")
            runtime_skill_context = RuntimeSkillInjector().format(runtime_skill)
            if runtime_skill_context and runtime_skill:
                injection_span = self.monitor.start_span(trace, "runtime_skill_injection")
                if not final_context:
                    final_context = _format_final_additional_context(
                        interpreted_request=validated_task.interpreted_request,
                        memories=result.memories,
                        runtime_skill=runtime_skill if skill_decision.skill_needed else None,
                        validated_task=validated_task,
                    )
                    final_context_sha256 = hashlib.sha256(final_context.encode("utf-8", errors="replace")).hexdigest() if final_context else None
                fragment_rule_mappings = _fragment_rule_mappings(
                    runtime_skill,
                    validated_task,
                    final_context,
                    strict_privacy=self.config.strict_privacy,
                )
                final_rule_hashes = _final_rule_hashes(
                    runtime_skill,
                    validated_task,
                    final_context,
                    strict_privacy=self.config.strict_privacy,
                )
                if fragment_rule_mappings:
                    runtime_skill = replace(runtime_skill, fragment_rule_mappings=fragment_rule_mappings)
                evidence_chain = _runtime_skill_evidence_chain(runtime_skill, distillation, memory_basis)
                injection = self.ledger.record_runtime_skill_injection(
                    task_prompt,
                    runtime_skill.to_dict(),
                    session_id=session_id,
                    turn_id=turn_id,
                    cwd=cwd,
                    project_key=project_key_for_cwd(cwd) if cwd else None,
                    strict_privacy=self.config.strict_privacy,
                )
                self.ledger.patch_cognitive_record_metadata(
                    str(injection["id"]),
                    {
                        "trace_id": trace.trace_id,
                        "task_profile": runtime_skill.task_profile,
                        "source_skill_ids": runtime_skill.source_skill_ids,
                        "distilled_from": runtime_skill.distilled_from,
                        "selected_fragments": _selected_fragments_for_metadata(runtime_skill, strict_privacy=self.config.strict_privacy),
                        "fragment_rule_mappings": fragment_rule_mappings,
                        "final_rule_hashes": final_rule_hashes,
                        "final_context_sha256": final_context_sha256,
                        "formatted_context_version": "validated_task_contract_v1",
                        "workflow_required_steps": runtime_skill.workflow_required_steps,
                        "review": {
                            "status": review.get("status"),
                            "reasons": review.get("reasons") or [],
                            "risk_flags": review.get("risk_flags") or [],
                            "basis_precedence": review.get("basis_precedence"),
                        },
                        "latency": {
                            "skill_need_latency_ms": task_understanding_latency_ms,
                            "task_understanding_latency_ms": task_understanding_latency_ms,
                            "memory_retrieval_latency_ms": memory_retrieval_latency_ms,
                            "skill_synthesis_latency_ms": skill_synthesis_latency_ms,
                            "review_latency_ms": review_latency_ms,
                            "total_prompt_context_latency_ms": _elapsed_ms(total_started),
                            "model_timeout_count": 0,
                            "fallback_count": fallback_count,
                            "cache_hit": cache_hit,
                        },
                    },
                )
                latency = {
                    "skill_need_latency_ms": task_understanding_latency_ms,
                    "task_understanding_latency_ms": task_understanding_latency_ms,
                    "memory_retrieval_latency_ms": memory_retrieval_latency_ms,
                    "skill_synthesis_latency_ms": skill_synthesis_latency_ms,
                    "review_latency_ms": review_latency_ms,
                    "total_prompt_context_latency_ms": _elapsed_ms(total_started),
                    "model_timeout_count": 0,
                    "fallback_count": fallback_count,
                    "cache_hit": cache_hit,
                }
                self.monitor.event(
                    trace,
                    "runtime_skill_injected",
                    span_id=str(injection_span["id"]),
                    subject_type="runtime_skill_injection",
                    subject_id=str(injection["id"]),
                    metadata={
                        "injection_id": str(injection["id"]),
                        "skill_name": runtime_skill.name,
                        "memory_basis_ids": runtime_skill.memory_basis_ids,
                        "durable_skill_ids": runtime_skill.durable_skill_ids,
                        "seed_skill_ids": runtime_skill.seed_skill_ids,
                        "source_skill_ids": runtime_skill.source_skill_ids,
                        "selected_fragments": _selected_fragments_for_metadata(runtime_skill, strict_privacy=self.config.strict_privacy),
                        "fragment_rule_mappings": fragment_rule_mappings,
                        "final_rule_hashes": final_rule_hashes,
                        "final_context_sha256": final_context_sha256,
                        "workflow_required_steps": runtime_skill.workflow_required_steps,
                        "task_profile": runtime_skill.task_profile,
                        "evidence_chain": evidence_chain,
                        "distilled_principles": evidence_chain.get("distilled_material", {}).get("principles") or [],
                        "distilled_workflow_steps": evidence_chain.get("distilled_material", {}).get("workflow_steps") or [],
                        "distilled_verification": evidence_chain.get("distilled_material", {}).get("verification") or [],
                        "distilled_avoid": evidence_chain.get("distilled_material", {}).get("avoid") or [],
                        "runtime_skill": _runtime_skill_public_dict(runtime_skill),
                        "runtime_skill_context_sha256": hashlib.sha256(runtime_skill_context.encode("utf-8", errors="replace")).hexdigest(),
                        "runtime_skill_context_preview": runtime_skill_context[:1200],
                        "strict_privacy": self.config.strict_privacy,
                        "latency": latency,
                        "validated_task": validated_task.to_dict(),
                    },
                )
                self._trace_development_audit(
                    trace,
                    "development_audit_runtime_skill_injection",
                    {
                        "injection_id": str(injection["id"]),
                        "runtime_skill": runtime_skill.to_dict(),
                        "fragment_rule_mappings": fragment_rule_mappings,
                        "final_rule_hashes": final_rule_hashes,
                        "final_context_sha256": final_context_sha256,
                        "evidence_chain": _runtime_skill_evidence_chain(
                            runtime_skill,
                            distillation,
                            memory_basis,
                            include_source_content=True,
                        ),
                        "runtime_skill_context": runtime_skill_context,
                    },
                )
                self.monitor.link(trace, "runtime_skill_injection", str(injection["id"]), "created")
                self.monitor.update_trace(trace, status="runtime_skill_injected", runtime_skill_injection_id=str(injection["id"]))
                self.runtime.attach_runtime_skill_to_workflow(
                    session_id=session_id,
                    turn_id=turn_id,
                    cwd=cwd,
                    injection_id=str(injection["id"]),
                    skill=runtime_skill.to_dict(),
                )
                self.monitor.end_span(str(injection_span["id"]))
        runtime_context = ""
        if self.config.enable_runtime_observer:
            if active_workflow or skill_decision.domain == "software_engineering":
                runtime_context = self.runtime.injection_context(validated_task.interpreted_request, limit=limit, cwd=cwd, session_id=session_id, turn_id=turn_id)
                if stale_workflow:
                    runtime_context = ""
                workflow_id = str(active_workflow.get("id")) if active_workflow else None
                self.monitor.event(trace, "workflow_guard_context_injected", subject_type="workflow", subject_id=workflow_id, metadata={"active_workflow": workflow_id, "domain": skill_decision.domain})
                self.monitor.link(trace, "workflow", workflow_id, "used")
        logger.debug("memory recall completed", prompt_chars=len(prompt), route=result.route, recall_id=recall_id, budget=budget, memory_count=len(result.memories))
        memory_context = result.context
        if not final_context:
            final_context = _format_final_additional_context(
                interpreted_request=validated_task.interpreted_request,
                memories=result.memories,
                runtime_skill=runtime_skill if skill_decision.skill_needed else None,
                validated_task=validated_task,
            )
            final_context_sha256 = hashlib.sha256(final_context.encode("utf-8", errors="replace")).hexdigest() if final_context else None
        self.monitor.event(
            trace,
            "final_context_built",
            metadata={
                "formatted_context_version": "validated_task_contract_v1",
                "final_context_chars": len(final_context),
                "final_context_sha256": final_context_sha256,
                "has_runtime_skill_context": bool(runtime_skill_context),
                "has_runtime_control_context": bool(runtime_context),
                "has_memory_context": bool(memory_context),
                "codex_context_limitation": "additionalContext only",
            },
        )
        self._trace_development_audit(
            trace,
            "development_audit_prompt_context_built",
            {
                "runtime_skill_context": runtime_skill_context,
                "memory_context": memory_context,
                "runtime_control_context": runtime_context,
                "formatted_context_version": "validated_task_contract_v1",
                "validated_task": validated_task.to_dict(),
                "final_additional_context": final_context,
                "final_combined_context_sent": final_context,
                "final_combined_context_chars": len(final_context),
                "final_combined_context_sha256": final_context_sha256,
                "codex_context_limitation": "Codex final model input is assembled by the host app; this plugin can audit only hook payloads and the additionalContext it returns.",
            },
        )
        if not skill_decision.skill_needed:
            self.monitor.complete_trace(trace, final_outcome="direct_answer_no_runtime_skill")
            self._trace_closed_loop(trace)
        else:
            self.outcome_attribution.refresh(trace.trace_id)
        return final_context

    def search_context(
        self,
        user_message: str,
        limit: int = 5,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        memories = self.ledger_view.list_recallable_memories(cwd=cwd, session_id=session_id, limit=200)
        edges = self.ledger_view.list_edges([str(item["id"]) for item in memories if item.get("id")])
        result = MemoryRecall(memories, edges=edges).recall(user_message, limit=limit)
        return {"route": result.route, "memories": result.memories, "context": result.context}

    def apply_recall_outcome(self, session_id: str | None, turn_id: str | None, assistant_message: str) -> dict[str, Any]:
        result = self.ledger.record_recall_outcome(session_id, turn_id, assistant_message)
        logger.debug("memory recall outcome recorded", session_id=session_id, turn_id=turn_id, result=result)
        return result

    def apply_natural_feedback(
        self,
        prompt: str,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        result = MemoryGovernance(self.ledger).apply_natural_feedback(prompt, session_id=session_id)
        skill_feedback = self._record_runtime_skill_natural_feedback(prompt, session_id=session_id, turn_id=turn_id)
        if skill_feedback:
            result["runtime_skill_feedback"] = skill_feedback
            trace = self._trace_for_feedback(skill_feedback, prompt, session_id=session_id, turn_id=turn_id)
            self._trace_feedback(trace, skill_feedback, source="natural_feedback")
            closed_loop = self._trace_closed_loop(trace)
            if closed_loop:
                result["outcome_attribution"] = closed_loop.get("outcome_attribution")
        logger.debug("natural memory feedback checked", session_id=session_id, result=result)
        return result

    def recall_feedback(self, memory_id: str, outcome: str, note: str = "") -> dict[str, Any]:
        return self.ledger.register_recall_feedback(memory_id, outcome, note)

    def _record_runtime_skill_workflow_feedback(self, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
        injection = self.ledger.latest_runtime_skill_injection(
            session_id=str(payload.get("session_id") or "") or None,
            turn_id=str(payload.get("turn_id") or "") or None,
        )
        if not injection:
            return None
        if not result.get("observed"):
            outcome = "unknown"
        else:
            high_violations = [
                item
                for item in result.get("violations") or []
                if (item.get("metadata_json") or {}).get("severity") == "high"
            ]
            workflow_id = str(result.get("workflow_id") or "")
            if high_violations:
                outcome = "failure"
            elif workflow_id and self.ledger.latest_state_for("workflow", workflow_id) == "completed":
                outcome = "success"
            else:
                outcome = "unknown"
        return self.ledger.record_runtime_skill_feedback(
            str(injection["id"]),
            outcome,
            {
                "source": "workflow_stop",
                "workflow_id": result.get("workflow_id"),
                "event_id": result.get("event_id"),
                "matched_reason": "session_turn_workflow_stop" if payload.get("turn_id") else "session_workflow_stop",
                "adjust_durable_skill_strength": True,
            },
        )

    def _record_runtime_skill_natural_feedback(
        self,
        prompt: str,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any] | None:
        decision = RuntimeSkillFeedbackClassifier(self.model, enable_model=self.config.enable_feedback_model).classify(prompt)
        if not decision:
            return None
        injection = self.ledger.latest_runtime_skill_injection(session_id=session_id, turn_id=turn_id, max_age_minutes=30)
        if not injection:
            return None
        matched_reason = "same_turn_recent_feedback" if turn_id else "same_session_recent_feedback"
        prompt_evidence = _feedback_prompt_evidence(prompt, self.config.strict_privacy)
        return self.ledger.record_runtime_skill_feedback(
            str(injection["id"]),
            decision.outcome,
            {
                "source": "natural_feedback",
                "matched_reason": matched_reason,
                "injection_created_at": injection.get("created_at"),
                **prompt_evidence,
                **decision.to_evidence(),
            },
        )

    def _trace_tool_observation(self, trace: TraceContext, result: dict[str, Any]) -> None:
        workflow_id = str(result.get("workflow_id") or "") or None
        self.monitor.update_trace(trace, status="observing_tools", workflow_id=workflow_id)
        self.monitor.link(trace, "workflow", workflow_id, "used")
        observations = []
        if result.get("observation"):
            observations.append(result.get("observation"))
        if result.get("observations"):
            observations.extend(result.get("observations") or [])
        if not observations:
            self.monitor.event(trace, "tool_observed", subject_type="workflow", subject_id=workflow_id, metadata={"observed": bool(result.get("observed"))})
            return
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            summary = observation.get("summary") or {}
            matched_step = observation.get("matched_step_id")
            self.monitor.event(
                trace,
                "tool_observed",
                subject_type="tool_observation",
                subject_id=str(observation.get("record_id") or ""),
                metadata={
                    "tool_name": observation.get("tool_name") or summary.get("tool_name"),
                    "tool_kind": observation.get("tool_kind") or summary.get("tool_kind"),
                    "confidence": summary.get("confidence"),
                    "exit_code": summary.get("exit_code"),
                    "failed": summary.get("failed"),
                    "matched_step_id": matched_step,
                    "soft_evidence": observation.get("soft_evidence"),
                    "command": observation.get("command") or summary.get("command"),
                    "files_changed": summary.get("files_changed") or [],
                },
            )
            self._trace_development_audit(
                trace,
                "development_audit_tool_observation",
                {
                    "workflow_id": workflow_id,
                    "observation": observation,
                    "summary": summary,
                },
            )
            if matched_step:
                self.monitor.event(trace, "workflow_step_completed", subject_type="workflow", subject_id=workflow_id, metadata={"matched_step_id": matched_step})
            if matched_step == "execute_and_verify" and (summary.get("failed") or observation.get("test_failed")):
                self.monitor.event(trace, "verification_failed", severity="warn", subject_type="workflow", subject_id=workflow_id, metadata={"matched_step_id": matched_step})

    def _trace_stop(self, trace: TraceContext, result: dict[str, Any]) -> None:
        workflow_id = str(result.get("workflow_id") or "") or None
        violations = result.get("violations") or []
        acceptance_coverage = result.get("acceptance_coverage") or {}
        high = [item for item in violations if (item.get("metadata_json") or {}).get("severity") == "high"]
        completed = bool(workflow_id and self.ledger.latest_state_for("workflow", workflow_id) == "completed")
        outcome = "failure" if high else "success" if completed else "unknown"
        self.monitor.event(trace, "stop_observed", subject_type="workflow", subject_id=workflow_id, metadata={"observed": result.get("observed")})
        self.monitor.event(
            trace,
            "workflow_stop_audited",
            subject_type="workflow",
            subject_id=workflow_id,
            metadata={"workflow_id": workflow_id, "observed": result.get("observed"), "completed": completed, "violations": [item.get("id") for item in violations], "high_violation_count": len(high), "acceptance_coverage": acceptance_coverage},
            severity="error" if high else "info",
        )
        if acceptance_coverage:
            summary = acceptance_coverage.get("summary") or {}
            missing = int(summary.get("missing") or 0)
            failed = int(summary.get("failed") or 0)
            self.monitor.event(
                trace,
                "acceptance_coverage_evaluated",
                subject_type="workflow",
                subject_id=workflow_id,
                metadata={"workflow_id": workflow_id, "summary": summary, "acceptance_coverage": acceptance_coverage},
                severity="error" if failed else "warn" if missing else "info",
            )
            for criterion in acceptance_coverage.get("criteria") or []:
                if not isinstance(criterion, dict) or criterion.get("status") not in {"missing", "failed"}:
                    continue
                event_name = "acceptance_failed" if criterion.get("status") == "failed" else "acceptance_missing"
                self.monitor.event(
                    trace,
                    event_name,
                    subject_type="workflow",
                    subject_id=workflow_id,
                    metadata={
                        "workflow_id": workflow_id,
                        "criterion_id": criterion.get("id"),
                        "criterion_text": criterion.get("criterion_text"),
                        "status": criterion.get("status"),
                        "required_steps": criterion.get("required_steps") or [],
                        "missing_steps": criterion.get("missing_steps") or [],
                        "attribution_signal": event_name,
                    },
                    severity="error",
                )
        self._trace_development_audit(
            trace,
            "development_audit_stop",
            {
                "workflow_id": workflow_id,
                "result": result,
                "workflow": self.ledger.get_cognitive_record(workflow_id) if workflow_id else None,
            },
        )
        for violation in violations:
            severity = (violation.get("metadata_json") or {}).get("severity") or "info"
            self.monitor.event(trace, "workflow_violation_detected", severity="error" if severity == "high" else "warn", subject_type="violation", subject_id=str(violation.get("id")), metadata={"violation_type": (violation.get("metadata_json") or {}).get("violation_type"), "severity": severity})
            self.monitor.link(trace, "violation", str(violation.get("id")), "violated")
        learned = result.get("learned") or {}
        recipe = learned.get("verification_recipe")
        if recipe:
            self.monitor.event(trace, "verification_recipe_learned", subject_type="verification_recipe", subject_id=str(recipe.get("id")), metadata={"workflow_id": workflow_id})
            self.monitor.link(trace, "verification_recipe", str(recipe.get("id")), "created")
        dynamic_skill = learned.get("dynamic_skill")
        if dynamic_skill:
            self.monitor.event(trace, "dynamic_skill_candidate_created", subject_type="dynamic_skill", subject_id=str(dynamic_skill.get("id")), metadata={"workflow_id": workflow_id, "status": dynamic_skill.get("status")})
            self.monitor.link(trace, "dynamic_skill", str(dynamic_skill.get("id")), "created")
        if outcome == "failure":
            self.monitor.fail_trace(trace, final_outcome=outcome, metadata={"acceptance_coverage": acceptance_coverage})
        else:
            self.monitor.complete_trace(trace, final_outcome=outcome, metadata={"acceptance_coverage": acceptance_coverage})

    def _trace_feedback(self, trace: TraceContext, feedback: dict[str, Any], source: str) -> None:
        metadata = feedback.get("metadata_json") or {}
        evidence = metadata.get("evidence") or {}
        self.monitor.event(
            trace,
            "feedback_classified",
            subject_type="runtime_skill_feedback",
            subject_id=str(feedback.get("id")),
            metadata={
                "outcome": metadata.get("outcome"),
                "feedback_target": evidence.get("feedback_target"),
                "dimensions": metadata.get("dimensions") or {},
                "fragment_attribution": metadata.get("fragment_attribution") or evidence.get("fragment_attribution") or [],
                "adjust_seed_skill_strength": evidence.get("adjust_seed_skill_strength"),
                "adjust_durable_skill_strength": evidence.get("adjust_durable_skill_strength"),
                "source": source,
            },
        )
        self.monitor.event(
            trace,
            "runtime_skill_feedback_recorded",
            subject_type="runtime_skill_feedback",
            subject_id=str(feedback.get("id")),
            metadata={
                "feedback_id": feedback.get("id"),
                "outcome": metadata.get("outcome"),
                "feedback_target": evidence.get("feedback_target"),
                "dimensions": metadata.get("dimensions") or {},
                "fragment_attribution": metadata.get("fragment_attribution") or evidence.get("fragment_attribution") or [],
                "adjust_seed_skill_strength": evidence.get("adjust_seed_skill_strength"),
                "adjust_durable_skill_strength": evidence.get("adjust_durable_skill_strength"),
            },
        )
        self.monitor.link(trace, "runtime_skill_feedback", str(feedback.get("id")), "created")
        self.monitor.link(trace, "runtime_skill_injection", metadata.get("injection_id"), "feedback_for")

    def _trace_closed_loop(self, trace: TraceContext) -> dict[str, Any]:
        events = self.monitor.trace_events(trace.trace_id, limit=5000)
        acceptance = build_acceptance_coverage(events)
        if any(event.get("name") == "workflow_stop_audited" for event in events):
            self.monitor.event(
                trace,
                "acceptance_coverage_evaluated",
                metadata=acceptance,
                severity="warn" if acceptance.get("status") != "passed" else "info",
            )
            events = self.monitor.trace_events(trace.trace_id, limit=5000)
        attribution = build_outcome_attribution(events)
        persisted_layers = self.ledger.record_outcome_attributions(trace.trace_id, attribution.get("layers") or [])
        attribution = {**attribution, "persisted_layer_count": len(persisted_layers)}
        self.monitor.event(
            trace,
            "outcome_attribution_completed",
            metadata=attribution,
            severity="warn" if attribution.get("primary_failure_layer") else "info",
        )
        self._trace_development_audit(
            trace,
            "development_audit_closed_loop_attribution",
            {
                "acceptance_coverage": acceptance,
                "outcome_attribution": attribution,
            },
        )
        return {"acceptance_coverage": acceptance, "outcome_attribution": attribution}

    def _trace_development_audit(self, trace: TraceContext, name: str, metadata: dict[str, Any]) -> None:
        if not self.config.development_audit:
            return
        payload = {
            "development_audit": True,
            "privacy_mode": "development_full_local_audit",
            **metadata,
        }
        self.monitor.event(trace, name, metadata=payload)

    def _trace_skill_need_audit(self, trace: TraceContext, source: str, skill_decision: Any) -> None:
        decision = skill_decision.to_dict()
        self._trace_development_audit(
            trace,
            "development_audit_skill_need_decision",
            {
                "source": source,
                "skill_need_decision": decision,
                "decision_chain": decision.get("decision_chain") or {},
            },
        )

    def _trace_for_feedback(
        self,
        feedback: dict[str, Any],
        prompt: str,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> TraceContext:
        trace_id = (feedback.get("metadata_json") or {}).get("trace_id")
        if trace_id:
            existing = self.monitor.get_trace(str(trace_id))
            if existing:
                return TraceContext(
                    str(trace_id),
                    str(existing.get("session_id") or "") or session_id,
                    str(existing.get("turn_id") or "") or turn_id,
                    str(existing.get("cwd") or "") or None,
                    str(existing.get("project_key") or "") or None,
                )
        return self.monitor.get_or_start_trace(prompt, session_id=session_id, turn_id=turn_id)

    def consolidate_memories(self) -> dict[str, Any]:
        result = MemoryConsolidator(self.ledger, self.model, self.reviewer).consolidate()
        for item in result.get("created") or []:
            if item.get("id"):
                self.runtime.sync_memory(str(item["id"]))
        return result

    def govern_memories(self, apply: bool = False) -> dict[str, Any]:
        result = MemoryGovernance(self.ledger).evaluate(apply=apply)
        if apply:
            self.runtime.sync_all_active()
            self.runtime.sync_governance_policies()
            result["cognitive_governance"] = self.govern_cognitive(apply=True, full=True)
        logger.info("memory governance completed", apply=apply, result=result)
        return result

    def govern_cognitive(self, apply: bool = False, full: bool = False) -> dict[str, Any]:
        self.runtime.sync_all_active()
        self.runtime.sync_governance_policies()
        if full:
            self.knowledge_build(source="all")
            self.skill_build()
        result = CognitiveGovernance(self.ledger).evaluate(apply=apply)
        logger.info("cognitive governance completed", apply=apply, result=result)
        return result

    def knowledge_build(self, source: str = "all") -> dict[str, Any]:
        return KnowledgeBuilder(self.ledger, _repo_root()).build(source=source)

    def knowledge_search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return KnowledgeBuilder(self.ledger, _repo_root()).search(query, limit=limit)

    def knowledge_audit(self) -> dict[str, Any]:
        return KnowledgeBuilder(self.ledger, _repo_root()).audit()

    def skill_build(self) -> dict[str, Any]:
        return SkillEngine(self.ledger).build()

    def skill_list(self, limit: int = 50) -> list[dict[str, Any]]:
        return SkillEngine(self.ledger).list(limit=limit)

    def skill_audit(self) -> dict[str, Any]:
        return SkillEngine(self.ledger).audit()

    def seed_skills(
        self,
        source: str | None = None,
        repo_url: str | None = None,
        limit: int | None = None,
        category: str | None = None,
        dry_run: bool = False,
        activate: bool = False,
    ) -> dict[str, Any]:
        target_ledger = self.baseline_ledger if source is None and repo_url is None and self.baseline_ledger is not None else self.ledger
        result = AgencySkillSeeder(target_ledger).seed(source=source, repo_url=repo_url, limit=limit, category=category, dry_run=dry_run, activate=activate)
        if isinstance(result, dict):
            result["target_ledger"] = "baseline" if target_ledger is self.baseline_ledger else "user"
        return result

    def _ensure_default_seed_skills(self) -> dict[str, Any]:
        if self._default_seed_skills_checked:
            return {"imported": False, "reason": "already_checked"}
        self._default_seed_skills_checked = True
        target_ledger = self.baseline_ledger or self.ledger
        existing = [
            record
            for record in target_ledger.list_cognitive_records(layer="skill", limit=200)
            if record.get("record_type") == "seed_skill"
        ]
        if existing:
            return {"imported": False, "reason": "seed_skills_already_present"}
        if not default_seed_source_available():
            return {"imported": False, "reason": "bundled_seed_source_missing"}
        try:
            result = AgencySkillSeeder(target_ledger).seed(activate=True)
            result["target_ledger"] = "baseline" if target_ledger is self.baseline_ledger else "user"
        except Exception as exc:
            logger.warn("default seed skill import failed", error=str(exc)[:240])
            return {"imported": False, "reason": "default_seed_skill_import_failed", "error": str(exc)[:240]}
        return {"imported": bool(result.get("ok")), "result": result}

    def _ensure_user_seed_overlays(self, seed_skill_ids: list[str], *, reason: str) -> list[str]:
        created = []
        for seed_id in seed_skill_ids:
            if self.ledger.get_cognitive_record(str(seed_id)):
                continue
            source = self.ledger_view.get_cognitive_record(str(seed_id))
            if not source or source.get("record_type") != "seed_skill":
                continue
            overlay = clone_cognitive_record_to_user(self.ledger, source, overlay_reason=reason)
            if overlay:
                created.append(str(overlay.get("id") or seed_id))
        return created

    def list_runtime_skills(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            public_skill_record(record)
            for record in self.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=max(limit, 200))
            if record.get("record_type") == "injection"
        ][:limit]

    def get_runtime_skill(self, injection_id: str) -> dict[str, Any] | None:
        record = self.ledger.get_cognitive_record(injection_id)
        if not record or record.get("layer") != "runtime_skill" or record.get("record_type") != "injection":
            return None
        return public_skill_record(record)

    def runtime_skill_feedback(self, injection_id: str, outcome: str, target: str = "final_result", note: str = "") -> dict[str, Any] | None:
        evidence = {
            "source": "manual_cli",
            "feedback_target": target,
            "note": note,
            "adjust_seed_skill_strength": target in {"seed_skill", "skill_strategy", "first_action"},
            "adjust_durable_skill_strength": target in {"durable_skill", "skill_strategy", "first_action", "execution"},
        }
        feedback = self.ledger.record_runtime_skill_feedback(injection_id, outcome, evidence)
        if feedback:
            trace = self._trace_for_feedback(
                feedback,
                note,
                session_id=str(feedback.get("session_id") or "") or None,
                turn_id=(feedback.get("metadata_json") or {}).get("turn_id"),
            )
            self._trace_feedback(trace, feedback, source="manual_cli")
            self._trace_closed_loop(trace)
        return feedback

    def runtime_skill_audit(self) -> dict[str, Any]:
        records = self.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=1000)
        injections = [public_skill_record(item) for item in records if item.get("record_type") == "injection"]
        feedback = [item for item in records if item.get("record_type") == "feedback"]
        return {
            "injection_count": len(injections),
            "feedback_count": len(feedback),
            "recent_injections": injections[:10],
            "recent_feedback": feedback[:10],
        }

    def list_traces(self, session_id: str | None = None, turn_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self.monitor.list_traces(session_id=session_id, turn_id=turn_id, limit=limit)

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        trace = self.monitor.get_trace(trace_id)
        if not trace:
            return None
        return {
            "trace": trace,
            "spans": self.ledger.list_trace_spans(trace_id),
            "links": self.ledger.list_trace_links(trace_id),
            "summary": self.monitor.trace_summary(trace_id),
            "attribution": self.trace_attribution(trace_id),
        }

    def trace_events(self, trace_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return self.monitor.trace_events(trace_id, limit=limit)

    def trace_summary(self, trace_id: str) -> dict[str, Any] | None:
        summary = self.monitor.trace_summary(trace_id)
        if summary is not None:
            attribution = self.trace_attribution(trace_id)
            summary["outcome_attribution"] = {
                "overall_outcome": (attribution or {}).get("overall_outcome"),
                "primary_failure_layer": (attribution or {}).get("primary_failure_layer"),
                "layers": [
                    {
                        "layer": layer.get("layer"),
                        "outcome": layer.get("outcome"),
                        "contribution": layer.get("contribution"),
                        "confidence": layer.get("confidence"),
                        "summary": layer.get("summary"),
                    }
                    for layer in (attribution or {}).get("layers") or []
                ],
            }
        return summary

    def trace_attribution(self, trace_id: str) -> dict[str, Any] | None:
        return self.outcome_attribution.get(trace_id, refresh=True)

    def list_outcome_attributions(
        self,
        trace_id: str | None = None,
        layer: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return self.outcome_attribution.list(trace_id=trace_id, layer=layer, limit=limit)

    def trace_audit(self) -> dict[str, Any]:
        return self.monitor.trace_audit()

    def export_trace(self, trace_id: str) -> dict[str, Any] | None:
        return self.monitor.export_trace(trace_id)

    def prune_traces(self, older_than_days: int | None = None) -> dict[str, Any]:
        return self.monitor.prune_traces(older_than_days=older_than_days)

    def list_seed_skills(self, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_default_seed_skills()
        records = [
            record
            for record in self.ledger_view.list_cognitive_records(layer="skill", limit=max(limit, 200))
            if record.get("record_type") == "seed_skill"
        ]
        records.sort(key=_seed_skill_sort_key)
        return [public_skill_record(record) for record in records[:limit]]

    def seed_skill_page(
        self,
        page: int = 1,
        page_size: int = 20,
        name: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_default_seed_skills()
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        name_query = str(name or "").strip().lower()
        category_query = str(category or "").strip()
        records = [
            record
            for record in self.ledger_view.list_cognitive_records(layer="skill", limit=5000)
            if record.get("record_type") == "seed_skill"
        ]
        categories = sorted({str((record.get("metadata_json") or {}).get("category") or record.get("domain") or "") for record in records if str((record.get("metadata_json") or {}).get("category") or record.get("domain") or "")})
        filtered = []
        for record in records:
            metadata = record.get("metadata_json") or {}
            if category_query and str(metadata.get("category") or record.get("domain") or "") != category_query:
                continue
            if name_query and name_query not in str(metadata.get("name") or "").lower():
                continue
            filtered.append(record)
        filtered.sort(key=_seed_skill_sort_key)
        start = (page - 1) * page_size
        items = [public_skill_record(record) for record in filtered[start : start + page_size]]
        status_counts: dict[str, int] = {}
        for record in filtered:
            status = str(record.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "items": items,
            "total": len(filtered),
            "page": page,
            "page_size": page_size,
            "categories": categories,
            "status_counts": status_counts,
            "filters": {"name": name or "", "category": category or ""},
        }

    def get_seed_skill(self, skill_id: str) -> dict[str, Any] | None:
        self._ensure_default_seed_skills()
        record = self.ledger_view.get_cognitive_record(skill_id)
        if not record or record.get("record_type") != "seed_skill":
            return None
        return public_skill_record(record)

    def set_seed_skill_trust_state(self, skill_id: str, trust_state: str) -> dict[str, Any] | None:
        record = self.get_seed_skill(skill_id)
        if not record:
            return None
        self._ensure_user_seed_overlays([skill_id], reason="manual_trust_state_override")
        patch = {"trust_state": trust_state, "last_status_change_at": _now()}
        status = "active"
        if trust_state == "disabled":
            patch["disabled_at"] = _now()
            status = "deprecated"
        elif trust_state == "suppressed":
            patch["suppressed_at"] = _now()
            status = "suppressed"
        elif trust_state == "unverified":
            status = "active" if (record.get("metadata_json") or {}).get("source_verified") else "candidate"
        elif trust_state not in {"trusted", "unverified"}:
            status = str(record.get("status") or "active")
        return self.ledger.set_cognitive_record_status(skill_id, status, patch)

    def seed_skill_stats(self) -> dict[str, Any]:
        skills = self.list_seed_skills(limit=1000)
        counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        for skill in skills:
            metadata = skill.get("metadata_json") or {}
            state = str(metadata.get("trust_state") or "unknown")
            counts[state] = counts.get(state, 0) + 1
            status = str(skill.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        return {"count": len(skills), "by_status": status_counts, "by_trust_state": counts, "skills": skills[:20]}

    def list_dynamic_skills(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [public_skill_record(record) for record in DurableSkillManager(self.ledger).list(status=status, limit=limit)]

    def get_dynamic_skill(self, skill_id: str) -> dict[str, Any] | None:
        record = DurableSkillManager(self.ledger).get(skill_id)
        return public_skill_record(record) if record else None

    def promote_dynamic_skill(self, skill_id: str, note: str = "") -> dict[str, Any] | None:
        return DurableSkillManager(self.ledger).promote(skill_id, note)

    def reject_dynamic_skill(self, skill_id: str, note: str = "") -> dict[str, Any] | None:
        return DurableSkillManager(self.ledger).reject(skill_id, note)

    def deprecate_dynamic_skill(self, skill_id: str, note: str = "") -> dict[str, Any] | None:
        return DurableSkillManager(self.ledger).deprecate(skill_id, note)

    def suppress_dynamic_skill(self, skill_id: str, reason: str = "") -> dict[str, Any] | None:
        return DurableSkillManager(self.ledger).suppress(skill_id, reason)

    def dynamic_skill_stats(self) -> dict[str, Any]:
        return DurableSkillManager(self.ledger).stats()

    def skill_promote(self, skill_id: str) -> dict[str, Any] | None:
        return SkillEngine(self.ledger).promote(skill_id)

    def skill_deprecate(self, skill_id: str) -> dict[str, Any] | None:
        return SkillEngine(self.ledger).deprecate(skill_id)

    def periodic_governance(self, interval_minutes: int = 60) -> dict[str, Any]:
        result = MemoryGovernance(self.ledger).run_periodic_if_due(interval_minutes=interval_minutes)
        logger.debug("periodic governance checked", result=result)
        return result

    def status(self) -> dict[str, Any]:
        return {
            "store": self.store.status(),
            "model": self.config.model,
            "primary_store": self.config.primary_store,
            "ledger_layers": self.ledger_view.stats(),
            "privacy": _privacy_status(self.config),
            "cognitive": self.runtime.snapshot()["records"],
        }

    def lightweight_status(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger.stats(),
            "ledger_layers": self.ledger_view.stats(),
            "model": self.config.model,
            "privacy": _privacy_status(self.config),
        }

    def runtime_status(self, cwd: str | None = None, session_id: str | None = None, turn_id: str | None = None) -> dict[str, Any]:
        status = self.runtime.runtime_status(cwd=cwd, session_id=session_id, turn_id=turn_id)
        status["runtime_observer"] = {
            "enabled": self.config.enable_runtime_observer,
            "observation_previews": "stored" if self.config.store_runtime_observation_previews else "redacted",
            "strict_privacy": self.config.strict_privacy,
        }
        return status

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        return self.ledger.get_usable_memory(memory_id)

    def list_memories(
        self,
        status: str | None = None,
        memory_type: str | None = None,
        name: str | None = None,
        scope: str | None = None,
        project_key: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self.ledger.list_memories(
            status=status,
            exclude_statuses=None if status else MEMORY_EXCLUDED_STATUSES,
            memory_type=memory_type,
            exclude_memory_types=None if memory_type else LEDGER_MEMORY_EXCLUDED_TYPES,
            name=name,
            scope=scope,
            project_key=project_key,
            session_id=session_id,
            limit=limit,
        )

    def memory_page(
        self,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        exclude_statuses: list[str] | tuple[str, ...] | None = None,
        memory_type: str | None = None,
        name: str | None = None,
        scope: str | None = None,
        project_key: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return self.ledger.memory_page(
            page=page,
            page_size=page_size,
            status=status,
            exclude_statuses=exclude_statuses if status else (exclude_statuses or MEMORY_EXCLUDED_STATUSES),
            memory_type=memory_type,
            exclude_memory_types=None if memory_type else LEDGER_MEMORY_EXCLUDED_TYPES,
            name=name,
            scope=scope,
            project_key=project_key,
            session_id=session_id,
        )

    def user_preferences_page(
        self,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        name: str | None = None,
        scope: str | None = None,
        project_key: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return self.memory_page(
            page=page,
            page_size=page_size,
            status=status or "active",
            memory_type=USER_PREFERENCE_MEMORY_TYPE,
            exclude_statuses=None,
            name=name,
            scope=scope,
            project_key=project_key,
            session_id=session_id,
        )

    def get_user_preference(self, memory_id: str) -> dict[str, Any] | None:
        memory = self.get_memory(memory_id)
        if memory is None or memory.get("memory_type") != USER_PREFERENCE_MEMORY_TYPE or memory.get("status") == "deleted":
            return None
        return memory

    def create_user_preference(self, content: str, scope: str = "global", note: str = "") -> dict[str, Any]:
        from .schema import Evidence, MemoryCandidate

        text = " ".join(str(content or "").split())
        if not text:
            raise ValueError("user preference content cannot be empty")
        normalized_scope = str(scope or "global").strip() or "global"
        if normalized_scope not in {"global", "project", "session"}:
            raise ValueError("user preference scope must be global, project, or session")
        candidate = MemoryCandidate(
            content=text,
            memory_type=USER_PREFERENCE_MEMORY_TYPE,
            scope=normalized_scope,
            ttl="long",
            confidence=0.98,
            importance=0.9,
            evidence=[Evidence(source="manual_user_preference", quote=text[:500])],
            reason=note or "Manually created user preference.",
            proposed_action="store",
            domain="user_profile",
            category="preference",
            subcategory="manual",
            abstraction_level="concrete",
            triggers=[],
        )
        memory_id = self.ledger.add_candidate(
            candidate,
            "active",
            {"status": "active", "manual_create": {"note": note, "at": local_now_iso()}},
        )
        return self.get_memory(memory_id) or {"id": memory_id}

    def update_user_preference(self, memory_id: str, content: str, note: str = "") -> dict[str, Any]:
        if self.get_user_preference(memory_id) is None:
            raise ValueError(f"user preference not found: {memory_id}")
        return self.update_memory_content(memory_id, content, note=note)

    def delete_user_preference(self, memory_id: str, note: str = "") -> dict[str, Any]:
        if self.get_user_preference(memory_id) is None:
            raise ValueError(f"user preference not found: {memory_id}")
        return self.delete_memory(memory_id, note=note)

    def optimize_user_preference(self, memory_id: str, instruction: str = "") -> dict[str, Any]:
        if self.get_user_preference(memory_id) is None:
            raise ValueError(f"user preference not found: {memory_id}")
        return self.optimize_memory_content(memory_id, instruction=instruction)

    def update_memory_content(self, memory_id: str, content: str, note: str = "") -> dict[str, Any]:
        text = " ".join(str(content or "").split())
        if not text:
            raise ValueError("memory content cannot be empty")
        return self.ledger.update_memory_content(memory_id, text, note=note)

    def optimize_memory_content(self, memory_id: str, instruction: str = "") -> dict[str, Any]:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise ValueError(f"memory not found: {memory_id}")
        current = str(memory.get("content") or "")
        request = (
            "Optimize memory content for a reviewed Codex Cognitive Runtime Ledger entry. "
            "Keep the same meaning, avoid adding new facts, remove redundancy, preserve constraints, and return concise Chinese when the original is Chinese. "
            "Do not change status, scope, type, evidence, IDs, or review metadata.\n\n"
            f"Memory type: {memory.get('memory_type')}\n"
            f"Scope: {memory.get('scope')}\n"
            f"Instruction: {redact_secrets(instruction)[:500]}\n"
            f"Current content: {redact_secrets(current)[:2000]}"
        )
        schema = {
            "optimized_content": "rewritten memory content only",
            "summary": "short explanation of the improvement",
            "changed": True,
        }
        try:
            result = self.model.complete_json(request, schema, timeout_seconds=MEMORY_OPTIMIZE_MODEL_TIMEOUT_SECONDS)
        except (ModelError, ValueError, TypeError) as exc:
            raise ValueError(f"memory optimization failed: {type(exc).__name__}") from exc
        if not isinstance(result, dict):
            raise ValueError("memory optimization failed: invalid model result")
        optimized = " ".join(str(result.get("optimized_content") or "").split())
        if not optimized:
            raise ValueError("memory optimization failed: empty optimized content")
        return {
            "memory_id": memory_id,
            "original_content": current,
            "optimized_content": optimized,
            "summary": str(result.get("summary") or "")[:500],
            "changed": optimized != current,
        }

    def privacy_status(self) -> dict[str, Any]:
        return {
            "privacy": _privacy_status(self.config),
            "state_dir": str(self.config.state_dir),
            "ledger_path": str(self.config.ledger_path),
            "ledger_layers": self.ledger_view.stats(),
            "primary_store": self.config.primary_store,
            "mcp_permissions": {
                "write_tools": self.config.enable_mcp_write_tools,
                "review_tools": self.config.enable_mcp_review_tools,
                "admin_tools": self.config.enable_mcp_admin_tools,
                "dangerous_tools": self.config.enable_dangerous_mcp_tools,
            },
        }

    def run_doctor(self, model_check: bool = False, privacy: bool = False) -> dict[str, Any]:
        result = run_doctor(self.config, model_check=model_check, privacy=privacy)
        self.ledger.set_governance_state(
            "last_doctor_result",
            {
                "ran_at": _now(),
                "model_check": model_check,
                "privacy": privacy,
                "result": result,
            },
        )
        return result

    def doctor_status(self) -> dict[str, Any]:
        stored = self.ledger.get_governance_state("last_doctor_result")
        if not isinstance(stored, dict):
            return {"last_run": None}
        return {"last_run": stored}

    def workflow_violations(
        self,
        limit: int = 50,
        session_id: str | None = None,
        turn_id: str | None = None,
        cwd: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.ledger.list_open_workflow_violations(
            session_id=session_id,
            turn_id=turn_id,
            project_key=project_key_for_cwd(cwd) if cwd else None,
            limit=limit,
        )

    def resolve_workflow_violation(self, violation_id: str, note: str = "") -> dict[str, Any] | None:
        return self.ledger.resolve_runtime_violation(violation_id, note=note)

    def verification_recipes(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            public_skill_record(record)
            for record in self.ledger.list_cognitive_records(layer="skill", status="active", limit=max(limit, 200))
            if record.get("record_type") == "verification_recipe"
        ][:limit]

    def governance_policies(self, policy_type: str | None = None, active: bool = True) -> list[dict[str, Any]]:
        return self.ledger.list_governance_policies(policy_type=policy_type, active=active)

    def export_data(self, limit: int = 5000, target: str = "user") -> dict[str, Any]:
        target = str(target or "user").strip().lower()
        if target in {"user", "personal"}:
            data = self.ledger.export_data(limit=limit)
            data["target_ledger"] = "user"
            return self._sanitize_export(data)
        if target == "baseline":
            data = self.baseline_ledger.export_data(limit=limit)
            data["target_ledger"] = "baseline"
            data["github_safe"] = True
            return self._sanitize_baseline_export(data)
        if target == "team":
            if not self.team_ledger:
                return {"target_ledger": "team", "available": False, "reason": "team_ledger_not_configured"}
            data = self.team_ledger.export_data(limit=limit)
            data["target_ledger"] = "team"
            return self._sanitize_export(data)
        if target == "all":
            return {
                "version": 1,
                "exported_at": _now(),
                "target_ledger": "all",
                "ledgers": {
                    "user": self._sanitize_export(self.ledger.export_data(limit=limit)),
                    "team": self._sanitize_export(self.team_ledger.export_data(limit=limit)) if self.team_ledger else None,
                    "baseline": self._sanitize_baseline_export(self.baseline_ledger.export_data(limit=limit)),
                },
            }
        raise ValueError("export target must be user, team, baseline, or all")

    def _sanitize_export(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.config.strict_privacy:
            for record in data.get("cognitive_records") or []:
                if record.get("record_type") == "seed_skill":
                    record["content"] = ""
                    metadata = record.get("metadata_json") or {}
                    metadata["content_export"] = "omitted_by_strict_privacy"
                    record["metadata_json"] = metadata
                if record.get("layer") == "runtime_skill":
                    metadata = record.get("metadata_json") or {}
                    skill = metadata.get("skill") if isinstance(metadata.get("skill"), dict) else None
                    if skill:
                        metadata["skill"] = {
                            "skill_type": skill.get("skill_type"),
                            "name": skill.get("name"),
                            "intent": skill.get("intent"),
                            "domain": skill.get("domain"),
                            "confidence": skill.get("confidence"),
                            "memory_basis_ids": skill.get("memory_basis_ids") or [],
                            "durable_skill_ids": skill.get("durable_skill_ids") or [],
                            "seed_skill_ids": skill.get("seed_skill_ids") or [],
                            "source_skill_ids": skill.get("source_skill_ids") or [],
                            "selected_fragments": [
                                {key: value for key, value in dict(item).items() if key not in {"text_preview", "source_path"}}
                                for item in skill.get("selected_fragments") or []
                                if isinstance(item, dict)
                            ],
                            "fragment_rule_mappings": [
                                {key: value for key, value in dict(item).items() if key not in {"source_text_preview", "final_rule_preview"}}
                                for item in skill.get("fragment_rule_mappings") or []
                                if isinstance(item, dict)
                            ],
                            "content_export": "omitted_by_strict_privacy",
                        }
                    if "prompt_preview" in metadata:
                        metadata.pop("prompt_preview", None)
                    record["metadata_json"] = metadata
        return data

    def _sanitize_baseline_export(self, data: dict[str, Any]) -> dict[str, Any]:
        data["events"] = []
        data["recall_events"] = []
        data["runtime_state_transitions"] = []
        data["runtime_traces"] = []
        data["runtime_trace_spans"] = []
        data["runtime_trace_events"] = []
        data["runtime_trace_links"] = []
        data["outcome_attributions"] = []
        data["memories"] = [
            memory
            for memory in data.get("memories") or []
            if (memory.get("review_json") or {}).get("source_id") == "default:global_agents_collaboration_rules"
            or (memory.get("review_json") or {}).get("source_kind") == "bundled_default_memory"
        ]
        data["cognitive_records"] = [
            record
            for record in data.get("cognitive_records") or []
            if record.get("record_type") == "seed_skill"
            and str(record.get("source_kind") or "") in {"bundled_seed_skill", "agency_agents_seed", "seed_skill"}
        ]
        data["github_safe"] = True
        return data

    def wipe_data(self) -> dict[str, Any]:
        return self.ledger.wipe_all()

    def prune_events(self, older_than_days: int | None = None) -> dict[str, Any]:
        return self.ledger.prune_events(older_than_days=older_than_days)

    def prune_runtime(self, older_than_days: int | None = None, include_recipes: bool = False, include_skills: bool = False) -> dict[str, Any]:
        return self.ledger.prune_runtime_records(older_than_days=older_than_days, include_recipes=include_recipes, include_skills=include_skills)

    def cognitive_snapshot(self) -> dict[str, Any]:
        self.runtime.sync_all_active()
        return self.runtime.snapshot()

    def workflow_plan(
        self,
        prompt: str,
        limit: int = 6,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.runtime.sync_all_active()
        return self.runtime.plan_workflow(prompt, limit=limit, cwd=cwd, session_id=session_id)

    def workflow_execute(
        self,
        prompt: str,
        limit: int = 6,
        cwd: str | None = None,
        session_id: str | None = None,
        fail_step: str | None = None,
    ) -> dict[str, Any]:
        self.runtime.sync_all_active()
        return self.runtime.execute_workflow(prompt, limit=limit, cwd=cwd, session_id=session_id, fail_step=fail_step)

    def workflow_simulate(
        self,
        prompt: str,
        limit: int = 6,
        cwd: str | None = None,
        session_id: str | None = None,
        fail_step: str | None = None,
    ) -> dict[str, Any]:
        return self.workflow_execute(prompt, limit=limit, cwd=cwd, session_id=session_id, fail_step=fail_step)

    def workflow_resume(self, workflow_id: str) -> dict[str, Any]:
        return self.runtime.resume_workflow(workflow_id)

    def workflow_cancel(self, workflow_id: str) -> dict[str, Any]:
        return self.runtime.cancel_workflow(workflow_id)

    def workflow_audit(self, workflow_id: str) -> dict[str, Any]:
        return self.runtime.audit_workflow(workflow_id)

def _candidate_from_memory(memory: dict[str, Any]):
    from .schema import Evidence, MemoryCandidate

    evidence = []
    for item in memory.get("evidence_json") or []:
        if isinstance(item, dict):
            evidence.append(Evidence(source=str(item.get("source", "")), quote=str(item.get("quote", ""))))
    return MemoryCandidate(
        content=str(memory.get("content") or ""),
        memory_type=str(memory.get("memory_type") or "temporary"),
        proposed_action="store",
        confidence=float(memory.get("confidence") or 0),
        importance=float(memory.get("importance") or 0),
        ttl=str(memory.get("ttl") or "session"),
        scope=str(memory.get("scope") or "session"),
        domain=memory.get("domain"),
        category=memory.get("category"),
        subcategory=memory.get("subcategory"),
        abstraction_level=memory.get("abstraction_level"),
        triggers=[str(item) for item in memory.get("triggers_json") or []],
        evidence=evidence,
        reason=str(memory.get("reason") or ""),
    )


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def _privacy_status(config: Config) -> dict[str, Any]:
    status = {
        "store_raw_events": config.store_raw_events,
        "runtime_observer_enabled": config.enable_runtime_observer,
        "runtime_observation_previews": "stored" if config.store_runtime_observation_previews else "redacted",
        "strict_privacy": config.strict_privacy,
    }
    if config.store_raw_events:
        status["warning"] = "raw event payload storage is enabled"
    if config.store_runtime_observation_previews:
        status["runtime_warning"] = "runtime observation stdout/stderr previews are stored"
    return status


def _memory_storage_opt_out(prompt: str) -> bool:
    lowered = prompt.lower()
    signals = (
        "不要记忆",
        "别记忆",
        "不要保存",
        "别保存",
        "不要记录",
        "别记录",
        "不要把这",
        "不要存",
        "do not remember",
        "don't remember",
        "do not save",
        "don't save",
        "do not store",
        "don't store",
    )
    return any(signal in lowered for signal in signals)


def _runtime_skill_feedback_sentiment(prompt: str) -> str | None:
    lowered = prompt.lower()
    positive = ("很好", "正是", "可以", "有用", "useful", "good", "works")
    negative = ("不对", "不是", "不要这样", "没用", "wrong", "not useful", "bad")
    if any(signal in lowered for signal in negative):
        return "negative"
    if any(signal in lowered for signal in positive):
        return "positive"
    return None


def _is_thread_resume_prompt(prompt: str) -> bool:
    text = prompt.strip().lower()
    if not text:
        return False
    resume_terms = (
        "读取会话",
        "读取对话",
        "继续工作",
        "继续处理",
        "继续这个会话",
        "继续这个对话",
        "恢复会话",
        "恢复上下文",
        "接着做",
        "read the conversation",
        "read conversation",
        "continue this thread",
        "continue the conversation",
        "resume the thread",
        "resume this session",
    )
    return any(term in text for term in resume_terms)


def _active_workflow_matches_prompt_context(active_workflow: dict[str, Any], prompt: str, turn_id: str | None) -> bool:
    if turn_id:
        return True
    if _is_thread_resume_prompt(prompt):
        return True
    metadata = active_workflow.get("metadata_json") or {}
    active_prompt = " ".join(str(metadata.get("user_goal") or "").split())
    incoming_prompt = " ".join(str(prompt or "").split())
    return bool(incoming_prompt and incoming_prompt == active_prompt)


def _natural_feedback_target(prompt: str) -> str:
    lowered = prompt.lower()
    first_action_signals = ("提问", "问题", "question")
    if any(signal in lowered for signal in first_action_signals):
        return "first_action"
    strategy_signals = (
        "方向",
        "方法",
        "策略",
        "流程",
        "skill",
        "strategy",
        "method",
        "approach",
        "question",
        "workflow",
    )
    if any(signal in lowered for signal in strategy_signals):
        return "skill_strategy"
    return "final_result"


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _build_context_packet(
    service: MemoryService,
    prompt: str,
    *,
    cwd: str | None,
    session_id: str | None,
    turn_id: str | None,
    active_workflow: dict[str, Any] | None,
) -> dict[str, Any]:
    recent_traces = [
        trace
        for trace in service.monitor.list_traces(session_id=session_id, limit=8)
        if str(trace.get("turn_id") or "") != str(turn_id or "")
    ]
    parent = _candidate_parent_task(service, recent_traces)
    return {
        "current_prompt": prompt,
        "cwd": cwd,
        "session_id": session_id,
        "turn_id": turn_id,
        "candidate_parent_task": parent,
        "recent_traces": [
            {
                "trace_id": trace.get("id"),
                "prompt_preview": trace.get("prompt_preview"),
                "status": trace.get("status"),
                "final_outcome": trace.get("final_outcome"),
                "updated_at": trace.get("updated_at"),
            }
            for trace in recent_traces[:5]
        ],
        "active_workflow": _active_workflow_packet(active_workflow),
        "project_context": {
            "cwd": cwd,
            "project_key": project_key_for_cwd(cwd) if cwd else None,
        },
    }


def _candidate_parent_task(service: MemoryService, recent_traces: list[dict[str, Any]]) -> dict[str, Any]:
    for trace in recent_traces:
        trace_id = str(trace.get("id") or "")
        if not trace_id:
            continue
        events = service.monitor.trace_events(trace_id, limit=500)
        validated = _latest_event_metadata(events, "development_audit_task_understanding").get("validated_task")
        final_context = _latest_event_metadata(events, "development_audit_prompt_context_built").get("final_combined_context_sent")
        if validated:
            return {
                "trace_id": trace_id,
                "prompt_preview": trace.get("prompt_preview"),
                "interpreted_request": validated.get("interpreted_request") or _extract_user_request_from_context(str(final_context or "")),
                "role_profile": validated.get("role_profile") or {},
                "task_type": validated.get("task_type"),
                "surfaces": validated.get("surfaces") or [],
                "implementation_scope": validated.get("implementation_scope") or [],
                "acceptance_criteria": validated.get("acceptance_criteria") or [],
            }
        request = _extract_user_request_from_context(str(final_context or ""))
        if request:
            return {
                "trace_id": trace_id,
                "prompt_preview": trace.get("prompt_preview"),
                "interpreted_request": request,
                "role_profile": {},
                "task_type": "",
                "surfaces": [],
                "implementation_scope": [],
                "acceptance_criteria": [],
            }
    return {}


def _latest_event_metadata(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("name") == name:
            metadata = event.get("metadata_json") or {}
            return metadata if isinstance(metadata, dict) else {}
    return {}


def _extract_user_request_from_context(context: str) -> str:
    text = str(context or "")
    marker = "用户需求："
    if marker not in text:
        return ""
    after = text.split(marker, 1)[1]
    return after.split("\n", 1)[0].strip()


def _active_workflow_packet(active_workflow: dict[str, Any] | None) -> dict[str, Any]:
    if not active_workflow:
        return {}
    metadata = active_workflow.get("metadata_json") or {}
    return {
        "workflow_id": active_workflow.get("id"),
        "user_goal": metadata.get("user_goal"),
        "required_steps": metadata.get("required_steps") or [],
        "completed_steps": metadata.get("completed_steps") or [],
        "observations": metadata.get("observations") or [],
    }


def _task_profile_from_validated_task(validated_task: Any, cwd: str | None, recent_observations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    base = infer_task_profile(str(getattr(validated_task, "interpreted_request", "") or ""), cwd=cwd, recent_observations=recent_observations)
    surfaces = sorted(set(base.get("surfaces") or []) | {str(item) for item in getattr(validated_task, "surfaces", []) or []})
    task_type = str(getattr(validated_task, "task_type", "") or base.get("task_type") or "general_task")
    if base.get("task_type") == "fullstack_integration_change" or {"frontend", "backend"}.issubset(set(surfaces)):
        task_type = "fullstack_integration_change"
    evidence = dict(base.get("evidence") or {})
    evidence["validated_task"] = {
        "task_type": getattr(validated_task, "task_type", ""),
        "role_profile": getattr(getattr(validated_task, "role_profile", None), "to_dict", lambda: {})(),
    }
    return {
        **base,
        "task_type": task_type,
        "surfaces": surfaces,
        "confidence": max(float(base.get("confidence") or 0), 0.86 if surfaces else 0.56),
        "evidence": evidence,
    }


def _runtime_skill_cache_key(prompt: str, memory_basis: dict[str, Any], model: str, strict_privacy: bool) -> str:
    basis = {
        "prompt_sha256": hashlib.sha256(str(prompt or "").encode("utf-8", errors="replace")).hexdigest(),
        "model": model,
        "strict_privacy": bool(strict_privacy),
        "memories": [_basis_cache_marker(item, include_trust=False) for item in memory_basis.get("memories") or []],
        "durable_skills": [_basis_cache_marker(item, include_trust=True) for item in memory_basis.get("durable_skills") or []],
        "seed_skills": [_basis_cache_marker(item, include_trust=True) for item in memory_basis.get("seed_skills") or []],
        "task_profile": memory_basis.get("task_profile") or {},
        "skill_distillation": {
            "source_skill_ids": (memory_basis.get("skill_distillation") or {}).get("source_skill_ids") or [],
            "selected_fragments": [
                {
                    "fragment_id": item.get("fragment_id"),
                    "text_sha256": item.get("text_sha256"),
                    "score": item.get("score"),
                    "risk": item.get("risk"),
                }
                for item in (memory_basis.get("skill_distillation") or {}).get("selected_fragments") or []
                if isinstance(item, dict)
            ],
            "workflow_required_steps": (memory_basis.get("skill_distillation") or {}).get("workflow_required_steps") or [],
        },
    }
    payload = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _format_final_additional_context(
    interpreted_request: str,
    memories: list[dict[str, Any]],
    runtime_skill: Any | None = None,
    validated_task: Any | None = None,
) -> str:
    user_rules = _summarize_rules(
        [memory for memory in memories if memory.get("memory_type") == USER_PREFERENCE_MEMORY_TYPE],
        limit=3,
    )
    project_rules = _summarize_rules(
        [memory for memory in memories if memory.get("memory_type") == "project_context"],
        limit=3,
    )
    lines = [
        f"用户需求：{_clean_rule_text(interpreted_request) or '未明确'}",
        "",
        "遵循以下规则：",
        f"基础规则：{user_rules or '无'}",
        f"项目规则：{project_rules or '无'}",
    ]
    if validated_task and getattr(validated_task, "skill_needed", False):
        role = getattr(validated_task, "role_profile", None)
        role_text = _role_profile_text(role)
        lines.append(f"本次对话你的角色是：{role_text}")
        basis = _requirement_basis_from_task(validated_task)
        if basis:
            lines.append("需求依据：")
            lines.extend(f"- {item}" for item in basis)
        scope = [str(item) for item in getattr(validated_task, "implementation_scope", []) or [] if str(item)]
        if scope:
            lines.append("实现范围：")
            lines.extend(f"- {_clean_rule_text(item)}" for item in scope)
        out_of_scope = [str(item) for item in getattr(validated_task, "out_of_scope", []) or [] if str(item)]
        if out_of_scope:
            lines.append("不做事项：")
            lines.extend(f"- {_clean_rule_text(item)}" for item in out_of_scope)
        acceptance = [str(item) for item in getattr(validated_task, "acceptance_criteria", []) or [] if str(item)]
        if acceptance:
            lines.append("验收标准：")
            lines.extend(f"- {_clean_rule_text(item)}" for item in acceptance)
        task_rules = _task_rules_from_runtime_skill(runtime_skill, validated_task=validated_task)
        lines.append("任务规则：")
        lines.extend(f"- {item}" for item in (task_rules or ["按需求依据、实现范围和验收标准执行，不新增无依据功能。"]))
    elif runtime_skill:
        task_rules = _task_rules_from_runtime_skill(runtime_skill, validated_task=validated_task)
        if task_rules:
            lines.append(f"本次对话你的角色是：{_role_from_runtime_skill(runtime_skill)}")
            lines.append("任务规则：")
            lines.extend(f"- {item}" for item in task_rules)
    return "\n".join(lines)


def _summarize_rules(memories: list[dict[str, Any]], limit: int = 3) -> str:
    rules = []
    seen = set()
    for memory in memories:
        content = _clean_rule_text(str(memory.get("content") or ""))
        if not content or _is_internal_context_fragment(content):
            continue
        key = content.lower()
        if key in seen:
            continue
        seen.add(key)
        rules.append(content)
        if len(rules) >= limit:
            break
    return "；".join(rules)


def _task_rules_from_runtime_skill(runtime_skill: Any | None, validated_task: Any | None = None) -> list[str]:
    if not runtime_skill:
        return []
    allowed = _allowed_rule_terms(validated_task)
    candidates = [
        getattr(runtime_skill, "goal", ""),
        *list(getattr(runtime_skill, "strategy", []) or []),
        *list(getattr(runtime_skill, "avoid", []) or []),
    ]
    rules = []
    seen = set()
    for item in candidates:
        text = _clean_rule_text(str(item or ""))
        if not text or _is_internal_context_fragment(text):
            continue
        if allowed and not _rule_matches_task(text, allowed):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        rules.append(text)
        if len(rules) >= 5:
            break
    return rules


def _selected_fragments_for_metadata(runtime_skill: Any | None, strict_privacy: bool = False) -> list[dict[str, Any]]:
    fragments = [dict(item) for item in getattr(runtime_skill, "selected_fragments", []) or [] if isinstance(item, dict)]
    if not strict_privacy:
        return fragments
    sanitized = []
    for fragment in fragments:
        item = dict(fragment)
        item.pop("text_preview", None)
        item.pop("source_path", None)
        sanitized.append(item)
    return sanitized


def _fragment_rule_mappings(
    runtime_skill: Any | None,
    validated_task: Any | None,
    final_context: str,
    strict_privacy: bool = False,
) -> list[dict[str, Any]]:
    if not runtime_skill:
        return []
    fragments = _selected_fragments_for_metadata(runtime_skill, strict_privacy=False)
    rules = _final_task_rule_records(runtime_skill, validated_task, final_context)
    if not fragments or not rules:
        return []
    mappings: list[dict[str, Any]] = []
    final_context_sha256 = hashlib.sha256(str(final_context or "").encode("utf-8", errors="replace")).hexdigest()
    for fragment in fragments[:40]:
        source_field = str(fragment.get("source_field") or "")
        for target_field in _target_fields_for_fragment(source_field):
            candidates = [rule for rule in rules if rule["target_field"] == target_field]
            if not candidates:
                continue
            rule, overlap = _best_rule_for_fragment(fragment, candidates)
            score = _fragment_mapping_score(fragment, rule, overlap)
            risk_flags = [str(flag) for flag in fragment.get("risk_flags") or [] if flag]
            if overlap < 0.05:
                risk_flags.append("low_text_similarity")
            mapping = {
                "fragment_id": str(fragment.get("fragment_id") or ""),
                "source_skill_id": str(fragment.get("source_skill_id") or ""),
                "source_kind": str(fragment.get("source_kind") or ""),
                "source_name": str(fragment.get("source_name") or ""),
                "source_field": source_field,
                "source_text_sha256": str(fragment.get("text_sha256") or ""),
                "target_field": target_field,
                "target_rule_index": rule["target_rule_index"],
                "final_rule_hash": rule["final_rule_hash"],
                "final_context_sha256": final_context_sha256,
                "reason": (
                    f"{source_field} fragment mapped to {target_field}; "
                    f"field_affinity={_field_affinity(source_field, target_field):.2f}; "
                    f"lexical_overlap={overlap:.2f}; "
                    f"{fragment.get('reason') or 'selected fragment'}"
                ),
                "score": score,
                "risk": _mapping_risk(str(fragment.get("risk") or "low"), score),
                "risk_flags": list(dict.fromkeys(risk_flags)),
            }
            if not strict_privacy:
                mapping["source_text_preview"] = str(fragment.get("text_preview") or "")[:240]
                mapping["final_rule_preview"] = rule["rule_text"]
            mappings.append(mapping)
    return mappings[:120]


def _final_rule_hashes(runtime_skill: Any | None, validated_task: Any | None, final_context: str, strict_privacy: bool = False) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rule in _final_task_rule_records(runtime_skill, validated_task, final_context):
        item = {
            "target_rule_index": rule["target_rule_index"],
            "final_rule_hash": rule["final_rule_hash"],
        }
        if not strict_privacy:
            item["final_rule_preview"] = rule["rule_text"]
        grouped.setdefault(rule["target_field"], []).append(item)
    return {
        "final_context_sha256": hashlib.sha256(str(final_context or "").encode("utf-8", errors="replace")).hexdigest() if final_context else None,
        "rules": grouped,
    }


def _final_task_rule_records(runtime_skill: Any | None, validated_task: Any | None, final_context: str) -> list[dict[str, Any]]:
    final_context_sha256 = hashlib.sha256(str(final_context or "").encode("utf-8", errors="replace")).hexdigest()
    records: list[dict[str, Any]] = []
    for target_field, values in (
        ("implementation_scope", getattr(validated_task, "implementation_scope", []) if validated_task else []),
        ("acceptance_criteria", getattr(validated_task, "acceptance_criteria", []) if validated_task else []),
    ):
        for index, value in enumerate(values or []):
            text = _clean_rule_text(str(value))
            if text:
                records.append(_rule_record(target_field, index, text, final_context_sha256))
    task_rules = _task_rules_from_runtime_skill(runtime_skill, validated_task=validated_task)
    if not task_rules and validated_task and getattr(validated_task, "skill_needed", False):
        task_rules = ["按需求依据、实现范围和验收标准执行，不新增无依据功能。"]
    for index, value in enumerate(task_rules):
        text = _clean_rule_text(str(value))
        if text:
            records.append(_rule_record("final_context.task_rules", index, text, final_context_sha256))
    return records


def _rule_record(target_field: str, index: int, text: str, final_context_sha256: str) -> dict[str, Any]:
    return {
        "target_field": target_field,
        "target_rule_index": index,
        "rule_text": text,
        "final_rule_hash": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        "final_context_sha256": final_context_sha256,
    }


def _target_fields_for_fragment(source_field: str) -> tuple[str, ...]:
    return {
        "workflow_steps": ("implementation_scope", "final_context.task_rules"),
        "verification": ("acceptance_criteria", "final_context.task_rules"),
        "principles": ("final_context.task_rules",),
        "avoid": ("final_context.task_rules",),
    }.get(source_field, ("final_context.task_rules",))


def _best_rule_for_fragment(fragment: dict[str, Any], rules: list[dict[str, Any]]) -> tuple[dict[str, Any], float]:
    text = str(fragment.get("text_preview") or "")
    ranked = [(_text_overlap_score(text, str(rule.get("rule_text") or "")), rule) for rule in rules]
    ranked.sort(key=lambda item: (item[0], -int(item[1].get("target_rule_index") or 0)), reverse=True)
    return ranked[0][1], ranked[0][0]


def _fragment_mapping_score(fragment: dict[str, Any], rule: dict[str, Any], lexical_overlap: float) -> float:
    source_field = str(fragment.get("source_field") or "")
    target_field = str(rule.get("target_field") or "")
    source_score = min(0.25, max(0.0, float(fragment.get("score") or 0.0) / 20.0))
    score = _field_affinity(source_field, target_field) + (lexical_overlap * 0.5) + source_score
    return round(max(0.0, min(1.0, score)), 3)


def _field_affinity(source_field: str, target_field: str) -> float:
    affinities = {
        ("workflow_steps", "implementation_scope"): 0.42,
        ("workflow_steps", "final_context.task_rules"): 0.3,
        ("verification", "acceptance_criteria"): 0.45,
        ("verification", "final_context.task_rules"): 0.28,
        ("principles", "final_context.task_rules"): 0.36,
        ("avoid", "final_context.task_rules"): 0.32,
    }
    return affinities.get((source_field, target_field), 0.22)


def _mapping_risk(fragment_risk: str, score: float) -> str:
    if fragment_risk == "high":
        return "high"
    if fragment_risk == "medium" or score < 0.35:
        return "medium"
    return "low"


def _text_overlap_score(left: str, right: str) -> float:
    left_terms = _mapping_terms(left)
    right_terms = _mapping_terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / max(1, min(len(left_terms), len(right_terms)))


def _mapping_terms(text: str) -> set[str]:
    lowered = str(text or "").lower()
    separators = ",.;:!?()[]{}，。；：！？（）【】、/|"
    normalized = lowered
    for separator in separators:
        normalized = normalized.replace(separator, " ")
    terms = {part for part in normalized.split() if len(part) >= 2}
    terms.update(ch for ch in lowered if "\u4e00" <= ch <= "\u9fff")
    return terms


def _role_profile_text(role: Any | None) -> str:
    if not role:
        return "任务执行专家"
    primary = str(getattr(role, "primary", "") or "任务执行专家")
    supporting = [str(item) for item in getattr(role, "supporting", []) or [] if str(item)]
    if supporting:
        return f"{primary}，并联合{'、'.join(supporting)}"
    return primary


def _requirement_basis_from_task(validated_task: Any) -> list[str]:
    basis = []
    interpreted = str(getattr(validated_task, "interpreted_request", "") or "")
    if interpreted:
        basis.append(f"用户真实需求：{_clean_rule_text(interpreted)}")
    if getattr(validated_task, "is_followup", False) and getattr(validated_task, "parent_trace_id", ""):
        basis.append(f"继承上一轮任务：{getattr(validated_task, 'parent_trace_id')}")
    reason = getattr(getattr(validated_task, "role_profile", None), "reason", "")
    if reason:
        basis.append(_clean_rule_text(reason))
    return basis[:4]


def _allowed_rule_terms(validated_task: Any | None) -> set[str]:
    if not validated_task:
        return set()
    surfaces = {str(item).lower() for item in getattr(validated_task, "surfaces", []) or []}
    terms = {"检查", "验证", "上下文", "需求", "范围", "测试", "typecheck", "chrome"}
    if surfaces & {"frontend", "ui", "ux"}:
        terms.update({"ui", "ux", "前端", "页面", "布局", "视觉", "信息", "交互", "组件", "样式", "浏览器", "chrome", "设计", "产品", "小程序", "微信", "wxml", "wxss", "wechat", "mini program"})
    if "backend" in surfaces:
        terms.update({"后端", "api", "接口", "服务", "数据库", "server"})
    return terms


def _rule_matches_task(text: str, allowed_terms: set[str]) -> bool:
    lowered = text.lower()
    blocked = ("filament", "roblox", "sales", "marketing", "销售", "营销")
    if any(item in lowered for item in blocked):
        return False
    return any(term.lower() in lowered for term in allowed_terms)


def _role_from_runtime_skill(runtime_skill: Any | None) -> str:
    if not runtime_skill:
        return "通用任务专家"
    domain = str(getattr(runtime_skill, "domain", "") or "").lower()
    name = str(getattr(runtime_skill, "name", "") or "")
    applies_to = str(getattr(runtime_skill, "applies_to", "") or "").lower()
    text = " ".join([domain, name.lower(), applies_to])
    if "software" in text or "engineering" in text or "frontend" in text or "backend" in text or "代码" in text:
        if "frontend" in text or "ui" in text:
            return "前端工程专家"
        return "软件工程专家"
    if "brand" in text or "logo" in text or "design" in text or "品牌" in text:
        return "品牌设计专家"
    if "product" in text or "产品" in text:
        return "产品策略专家"
    return "任务执行专家"


def _clean_rule_text(value: str, max_chars: int = 180) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    text = text.strip(" -")
    concise = _concise_known_rule(text)
    if concise:
        text = concise
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def _concise_known_rule(text: str) -> str:
    lowered = text.lower()
    if "默认使用中文回答" in text and "回答应简洁" in text:
        return "默认使用中文回答；回答简洁、直接、准确；不主动扩展需求范围；重要不确定项先确认"
    known = {
        "complete the engineering task through inspection, minimal change, and verification evidence.": "先检查上下文，再做最小必要改动，并提供验证证据",
        "inspect the relevant repository context before editing.": "修改前检查相关仓库上下文",
        "make the smallest focused change that satisfies the task.": "只做满足需求的最小聚焦改动",
        "run the most relevant test, build, or lint command and report the result honestly.": "运行最相关的测试、构建或 lint，并如实报告结果",
        "do not edit before inspecting relevant files.": "不要在未检查相关文件前编辑",
    }
    return known.get(lowered, "")


def _is_internal_context_fragment(text: str) -> bool:
    lowered = text.lower()
    blocked = (
        "runtime skill:",
        "runtime control:",
        "workflow checks:",
        "seed skill basis:",
        "durable skill basis:",
        "recommended verification recipe",
        "recommended next steps",
        "source guidance",
        "agent personality",
        "home/end",
        "do not paste full seed",
        "default to finding issues",
    )
    if any(item in lowered for item in blocked):
        return True
    return text.startswith("[") and text.endswith("]")


def _feedback_prompt_evidence(prompt: str, strict_privacy: bool) -> dict[str, Any]:
    if strict_privacy:
        return {
            "prompt_sha256": hashlib.sha256(str(prompt or "").encode("utf-8", errors="replace")).hexdigest(),
            "prompt_chars": len(str(prompt or "")),
        }
    return {"prompt_preview": str(prompt or "")[:160]}


def _basis_cache_marker(item: dict[str, Any], include_trust: bool) -> dict[str, Any]:
    metadata = item.get("metadata_json") or {}
    marker = {
        "id": str(item.get("id")),
        "updated_at": item.get("updated_at"),
        "status": item.get("status"),
        "confidence": item.get("confidence"),
        "importance": item.get("importance"),
        "strength": item.get("strength"),
    }
    if include_trust:
        marker["trust_state"] = metadata.get("trust_state")
        marker["success_count"] = metadata.get("success_count")
        marker["failure_count"] = metadata.get("failure_count")
    return marker


def _seed_skill_sort_key(record: dict[str, Any]) -> tuple[int, str, str, str]:
    metadata = record.get("metadata_json") or {}
    layer = str(record.get("_ledger_layer") or metadata.get("ledger_layer") or "user")
    return (
        -{"user": 3, "team": 2, "baseline": 1}.get(layer, 3),
        str(metadata.get("category") or record.get("domain") or ""),
        str(metadata.get("source_path") or record.get("source_id") or record.get("id") or ""),
        str(metadata.get("name") or record.get("content") or ""),
    )


def _records_for_development_audit(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exported = []
    for record in records:
        exported.append(
            {
                "id": record.get("id"),
                "layer": record.get("layer"),
                "record_type": record.get("record_type"),
                "memory_type": record.get("memory_type"),
                "status": record.get("status"),
                "scope": record.get("scope"),
                "domain": record.get("domain"),
                "category": record.get("category"),
                "subcategory": record.get("subcategory"),
                "confidence": record.get("confidence"),
                "importance": record.get("importance"),
                "strength": record.get("strength"),
                "content": record.get("content"),
                "metadata_json": record.get("metadata_json"),
                "updated_at": record.get("updated_at"),
            }
        )
    return exported


def _runtime_skill_evidence_chain(
    runtime_skill: Any,
    distillation: dict[str, Any],
    memory_basis: dict[str, Any],
    include_source_content: bool = False,
) -> dict[str, Any]:
    source_ids = [str(item) for item in getattr(runtime_skill, "source_skill_ids", []) or [] if item]
    source_records = {
        str(record.get("id")): record
        for record in [
            *(memory_basis.get("seed_skills") or []),
            *(memory_basis.get("durable_skills") or []),
        ]
        if record.get("id")
    }
    return {
        "source_skills": [
            _source_skill_evidence(source_id, source_records.get(source_id), include_source_content=include_source_content)
            for source_id in source_ids
        ],
        "distilled_material": {
            "principles": distillation.get("principles") or [],
            "workflow_steps": distillation.get("workflow_steps") or [],
            "verification": distillation.get("verification") or [],
            "avoid": distillation.get("avoid") or [],
            "summary": distillation.get("summary") or "",
            "distilled_from": distillation.get("distilled_from") or getattr(runtime_skill, "distilled_from", []) or [],
        },
        "runtime_skill": _runtime_skill_public_dict(runtime_skill),
        "selected_fragments": getattr(runtime_skill, "selected_fragments", []) or distillation.get("selected_fragments") or [],
        "fragment_rule_mappings": getattr(runtime_skill, "fragment_rule_mappings", []) or [],
        "workflow_required_steps": getattr(runtime_skill, "workflow_required_steps", []) or [],
    }


def _source_skill_evidence(source_id: str, record: dict[str, Any] | None, include_source_content: bool = False) -> dict[str, Any]:
    metadata = (record or {}).get("metadata_json") or {}
    evidence = {
        "id": source_id,
        "name": metadata.get("name") or metadata.get("title") or (record or {}).get("content") or source_id,
        "kind": (record or {}).get("record_type"),
        "category": metadata.get("category") or (record or {}).get("domain"),
        "source_path": metadata.get("source_path") or metadata.get("source_id"),
        "matched": bool(record),
    }
    if include_source_content and record:
        evidence["content_excerpt"] = str(redact_secrets(record.get("content") or ""))[:2000]
        evidence["metadata_json"] = metadata
    return evidence


def _runtime_skill_public_dict(runtime_skill: Any) -> dict[str, Any]:
    skill = runtime_skill.to_dict() if hasattr(runtime_skill, "to_dict") else dict(runtime_skill or {})
    return {
        "name": skill.get("name"),
        "applies_to": skill.get("applies_to"),
        "goal": skill.get("goal"),
        "strategy": skill.get("strategy") or [],
        "first_action": skill.get("first_action") or {},
        "avoid": skill.get("avoid") or [],
        "task_profile": skill.get("task_profile") or {},
        "source_skill_ids": skill.get("source_skill_ids") or [],
        "selected_fragments": skill.get("selected_fragments") or [],
        "fragment_rule_mappings": skill.get("fragment_rule_mappings") or [],
        "workflow_required_steps": skill.get("workflow_required_steps") or [],
        "confidence": skill.get("confidence"),
    }


def _now() -> str:
    return local_now_iso()
