from __future__ import annotations

import re
from typing import Any


TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+|[\u4e00-\u9fff]{2,}")

DOMAIN_KEYWORDS = {
    "memory_system": {
        "记忆", "召回", "联想", "memory", "review", "hook", "hooks", "mcp", "codex-memory", "ledger"
    },
    "software_engineering": {
        "代码", "工程", "插件", "api", "python", "javascript", "typescript", "sqlite", "测试", "日志", "worker", "codex",
        "vue", "react", "jquery", "前端", "小程序", "管理平台", "门户"
    },
    "life": {"生活", "电灯", "灯泡", "开关", "插座", "家里", "故障", "维修"},
    "water_engineering": {"水利", "水工", "泵站", "闸门", "管网", "河道", "水库", "排水"},
    "user_profile": {"偏好", "希望", "默认", "不要", "必须", "用户", "语气", "回答"},
}

SUBCATEGORY_KEYWORDS = {
    "hook": {"hook", "hooks", "钩子", "userpromptsubmit", "sessionstart", "stop", "posttooluse"},
    "mcp": {"mcp", "server", "服务器"},
    "logging": {"日志", "log", "debug", "debugger", "pretty"},
    "review": {"review", "评审", "准入", "变傻", "质量"},
    "recall": {"召回", "联想", "检索", "注入", "上下文"},
    "ledger": {"ledger", "sqlite", "数据库", "主存储"},
    "testing": {"测试", "模拟", "覆盖率", "验证", "simulation"},
    "frontend": {"前端", "vue", "react", "jquery", "组件", "状态管理", "路由", "构建", "vite", "webpack"},
    "project_type": {"管理平台", "后台", "门户", "官网", "小程序", "miniapp", "dashboard", "portal"},
    "lighting": {"电灯", "灯泡", "开关", "照明", "灯不亮"},
    "water": {"水利", "泵站", "闸门", "管网", "河道"},
}

CATEGORY_KEYWORDS = {
    "preference": {"偏好", "希望", "默认", "不要", "必须", "用中文", "回答"},
    "architecture": {"架构", "设计", "路径", "分离", "不重叠", "层级", "模块"},
    "troubleshooting": {"故障", "失败", "不可用", "修复", "死循环", "问题", "不亮"},
    "lesson": {"经验", "教训", "否则", "需要", "应该", "必须"},
    "workflow": {"流程", "命令", "步骤", "查看", "运行", "发布"},
    "quality": {"评审", "质量", "准入", "精准", "可靠", "覆盖率", "健壮"},
}

NEAR_DUPLICATE_ANCHORS = {
    "mcp", "hook", "hooks", "codex", "codex-memory", "internal", "gpt-5.4-mini",
    "不重叠", "互相调用", "中文", "简洁", "emoji", "debug", "jsonl", "pretty",
    "vue", "react", "jquery", "前端", "管理平台", "门户", "小程序", "接口封装",
    "电灯", "灯泡", "开关", "水利", "闸门", "水位", "测试", "质量", "注入",
}


def enrich_candidate(candidate: Any) -> Any:
    inferred = classify(candidate.content, candidate.memory_type)
    if not candidate.domain:
        candidate.domain = inferred["domain"]
    if not candidate.category:
        candidate.category = inferred["category"]
    if not candidate.subcategory:
        candidate.subcategory = inferred["subcategory"]
    if not candidate.abstraction_level:
        candidate.abstraction_level = inferred["abstraction_level"]
    if not candidate.triggers:
        candidate.triggers = inferred["triggers"]
    return candidate


def classify(text: str, memory_type: str | None = None) -> dict[str, Any]:
    tokens = tokenize(text)
    lowered = text.lower()
    domain = _best_match(tokens, lowered, DOMAIN_KEYWORDS) or "general"
    category = _best_match(tokens, lowered, CATEGORY_KEYWORDS)
    subcategory = _best_match(tokens, lowered, SUBCATEGORY_KEYWORDS)

    if not category:
        category = {
            "user_preference": "preference",
            "experience": "lesson",
            "project_context": "architecture",
            "task_state": "workflow",
            "fact": "fact",
        }.get(str(memory_type or ""), "fact")

    if domain == "memory_system" and not subcategory:
        subcategory = "recall" if "召回" in lowered or "联想" in lowered else "review"

    return {
        "domain": domain,
        "category": category,
        "subcategory": subcategory or "general",
        "abstraction_level": _abstraction_level(lowered),
        "triggers": _triggers(tokens, lowered),
    }


