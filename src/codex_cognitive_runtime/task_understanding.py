from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .model_client import ModelError
from .skill_need import SkillNeedDecision
from .task_profile import infer_task_profile

TASK_UNDERSTANDING_TIMEOUT_SECONDS = 12


FOLLOWUP_SIGNALS = (
    "按这个",
    "按照这个",
    "就这样",
    "这样做",
    "实现一版",
    "实现它",
    "继续",
    "继续实现",
    "开始实现",
    "落地",
    "照这个",
    "do it",
    "implement this",
)

UI_TASK_SIGNALS = (
    "ui",
    "ux",
    "页面",
    "界面",
    "布局",
    "样式",
    "美观",
    "视觉",
    "按钮",
    "卡片",
    "面板",
    "看板",
    "仪表盘",
    "表格",
    "列表",
    "vue",
    "react",
    "css",
    "less",
    "交互",
    "信息架构",
)

DIRECT_FACT_SIGNALS = ("现在时间", "几点", "天气", "翻译", "是什么意思", "是什么页面", "家里灯", "灯不亮", "电灯", "灯泡")
MEMORY_STATEMENT_PREFIXES = ("经验：", "事实：", "用户偏好", "项目约定", "记住", "请记住")
BRAND_SIGNALS = ("logo", "标志", "品牌", "视觉识别", "visual identity")
NON_EXECUTION_SIGNALS = ("读取会话", "继续工作", "继续处理", "对吧", "是不是", "是否", "为什么", "解释", "怎么办", "怎么处理")
EXECUTION_SIGNALS = ("实现", "修复", "修改", "调整", "检查", "补", "代码", "跑测试", "验证", "implement", "fix", "debug")
AMBIGUOUS_SHORT_SIGNALS = {"测试", "test"}


@dataclass(frozen=True)
class RoleProfile:
    primary: str
    supporting: list[str] = field(default_factory=list)
    reason: str = ""
    locked_for_task: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "supporting": self.supporting,
            "reason": self.reason,
            "locked_for_task": self.locked_for_task,
        }


@dataclass(frozen=True)
class ValidatedTask:
    interpreted_request: str
    request_type: str
    is_followup: bool
    parent_trace_id: str
    continuity_confidence: float
    skill_needed: bool
    domain: str
    task_type: str
    surfaces: list[str]
    role_profile: RoleProfile
    implementation_scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    clarification_required: bool = False
    uncertainties: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    degraded: bool = False
    source: str = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "interpreted_request": self.interpreted_request,
            "request_type": self.request_type,
            "is_followup": self.is_followup,
            "parent_trace_id": self.parent_trace_id,
            "continuity_confidence": self.continuity_confidence,
            "skill_needed": self.skill_needed,
            "domain": self.domain,
            "task_type": self.task_type,
            "surfaces": self.surfaces,
            "role_profile": self.role_profile.to_dict(),
            "implementation_scope": self.implementation_scope,
            "out_of_scope": self.out_of_scope,
            "acceptance_criteria": self.acceptance_criteria,
            "clarification_required": self.clarification_required,
            "uncertainties": self.uncertainties,
            "corrections": self.corrections,
            "violations": self.violations,
            "degraded": self.degraded,
            "source": self.source,
        }

    def to_skill_decision(self) -> SkillNeedDecision:
        mode = "generate_runtime_skill" if self.skill_needed else "direct_answer"
        complexity = "medium" if self.skill_needed else "low"
        intent = self.task_type or ("software_engineering_change" if self.skill_needed else "direct_answer")
        final = {
            "skill_needed": self.skill_needed,
            "mode": mode,
            "intent": intent,
            "domain": self.domain or "general",
            "requires_memory": self.skill_needed,
            "requires_clarification": self.clarification_required,
            "interpreted_request": self.interpreted_request,
        }
        return SkillNeedDecision(
            self.skill_needed,
            mode,
            intent,
            self.domain or "general",
            complexity,
            self.skill_needed,
            self.clarification_required,
            "task understanding validated request",
            self.interpreted_request,
            {
                "validated_task": self.to_dict(),
                "corrections": self.corrections,
                "violations": self.violations,
                "stages": [
                    {"stage": "model_classification" if self.source == "model" else "fallback_heuristic", "decision": final},
                    {"stage": "rule_validation", "decision": final, "corrections": self.corrections, "violations": self.violations},
                ],
                "final": final,
            },
        )


