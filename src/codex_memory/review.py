from __future__ import annotations

import re
from typing import Any

from .config import Config
from .model_client import CodexMiniClient, ModelError
from .schema import ACTIONS, MEMORY_TYPES, SCOPES, TTLS, MemoryCandidate


SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b[A-Za-z0-9_]*(?:token|secret|api[_-]?key)[A-Za-z0-9_]*\s*[:=]", re.I),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
]

SPECULATIVE_PATTERNS = [
    re.compile(r"\b可能\b"),
    re.compile(r"\b似乎\b"),
    re.compile(r"\bmaybe\b", re.I),
    re.compile(r"\bprobably\b", re.I),
    re.compile(r"\bseems like\b", re.I),
]

TEMPORARY_PATTERNS = [
    re.compile(r"这次"),
    re.compile(r"本次"),
    re.compile(r"临时"),
    re.compile(r"当前"),
    re.compile(r"正在"),
    re.compile(r"测试"),
    re.compile(r"\bdebug\b", re.I),
    re.compile(r"\btemporary\b", re.I),
]

EXPLICIT_PREFERENCE_PATTERNS = [
    re.compile(r"记住"),
    re.compile(r"默认"),
    re.compile(r"希望"),
    re.compile(r"偏好"),
    re.compile(r"不要"),
    re.compile(r"必须"),
    re.compile(r"\balways\b", re.I),
    re.compile(r"\bprefer\b", re.I),
]

EXPERIENCE_PATTERNS = [
    re.compile(r"经验"),
    re.compile(r"教训"),
    re.compile(r"如果"),
    re.compile(r"否则"),
    re.compile(r"避免"),
    re.compile(r"解决"),
]

WEAK_EVIDENCE_SOURCES = {"model", "assistant", "memory_context", "hook_context", "summary"}
STRONG_EVIDENCE_SOURCES = {"user_message", "AGENTS.md", "tool", "tool_result", "manual", "config", "consolidation"}


