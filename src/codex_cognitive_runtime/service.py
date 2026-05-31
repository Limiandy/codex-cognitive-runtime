from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .consolidation import MemoryConsolidator
from .config import Config, ensure_state_dir
from .cognitive_governance import CognitiveGovernance
from .cognitive_runtime import CognitiveRuntime
from .durable_skills import DurableSkillManager
from .engine import MemoryEngine
from .feedback_classifier import RuntimeSkillFeedbackClassifier
from .governance import MemoryGovernance
from .knowledge import KnowledgeBuilder
from .ledger import Ledger, project_key_for_cwd
from . import logger
from .local_store import LocalCognitiveStore
from .model_client import CodexMiniClient
from .memory_retriever import CleanMemoryRetriever
from .recall import MemoryRecall
from .review import MemoryReviewer
from .runtime_skill import RuntimeSkillInjector, RuntimeSkillReviewer, RuntimeSkillSynthesizer
from .runtime_monitor import RuntimeMonitor, TraceContext
from .security import sanitize_payload, summarize_payload, summarize_candidate
from .seed_skills import AgencySkillSeeder, DEFAULT_AGENCY_AGENTS_REPO
from .skill_need import SkillNeedClassifier
from .skills import SkillEngine


class MemoryService:
    def __init__(self, config: Config):
        ensure_state_dir(config)
        self.config = config
        self.ledger = Ledger(config.ledger_path)
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
        self._runtime_skill_cache: dict[str, tuple[Any, dict[str, Any]]] = {}

    def close(self) -> None:
        self.ledger.close()

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
        return context

    def start_task_from_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.enable_runtime_observer:
            return {"started": False, "reason": "runtime_observer_disabled"}
        return self.runtime.start_task_from_prompt(payload)

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
        return result

    def _stored_event_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.config.store_raw_events:
            return {**payload, "_raw_payload_stored": True}
        sanitized = sanitize_payload(payload)
        sanitized["_raw_payload_stored"] = False
        return sanitized

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
        total_started = time.perf_counter()
        budget = MemoryGovernance(self.ledger).injection_budget(prompt, limit)
        limit = int(budget["limit"])
        skill_need_started = time.perf_counter()
        skill_span = self.monitor.start_span(trace, "skill_need")
        skill_decision = SkillNeedClassifier(self.model).classify(prompt)
        skill_need_latency_ms = _elapsed_ms(skill_need_started)
        self.monitor.event(trace, "skill_need_decision", span_id=str(skill_span["id"]), metadata=skill_decision.to_dict())
        self.monitor.end_span(str(skill_span["id"]), metadata={"duration_ms": skill_need_latency_ms})
        active_workflow = self.runtime.active_workflow_for_session(session_id=session_id, turn_id=turn_id, cwd=cwd) if self.config.enable_runtime_observer else None
        if (
            not skill_decision.skill_needed
            and not skill_decision.requires_memory
            and not active_workflow
            and _prompt_can_skip_recall(prompt, skill_decision.intent)
        ):
            logger.debug("prompt context skipped", prompt_chars=len(prompt), reason="direct_answer_without_memory_need")
            self.monitor.event(trace, "recall_skipped", metadata={"reason": "direct_answer_without_memory_need", "intent": skill_decision.intent})
            self.monitor.complete_trace(trace, final_outcome="direct_answer_no_runtime_skill")
            return ""

        recall_started = time.perf_counter()
        recall_span = self.monitor.start_span(trace, "memory_recall")
        memories = self.ledger.list_recallable_memories(cwd=cwd, session_id=session_id, limit=200)
        edges = self.ledger.list_edges([str(item["id"]) for item in memories if item.get("id")])
        result = MemoryRecall(memories, edges=edges).recall(prompt, limit=limit)
        recall_id = self.ledger.record_recall(prompt, result.route, result.memories, cwd=cwd, session_id=session_id, turn_id=turn_id)
        memory_retrieval_latency_ms = _elapsed_ms(recall_started)
        recalled_ids = [str(item.get("id")) for item in result.memories if item.get("id")]
        self.monitor.event(trace, "memory_recall_completed", span_id=str(recall_span["id"]), subject_type="recall", subject_id=recall_id, metadata={"route": result.route, "memory_count": len(recalled_ids), "memory_ids": recalled_ids, "recall_id": recall_id})
        self.monitor.link(trace, "recall", recall_id, "created")
        for memory_id in recalled_ids:
            self.monitor.link(trace, "memory", memory_id, "used")
        self.monitor.end_span(str(recall_span["id"]), metadata={"duration_ms": memory_retrieval_latency_ms})
        runtime_skill_context = ""
        runtime_skill = None
        cache_hit = False
        fallback_count = 0
        if skill_decision.skill_needed:
            basis_started = time.perf_counter()
            basis_span = self.monitor.start_span(trace, "basis_retrieval")
            memory_basis = CleanMemoryRetriever(self.ledger).retrieve(prompt, cwd=cwd, session_id=session_id, limit=limit)
            memory_retrieval_latency_ms += _elapsed_ms(basis_started)
            memory_basis_ids = [str(item.get("id")) for item in memory_basis.get("memories") or [] if item.get("id")]
            durable_skill_ids = [str(item.get("id")) for item in memory_basis.get("durable_skills") or [] if item.get("id")]
            seed_skill_ids = [str(item.get("id")) for item in memory_basis.get("seed_skills") or [] if item.get("id")]
            basis_metadata = {
                "memory_basis_ids": memory_basis_ids,
                "durable_skill_ids": durable_skill_ids,
                "seed_skill_ids": seed_skill_ids,
                "memory_basis_count": len(memory_basis_ids),
                "durable_skill_count": len(durable_skill_ids),
                "seed_skill_count": len(seed_skill_ids),
            }
            self.monitor.event(trace, "basis_retrieved", span_id=str(basis_span["id"]), metadata=basis_metadata)
            for memory_id in memory_basis_ids:
                self.monitor.link(trace, "memory", memory_id, "used")
            for skill_id in durable_skill_ids:
                self.monitor.link(trace, "durable_skill", skill_id, "used")
            for seed_id in seed_skill_ids:
                self.monitor.link(trace, "seed_skill", seed_id, "used")
            self.monitor.end_span(str(basis_span["id"]))
            cache_key = _runtime_skill_cache_key(prompt, memory_basis, model=self.config.model, strict_privacy=self.config.strict_privacy)
            cached = self._runtime_skill_cache.get(cache_key)
            if cached:
                runtime_skill, review = cached
                cache_hit = True
                self.monitor.event(trace, "runtime_skill_cache_hit", metadata={"cache_hit": True})
            else:
                synthesis_started = time.perf_counter()
                synthesis_span = self.monitor.start_span(trace, "runtime_skill_synthesis")
                runtime_skill = RuntimeSkillSynthesizer(self.model).synthesize(prompt, skill_decision, memory_basis)
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
                injection = self.ledger.record_runtime_skill_injection(
                    prompt,
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
                        "review": {
                            "status": review.get("status"),
                            "reasons": review.get("reasons") or [],
                            "risk_flags": review.get("risk_flags") or [],
                                "basis_precedence": review.get("basis_precedence"),
                        },
                        "latency": {
                            "skill_need_latency_ms": skill_need_latency_ms,
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
                    "skill_need_latency_ms": skill_need_latency_ms,
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
                        "strict_privacy": self.config.strict_privacy,
                        "latency": latency,
                    },
                )
                self.monitor.link(trace, "runtime_skill_injection", str(injection["id"]), "created")
                self.monitor.update_trace(trace, status="runtime_skill_injected", runtime_skill_injection_id=str(injection["id"]))
                self.monitor.end_span(str(injection_span["id"]))
        runtime_context = ""
        if self.config.enable_runtime_observer:
            if active_workflow or skill_decision.domain == "software_engineering":
                runtime_context = self.runtime.injection_context(prompt, limit=limit, cwd=cwd, session_id=session_id, turn_id=turn_id)
                workflow_id = str(active_workflow.get("id")) if active_workflow else None
                self.monitor.event(trace, "workflow_guard_context_injected", subject_type="workflow", subject_id=workflow_id, metadata={"active_workflow": workflow_id, "domain": skill_decision.domain})
                self.monitor.link(trace, "workflow", workflow_id, "used")
        logger.debug("memory recall completed", prompt_chars=len(prompt), route=result.route, recall_id=recall_id, budget=budget, memory_count=len(result.memories))
        memory_context = "" if runtime_skill_context else result.context
        return "\n\n".join(part for part in (runtime_skill_context, memory_context, runtime_context) if part)

    def search_context(
        self,
        user_message: str,
        limit: int = 5,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        memories = self.ledger.list_recallable_memories(cwd=cwd, session_id=session_id, limit=200)
        edges = self.ledger.list_edges([str(item["id"]) for item in memories if item.get("id")])
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
            if matched_step:
                self.monitor.event(trace, "workflow_step_completed", subject_type="workflow", subject_id=workflow_id, metadata={"matched_step_id": matched_step})
            if matched_step == "execute_and_verify" and (summary.get("failed") or observation.get("test_failed")):
                self.monitor.event(trace, "verification_failed", severity="warn", subject_type="workflow", subject_id=workflow_id, metadata={"matched_step_id": matched_step})

    def _trace_stop(self, trace: TraceContext, result: dict[str, Any]) -> None:
        workflow_id = str(result.get("workflow_id") or "") or None
        violations = result.get("violations") or []
        high = [item for item in violations if (item.get("metadata_json") or {}).get("severity") == "high"]
        completed = bool(workflow_id and self.ledger.latest_state_for("workflow", workflow_id) == "completed")
        outcome = "failure" if high else "success" if completed else "unknown"
        self.monitor.event(trace, "stop_observed", subject_type="workflow", subject_id=workflow_id, metadata={"observed": result.get("observed")})
        self.monitor.event(
            trace,
            "workflow_stop_audited",
            subject_type="workflow",
            subject_id=workflow_id,
            metadata={"workflow_id": workflow_id, "observed": result.get("observed"), "completed": completed, "violations": [item.get("id") for item in violations], "high_violation_count": len(high)},
            severity="error" if high else "info",
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
            self.monitor.fail_trace(trace, final_outcome=outcome)
        else:
            self.monitor.complete_trace(trace, final_outcome=outcome)

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
                "adjust_seed_skill_strength": evidence.get("adjust_seed_skill_strength"),
                "adjust_durable_skill_strength": evidence.get("adjust_durable_skill_strength"),
            },
        )
        self.monitor.link(trace, "runtime_skill_feedback", str(feedback.get("id")), "created")
        self.monitor.link(trace, "runtime_skill_injection", metadata.get("injection_id"), "feedback_for")

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
        repo_url: str = DEFAULT_AGENCY_AGENTS_REPO,
        limit: int | None = None,
        category: str | None = None,
        dry_run: bool = False,
        activate: bool = False,
    ) -> dict[str, Any]:
        return AgencySkillSeeder(self.ledger).seed(source=source, repo_url=repo_url, limit=limit, category=category, dry_run=dry_run, activate=activate)

    def list_runtime_skills(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            record
            for record in self.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=max(limit, 200))
            if record.get("record_type") == "injection"
        ][:limit]

    def get_runtime_skill(self, injection_id: str) -> dict[str, Any] | None:
        record = self.ledger.get_cognitive_record(injection_id)
        if not record or record.get("layer") != "runtime_skill" or record.get("record_type") != "injection":
            return None
        return record

    def runtime_skill_feedback(self, injection_id: str, outcome: str, target: str = "final_result", note: str = "") -> dict[str, Any] | None:
        evidence = {
            "source": "manual_cli",
            "feedback_target": target,
            "note": note,
            "adjust_seed_skill_strength": target in {"seed_skill", "skill_strategy", "first_action"},
            "adjust_durable_skill_strength": target in {"durable_skill", "skill_strategy", "first_action", "execution"},
        }
        return self.ledger.record_runtime_skill_feedback(injection_id, outcome, evidence)

    def runtime_skill_audit(self) -> dict[str, Any]:
        records = self.ledger.list_cognitive_records(layer="runtime_skill", status="active", limit=1000)
        injections = [item for item in records if item.get("record_type") == "injection"]
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
        return {"trace": trace, "spans": self.ledger.list_trace_spans(trace_id), "links": self.ledger.list_trace_links(trace_id), "summary": self.monitor.trace_summary(trace_id)}

    def trace_events(self, trace_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return self.monitor.trace_events(trace_id, limit=limit)

    def trace_summary(self, trace_id: str) -> dict[str, Any] | None:
        return self.monitor.trace_summary(trace_id)

    def trace_audit(self) -> dict[str, Any]:
        return self.monitor.trace_audit()

    def export_trace(self, trace_id: str) -> dict[str, Any] | None:
        return self.monitor.export_trace(trace_id)

    def prune_traces(self, older_than_days: int | None = None) -> dict[str, Any]:
        return self.monitor.prune_traces(older_than_days=older_than_days)

    def list_seed_skills(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            record
            for record in self.ledger.list_cognitive_records(layer="skill", limit=max(limit, 200))
            if record.get("record_type") == "seed_skill"
        ][:limit]

    def get_seed_skill(self, skill_id: str) -> dict[str, Any] | None:
        record = self.ledger.get_cognitive_record(skill_id)
        if not record or record.get("record_type") != "seed_skill":
            return None
        return record

    def set_seed_skill_trust_state(self, skill_id: str, trust_state: str) -> dict[str, Any] | None:
        record = self.get_seed_skill(skill_id)
        if not record:
            return None
        patch = {"trust_state": trust_state, "last_status_change_at": _utc_now()}
        status = "active"
        if trust_state == "disabled":
            patch["disabled_at"] = _utc_now()
            status = "deprecated"
        elif trust_state == "suppressed":
            patch["suppressed_at"] = _utc_now()
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
        return DurableSkillManager(self.ledger).list(status=status, limit=limit)

    def get_dynamic_skill(self, skill_id: str) -> dict[str, Any] | None:
        return DurableSkillManager(self.ledger).get(skill_id)

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
            "privacy": _privacy_status(self.config),
            "cognitive": self.runtime.snapshot()["records"],
        }

    def lightweight_status(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger.stats(),
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

    def list_memories(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.ledger.list_memories(status=status, limit=limit)

    def export_data(self, limit: int = 5000) -> dict[str, Any]:
        data = self.ledger.export_data(limit=limit)
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
                            "content_export": "omitted_by_strict_privacy",
                        }
                    if "prompt_preview" in metadata:
                        metadata.pop("prompt_preview", None)
                    record["metadata_json"] = metadata
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


def _prompt_asks_for_memory(prompt: str) -> bool:
    lowered = prompt.lower()
    signals = (
        "偏好",
        "记忆",
        "记得",
        "上次",
        "之前",
        "我的",
        "memory",
        "remember",
        "preference",
        "previous",
    )
    return any(signal in lowered for signal in signals)


def _prompt_can_skip_recall(prompt: str, intent: str) -> bool:
    if _prompt_asks_for_memory(prompt):
        return False
    if intent == "ambiguous_short_prompt":
        return True
    lowered = prompt.lower()
    simple_signals = (
        "天气",
        "几点",
        "现在时间",
        "汇率",
        "翻译",
        "解释这个词",
        "什么意思",
        "weather",
        "time",
        "exchange rate",
        "translate",
        "define",
    )
    return any(signal in lowered for signal in simple_signals)


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


def _runtime_skill_cache_key(prompt: str, memory_basis: dict[str, Any], model: str, strict_privacy: bool) -> str:
    basis = {
        "prompt_sha256": hashlib.sha256(str(prompt or "").encode("utf-8", errors="replace")).hexdigest(),
        "model": model,
        "strict_privacy": bool(strict_privacy),
        "memories": [_basis_cache_marker(item, include_trust=False) for item in memory_basis.get("memories") or []],
        "durable_skills": [_basis_cache_marker(item, include_trust=True) for item in memory_basis.get("durable_skills") or []],
        "seed_skills": [_basis_cache_marker(item, include_trust=True) for item in memory_basis.get("seed_skills") or []],
    }
    payload = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


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


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
