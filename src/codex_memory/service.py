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
from .memory_retriever import CleanMemoryRetriever
from .recall import MemoryRecall
from .review import MemoryReviewer
from .runtime_skill import RuntimeSkillInjector, RuntimeSkillReviewer, RuntimeSkillSynthesizer
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
        self.runtime = CognitiveRuntime(self.ledger, store_observation_previews=config.store_runtime_observation_previews)
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
                str(payload.get("turn_id") or "") or None,
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
        if not self.config.enable_runtime_observer:
            return {"started": False, "reason": "runtime_observer_disabled"}
        return self.runtime.start_task_from_prompt(payload)

    def observe_tool_use(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = self.record_event("after_tool_call", payload, processed=True)
        if not self.config.enable_runtime_observer:
            return {"observed": False, "reason": "runtime_observer_disabled", "event_id": event_id, "hook_output": {}}
        result = self.runtime.observe_tool_use(payload)
        result["event_id"] = event_id
        return result

    def observe_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = self.record_event("session_end", payload, processed=True)
        if not self.config.enable_runtime_observer:
            return {"observed": False, "reason": "runtime_observer_disabled", "event_id": event_id, "hook_output": {}}
        result = self.runtime.observe_stop(payload)
        result["event_id"] = event_id
        feedback = self._record_runtime_skill_workflow_feedback(payload, result)
        if feedback:
            result["runtime_skill_feedback"] = feedback
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
        skill_decision = SkillNeedClassifier(self.model).classify(prompt)
        active_workflow = self.runtime.active_workflow_for_session(session_id=session_id, turn_id=turn_id, cwd=cwd) if self.config.enable_runtime_observer else None
        if (
            not skill_decision.skill_needed
            and not skill_decision.requires_memory
            and not active_workflow
            and _prompt_can_skip_recall(prompt, skill_decision.intent)
        ):
            logger.debug("prompt context skipped", prompt_chars=len(prompt), reason="direct_answer_without_memory_need")
            return ""

        memories = self.ledger.list_recallable_memories(cwd=cwd, session_id=session_id, limit=200)
        edges = self.ledger.list_edges([str(item["id"]) for item in memories if item.get("id")])
        result = MemoryRecall(memories, edges=edges).recall(prompt, limit=limit)
        recall_id = self.ledger.record_recall(prompt, result.route, result.memories, cwd=cwd, session_id=session_id, turn_id=turn_id)
        runtime_skill_context = ""
        runtime_skill = None
        if skill_decision.skill_needed:
            memory_basis = CleanMemoryRetriever(self.ledger).retrieve(prompt, cwd=cwd, session_id=session_id, limit=limit)
            runtime_skill = RuntimeSkillSynthesizer(self.model).synthesize(prompt, skill_decision, memory_basis)
            review = RuntimeSkillReviewer().review(runtime_skill, skill_decision, memory_basis)
            runtime_skill = review.get("skill")
            runtime_skill_context = RuntimeSkillInjector().format(runtime_skill)
            if runtime_skill_context and runtime_skill:
                injection = self.ledger.record_runtime_skill_injection(
                    prompt,
                    runtime_skill.to_dict(),
                    session_id=session_id,
                    turn_id=turn_id,
                    cwd=cwd,
                    project_key=project_key_for_cwd(cwd) if cwd else None,
                )
                self.ledger.patch_cognitive_record_metadata(
                    str(injection["id"]),
                    {
                        "review": {
                            "status": review.get("status"),
                            "reasons": review.get("reasons") or [],
                            "risk_flags": review.get("risk_flags") or [],
                            "basis_precedence": review.get("basis_precedence"),
                        }
                    },
                )
        runtime_context = ""
        if self.config.enable_runtime_observer:
            if active_workflow or skill_decision.domain == "software_engineering":
                runtime_context = self.runtime.injection_context(prompt, limit=limit, cwd=cwd, session_id=session_id, turn_id=turn_id)
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
            },
        )

    def _record_runtime_skill_natural_feedback(
        self,
        prompt: str,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any] | None:
        outcome = _runtime_skill_feedback_sentiment(prompt)
        if not outcome:
            return None
        injection = self.ledger.latest_runtime_skill_injection(session_id=session_id, turn_id=turn_id, max_age_minutes=30)
        if not injection:
            return None
        matched_reason = "same_turn_recent_feedback" if turn_id else "same_session_recent_feedback"
        feedback_target = _natural_feedback_target(prompt)
        return self.ledger.record_runtime_skill_feedback(
            str(injection["id"]),
            outcome,
            {
                "source": "natural_feedback",
                "prompt_preview": prompt[:160],
                "matched_reason": matched_reason,
                "injection_created_at": injection.get("created_at"),
                "feedback_target": feedback_target,
                "adjust_seed_skill_strength": feedback_target in {"skill_strategy", "first_action"},
            },
        )

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
    ) -> dict[str, Any]:
        return AgencySkillSeeder(self.ledger).seed(source=source, repo_url=repo_url, limit=limit, category=category, dry_run=dry_run)

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
        }
        return status

    def list_memories(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self.ledger.list_memories(status=status, limit=limit)

    def export_data(self, limit: int = 5000) -> dict[str, Any]:
        return self.ledger.export_data(limit=limit)

    def wipe_data(self) -> dict[str, Any]:
        return self.ledger.wipe_all()

    def prune_events(self, older_than_days: int | None = None) -> dict[str, Any]:
        return self.ledger.prune_events(older_than_days=older_than_days)

    def prune_runtime(self, older_than_days: int | None = None, include_recipes: bool = False) -> dict[str, Any]:
        return self.ledger.prune_runtime_records(older_than_days=older_than_days, include_recipes=include_recipes)

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