class MemoryReviewer:
    def __init__(self, config: Config, model: CodexMiniClient):
        self.config = config
        self.model = model

    def review(self, candidate: MemoryCandidate, duplicates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        failures = self._hard_failures(candidate)
        if failures:
            return {"status": "rejected", "reasons": failures, "risk_flags": ["hard_failure"]}

        warnings = self._warnings(candidate)
        if duplicates:
            warnings.append("near_duplicate")

        model_review = self._model_review(candidate, duplicates or [])
        model_decision = str(model_review.get("decision", "")).lower()
        risk_flags = list(model_review.get("risk_flags") or [])

        if candidate.confidence < self.config.min_quarantine_confidence:
            return {"status": "rejected", "reasons": ["confidence_below_quarantine_threshold"], "model": model_review}

        if candidate.confidence < self.config.min_active_confidence:
            return {
                "status": "quarantined",
                "reasons": ["confidence_below_active_threshold", *warnings],
                "risk_flags": risk_flags,
                "model": model_review,
            }

        active_blockers = self._active_blockers(candidate)

        if model_decision in {"reject", "rejected"}:
            return {"status": "rejected", "reasons": ["model_reviewer_rejected", *warnings], "model": model_review}

        if model_decision in {"quarantine", "quarantined"} or warnings or risk_flags or active_blockers:
            return {
                "status": "quarantined",
                "reasons": [*warnings, *active_blockers] or ["model_reviewer_requested_quarantine"],
                "risk_flags": risk_flags,
                "model": model_review,
            }

        return {"status": "active", "reasons": ["passed_review"], "risk_flags": [], "model": model_review}

    def _hard_failures(self, candidate: MemoryCandidate) -> list[str]:
        reasons = []
        if not candidate.content or len(candidate.content) < 8:
            reasons.append("content_too_short")
        if len(candidate.content) > 2000:
            reasons.append("content_too_long")
        if candidate.memory_type not in MEMORY_TYPES:
            reasons.append("invalid_memory_type")
        if candidate.proposed_action not in ACTIONS:
            reasons.append("invalid_action")
        if candidate.ttl not in TTLS:
            reasons.append("invalid_ttl")
        if candidate.scope not in SCOPES:
            reasons.append("invalid_scope")
        if candidate.proposed_action == "skip":
            reasons.append("model_requested_skip")
        if not candidate.evidence:
            reasons.append("missing_evidence")
        if any(not e.quote or not e.source for e in candidate.evidence):
            reasons.append("incomplete_evidence")
        if any(pattern.search(candidate.content) for pattern in SECRET_PATTERNS):
            reasons.append("contains_secret_like_text")
        if any(pattern.search(e.quote) for e in candidate.evidence for pattern in SECRET_PATTERNS):
            reasons.append("evidence_contains_secret_like_text")
        if _is_noise(candidate.content) and candidate.memory_type != "temporary":
            reasons.append("routine_test_or_chatter")
        return reasons

    def _warnings(self, candidate: MemoryCandidate) -> list[str]:
        warnings = []
        if candidate.ttl == "long" and candidate.memory_type in {"temporary", "task_state"}:
            warnings.append("unstable_type_marked_long_ttl")
        if candidate.memory_type in {"user_preference", "relationship"} and candidate.scope == "session":
            warnings.append("high_impact_memory_session_scoped")
        if any(pattern.search(candidate.content) for pattern in SPECULATIVE_PATTERNS):
            warnings.append("speculative_content")
        if candidate.importance < 0.35 and candidate.ttl == "long":
            warnings.append("low_importance_long_ttl")
        if _has_temporary_language(candidate) and candidate.ttl == "long":
            warnings.append("temporary_language_marked_long")
        if candidate.memory_type == "temporary":
            warnings.append("temporary_memory_never_active")
        if candidate.memory_type == "task_state" and not _is_resume_point(candidate.content):
            warnings.append("task_state_not_resume_point")
        if any(_weak_source(e.source) for e in candidate.evidence):
            warnings.append("weak_evidence_source")
        return warnings

    def _active_blockers(self, candidate: MemoryCandidate) -> list[str]:
        blockers = []
        if not _has_strong_evidence(candidate):
            blockers.append("active_requires_strong_evidence")
        if candidate.confidence < 0.88:
            blockers.append("active_requires_high_confidence")
        if candidate.importance < 0.55:
            blockers.append("active_requires_cross_session_importance")
        if _has_temporary_language(candidate):
            blockers.append("active_rejects_temporary_language")
        if _external_action_without_confirmation(candidate):
            blockers.append("external_action_without_confirmation")
        if _unsafe_skip_reasoning_or_evidence(candidate):
            blockers.append("unsafe_skip_reasoning_or_evidence")
        if _supply_chain_single_vendor_risk(candidate.content):
            blockers.append("supply_chain_single_vendor_risk")
        if candidate.memory_type == "user_preference":
            if candidate.scope == "session":
                blockers.append("preference_session_scope_not_active")
            if not _explicit_preference(candidate):
                blockers.append("preference_requires_explicit_signal")
            if _overbroad_injection_preference(candidate.content):
                blockers.append("overbroad_injection_preference")
            if _low_quality_verbose_preference(candidate.content):
                blockers.append("low_quality_verbose_preference")
            if _overbroad_visual_style_preference(candidate.content):
                blockers.append("overbroad_visual_style_preference")
        elif candidate.memory_type == "project_context":
            if candidate.scope == "session":
                blockers.append("project_context_session_scope_not_active")
            if _mcp_hook_mutual_call_claim(candidate.content):
                blockers.append("mcp_hook_mutual_call_requires_manual_review")
        elif candidate.memory_type == "experience":
            if not any(pattern.search(candidate.content) for pattern in EXPERIENCE_PATTERNS):
                blockers.append("experience_requires_lesson_signal")
        elif candidate.memory_type == "fact":
            if candidate.confidence < 0.92:
                blockers.append("fact_requires_very_high_confidence")
        elif candidate.memory_type == "task_state":
            blockers.append("task_state_not_active")
        elif candidate.memory_type == "temporary":
            blockers.append("temporary_not_active")
        elif candidate.memory_type == "relationship":
            blockers.append("relationship_requires_manual_review")
        return blockers

    def _model_review(self, candidate: MemoryCandidate, duplicates: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (
            "Review candidate memory. Your job is to find reasons this should not enter long-term memory. "
            "Return decision active, quarantine, or reject.\n\n"
            f"Candidate:\n{candidate.to_dict()}\n\nDuplicates:\n{duplicates[:3]}"
        )
        schema = {"decision": "active|quarantine|reject", "reasons": ["string"], "risk_flags": ["string"]}
        try:
            result = self.model.complete_json(prompt, schema)
            if isinstance(result, dict):
                return result
        except ModelError as exc:
            return {"decision": "quarantine", "reasons": [str(exc)], "risk_flags": ["model_review_failed"]}
        return {"decision": "quarantine", "reasons": ["invalid_model_review"], "risk_flags": ["model_review_failed"]}


def _is_noise(content: str) -> bool:
    text = content.strip().lower()
    return text in {"这是一条测试消息", "普通测试消息", "test", "hello", "hi"} or "这是一条普通测试消息" in text


def _has_temporary_language(candidate: MemoryCandidate) -> bool:
    text = f"{candidate.content}\n" + "\n".join(e.quote for e in candidate.evidence)
    return any(pattern.search(text) for pattern in TEMPORARY_PATTERNS)


def _weak_source(source: str) -> bool:
    lowered = source.strip().lower()
    return lowered in WEAK_EVIDENCE_SOURCES or "context" in lowered or "summary" in lowered


def _has_strong_evidence(candidate: MemoryCandidate) -> bool:
    return any(e.source in STRONG_EVIDENCE_SOURCES and e.quote.strip() for e in candidate.evidence)


def _explicit_preference(candidate: MemoryCandidate) -> bool:
    text = f"{candidate.content}\n" + "\n".join(e.quote for e in candidate.evidence)
    return any(pattern.search(text) for pattern in EXPLICIT_PREFERENCE_PATTERNS)


def _overbroad_injection_preference(content: str) -> bool:
    text = content.lower()
    if not any(term in text for term in ("每次", "所有项目", "所有测试建议", "每次回答", "所有回答")):
        return False
    return any(term in text for term in ("注入", "完整流程", "流程提醒", "测试建议", "长期注入", "检查清单", "checklist"))


def _low_quality_verbose_preference(content: str) -> bool:
    text = content.lower()
    verbose_terms = (
        "极长",
        "特别长",
        "越长越好",
        "铺垫很多",
        "大量铺垫",
        "长篇铺垫",
        "very long",
        "verbose",
    )
    answer_terms = ("回答", "回复", "写作", "文章", "response", "answer", "writing")
    return any(term in text for term in verbose_terms) and any(term in text for term in answer_terms)


def _overbroad_visual_style_preference(content: str) -> bool:
    text = content.lower()
    design_terms = ("设计", "ui", "视觉", "visual", "design")
    broad_terms = ("所有设计", "所有ui", "所有视觉", "以后所有", "全部设计", "all designs", "every design")
    mandate_terms = ("必须", "都用", "使用", "采用", "偏好", "must", "always use", "prefer")
    noisy_style_terms = (
        "复杂渐变",
        "巨大渐变",
        "高饱和",
        "黑金配色",
        "noisy gradient",
        "complex gradient",
        "huge gradient",
        "high saturation",
        "black-gold",
    )
    return (
        any(term in text for term in design_terms)
        and any(term in text for term in broad_terms)
        and any(term in text for term in mandate_terms)
        and any(term in text for term in noisy_style_terms)
    )


def _supply_chain_single_vendor_risk(content: str) -> bool:
    text = content.lower()
    procurement_terms = ("采购", "供应商", "供货商", "supplier", "vendor", "sourcing", "procurement")
    default_terms = ("默认", "以后", "总是", "全部", "都", "always", "default")
    cheapest_terms = ("最便宜", "最低价", "最低成本", "cheapest", "lowest price", "lowest-cost")
    single_source_terms = ("单一供应商", "单一供货商", "单一来源", "single vendor", "single supplier", "single-source")
    return (
        any(term in text for term in procurement_terms)
        and any(term in text for term in default_terms)
        and any(term in text for term in cheapest_terms)
        and any(term in text for term in single_source_terms)
    )


def _mcp_hook_mutual_call_claim(content: str) -> bool:
    text = content.lower()
    if not ("mcp" in text and "hook" in text):
        return False
    if any(term in text for term in ("不能互相调用", "不得互相调用", "不允许互相调用", "不能调用", "路径隔离", "不重叠")):
        return False
    return any(term in text for term in ("互相调用", "互调", "统一链路", "合并链路", "单一链路", "同一链路"))


def _external_action_without_confirmation(candidate: MemoryCandidate) -> bool:
    text = f"{candidate.content}\n" + "\n".join(e.quote for e in candidate.evidence)
    lowered = text.lower()
    no_confirm = (
        "不用确认",
        "不需要确认",
        "无需确认",
        "不用让我确认",
        "无需让我确认",
        "不要确认",
        "直接发送",
        "直接发邮件",
        "直接下单",
        "直接购买",
        "直接付款",
        "直接支付",
        "直接转账",
        "直接删除",
        "直接覆盖",
        "直接改日程",
        "without confirmation",
        "without asking",
    )
    if not any(term in lowered for term in no_confirm):
        return False
    external_actions = (
        "发送",
        "发邮件",
        "邮件",
        "下单",
        "购买",
        "付款",
        "支付",
        "转账",
        "删除",
        "覆盖",
        "改日程",
        "日程",
        "send",
        "email",
        "purchase",
        "pay",
        "delete",
        "overwrite",
    )
    return any(term in lowered for term in external_actions)


def _unsafe_skip_reasoning_or_evidence(candidate: MemoryCandidate) -> bool:
    text = f"{candidate.content}\n" + "\n".join(e.quote for e in candidate.evidence)
    lowered = text.lower()
    high_stakes_or_reasoning = (
        "数学",
        "证明",
        "证据",
        "来源",
        "研究",
        "法律",
        "合同",
        "医疗",
        "诊断",
        "投资",
        "金融",
        "测试",
        "验证",
        "math",
        "proof",
        "evidence",
        "source",
        "legal",
        "medical",
        "test",
        "verify",
    )
    skip_reasoning = (
        "不用证明",
        "不要证明",
        "无需证明",
        "不用证据",
        "不要证据",
        "不用来源",
        "不要来源",
        "只引用一个来源",
        "一个来源就够",
        "单一来源",
        "不需要多来源",
        "无需多来源",
        "不用验证",
        "不用测试",
        "跳过测试",
        "跳过验证",
        "直接给结论",
        "不用说明不确定",
        "don't prove",
        "no proof",
        "only one source",
        "single source",
        "single-source",
        "no need for multiple sources",
        "skip tests",
        "skip verification",
        "just answer",
    )
    if not any(term in lowered for term in high_stakes_or_reasoning):
        return False
    return any(term in lowered for term in skip_reasoning)


def _is_resume_point(content: str) -> bool:
    return any(marker in content for marker in ("未完成", "下一步", "恢复", "待办", "继续处理"))
