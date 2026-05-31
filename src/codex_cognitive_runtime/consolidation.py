from __future__ import annotations

from collections import defaultdict
from typing import Any

from .model_client import ModelError
from .schema import Evidence, MemoryCandidate
from .taxonomy import tokenize


FRONTEND_STACKS = {
    "Vue": {"vue", "vue2", "vue3", "pinia", "vuex"},
    "React": {"react", "redux", "zustand", "next.js", "nextjs"},
    "jQuery": {"jquery", "jqery", "jq"},
}

PROJECT_TYPES = {
    "管理平台": {"管理平台", "后台", "dashboard", "admin", "中台"},
    "门户": {"门户", "portal", "官网", "站点"},
    "小程序": {"小程序", "miniapp", "微信小程序", "uniapp"},
}

COMMON_FRONTEND_TOPICS = {
    "组件边界": {"组件", "component"},
    "状态管理": {"状态", "state", "store", "pinia", "vuex", "redux", "zustand"},
    "接口封装": {"接口", "api", "请求", "axios", "fetch"},
    "路由与权限": {"路由", "权限", "router", "permission"},
    "构建调试": {"构建", "调试", "vite", "webpack", "build"},
}

STOP_TOPICS = {
    "经验", "项目", "开发", "通用", "应该", "需要", "必须", "避免", "形成", "统一", "稳定",
    "project", "memory", "active", "global", "lesson",
}


