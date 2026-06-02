from __future__ import annotations

import json
from typing import Any

from .schema import Evidence, MemoryCandidate
from .timeutil import local_now_iso


DEFAULT_AGENTS_MEMORY_VERSION = 2
DEFAULT_AGENTS_MEMORY_TITLE = "全局 AGENTS 协作规则"
DEFAULT_AGENTS_MEMORY_OLD_TITLE = "全局 AGENTS.md 协作规则"


DEFAULT_AGENTS_MEMORY_CONTENT = (
    "全局 AGENTS 协作规则：默认中文回答；回答简洁、直接、准确，避免空话、废话、重复和无意义铺垫；"
    "不主动扩展需求范围，不添加未要求的功能、优化或重构；影响架构、数据、安全、边界的重要不确定项必须先确认，"
    "局部细节可合理假设但需说明。默认只读，除非用户明确要求，不主动修改或删除文件；删除、覆盖、批量修改、"
    "外部写入等不可逆操作前必须确认；不输出密钥、Token、凭证或敏感信息；不伪造执行结果、测试结果、接口返回或运行状态。"
    "工程问题必须从全局到细节完整推导，尽量一次性给出完整、最终、可落地方案；明确区分已确认内容、假设内容、"
    "不确定内容，不确定项单独列出并说明原因；优先保证系统完整性、一致性、长期可维护性和真实落地能力；"
    "当前代码、仓库状态、当前需求优先级高于历史假设。工程规范：避免过度设计和提前抽象，保持实现简单直接可维护；"
    "不创建只使用一次的工具函数、封装或中间层；不添加无意义注释，不在未修改代码上新增注释、类型或文档；"
    "确认无用代码后直接删除；不为低概率理论场景增加复杂度，但保留必要安全保护、边界处理与错误处理；"
    "优先修复根因，复用现有机制。产品与实现态度：默认按最终可落地版本推进；产品初期可推导和引导讨论以明确真实意图；"
    "不迎合用户错误判断，必要时明确反驳并说明理由；在工程和项目问题上保持独立判断，目标是把产品真正做好；"
    "实事求是，能就是能，不能就是不能。多任务或主线/支线并行时必须先识别主线目标、支线目标和相互影响，"
    "清晰规划后一次完整实现；不得为了支线局部闭合破坏、绕开或污染已经确定的主线设计。冲突优先级：安全边界 > 当前用户明确要求 > 工程可执行性与系统正确性 > "
    "系统完整性与长期可维护性 > 简洁性与避免过度设计 > 历史实现风格与既有习惯；冲突无法自动判断时停止执行并明确提出冲突点。"
)


def ensure_default_memories(ledger: Any) -> dict[str, Any]:
    bundled = _existing_bundled_agents_memories(ledger)
    if bundled:
        active = [memory for memory in bundled if memory.get("status") == "active"]
        updated = [_update_default_agents_memory(ledger, memory) for memory in active]
        return {
            "created": [],
            "existing": [str(memory.get("id")) for memory in active if memory.get("id")],
            "updated": [memory_id for memory_id in updated if memory_id],
            "skipped": [] if active else [str(memory.get("id")) for memory in bundled if memory.get("id")],
        }
    existing = ledger.find_active_duplicates(DEFAULT_AGENTS_MEMORY_CONTENT, "user_preference", "global")
    if existing:
        updated = [_update_default_agents_memory(ledger, memory) for memory in existing]
        return {
            "created": [],
            "existing": [str(memory.get("id")) for memory in existing if memory.get("id")],
            "updated": [memory_id for memory_id in updated if memory_id],
            "skipped": [],
        }
    memory_id = ledger.add_candidate(_default_agents_candidate(), "active", _default_agents_review())
    return {"created": [memory_id], "existing": [], "updated": [], "skipped": []}


def _default_agents_candidate() -> MemoryCandidate:
    return MemoryCandidate(
        content=DEFAULT_AGENTS_MEMORY_CONTENT,
        memory_type="user_preference",
        proposed_action="store",
        confidence=0.98,
        importance=0.96,
        ttl="long",
        scope="global",
        domain="software_engineering",
        category="collaboration_rules",
        subcategory="agents",
        abstraction_level="principle",
        triggers=["AGENTS", "全局协作规则", "工程原则", "安全边界", "产品与实现态度"],
        evidence=[Evidence(source="bundled_default_memory", quote=DEFAULT_AGENTS_MEMORY_TITLE)],
        reason="Bundled default global collaboration rules distributed with the plugin.",
    )


def _default_agents_review() -> dict[str, Any]:
    return {
        "decision": "accept",
        "status": "active",
        "storage": "ledger_only",
        "reasons": ["bundled_default_memory", "global_collaboration_preference"],
        "risk_flags": [],
        "source_kind": "bundled_default_memory",
        "source_id": "default:global_agents_collaboration_rules",
        "version": DEFAULT_AGENTS_MEMORY_VERSION,
        "title": DEFAULT_AGENTS_MEMORY_TITLE,
    }


def _existing_bundled_agents_memories(ledger: Any) -> list[dict[str, Any]]:
    rows = ledger.conn.execute(
        """
        SELECT * FROM memories
        WHERE memory_type='user_preference'
          AND scope='global'
          AND review_json LIKE ?
        ORDER BY created_at DESC
        """,
        ('%"source_id": "default:global_agents_collaboration_rules"%',),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _update_default_agents_memory(ledger: Any, memory: dict[str, Any]) -> str | None:
    memory_id = str(memory.get("id") or "")
    content = str(memory.get("content") or "")
    review = dict(memory.get("review_json") or {})
    current_version = int(review.get("version") or 0)
    needs_title_update = DEFAULT_AGENTS_MEMORY_OLD_TITLE in content
    needs_content_update = current_version < DEFAULT_AGENTS_MEMORY_VERSION
    if not memory_id or not needs_title_update and not needs_content_update:
        return None
    updated_content = DEFAULT_AGENTS_MEMORY_CONTENT if needs_content_update else content.replace(DEFAULT_AGENTS_MEMORY_OLD_TITLE, DEFAULT_AGENTS_MEMORY_TITLE, 1)
    review["title"] = DEFAULT_AGENTS_MEMORY_TITLE
    review["version"] = max(int(review.get("version") or 0), DEFAULT_AGENTS_MEMORY_VERSION)
    ledger.conn.execute(
        "UPDATE memories SET content=?, review_json=?, updated_at=? WHERE id=?",
        (updated_content, json.dumps(review, ensure_ascii=False), local_now_iso(), memory_id),
    )
    ledger._commit()
    return memory_id


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("triggers_json", "evidence_json", "review_json", "review_feedback_json"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                data[key] = [] if key != "review_json" else {}
    return data