def tokenize(text: str) -> set[str]:
    return {item.lower() for item in TOKEN_RE.findall(text or "") if len(item.strip()) > 1}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def near_duplicate_text(left: str, right: str) -> bool:
    left_norm = _compact_text(left)
    right_norm = _compact_text(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    shorter, longer = sorted((left_norm, right_norm), key=len)
    if len(shorter) >= 8 and shorter in longer:
        return True
    left_grams = _ngrams(left_norm, 2)
    right_grams = _ngrams(right_norm, 2)
    if not left_grams or not right_grams:
        return False
    overlap = len(left_grams & right_grams)
    jaccard = overlap / len(left_grams | right_grams)
    containment = overlap / min(len(left_grams), len(right_grams))
    if jaccard >= 0.68 or containment >= 0.82:
        return True
    shared_anchors = _near_duplicate_anchors(left, right)
    return len(shared_anchors) >= 3 and (jaccard >= 0.48 or containment >= 0.62)


def _best_match(tokens: set[str], lowered: str, mapping: dict[str, set[str]]) -> str | None:
    scored = []
    order = {name: index for index, name in enumerate(mapping)}
    for name, keywords in mapping.items():
        score = 0
        for keyword in keywords:
            key = keyword.lower()
            if key in tokens:
                score += 3
            elif key in lowered:
                score += 2
        if score:
            scored.append((score, name))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], order[item[1]]))
    return scored[0][1]


def _compact_text(text: str) -> str:
    lowered = (text or "").lower()
    for old, new in (
        ("默认使用中文", "默认中文"),
        ("默认用中文", "默认中文"),
        ("使用中文", "中文"),
        ("用中文", "中文"),
        ("用户不希望我主动使用", "不要主动使用"),
        ("用户不希望主动使用", "不要主动使用"),
        ("不希望我主动使用", "不要主动使用"),
        ("不希望主动使用", "不要主动使用"),
        ("不要主动使用", "不要主动使用"),
        ("用户", ""),
        ("我", ""),
        ("并且", ""),
        ("而且", ""),
        ("以及", ""),
        ("且", ""),
    ):
        lowered = lowered.replace(old, new)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", lowered)


def _near_duplicate_anchors(left: str, right: str) -> set[str]:
    left_lower = left.lower()
    right_lower = right.lower()
    return {
        term
        for term in NEAR_DUPLICATE_ANCHORS
        if term.lower() in left_lower and term.lower() in right_lower
    }


def _ngrams(text: str, size: int) -> set[str]:
    if len(text) <= size:
        return {text}
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _abstraction_level(lowered: str) -> str:
    if any(term in lowered for term in ("原则", "必须", "不能", "不要", "应该", "架构", "准入")):
        return "principle"
    if any(term in lowered for term in ("经验", "否则", "遇到", "失败", "处理")):
        return "pattern"
    return "concrete"


def _triggers(tokens: set[str], lowered: str) -> list[str]:
    chosen = []
    for mapping in (SUBCATEGORY_KEYWORDS, DOMAIN_KEYWORDS, CATEGORY_KEYWORDS):
        for keywords in mapping.values():
            for keyword in keywords:
                key = keyword.lower()
                if key in tokens or key in lowered:
                    chosen.append(keyword)
    for token in sorted(tokens, key=len, reverse=True):
        if len(chosen) >= 12:
            break
        if len(token) >= 3:
            chosen.append(token)
    deduped = []
    seen = set()
    for item in chosen:
        text = str(item).strip().lower()
        if text and text not in seen:
            seen.add(text)
            deduped.append(str(item).strip())
    return deduped[:12]