class MemoryConsolidator:
    def __init__(self, ledger: Any, model: Any, reviewer: Any):
        self.ledger = ledger
        self.model = model
        self.reviewer = reviewer

    def consolidate(self) -> dict[str, Any]:
        memories = [
            item
            for item in self.ledger.list_memories(status="active", limit=200)
            if item.get("scope") == "project" and item.get("project_key")
        ]
        created = []
        for cluster in self._frontend_experience(memories):
            memory_id = self._store(cluster)
            if memory_id:
                created.append({"id": memory_id, "kind": cluster["kind"], "source_ids": cluster["source_ids"]})
        for cluster in self._project_type_experience(memories):
            memory_id = self._store(cluster)
            if memory_id:
                created.append({"id": memory_id, "kind": cluster["kind"], "source_ids": cluster["source_ids"]})
        for cluster in self._dynamic_cross_project_experience(memories):
            memory_id = self._store(cluster)
            if memory_id:
                created.append({"id": memory_id, "kind": cluster["kind"], "source_ids": cluster["source_ids"]})
        return {"created_count": len(created), "created": created}

    def _frontend_experience(self, memories: list[dict[str, Any]]):
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in memories:
            stacks = _matched_names(str(item.get("content") or ""), FRONTEND_STACKS)
            if not stacks and item.get("subcategory") != "frontend":
                continue
            for stack in stacks or ["前端"]:
                buckets[stack].append(item)
        selected = _distinct_project_items([item for bucket in buckets.values() for item in bucket])
        stacks = sorted(name for name, items in buckets.items() if items)
        if len(stacks) < 2 or len({item.get("project_key") for item in selected}) < 2:
            return []
        topics = _topics(selected)
        return [
            {
                "kind": "frontend_cross_stack",
                "title": "前端通用经验",
                "required_terms": ["前端"],
                "source_items": selected,
                "source_ids": [str(item["id"]) for item in selected if item.get("id")],
                "metadata": {
                    "stacks": stacks,
                    "topics": topics,
                    "domain": "software_engineering",
                    "category": "lesson",
                    "subcategory": "frontend",
                    "triggers": ["前端", "Vue", "React", "jQuery", *topics],
                    "importance": 0.88,
                    "reason": "多项目、多技术栈经验整理形成的前端通用经验。",
                },
            }
        ]

    def _project_type_experience(self, memories: list[dict[str, Any]]):
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in memories:
            for project_type in _matched_names(str(item.get("content") or ""), PROJECT_TYPES):
                buckets[project_type].append(item)
        selected = _distinct_project_items([item for bucket in buckets.values() for item in bucket])
        project_types = sorted(name for name, items in buckets.items() if items)
        if len(project_types) < 2 or len({item.get("project_key") for item in selected}) < 2:
            return []
        topics = _topics(selected)
        return [
            {
                "kind": "project_type_cross_project",
                "title": "项目类型经验",
                "required_terms": ["项目类型"],
                "source_items": selected,
                "source_ids": [str(item["id"]) for item in selected if item.get("id")],
                "metadata": {
                    "project_types": project_types,
                    "topics": topics,
                    "domain": "software_engineering",
                    "category": "lesson",
                    "subcategory": "project_type",
                    "triggers": ["项目类型", "管理平台", "门户", "小程序", *topics],
                    "importance": 0.86,
                    "reason": "多项目类型经验整理形成的项目类型经验。",
                },
            }
        ]

    def _store(self, cluster: dict[str, Any]) -> str | None:
        kind = str(cluster["kind"])
        source_ids = list(cluster["source_ids"])
        if _existing_consolidation(self.ledger.list_memories(status="active", limit=200), kind, source_ids):
            return None
        candidate = self._candidate_from_cluster(cluster)
        if self.ledger.find_active_duplicates(candidate.content, candidate.memory_type, candidate.scope):
            return None
        review = self.reviewer.review(candidate, [])
        if review.get("status") != "active":
            return None
        review = {**review, "kind": kind, "source_ids": source_ids, "consolidation_gate": "rules_clustered_model_abstracted"}
        memory_id = self.ledger.add_candidate(candidate, "active", review)
        for source_id in source_ids:
            self.ledger.upsert_edge(memory_id, source_id, "abstracts", 0.95, {"kind": kind})
            self.ledger.upsert_edge(source_id, memory_id, "evidence_for", 0.95, {"kind": kind})
        return memory_id

    def _dynamic_cross_project_experience(self, memories: list[dict[str, Any]]):
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in memories:
            for topic in _dynamic_topics(item):
                buckets[topic].append(item)

        clusters = []
        for topic, items in sorted(buckets.items(), key=lambda pair: (-len(pair[1]), pair[0])):
            selected = _distinct_project_items(items)
            project_count = len({item.get("project_key") for item in selected})
            if project_count < 2 or len(selected) < 2:
                continue
            if _is_seed_topic(topic):
                continue
            title = _dynamic_title(topic)
            topics = _topics(selected)
            clusters.append(
                {
                    "kind": f"dynamic_cross_project:{topic}",
                    "title": title,
                    "required_terms": [title],
                    "source_items": selected,
                    "source_ids": [str(item["id"]) for item in selected if item.get("id")],
                    "metadata": {
                        "topic": topic,
                        "topics": topics,
                        "domain": str(selected[0].get("domain") or "general"),
                        "category": "lesson",
                        "subcategory": topic,
                        "triggers": [title, topic, *topics],
                        "importance": 0.84,
                        "reason": "动态跨项目主题整理形成的通用经验。",
                    },
                }
            )
            if len(clusters) >= 3:
                break
        return clusters

    def _candidate_from_cluster(self, cluster: dict[str, Any]) -> MemoryCandidate:
        metadata = dict(cluster["metadata"])
        source_items = list(cluster["source_items"])
        abstraction = self._model_abstraction(cluster)
        content = _valid_content(
            str(abstraction.get("content") or ""),
            str(cluster["title"]),
            _fallback_content(cluster),
        )
        triggers = _valid_triggers(abstraction.get("triggers"), metadata["triggers"])
        return MemoryCandidate(
            content=content,
            memory_type="experience",
            proposed_action="store",
            confidence=0.9,
            importance=float(metadata["importance"]),
            ttl="long",
            scope="global",
            domain=str(metadata["domain"]),
            category=str(metadata["category"]),
            subcategory=str(metadata["subcategory"]),
            abstraction_level="principle",
            triggers=triggers,
            evidence=[Evidence(source="consolidation", quote=_quote(source_items))],
            related_memory_ids=list(cluster["source_ids"]),
            reason=str(abstraction.get("reason") or metadata["reason"]),
        )

    def _model_abstraction(self, cluster: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "Consolidate memory cluster into one durable, reusable Chinese experience memory. "
            "The rules have already decided this cluster is eligible. Do not decide eligibility. "
            "Write a concise principle supported only by the evidence. Include the cluster title phrase.\n\n"
            f"Kind: {cluster['kind']}\n"
            f"Required terms: {cluster['required_terms']}\n"
            f"Metadata: {cluster['metadata']}\n"
            f"Source memories: {[{'id': item.get('id'), 'content': item.get('content')} for item in cluster['source_items']]}"
        )
        schema = {"content": "string", "triggers": ["string"], "reason": "string"}
        try:
            result = self.model.complete_json(prompt, schema)
            if isinstance(result, dict):
                return result
        except ModelError:
            return {}
        return {}


