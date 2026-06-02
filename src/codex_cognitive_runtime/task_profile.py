from __future__ import annotations

from pathlib import Path
from typing import Any


SURFACE_KEYWORDS = {
    "frontend": {
        "vue",
        "react",
        "ui",
        "css",
        "less",
        "页面",
        "前端",
        "组件",
        "样式",
        "浏览器",
        "chrome",
        "placeholder",
    },
    "backend": {
        "api",
        "接口",
        "后端",
        "service",
        "server",
        "ledger",
        "sqlite",
        "分页接口",
        "python",
    },
    "testing": {
        "测试",
        "验证",
        "typecheck",
        "build",
        "unittest",
        "pytest",
        "browser",
        "联调",
    },
    "governance": {"治理", "workflow", "runtime", "skill", "策略", "审计", "guard", "守卫"},
    "privacy": {"隐私", "脱敏", "权限", "token", "secret", "privacy"},
}


def infer_task_profile(
    prompt: str,
    cwd: str | None = None,
    recent_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    text = " ".join(str(prompt or "").split())
    lowered = text.lower()
    observations = recent_observations or []
    evidence: dict[str, Any] = {"prompt": [], "files": [], "commands": []}
    surfaces = set()
    for surface, keywords in SURFACE_KEYWORDS.items():
        matched = sorted(keyword for keyword in keywords if keyword in lowered)
        if matched:
            surfaces.add(surface)
            evidence["prompt"].extend(matched[:4])
    for observation in observations[-10:]:
        command = str(observation.get("command") or "")
        summary = observation.get("summary") or {}
        if command:
            evidence["commands"].append(command[:120])
        for path in summary.get("files_changed") or []:
            evidence["files"].append(str(path)[:160])
            surfaces.update(_surfaces_for_path(str(path)))
        surfaces.update(_surfaces_for_text(command))
    project_surfaces = _project_surfaces(cwd)
    if not surfaces:
        surfaces.update(project_surfaces[:2])
    if "frontend" in surfaces and "backend" in surfaces:
        task_type = "fullstack_integration_change"
    elif "frontend" in surfaces:
        task_type = "frontend_change"
    elif "backend" in surfaces:
        task_type = "backend_api_change"
    elif "privacy" in surfaces:
        task_type = "privacy_governance_change"
    elif "governance" in surfaces:
        task_type = "runtime_governance_change"
    elif "testing" in surfaces:
        task_type = "verification_change"
    else:
        task_type = "general_task"
    if {"frontend", "backend"}.issubset(set(project_surfaces)) or {"frontend", "backend"}.issubset(surfaces):
        project_type = "mixed"
    elif "frontend" in project_surfaces:
        project_type = "frontend"
    elif "backend" in project_surfaces:
        project_type = "backend"
    else:
        project_type = "unknown"
    if task_type == "fullstack_integration_change" and "testing" not in surfaces:
        surfaces.add("testing")
    confidence = 0.9 if evidence["prompt"] or evidence["files"] or evidence["commands"] else 0.56
    return {
        "project_type": project_type,
        "task_type": task_type,
        "surfaces": sorted(surfaces),
        "confidence": confidence,
        "evidence": {
            "prompt": sorted(set(evidence["prompt"]))[:12],
            "files": list(dict.fromkeys(evidence["files"]))[:12],
            "commands": list(dict.fromkeys(evidence["commands"]))[:8],
            "project_surfaces": project_surfaces,
        },
    }


def workflow_required_steps(task_profile: dict[str, Any]) -> list[str]:
    surfaces = set(task_profile.get("surfaces") or [])
    steps = ["inspect_repository"]
    if surfaces & {"frontend", "backend", "governance", "privacy"}:
        steps.append("execute_change")
    if "backend" in surfaces:
        steps.append("backend_test")
    if "frontend" in surfaces:
        steps.append("frontend_typecheck")
    if "frontend" in surfaces and "testing" in surfaces:
        steps.append("browser_verify")
    if not any(step in steps for step in ("backend_test", "frontend_typecheck", "browser_verify")):
        steps.append("execute_and_verify")
    return list(dict.fromkeys(steps))


def _project_surfaces(cwd: str | None) -> list[str]:
    if not cwd:
        return []
    root = Path(cwd).expanduser()
    surfaces = set()
    if (root / "package.json").exists() or (root / "vite.config.ts").exists():
        surfaces.add("frontend")
    if (root / "src").exists() and list(root.glob("src/**/*.py")):
        surfaces.add("backend")
    if (root / "tests").exists():
        surfaces.add("testing")
    if "ui" in root.name.lower():
        surfaces.add("frontend")
    if "runtime" in root.name.lower():
        surfaces.add("governance")
    return sorted(surfaces)


def _surfaces_for_text(text: str) -> set[str]:
    lowered = text.lower()
    return {surface for surface, keywords in SURFACE_KEYWORDS.items() if any(keyword in lowered for keyword in keywords)}


def _surfaces_for_path(path: str) -> set[str]:
    lowered = path.lower()
    surfaces = set()
    if lowered.endswith((".vue", ".tsx", ".ts", ".jsx", ".js", ".css", ".less")):
        surfaces.add("frontend")
    if lowered.endswith(".py") or "api" in lowered or "service" in lowered:
        surfaces.add("backend")
    if "test" in lowered or "spec" in lowered:
        surfaces.add("testing")
    if "privacy" in lowered or "security" in lowered:
        surfaces.add("privacy")
    if "workflow" in lowered or "runtime" in lowered or "governance" in lowered:
        surfaces.add("governance")
    return surfaces
