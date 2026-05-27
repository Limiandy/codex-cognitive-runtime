from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .ontology import cognitive_layer_for_memory, ontology_snapshot
from .recall import MemoryRecall
from .state_machine import RuntimeStateMachine
from .taxonomy import classify, near_duplicate_text, tokenize


class CognitiveRuntime:
    def __init__(self, ledger: Any):
        self.ledger = ledger
        self.state = RuntimeStateMachine()

    def begin_event(self, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.transition("event", event_id, "received", event_id=event_id, metadata={"event_type": event_type})
        self.transition("event", event_id, "processing", event_id=event_id, metadata={"event_type": event_type})
        self.ledger.record_cognitive_record(
            "audit",
            "event",
            event_id,
            f"{event_type}: {str(payload)[:500]}",
            "active",
            "session",
            metadata={"event_type": event_type, "payload_keys": sorted(payload.keys())},
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
        reasoning = self.ledger.record_cognitive_record(
            "reasoning",
            "reasoning_policy",
            None,
            _reasoning_policy_content(prompt, route, recalled.memories, selected_skills, selected_knowledge),
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
            },
        )
        steps = _workflow_steps(prompt, route, recalled.memories, selected_skills, selected_knowledge)
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
        }

    def execute_workflow(
        self,
        prompt: str,
        limit: int = 6,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        plan = self.plan_workflow(prompt, limit=limit, cwd=cwd, session_id=session_id)
        workflow_id = str(plan["workflow_id"])
        self.transition("workflow", workflow_id, "running", metadata={"prompt": prompt})
        executed_steps = []
        for index, step in enumerate(plan["steps"]):
            step_id = f"{workflow_id}:step:{index}:{step['name']}"
            self.ledger.record_cognitive_record(
                "workflow",
                "workflow_step",
                step_id,
                f"{step['name']}: {step['reason']}",
                "active",
                "session",
                domain=plan["route"]["domain"],
                category=plan["route"]["category"],
                subcategory=plan["route"]["subcategory"],
                confidence=0.86,
                importance=0.7,
                session_id=session_id,
                metadata={"workflow_id": workflow_id, "step": step, "index": index},
                source_kind="workflow",
            )
            self.transition("workflow_step", step_id, "pending", metadata={"workflow_id": workflow_id})
            self.transition("workflow_step", step_id, "running", metadata={"workflow_id": workflow_id})
            self.transition("workflow_step", step_id, "completed", metadata={"workflow_id": workflow_id})
            self.ledger.upsert_cognitive_edge(workflow_id, step_id, "instantiates", 0.8, {"index": index})
            executed_steps.append({"id": step_id, **step, "state": "completed"})
        self.transition("workflow", workflow_id, "completed", metadata={"step_count": len(executed_steps)})
        return {**plan, "workflow_state": "completed", "executed_steps": executed_steps}

    def injection_context(
        self,
        prompt: str,
        limit: int = 6,
        cwd: str | None = None,
        session_id: str | None = None,
    ) -> str:
        plan = self.plan_workflow(prompt, limit=limit, cwd=cwd, session_id=session_id)
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
        lines.append("workflow: " + " -> ".join(step["name"] for step in plan["steps"]))
        return "\n".join(lines)

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
) -> list[dict[str, Any]]:
    steps = [
        {"name": "read_context", "reason": "读取当前任务与运行约束"},
        {"name": "recall_memory", "reason": f"召回 {len(memories)} 条相关长期经验"},
    ]
    if knowledge:
        steps.append({"name": "apply_knowledge", "reason": f"应用 {len(knowledge)} 条组织知识"})
    if skills:
        steps.append({"name": "select_skill", "reason": f"选择 {len(skills)} 条可复用执行策略"})
    if route.get("domain") == "software_engineering" or any(term in prompt.lower() for term in ("代码", "测试", "实现", "工程")):
        steps.extend(
            [
                {"name": "inspect_repository", "reason": "先让代码库事实约束方案"},
                {"name": "execute_and_verify", "reason": "实施后用测试或命令验证"},
            ]
        )
    else:
        steps.append({"name": "reason_and_answer", "reason": "结合上下文直接推理回答"})
    steps.append({"name": "audit_outcome", "reason": "记录采用情况与治理反馈"})
    return steps


def _reasoning_policy_content(
    prompt: str,
    route: dict[str, Any],
    memories: list[dict[str, Any]],
    skills: list[dict[str, Any]],
    knowledge: list[dict[str, Any]],
) -> str:
    constraints = []
    if memories:
        constraints.append("先采用已召回的长期经验，避免重复犯错")
    if knowledge:
        constraints.append("组织知识优先于临时推测")
    if skills:
        constraints.append("用已抽象技能影响工具选择和执行顺序")
    if route.get("domain") == "software_engineering":
        constraints.append("工程任务必须读代码并验证")
    if not constraints:
        constraints.append("低记忆相关任务保持轻量推理")
    return f"Reasoning policy for {route['domain']}/{route['category']}: " + "；".join(constraints) + f"。Prompt: {prompt[:160]}"
