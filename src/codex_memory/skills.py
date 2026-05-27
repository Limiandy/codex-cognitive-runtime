from __future__ import annotations

from collections import defaultdict
from typing import Any

from .taxonomy import classify, tokenize


class SkillEngine:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def build(self) -> dict[str, Any]:
        created = []
        memories = [item for item in self.ledger.list_memories(status="active", limit=500) if item.get("memory_type") == "experience"]
        for cluster_key, items in self._clusters(memories).items():
            if len(items) < 2:
                continue
            record = self._record_cluster(cluster_key, items)
            if record:
                created.append({"id": record["id"], "source_memory_ids": [item["id"] for item in items]})
        for workflow in self._successful_workflows():
            record = self._record_workflow_skill(workflow)
            if record:
                created.append({"id": record["id"], "source_workflow_ids": [workflow["id"]]})
        self._supersede_versions()
        return {"created_count": len(created), "created": created}

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.ledger.list_cognitive_records(layer="skill", status="active", limit=limit)

    def audit(self) -> dict[str, Any]:
        skills = self.ledger.list_cognitive_records(layer="skill", limit=1000)
        return {
            "skill_count": len(skills),
            "active": len([item for item in skills if item.get("status") == "active"]),
            "deprecated": len([item for item in skills if item.get("status") == "deprecated"]),
            "quarantined": len([item for item in skills if item.get("status") == "quarantined"]),
        }

    def promote(self, skill_id: str) -> dict[str, Any] | None:
        return self.ledger.set_cognitive_record_status(skill_id, "active", {"manual_review": "promote"})

    def deprecate(self, skill_id: str) -> dict[str, Any] | None:
        return self.ledger.set_cognitive_record_status(skill_id, "deprecated", {"manual_review": "deprecate"})

    def record_use(self, skill_id: str, success: bool, workflow_id: str | None = None) -> None:
        skill = self.ledger.get_cognitive_record(skill_id)
        if not skill:
            return
        metadata = dict(skill.get("metadata_json") or {})
        metadata["reuse_count"] = int(metadata.get("reuse_count") or 0) + 1
        metadata["success_count"] = int(metadata.get("success_count") or 0) + (1 if success else 0)
        metadata["failure_count"] = int(metadata.get("failure_count") or 0) + (0 if success else 1)
        metadata["last_workflow_id"] = workflow_id
        delta = 0.12 if success else -0.3
        self.ledger.adjust_cognitive_record_strength(skill_id, delta, metadata)

    def _clusters(self, memories: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for memory in memories:
            route = classify(str(memory.get("content") or ""), str(memory.get("memory_type") or ""))
            key = "|".join(
                [
                    str(memory.get("domain") or route["domain"]),
                    str(memory.get("category") or route["category"]),
                    str(memory.get("subcategory") or route["subcategory"]),
                ]
            )
            clusters[key].append(memory)
        return clusters

    def _record_cluster(self, cluster_key: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        domain, category, subcategory = cluster_key.split("|")
        source_ids = [str(item["id"]) for item in items if item.get("id")]
        source_key = "skill:cluster:" + ":".join(sorted(source_ids)[:8])
        content = _skill_content(domain, category, subcategory, items)
        record = self.ledger.record_cognitive_record(
            "skill",
            "execution_strategy",
            source_key,
            content,
            "active",
            "global",
            domain=domain,
            category=category,
            subcategory=subcategory,
            confidence=0.88,
            importance=0.86,
            strength=1.1,
            metadata={
                "version": 1,
                "source_memory_ids": source_ids,
                "source_workflow_ids": [],
                "success_count": 0,
                "failure_count": 0,
                "reuse_count": 0,
                "last_used_at": None,
                "reasoning_policy": "reuse_clustered_experience",
                "tool_strategy": "inspect_first",
                "workflow_template": ["read_context", "apply_knowledge", "select_skill", "execute_and_verify", "audit_outcome"],
            },
            source_kind="skill_engine",
        )
        for source_id in source_ids:
            self.ledger.upsert_cognitive_edge(str(record["id"]), source_id, "derived_from", 0.9, {"source": "skill_engine"})
        return record

    def _successful_workflows(self) -> list[dict[str, Any]]:
        workflows = []
        for record in self.ledger.list_cognitive_records(layer="workflow", status="active", limit=500):
            if record.get("record_type") != "dynamic_workflow":
                continue
            if self.ledger.latest_state_for("workflow", str(record["id"])) == "completed":
                workflows.append(record)
        return workflows

    def _record_workflow_skill(self, workflow: dict[str, Any]) -> dict[str, Any] | None:
        metadata = workflow.get("metadata_json") or {}
        route = metadata.get("route") or {}
        source_id = f"skill:workflow:{workflow['id']}"
        content = f"Workflow skill: successful workflow for {workflow.get('domain')}/{workflow.get('category')} can reuse steps: {workflow.get('content')}"
        return self.ledger.record_cognitive_record(
            "skill",
            "workflow_template",
            source_id,
            content,
            "active",
            "global",
            domain=workflow.get("domain") or route.get("domain"),
            category=workflow.get("category") or route.get("category"),
            subcategory=workflow.get("subcategory") or route.get("subcategory"),
            confidence=0.82,
            importance=0.8,
            strength=1.05,
            metadata={
                "version": 1,
                "source_memory_ids": [],
                "source_workflow_ids": [workflow["id"]],
                "success_count": 1,
                "failure_count": 0,
                "reuse_count": 0,
                "last_used_at": None,
                "reasoning_policy": "reuse_successful_workflow",
                "tool_strategy": "execute_after_plan",
                "workflow_template": [step.get("name") for step in metadata.get("steps") or []],
            },
            source_kind="workflow",
        )

    def _supersede_versions(self) -> None:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for skill in self.ledger.list_cognitive_records(layer="skill", status="active", limit=1000):
            buckets["|".join([str(skill.get("record_type")), str(skill.get("domain")), str(skill.get("category")), str(skill.get("subcategory"))])].append(skill)
        for skills in buckets.values():
            if len(skills) < 2:
                continue
            winner = sorted(skills, key=lambda item: (float(item.get("strength") or 1), str(item.get("updated_at") or "")), reverse=True)[0]
            for loser in skills:
                if loser["id"] == winner["id"]:
                    continue
                self.ledger.set_cognitive_record_status(str(loser["id"]), "superseded", {"superseded_by": winner["id"]})
                self.ledger.upsert_cognitive_edge(str(loser["id"]), str(winner["id"]), "supersedes", 0.9, {"source": "skill_versioning"})


def _skill_content(domain: str, category: str, subcategory: str, items: list[dict[str, Any]]) -> str:
    terms: dict[str, int] = defaultdict(int)
    for item in items:
        for token in tokenize(str(item.get("content") or "")):
            if len(token) >= 3:
                terms[token] += 1
    top = [term for term, _count in sorted(terms.items(), key=lambda pair: (-pair[1], pair[0]))[:8]]
    return f"Reusable skill for {domain}/{category}/{subcategory}: apply lessons from {len(items)} experiences; key terms: {', '.join(top)}."
