from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from .security import redact_secrets
from .seed_calibration import seed_scoring_adjustment
from .taxonomy import tokenize
from .timeutil import local_now_iso


DEFAULT_AGENCY_AGENTS_REPO = "https://github.com/msitarzewski/agency-agents.git"
DEFAULT_BUNDLED_AGENCY_AGENTS_PATH = Path(__file__).resolve().parent / "vendor" / "agency_agents"


class AgencySkillSeeder:
    def __init__(self, ledger: Any):
        self.ledger = ledger

    def seed(
        self,
        source: str | None = None,
        repo_url: str | None = None,
        limit: int | None = None,
        category: str | None = None,
        dry_run: bool = False,
        activate: bool = False,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            root = _source_root(source, repo_url, Path(tmp))
            snapshot = _snapshot_metadata(root)
            commit = _git_commit(root) or snapshot.get("source_commit")
            source_repo = str(snapshot.get("source_repo") or repo_url or DEFAULT_AGENCY_AGENTS_REPO)
            license_detected = _license_detected(root)
            if "MIT" not in license_detected.upper():
                return {
                    "source": str(root),
                    "repo_url": source_repo,
                    "commit": commit,
                    "dry_run": dry_run,
                    "ok": False,
                    "error": "unsupported_seed_skill_license",
                    "license_detected": license_detected,
                    "skill_count": 0,
            }
            skills = _load_agent_skills(root, limit=limit, category=category)
            if dry_run:
                return {"source": str(root), "repo_url": source_repo, "commit": commit, "dry_run": True, "ok": True, "license_detected": license_detected, "skill_count": len(skills), "skills": [_summary(item) for item in skills[:20]]}
            created = []
            imported_at = _now()
            for skill in skills:
                active = bool(commit) or activate
                record = self.ledger.record_cognitive_record(
                    "skill",
                    "seed_skill",
                    f"agency-agents:{skill['path']}",
                    skill["content"],
                    "active" if active else "candidate",
                    "global",
                    domain=skill["category"],
                    category="seed_skill",
                    subcategory=skill["slug"],
                    confidence=0.76,
                    importance=0.68,
                    strength=0.95,
                    metadata={
                        "skill_type": "seed_skill",
                        "name": skill["name"],
                        "description": skill["description"],
                        "category": skill["category"],
                        "source_repo": source_repo,
                        "source_commit": commit,
                        "source_path": skill["path"],
                        "license": "MIT",
                        "license_detected": license_detected,
                        "trust_level": "external_seed",
                        "trust_state": "trusted" if commit else "unverified",
                        "source_verified": bool(commit),
                        "content_sha256": _sha256(skill["content"]),
                        "imported_at": imported_at,
                        "success_count": 0,
                        "failure_count": 0,
                        "reuse_count": 0,
                        "frontmatter": skill["frontmatter"],
                    },
                    source_kind="agency_agents_seed",
                )
                created.append({"id": record.get("id"), "name": skill["name"], "path": skill["path"]})
            return {"source": str(root), "repo_url": source_repo, "commit": commit, "dry_run": False, "ok": True, "license_detected": license_detected, "skill_count": len(created), "created": created[:50]}


def default_seed_source_available() -> bool:
    return DEFAULT_BUNDLED_AGENCY_AGENTS_PATH.exists()


def _source_root(source: str | None, repo_url: str | None, tmp: Path) -> Path:
    if source:
        return Path(source).expanduser().resolve()
    if repo_url:
        return _clone_repo(repo_url, tmp / "agency-agents")
    if DEFAULT_BUNDLED_AGENCY_AGENTS_PATH.exists():
        return DEFAULT_BUNDLED_AGENCY_AGENTS_PATH
    raise RuntimeError(f"bundled seed skill snapshot not found: {DEFAULT_BUNDLED_AGENCY_AGENTS_PATH}")


def _snapshot_metadata(root: Path) -> dict[str, Any]:
    path = root / "SNAPSHOT.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def relevant_seed_skills(ledger: Any, prompt: str, limit: int = 4, task_profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    tokens = _expanded_seed_query_tokens(prompt)
    profile_terms = _profile_terms(task_profile)
    tokens.update(profile_terms)
    if not tokens:
        return []
    candidates = []
    for record in ledger.list_cognitive_records(layer="skill", status="active", limit=1000):
        if not is_seed_skill_eligible(record):
            continue
        metadata = record.get("metadata_json") or {}
        success_count = int(metadata.get("success_count") or 0)
        failure_count = int(metadata.get("failure_count") or 0)
        haystack = " ".join(
            [
                str(metadata.get("name") or ""),
                str(metadata.get("description") or ""),
                str(metadata.get("category") or ""),
                str(record.get("content") or "")[:2000],
            ]
        )
        overlap = len(tokens.intersection(set(tokenize(haystack))))
        overlap += _profile_bonus(metadata, haystack, task_profile)
        if overlap <= 0:
            continue
        calibration = seed_scoring_adjustment(metadata, task_profile=task_profile, domain=_profile_domain(task_profile))
        profile_delta = float(calibration.get("score_delta") or 0.0) if calibration.get("applied") else 0.0
        feedback_score = profile_delta + min(2.0, success_count * 0.25) - min(1.0, failure_count * 0.1)
        candidates.append((overlap, feedback_score, float(record.get("importance") or 0), record))
    candidates.sort(key=lambda item: (item[0], item[1], item[2], str(item[3].get("updated_at") or "")), reverse=True)
    return [item[3] for item in candidates[:limit]]


def _profile_terms(task_profile: dict[str, Any] | None) -> set[str]:
    if not task_profile:
        return set()
    terms = set()
    for surface in task_profile.get("surfaces") or []:
        terms.add(str(surface).lower())
        terms.update(
            {
                "frontend": {"ui", "ux", "interface", "react", "vue", "designer", "wechat", "mini", "program", "wxml", "wxss"},
                "backend": {"backend", "api", "server", "architect", "database"},
                "testing": {"tester", "testing", "qa", "evidence", "verification"},
                "governance": {"architect", "reviewer", "workflow", "runtime"},
                "privacy": {"security", "privacy", "threat"},
            }.get(str(surface), set())
        )
    return terms


def _profile_domain(task_profile: dict[str, Any] | None) -> str:
    if not task_profile:
        return ""
    return str(task_profile.get("domain") or task_profile.get("task_domain") or "")


def _profile_bonus(metadata: dict[str, Any], haystack: str, task_profile: dict[str, Any] | None) -> int:
    if not task_profile:
        return 0
    lowered = haystack.lower()
    bonus = 0
    surfaces = set(str(item) for item in task_profile.get("surfaces") or [])
    if "frontend" in surfaces and any(term in lowered for term in ("frontend", "ui designer", "ux", "interface", "react", "vue")):
        bonus += 4
    if "frontend" in surfaces and any(term in lowered for term in ("mini program", "小程序", "wxml", "wxss")):
        bonus += 8
    if "backend" in surfaces and any(term in lowered for term in ("backend", "api", "server", "architect", "database")):
        bonus += 4
    if "testing" in surfaces and any(term in lowered for term in ("tester", "testing", "qa", "evidence", "verification", "api tester")):
        bonus += 4
    if metadata.get("category") in {"engineering", "testing", "design"} and surfaces & {"frontend", "backend", "testing"}:
        bonus += 1
    return bonus


def _expanded_seed_query_tokens(prompt: str) -> set[str]:
    tokens = set(tokenize(prompt))
    lowered = str(prompt or "").lower()
    aliases = {
        "品牌": {"brand", "identity", "visual"},
        "标志": {"logo", "brand", "identity"},
        "logo": {"logo", "brand", "identity"},
        "视觉": {"visual", "design", "identity"},
        "界面": {"ui", "interface", "frontend", "design"},
        "ui": {"ui", "interface", "design"},
        "前端": {"frontend", "react", "vue", "interface"},
        "小程序": {"wechat", "mini", "program", "miniapp", "wxml", "wxss", "frontend", "interface"},
        "微信": {"wechat", "mini", "program", "wxml", "wxss"},
        "wxml": {"wechat", "mini", "program", "wxml", "frontend"},
        "wxss": {"wechat", "mini", "program", "wxss", "frontend", "css"},
        "接口": {"api", "backend", "integration"},
        "后端": {"backend", "api", "architecture"},
        "测试": {"testing", "qa", "evidence"},
        "验证": {"testing", "verification", "evidence"},
        "营销": {"marketing", "growth", "content"},
        "产品": {"product", "manager", "strategy"},
        "销售": {"sales", "outbound", "deal"},
        "数据": {"data", "database", "analytics"},
        "安全": {"security", "threat", "risk"},
    }
    for signal, extra in aliases.items():
        if signal in lowered:
            tokens.update(extra)
    return tokens


def is_seed_skill_eligible(record: dict[str, Any]) -> bool:
    if record.get("record_type") != "seed_skill":
        return False
    status = str(record.get("status") or "")
    if status in {"suppressed", "deprecated", "deleted", "rejected"}:
        return False
    if status != "active":
        return False
    metadata = record.get("metadata_json") or {}
    if metadata.get("trust_level") != "external_seed":
        return False
    if metadata.get("trust_state") in {"suppressed", "deprecated", "disabled"}:
        return False
    return True


def seed_skill_basis_summary(skills: list[dict[str, Any]]) -> str:
    if not skills:
        return "No seed skills matched this task."
    parts = []
    for skill in skills[:4]:
        metadata = skill.get("metadata_json") or {}
        parts.append(f"{metadata.get('name')}: {metadata.get('description')}")
    return " | ".join(parts)


def _clone_repo(repo_url: str, target: Path) -> Path:
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", repo_url, str(target)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError("failed to clone seed skill repository: " + proc.stderr[:500])
    return target


def _git_commit(root: Path) -> str | None:
    if not (root / ".git").exists():
        return None
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _load_agent_skills(root: Path, limit: int | None = None, category: str | None = None) -> list[dict[str, Any]]:
    skills = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith((".git/", ".github/", "integrations/", "examples/")):
            continue
        if category and not relative.startswith(category.strip("/") + "/"):
            continue
        parsed = _parse_agent_file(root, path)
        if not parsed:
            continue
        skills.append(parsed)
        if limit and len(skills) >= limit:
            break
    return skills


def _parse_agent_file(root: Path, path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = _frontmatter(text)
    name = str(frontmatter.get("name") or "").strip()
    description = str(frontmatter.get("description") or "").strip()
    if not name or not description:
        return None
    relative = path.relative_to(root).as_posix()
    category = relative.split("/", 1)[0]
    content = _content(name, description, body)
    return {
        "name": name,
        "description": description,
        "path": relative,
        "category": category,
        "slug": path.stem,
        "frontmatter": frontmatter,
        "content": content,
    }


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    data = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"')
    body = text[end + 4 :].strip()
    return data, body


def _content(name: str, description: str, body: str) -> str:
    clean = str(redact_secrets(body)).strip()
    return f"# Seed Skill: {name}\n\nDescription: {description}\n\n## Source Guidance\n\n{clean[:12000]}"


def _summary(skill: dict[str, Any]) -> dict[str, str]:
    return {"name": skill["name"], "description": skill["description"], "path": skill["path"]}


def _license_detected(root: Path) -> str:
    for name in ("LICENSE", "LICENSE.md", "license", "license.md"):
        path = root / name
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")[:2000]
    return ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _now() -> str:
    return local_now_iso()
