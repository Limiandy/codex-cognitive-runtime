from __future__ import annotations

from dataclasses import dataclass
from typing import Any


COGNITIVE_LAYERS = {
    "memory",
    "knowledge",
    "skill",
    "workflow",
    "reasoning",
    "runtime_state",
    "policy",
    "audit",
}

EDGE_RELATIONS = {
    "derived_from",
    "supports",
    "contradicts",
    "supersedes",
    "deprecated_by",
    "merged_into",
    "abstracts",
    "instantiates",
    "uses_skill",
    "uses_knowledge",
    "governed_by",
    "transitioned_by",
}


@dataclass(frozen=True)
class OntologySpec:
    layer: str
    record_type: str
    lifecycle: str
    promotion: str
    recall_strategy: str
    conflict_strategy: str


MEMORY_ONTOLOGY: dict[str, OntologySpec] = {
    "user_preference": OntologySpec("memory", "user_preference", "long", "explicit_review", "high_precision", "latest_explicit_wins"),
    "project_context": OntologySpec("knowledge", "project_context", "long", "evidence_review", "project_scoped", "quarantine_conflict"),
    "experience": OntologySpec("skill", "experience", "long", "cross_context_consolidation", "associative", "merge_or_supersede"),
    "fact": OntologySpec("knowledge", "fact", "long", "evidence_review", "source_anchored", "quarantine_conflict"),
    "task_state": OntologySpec("runtime_state", "task_state", "session", "resume_checkpoint", "low_recall", "latest_state_wins"),
    "relationship": OntologySpec("memory", "relationship", "long", "explicit_review", "contextual", "quarantine_conflict"),
    "temporary": OntologySpec("audit", "temporary", "short", "never_promote_without_signal", "normally_suppressed", "expire"),
}


def spec_for_memory(memory_type: str | None) -> OntologySpec:
    return MEMORY_ONTOLOGY.get(str(memory_type or "temporary"), MEMORY_ONTOLOGY["temporary"])


def cognitive_layer_for_memory(memory: dict[str, Any]) -> str:
    return spec_for_memory(str(memory.get("memory_type") or memory.get("type") or "temporary")).layer


def ontology_snapshot() -> dict[str, Any]:
    return {
        name: {
            "layer": spec.layer,
            "record_type": spec.record_type,
            "lifecycle": spec.lifecycle,
            "promotion": spec.promotion,
            "recall_strategy": spec.recall_strategy,
            "conflict_strategy": spec.conflict_strategy,
        }
        for name, spec in MEMORY_ONTOLOGY.items()
    }