def _matched_names(text: str, mapping: dict[str, set[str]]) -> list[str]:
    lowered = text.lower()
    return [name for name, keywords in mapping.items() if any(keyword.lower() in lowered for keyword in keywords)]


def _distinct_project_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {}
    for item in items:
        if item.get("id"):
            by_id[str(item["id"])] = item
    return list(by_id.values())


def _topics(items: list[dict[str, Any]]) -> list[str]:
    topic_scores: dict[str, int] = defaultdict(int)
    for item in items:
        text = str(item.get("content") or "")
        lowered = text.lower()
        for topic, keywords in COMMON_FRONTEND_TOPICS.items():
            if any(keyword.lower() in lowered for keyword in keywords):
                topic_scores[topic] += 1
        for token in tokenize(text):
            if len(token) >= 3 and token not in {"经验", "项目", "开发"}:
                topic_scores[token] += 1
    ranked = sorted(topic_scores.items(), key=lambda pair: (-pair[1], pair[0]))
    return [topic for topic, _score in ranked[:5]] or ["复用边界", "工程约束"]


def _quote(items: list[dict[str, Any]]) -> str:
    parts = []
    for item in items[:6]:
        content = str(item.get("content") or "").strip()
        if content:
            parts.append(content[:120])
    return " | ".join(parts)


def _fallback_content(cluster: dict[str, Any]) -> str:
    metadata = dict(cluster["metadata"])
    if cluster["kind"] == "project_type_cross_project":
        return (
            "项目类型经验：管理平台、门户、小程序等不同交付形态应分别沉淀信息架构、权限入口、端侧约束和发布流程"
            f"；当前跨项目证据集中涉及：{'、'.join(metadata['topics'])}。"
        )
    if str(cluster["kind"]).startswith("dynamic_cross_project:"):
        return (
            f"{cluster['title']}：跨多个项目反复出现的 {metadata['topic']} 问题，应抽象为可复用的工程原则"
            f"；当前跨项目证据集中涉及：{'、'.join(metadata['topics'])}。"
        )
    return (
        "前端通用经验：在"
        f"{'、'.join(metadata['stacks'])}"
        "等不同技术栈项目中反复出现的做法，应优先沉淀为框架无关原则"
        f"；当前跨项目证据集中涉及：{'、'.join(metadata['topics'])}。"
    )


def _valid_content(content: str, title: str, fallback: str) -> str:
    text = content.strip()
    if len(text) < 20 or len(text) > 800:
        return fallback
    if title not in text:
        return fallback
    if any(marker in text for marker in ("可能", "也许", "似乎", "猜测")):
        return fallback
    return text


def _valid_triggers(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    triggers = [str(item).strip() for item in value if str(item).strip()]
    return triggers[:12] or fallback


def _existing_consolidation(memories: list[dict[str, Any]], kind: str, source_ids: list[str]) -> bool:
    wanted = set(source_ids)
    for item in memories:
        review = item.get("review_json") or {}
        if review.get("kind") != kind:
            continue
        existing = set(str(source_id) for source_id in review.get("source_ids") or [])
        if existing == wanted:
            return True
    return False


def _dynamic_topics(item: dict[str, Any]) -> list[str]:
    topics = []
    subcategory = str(item.get("subcategory") or "").strip().lower()
    if subcategory and subcategory not in {"general", "frontend", "project_type"}:
        topics.append(subcategory)
    for trigger in item.get("triggers_json") or []:
        text = str(trigger).strip().lower()
        if _is_dynamic_topic(text):
            topics.append(text)
    deduped = []
    seen = set()
    for topic in topics:
        if topic not in seen:
            seen.add(topic)
            deduped.append(topic)
    return deduped[:8]


def _is_dynamic_topic(topic: str) -> bool:
    if len(topic) < 2 or topic in STOP_TOPICS:
        return False
    if topic in {"vue", "react", "jquery", "管理平台", "门户", "小程序"}:
        return False
    return True


def _is_seed_topic(topic: str) -> bool:
    lowered = topic.lower()
    seed_terms = set()
    for mapping in (FRONTEND_STACKS, PROJECT_TYPES):
        for keywords in mapping.values():
            seed_terms.update(keyword.lower() for keyword in keywords)
    return lowered in seed_terms


def _dynamic_title(topic: str) -> str:
    if topic.endswith("经验"):
        return topic
    if topic.endswith("优化") or topic.endswith("治理") or topic.endswith("封装"):
        return f"{topic}通用经验"
    return f"{topic}经验"