class TaskUnderstandingEngine:
    def __init__(self, model: Any | None = None):
        self.model = model

    def understand(
        self,
        prompt: str,
        *,
        cwd: str | None = None,
        context_packet: dict[str, Any] | None = None,
        recalled_memory: dict[str, Any] | None = None,
    ) -> ValidatedTask:
        packet = context_packet or {}
        memory = recalled_memory or {}
        model_task = self._model_understand(prompt, packet, memory)
        base = model_task or self._fallback_understand(prompt, cwd=cwd, context_packet=packet)
        return self._validate(base, prompt, cwd=cwd, context_packet=packet)

    def _model_understand(self, prompt: str, context_packet: dict[str, Any], recalled_memory: dict[str, Any]) -> dict[str, Any] | None:
        if self.model is None:
            return None
        prompt_text = (
            "Understand the current Codex task. Use current user input, recent conversation context, active workflow, "
            "and recalled memories to produce the real task object. Return concise JSON only. "
            "If the user says to continue, implement this, or follow the previous plan, inherit the parent task semantics.\n\n"
            f"Current prompt:\n{prompt[:1200]}\n\n"
            f"Context packet:\n{context_packet}\n\n"
            f"Recalled memory classes:\n{recalled_memory}"
        )
        schema = {
            "interpreted_request": "normalized real request",
            "request_type": "direct_answer|planning|implementation|review|debugging",
            "is_followup": False,
            "parent_trace_id": "",
            "continuity_confidence": 0.0,
            "skill_needed": False,
            "domain": "general|software_engineering|brand_design|other",
            "task_type": "frontend_ui_redesign|frontend_change|backend_api_change|direct_answer|general_task",
            "surfaces": ["frontend|ui|ux|backend|testing|governance"],
            "role_profile": {"primary": "role", "supporting": ["roles"], "reason": "why"},
            "implementation_scope": ["scope items"],
            "out_of_scope": ["explicit non-goals"],
            "acceptance_criteria": ["criteria"],
            "clarification_required": False,
            "uncertainties": [],
        }
        try:
            result = self.model.complete_json(prompt_text, schema, timeout_seconds=TASK_UNDERSTANDING_TIMEOUT_SECONDS)
        except TypeError:
            try:
                result = self.model.complete_json(prompt_text, schema)
            except (ModelError, ValueError, TypeError):
                return None
        except (ModelError, ValueError, TypeError):
            return None
        if not isinstance(result, dict):
            return None
        result["_model_source"] = True
        return result

    def _fallback_understand(self, prompt: str, *, cwd: str | None, context_packet: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(str(prompt or "").split())
        lowered = text.lower()
        parent = context_packet.get("candidate_parent_task") or {}
        is_followup = _looks_like_followup(text)
        parent_request = str(parent.get("interpreted_request") or parent.get("prompt_preview") or "").strip()
        interpreted = text
        if is_followup and parent_request:
            interpreted = _join_followup_request(text, parent_request)
        brand_task = any(signal in lowered for signal in BRAND_SIGNALS)
        direct = (lowered in AMBIGUOUS_SHORT_SIGNALS or any(signal in lowered for signal in DIRECT_FACT_SIGNALS) or _looks_like_memory_statement(text)) and not is_followup
        ui_task = _looks_like_ui_task(interpreted) and not brand_task
        profile = infer_task_profile(interpreted, cwd=cwd)
        surfaces = set(profile.get("surfaces") or [])
        if ui_task:
            surfaces.update({"frontend", "ui", "ux"})
        role = RoleProfile("品牌设计专家", ["视觉识别策略专家"], "brand design task") if brand_task else _default_role_profile(surfaces, ui_task=ui_task, reason="deterministic fallback from task text and parent context")
        task_type = "frontend_ui_redesign" if ui_task and ("美观" in interpreted or "乱" in interpreted or "条理" in interpreted) else str(profile.get("task_type") or "general_task")
        if brand_task:
            task_type = "brand_logo_design"
        if ui_task and task_type == "general_task":
            task_type = "frontend_change"
        return {
            "interpreted_request": interpreted,
            "request_type": "direct_answer" if direct else ("implementation" if is_followup or any(s in lowered for s in ("实现", "修复", "改", "调整")) else "planning"),
            "is_followup": is_followup,
            "parent_trace_id": str(parent.get("trace_id") or "") if is_followup else "",
            "continuity_confidence": 0.88 if is_followup and parent_request else 0.0,
            "skill_needed": False if direct else (brand_task or ui_task or any(s in lowered for s in ("实现", "修复", "代码", "页面", "改", "调整", "优化方案"))),
            "domain": "general" if direct else ("brand_design" if brand_task else "software_engineering"),
            "task_type": task_type,
            "surfaces": sorted(surfaces),
            "role_profile": role.to_dict(),
            "implementation_scope": _default_scope(interpreted, ui_task=ui_task),
            "out_of_scope": _default_out_of_scope(ui_task=ui_task),
            "acceptance_criteria": _default_acceptance(ui_task=ui_task),
            "clarification_required": bool(brand_task or (is_followup and not parent_request)),
            "uncertainties": ["当前请求依赖上一轮方案，但未找到可继承任务"] if is_followup and not parent_request else [],
        }

    def _validate(self, raw: dict[str, Any], prompt: str, *, cwd: str | None, context_packet: dict[str, Any]) -> ValidatedTask:
        corrections: list[str] = []
        violations: list[str] = []
        text = " ".join(str(prompt or "").split())
        parent = context_packet.get("candidate_parent_task") or {}
        is_followup = bool(raw.get("is_followup")) or _looks_like_followup(text)
        interpreted = str(raw.get("interpreted_request") or text).strip()
        if is_followup:
            parent_request = str(parent.get("interpreted_request") or parent.get("prompt_preview") or "").strip()
            if parent_request and (interpreted == text or len(interpreted) < 30):
                interpreted = _join_followup_request(text, parent_request)
                corrections.append("inherited_parent_interpreted_request")
            elif not parent_request:
                violations.append("missing_parent_task_for_followup")
        lowered = text.lower()
        brand_task = _looks_like_brand_task(interpreted) or _looks_like_brand_task(text)
        memory_statement = _looks_like_memory_statement(text)
        non_execution = _looks_like_non_execution_request(text) and not _has_execution_signal(text)
        ui_task = (not brand_task) and (
            _looks_like_ui_task(interpreted) or any(str(item) in {"ui", "ux", "frontend"} for item in raw.get("surfaces") or [])
        )
        profile = infer_task_profile(interpreted, cwd=cwd)
        surfaces = {str(item) for item in (raw.get("surfaces") or [])}
        surfaces.update(str(item) for item in (profile.get("surfaces") or []))
        if brand_task:
            surfaces = {surface for surface in surfaces if surface not in {"frontend", "ui", "ux", "backend", "testing"}}
            surfaces.add("design")
        if ui_task:
            before = set(surfaces)
            surfaces.update({"frontend", "ui", "ux"})
            if surfaces != before:
                corrections.append("added_ui_ux_frontend_surfaces")
        role_raw = raw.get("role_profile") if isinstance(raw.get("role_profile"), dict) else {}
        role = RoleProfile(
            str(role_raw.get("primary") or ""),
            [str(item) for item in role_raw.get("supporting") or [] if str(item)],
            str(role_raw.get("reason") or ""),
            True,
        )
        if is_followup and isinstance(parent.get("role_profile"), dict):
            parent_role = parent.get("role_profile") or {}
            if parent_role.get("primary") and _role_is_weaker(role, parent_role):
                role = RoleProfile(
                    str(parent_role.get("primary")),
                    [str(item) for item in parent_role.get("supporting") or [] if str(item)],
                    str(parent_role.get("reason") or "inherited from parent task"),
                    True,
                )
                corrections.append("inherited_parent_role_profile")
        if brand_task:
            if role.primary != "品牌设计专家":
                corrections.append("brand_task_role_profile")
            role = RoleProfile("品牌设计专家", ["视觉识别策略专家"], "brand design task", True)
        elif ui_task and not _role_has_ui_coverage(role):
            role = _default_role_profile(surfaces, ui_task=True, reason="UI task requires UI/UX, frontend, and product design coverage")
            corrections.append("added_ui_joint_role_profile")
        elif not role.primary:
            role = _default_role_profile(surfaces, ui_task=ui_task, reason="default role from validated task surfaces")
            corrections.append("filled_missing_role_profile")
        direct = (lowered in AMBIGUOUS_SHORT_SIGNALS or any(signal in lowered for signal in DIRECT_FACT_SIGNALS) or memory_statement or non_execution) and not (is_followup and parent)
        skill_needed = bool(raw.get("skill_needed"))
        bare_followup_without_parent = is_followup and not parent and not _has_execution_signal(text)
        if bare_followup_without_parent:
            skill_needed = False
            corrections.append("missing_parent_followup_skill_false")
        elif direct:
            skill_needed = False
            if interpreted != text:
                interpreted = text
                corrections.append("direct_answer_preserved_user_request")
            corrections.append("hard_direct_answer_skill_false")
        elif brand_task:
            skill_needed = True
        elif ui_task or any(surface in surfaces for surface in ("frontend", "backend", "governance", "privacy", "testing")):
            skill_needed = True
        scope = [str(item) for item in raw.get("implementation_scope") or [] if str(item).strip()]
        out = [str(item) for item in raw.get("out_of_scope") or [] if str(item).strip()]
        acceptance = [str(item) for item in raw.get("acceptance_criteria") or [] if str(item).strip()]
        if skill_needed and ui_task:
            if not scope:
                scope = _default_scope(interpreted, ui_task=True)
                corrections.append("filled_ui_implementation_scope")
            if not out:
                out = _default_out_of_scope(ui_task=True)
                corrections.append("filled_ui_out_of_scope")
            if not acceptance:
                acceptance = _default_acceptance(ui_task=True)
                corrections.append("filled_ui_acceptance_criteria")
        if skill_needed and (not scope or not acceptance):
            violations.append("missing_scope_or_acceptance_criteria")
        task_type = str(raw.get("task_type") or profile.get("task_type") or "general_task")
        if brand_task:
            task_type = "brand_logo_design"
        elif profile.get("task_type") == "fullstack_integration_change":
            task_type = "fullstack_integration_change"
        elif ui_task and task_type in {"general_task", "frontend_change", "software_engineering_change"}:
            task_type = "frontend_ui_redesign" if any(s in interpreted for s in ("美观", "乱", "条理", "信息架构")) else "frontend_change"
        return ValidatedTask(
            interpreted_request=interpreted,
            request_type=str(raw.get("request_type") or ("implementation" if skill_needed else "direct_answer")),
            is_followup=is_followup,
            parent_trace_id=str(raw.get("parent_trace_id") or parent.get("trace_id") or "") if is_followup else "",
            continuity_confidence=float(raw.get("continuity_confidence") or (0.88 if is_followup and parent else 0.0)),
            skill_needed=skill_needed,
            domain="general" if not skill_needed else ("brand_design" if brand_task else str(raw.get("domain") or "software_engineering")),
            task_type=task_type,
            surfaces=sorted(surfaces),
            role_profile=role,
            implementation_scope=scope,
            out_of_scope=out,
            acceptance_criteria=acceptance,
            clarification_required=bool(raw.get("clarification_required")) or brand_task or ("missing_parent_task_for_followup" in violations),
            uncertainties=[str(item) for item in raw.get("uncertainties") or [] if str(item)],
            corrections=list(dict.fromkeys([*corrections, *[str(item) for item in raw.get("corrections") or []]])),
            violations=list(dict.fromkeys(violations)),
            degraded=raw.get("degraded", False),
            source="model" if raw.get("_model_source") else "deterministic",
        )


def classify_memory_for_task(memories: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "user_preferences": [m for m in memories if m.get("memory_type") == "user_preference"],
        "project_rules": [m for m in memories if m.get("memory_type") == "project_context"],
        "task_memories": [m for m in memories if m.get("memory_type") not in {"user_preference", "project_context"}],
        "excluded_memories": [],
    }


def _looks_like_followup(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(signal in lowered for signal in FOLLOWUP_SIGNALS)


def _looks_like_ui_task(text: str) -> bool:
    lowered = str(text or "").lower()
    for signal in UI_TASK_SIGNALS:
        if signal in {"ui", "ux"}:
            if re.search(rf"(?<![a-z0-9]){signal}(?![a-z0-9])", lowered):
                return True
            continue
        if signal in lowered:
            return True
    return False


def _looks_like_brand_task(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(signal in lowered for signal in BRAND_SIGNALS)


def _looks_like_memory_statement(text: str) -> bool:
    stripped = _strip_leading_label(str(text or "").strip())
    lowered = stripped.lower()
    prefixes = (
        *MEMORY_STATEMENT_PREFIXES,
        "经验:",
        "偏好：",
        "偏好:",
        "规则：",
        "规则:",
        "原则：",
        "原则:",
        "临时测试：",
        "临时测试:",
        "这次只需要临时",
        "水利工程经验",
        "治理规则",
    )
    return stripped.startswith(prefixes) or "经验：" in stripped[:40] or "默认使用中文回答" in stripped or lowered.startswith(("remember ", "note that "))


def _looks_like_non_execution_request(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(signal in lowered for signal in NON_EXECUTION_SIGNALS)


def _has_execution_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(signal in lowered for signal in EXECUTION_SIGNALS)


def _strip_leading_label(text: str) -> str:
    if text.startswith("[") and "]" in text[:120]:
        return text.split("]", 1)[1].strip()
    return text


def _join_followup_request(prompt: str, parent_request: str) -> str:
    clean_prompt = " ".join(str(prompt or "").split())
    clean_parent = " ".join(str(parent_request or "").split())
    if any(signal in clean_prompt for signal in ("实现", "落地", "做")) or any(signal in clean_prompt.lower() for signal in ("implement", "do it")):
        return f"基于上一轮确认的任务：{clean_parent}；现在按该方案实现一版。"
    return f"继续处理上一轮任务：{clean_parent}；当前补充要求：{clean_prompt}"


def _default_role_profile(surfaces: set[str], *, ui_task: bool, reason: str) -> RoleProfile:
    if ui_task or "ui" in surfaces or "ux" in surfaces:
        return RoleProfile("前端工程专家", ["UI/UX 信息架构专家", "软件产品设计专家"], reason)
    if "backend" in surfaces and "frontend" in surfaces:
        return RoleProfile("全栈工程专家", ["软件架构专家", "测试验证专家"], reason)
    if "backend" in surfaces:
        return RoleProfile("后端工程专家", ["软件架构专家", "测试验证专家"], reason)
    return RoleProfile("软件工程专家", ["测试验证专家"], reason)


def _role_has_ui_coverage(role: RoleProfile) -> bool:
    text = " ".join([role.primary, *role.supporting]).lower()
    return ("ui" in text or "ux" in text or "前端" in text) and ("产品" in text or "设计" in text or "ux" in text)


def _role_is_weaker(role: RoleProfile, parent_role: dict[str, Any]) -> bool:
    current = " ".join([role.primary, *role.supporting])
    parent = " ".join([str(parent_role.get("primary") or ""), *[str(item) for item in parent_role.get("supporting") or []]])
    return bool(parent) and len(current) < len(parent)


def _default_scope(interpreted: str, *, ui_task: bool) -> list[str]:
    if ui_task:
        return [
            "梳理页面信息层级和主要排查路径",
            "重构相关前端页面布局、视觉层级和交互状态",
            "保留现有业务能力，不新增无需求依据的入口",
        ]
    return ["检查相关上下文", "实施最小必要变更", "验证变更结果"]


def _default_out_of_scope(*, ui_task: bool) -> list[str]:
    if ui_task:
        return ["不新增无法追溯到需求依据的按钮、卡片或筛选项", "不修改后端 API，除非现有数据无法支撑需求"]
    return ["不做与当前需求无关的重构或功能扩展"]


def _default_acceptance(*, ui_task: bool) -> list[str]:
    if ui_task:
        return [
            "每个新增 UI 元素都能对应明确需求依据",
            "页面主要区域信息层级清晰且无明显重叠",
            "最终使用 Chrome 进行视觉和关键交互验证",
        ]
    return ["运行最相关验证命令并如实报告结果"]
