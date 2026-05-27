from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .taxonomy import classify, tokenize


INTERESTING_SUFFIXES = {".py", ".md", ".json", ".toml", ".sh"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "htmlcov"}


class KnowledgeBuilder:
    def __init__(self, ledger: Any, root: Path):
        self.ledger = ledger
        self.root = root.resolve()

    def build(self, source: str = "all") -> dict[str, Any]:
        created = []
        if source in {"repo", "all"}:
            created.extend(self._repo_knowledge())
        if source in {"git", "all"}:
            created.extend(self._git_knowledge())
        self._link_knowledge()
        return {"source": source, "created_count": len(created), "created": created}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        terms = tokenize(query)
        scored = []
        for record in self.ledger.list_cognitive_records(layer="knowledge", status="active", limit=2000):
            content = str(record.get("content") or "")
            metadata = record.get("metadata_json") or {}
            record_terms = tokenize(" ".join([content, str(metadata.get("path") or ""), str(metadata.get("kind") or "")]))
            score = len(terms & record_terms) * 4
            score += float(record.get("importance") or 0) * 2
            if score > 0:
                item = dict(record)
                item["knowledge_score"] = round(score, 3)
                scored.append(item)
        scored.sort(key=lambda item: item["knowledge_score"], reverse=True)
        return scored[: max(1, min(limit, 100))]

    def audit(self) -> dict[str, Any]:
        records = self.ledger.list_cognitive_records(layer="knowledge", limit=5000)
        by_type: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for record in records:
            by_type[str(record.get("record_type"))] = by_type.get(str(record.get("record_type")), 0) + 1
            metadata = record.get("metadata_json") or {}
            by_source[str(metadata.get("source") or record.get("source_kind") or "unknown")] = by_source.get(str(metadata.get("source") or record.get("source_kind") or "unknown"), 0) + 1
        return {"knowledge_count": len(records), "by_type": by_type, "by_source": by_source}

    def _repo_knowledge(self) -> list[dict[str, Any]]:
        created = []
        for path in self._iter_files():
            text = _read_limited(path)
            if not text:
                continue
            for item in _extract_repo_items(path, self.root, text):
                record = self._record(item)
                if record:
                    created.append({"id": record["id"], "type": record["record_type"], "source": item["source_id"]})
        return created

    def _git_knowledge(self) -> list[dict[str, Any]]:
        try:
            proc = subprocess.run(
                ["git", "log", "--oneline", "--decorate", "-n", "80"],
                cwd=str(self.root),
                check=False,
                text=True,
                capture_output=True,
            )
        except OSError:
            return []
        created = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            kind = "failure_lesson" if any(term in line.lower() for term in ("fix", "bug", "fail", "修复")) else "git_evolution"
            item = {
                "kind": kind,
                "content": f"Git evolution: {line}",
                "source_id": f"git:{line.split()[0]}",
                "source": "git",
                "path": ".git/log",
                "importance": 0.68 if kind == "git_evolution" else 0.78,
            }
            record = self._record(item)
            if record:
                created.append({"id": record["id"], "type": record["record_type"], "source": item["source_id"]})
        return created

    def _record(self, item: dict[str, Any]) -> dict[str, Any]:
        route = classify(str(item["content"]))
        return self.ledger.record_cognitive_record(
            "knowledge",
            str(item["kind"]),
            str(item["source_id"]),
            str(item["content"])[:1200],
            "active",
            "project",
            domain=route["domain"],
            category=route["category"],
            subcategory=route["subcategory"],
            confidence=0.84,
            importance=float(item.get("importance") or 0.7),
            strength=1.0,
            project_key=str(self.root).lower(),
            metadata={"source": item.get("source"), "path": item.get("path"), "kind": item.get("kind")},
            source_kind=str(item.get("source") or "repo"),
        )

    def _link_knowledge(self) -> None:
        records = self.ledger.list_cognitive_records(layer="knowledge", status="active", limit=2000)
        for index, left in enumerate(records):
            for right in records[index + 1 :]:
                relation = _relation(left, right)
                if not relation:
                    continue
                name, weight = relation
                self.ledger.upsert_cognitive_edge(str(left["id"]), str(right["id"]), name, weight, {"source": "knowledge_builder"})
                self.ledger.upsert_cognitive_edge(str(right["id"]), str(left["id"]), name, weight, {"source": "knowledge_builder"})

    def _iter_files(self):
        for path in self.root.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if not path.is_file() or path.suffix.lower() not in INTERESTING_SUFFIXES:
                continue
            if path.stat().st_size > 250_000:
                continue
            yield path


def _extract_repo_items(path: Path, root: Path, text: str) -> list[dict[str, Any]]:
    rel = str(path.relative_to(root))
    lowered = text.lower()
    items = []
    if path.name in {"README.md", "AGENTS.md"} or path.suffix == ".md":
        kind = "business_rule" if "must" in lowered or "必须" in text else "architecture_decision"
        items.append(_item(kind, f"{rel} documents project knowledge: {_snippet(text)}", rel, "repo_doc", 0.78))
    if "create table" in lowered or "cognitive_records" in lowered or "runtime_state_transitions" in lowered:
        items.append(_item("api_contract", f"{rel} defines persistent runtime data contracts.", rel, "repo_code", 0.86))
    if "hook" in lowered or "mcp" in lowered or "workflow" in lowered:
        items.append(_item("runtime_constraint", f"{rel} contains runtime boundary or workflow behavior: {_snippet(text)}", rel, "repo_code", 0.82))
    if path.name.startswith("test_"):
        items.append(_item("test_contract", f"{rel} specifies behavioral contract: {_snippet(text)}", rel, "repo_test", 0.84))
    if "secret" in lowered or "token" in lowered or "password" in lowered:
        items.append(_item("business_rule", f"{rel} contains security-sensitive handling constraints.", rel, "repo_code", 0.84))
    return items


def _item(kind: str, content: str, path: str, source: str, importance: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "content": content,
        "source_id": f"{source}:{path}:{kind}",
        "source": source,
        "path": path,
        "importance": importance,
    }


def _snippet(text: str) -> str:
    compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return compact[:500]


def _read_limited(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:20_000]
    except OSError:
        return ""


def _relation(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, float] | None:
    left_text = str(left.get("content") or "")
    right_text = str(right.get("content") or "")
    overlap = tokenize(left_text) & tokenize(right_text)
    if _contradict(left_text, right_text) and len(overlap) >= 2:
        return "contradicts", 0.9
    if len(overlap) >= 4:
        return "supports", 0.65
    return None


def _contradict(left: str, right: str) -> bool:
    neg = ("不能", "不要", "不应该", "不重叠", "分离", "never", "not ")
    pos = ("必须", "应该", "需要", "统一", "always")
    left_neg = any(item in left.lower() for item in neg)
    right_neg = any(item in right.lower() for item in neg)
    left_pos = any(item in left.lower() for item in pos)
    right_pos = any(item in right.lower() for item in pos)
    return (left_neg and right_pos) or (left_pos and right_neg)
