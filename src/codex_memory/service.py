from __future__ import annotations

from typing import Any

from .consolidation import MemoryConsolidator
from .config import Config, ensure_state_dir
from .cognitive_governance import CognitiveGovernance
from .cognitive_runtime import CognitiveRuntime
from .engine import MemoryEngine
from .governance import MemoryGovernance
from .knowledge import KnowledgeBuilder
from .ledger import Ledger, project_key_for_cwd
from . import logger
from .local_store import LocalCognitiveStore
from .model_client import CodexMiniClient
from .recall import MemoryRecall
from .review import MemoryReviewer
from .security import sanitize_payload, summarize_payload, summarize_candidate
from .skills import SkillEngine


class MemoryService:
    def __init__(self, config: Config):
        ensure_state_dir(config)
        self.config = config
        self.ledger = Ledger(config.ledger_path)
        self.model = CodexMiniClient(config)
        self.engine = MemoryEngine(config, self.model)
        self.reviewer = MemoryReviewer(config, self.model)
        self.runtime = CognitiveRuntime(self.ledger)
        self.store = LocalCognitiveStore(self.ledger)

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
        if event_type == "user_message":
            prompt = str(payload.get("prompt") or payload.get("text") or "")
            if _memory_storage_opt_out(prompt):
                result = {"event_id": event_id, "candidate_count": 0, "results": [], "skipped": "memory_storage_opt_out"}
                self.ledger.mark_event_processed(event_id)
                self.runtime.finish_event(event_id, result)
                logger.info("memory extraction skipped by user opt-out", event_id=event_id)
                return result
            feedback = self.apply_natural_feedback(
                prompt,
                str(payload.get("session_id") or "") or None,
            )
            if feedback.get("updated"):
                logger.info("natural memory feedback applied", event_id=event_id, feedback=feedback)
        candidates = self.engine.extract(event_type, payload)
        logger.debug("memory candidates extracted", event_id=event_id, candidate_count=len(candidates), candidates=[summarize_candidate(candidate) for candidate in candidates])
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
                memory_id = self.ledger.add_candidate(candidate, status, review, project_key=project_key, session_id=session_id)
                self.runtime.sync_memory(memory_id)
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

    def start_task_from_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.start_task_from_prompt(payload)

    def observe_tool_use(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = self.record_event("after_tool_call", payload, processed=True)
        result = self.runtime.observe_tool_use(payload)
        result["event_id"] = event_id
        return result

    def observe_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = self.record_event("session_end", payload, processed=True)
        result = self.runtime.observe_stop(payload)
        result["event_id"] = event_id
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
        budget = MemoryGovernance(self.ledger).injection_budget(prompt, limit)
        limit = int(budget["limit"])
        memories = self.ledger.list_recallable_memories(cwd=cwd, session_id=session_id, limit=200)
        edges = self.ledger.list_edges([str(item["id"]) for item in memories if item.get("id")])
        result = MemoryRecall(memories, edges=edges).recall(prompt, limit=limit)
        recall_id = self.ledger.record_recall(prompt, result.route, result.memories, cwd=cwd, session_id=session_id, turn_id=turn_id)
        runtime_context = self.runtime.injection_context(prompt, limit=limit, cwd=cwd, session_id=session_id)
        logger.debug("memory recall completed", prompt_chars=len(prompt), route=result.route, recall_id=recall_id, budget=budget, memory_count=len(result.memories))
        return "\n\n".join(part for part in (result.context, runtime_context) if part)

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

    def apply_natural_feedback(self, prompt: str, session_id: str | None = None) -> dict[str, Any]:
        result = MemoryGovernance(self.ledger).apply_natural_feedback(prompt, session_id=session_id)
        logger.debug("natural memory feedback checked", session_id=session_id, result=result)
        return result

    def recall_feedback(self, memory_id: str, outcome: str, note: str = "") -> dict[str, Any]:
        return self.ledger.register_recall_feedback(memory_id, outcome, note)

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

    def runtime_status(self, cwd: str | None = None, session_id: str | None = None) -> dict[str, Any]:
        return self.runtime.runtime_status(cwd=cwd, session_id=session_id)

    def list_memories(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.ledger.list_memories(status=status, limit=limit)

    def export_data(self, limit: int = 5000) -> dict[str, Any]:
        return self.ledger.export_data(limit=limit)

    def wipe_data(self) -> dict[str, Any]:
        return self.ledger.wipe_all()

    def prune_events(self, older_than_days: int | None = None) -> dict[str, Any]:
        return self.ledger.prune_events(older_than_days=older_than_days)

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
    status = {"store_raw_events": config.store_raw_events}
    if config.store_raw_events:
        status["warning"] = "raw event payload storage is enabled"
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
