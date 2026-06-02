from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .jsonutil import extract_json_object
from . import logger
from .security import sanitize_model_result, redact_secrets


class ModelError(RuntimeError):
    pass


class CodexMiniClient:
    def __init__(self, config: Config):
        self.config = config

    def complete_json(
        self,
        prompt: str,
        schema_hint: dict[str, Any] | None = None,
        timeout_seconds: int | float | None = None,
    ) -> dict[str, Any]:
        if os.environ.get("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"):
            result = self._fake_response(prompt)
            logger.debug("model fake response", prompt_chars=len(prompt), schema_keys=list((schema_hint or {}).keys()), result=sanitize_model_result(result))
            return result

        full_prompt = self._build_prompt(prompt, schema_hint)
        logger.debug("model request", model=self.config.model, prompt_chars=len(prompt), schema_keys=list((schema_hint or {}).keys()))
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as out:
            out_path = Path(out.name)
        try:
            cmd = [
                "codex",
                "exec",
                "--model",
                self.config.model,
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--output-last-message",
                str(out_path),
                "-",
            ]
            env = os.environ.copy()
            env["CODEX_COGNITIVE_RUNTIME_INTERNAL_CALL"] = "1"
            env["CODEX_COGNITIVE_RUNTIME_HOOK_DEPTH"] = "1"
            proc = subprocess.run(
                cmd,
                env=env,
                text=True,
                input=full_prompt,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds or 90,
            )
            if proc.returncode != 0:
                logger.error("model failed", model=self.config.model, stderr=_safe_error(proc.stderr), stdout=_safe_error(proc.stdout))
                raise ModelError(_safe_error(proc.stderr or proc.stdout))
            raw = out_path.read_text(encoding="utf-8", errors="replace")
            result = extract_json_object(raw)
            logger.debug(
                "model response",
                model=self.config.model,
                stdout_chars=len(proc.stdout or ""),
                stderr_chars=len(proc.stderr or ""),
                raw_chars=len(raw),
                result=sanitize_model_result(result),
            )
            return result
        except subprocess.TimeoutExpired as exc:
            logger.error("model timeout", model=self.config.model, prompt_chars=len(prompt))
            raise ModelError("model call timed out") from exc
        finally:
            try:
                out_path.unlink()
            except OSError:
                pass

    def _build_prompt(self, prompt: str, schema_hint: dict[str, Any] | None) -> str:
        schema_text = json.dumps(schema_hint or {}, ensure_ascii=False, indent=2)
        return (
            "You are the Codex Cognitive Runtime decision model. Return only one valid JSON object. "
            "Do not include markdown, prose, secrets, or hidden reasoning.\n\n"
            f"Required JSON shape:\n{schema_text}\n\n"
            f"Task:\n{prompt}"
        )

    def _fake_response(self, prompt: str) -> dict[str, Any]:
        lowered = prompt.lower()
        if "review candidate" in lowered:
            return {
                "decision": "active",
                "reasons": ["fake model approved for deterministic test"],
                "risk_flags": [],
            }
        if "rank memories" in lowered:
            return {"ranked_ids": [], "reason": "fake rank"}
        if "search intent" in lowered:
            return {"should_search": True, "queries": ["memory"]}
        if "optimize memory content" in lowered:
            target = prompt.split("Current content:", 1)[-1].strip().split("\n", 1)[0]
            return {
                "optimized_content": target.replace("用户偏好", "用户偏好").strip() or "用户偏好：默认使用中文回答。",
                "summary": "fake optimized memory content",
                "changed": True,
            }
        if "classify runtime skill need" in lowered:
            target = lowered.split("user request:", 1)[-1]
            if any(signal in target for signal in ("天气", "weather", "几点", "time", "汇率", "translate", "翻译")):
                return {
                    "skill_needed": False,
                    "mode": "direct_answer",
                    "intent": "simple_query",
                    "domain": "general",
                    "complexity": "low",
                    "requires_memory": False,
                    "requires_clarification": False,
                    "interpreted_request": target.strip()[:300],
                    "reason": "fake classifier direct answer",
                }
            if any(signal in target for signal in ("logo", "标志", "品牌", "视觉识别")):
                return {
                    "skill_needed": True,
                    "mode": "generate_runtime_skill",
                    "intent": "brand_logo_design",
                    "domain": "brand_design",
                    "complexity": "medium",
                    "requires_memory": True,
                    "requires_clarification": True,
                    "interpreted_request": target.strip()[:300],
                    "reason": "fake classifier brand design",
                }
            if any(
                signal in target
                for signal in (
                    "修复",
                    "实现",
                    "代码",
                    "bug",
                    "页面",
                    "tab",
                    "tab页",
                    "标签页",
                    "页签",
                    "可编辑",
                    "编辑",
                    "大模型",
                    "模型优化",
                    "滚动条",
                    "滚动",
                    "布局",
                    "样式",
                    "按钮",
                    "下拉",
                    "列表",
                    "表格",
                    "筛选",
                    "placeholder",
                    "ui",
                    "layout",
                    "scrollbar",
                    "scroll",
                    "button",
                    "select",
                    "dropdown",
                    "panel",
                    "table",
                    "filter",
                    "fix",
                    "implement",
                    "debug",
                    "test",
                )
            ):
                return {
                    "skill_needed": True,
                    "mode": "generate_runtime_skill",
                    "intent": "software_engineering_change",
                    "domain": "software_engineering",
                    "complexity": "medium",
                    "requires_memory": True,
                    "requires_clarification": False,
                    "interpreted_request": target.strip()[:300],
                    "reason": "fake classifier engineering task",
                }
            if any(signal in target for signal in ("品牌定位", "营销", "写作", "产品分析", "商业计划", "pitch", "融资", "架构", "研究计划", "strategy", "plan", "design")):
                return {
                    "skill_needed": True,
                    "mode": "generate_runtime_skill",
                    "intent": "complex_task",
                    "domain": "general",
                    "complexity": "medium",
                    "requires_memory": True,
                    "requires_clarification": False,
                    "interpreted_request": target.strip()[:300],
                    "reason": "fake classifier complex task",
                }
            return {
                "skill_needed": False,
                "mode": "direct_answer",
                "intent": "direct_answer",
                "domain": "general",
                "complexity": "low",
                "requires_memory": False,
                "requires_clarification": False,
                "interpreted_request": target.strip()[:300],
                "reason": "fake classifier no skill",
            }
        if "understand the current codex task" in lowered:
            current = _fake_section(prompt, "Current prompt:", "Context packet:") or prompt
            context = _fake_section(prompt, "Context packet:", "Recalled memory classes:")
            parent_request = ""
            parent_trace = ""
            if "interpreted_request" in context:
                parent_request = _fake_after(context, "'interpreted_request':")
                parent_request = parent_request.strip(" '\"{},")[:260]
            if "trace_id" in context:
                parent_trace = _fake_after(context, "'trace_id':").strip(" '\"{},")[:80]
            followup = any(signal in current for signal in ("按这个", "按照这个", "实现一版", "继续", "就这样")) or "implement this" in current.lower()
            current_lower = current.lower()
            brand = any(signal in current_lower for signal in ("logo", "标志", "品牌", "视觉识别", "visual identity"))
            fullstack = any(signal in current_lower for signal in ("api", "接口")) and any(signal in current_lower for signal in ("前端", "页面", "浏览器", "筛选"))
            ui = (not brand) and any(signal in (current + parent_request).lower() for signal in ("ui", "ux", "页面", "界面", "布局", "样式", "美观", "视觉", "vue", "日志", "按钮", "下拉", "列表", "表格", "筛选", "placeholder", "select"))
            direct = (
                any(signal in current for signal in ("天气", "现在时间", "翻译", "读取会话", "对吧", "默认使用中文回答"))
                or (followup and not parent_request and not any(signal in current_lower for signal in ("实现", "修复", "修改", "代码", "跑测试", "验证", "implement", "fix", "debug")))
                or current.strip().lower() in {"测试", "test"}
            )
            interpreted = current.strip()
            if followup and parent_request:
                interpreted = f"基于上一轮确认的任务：{parent_request}；现在按该方案实现一版。"
            role = {
                "primary": "品牌设计专家" if brand else ("全栈工程专家" if fullstack else ("前端工程专家" if ui else ("任务执行专家" if direct else "软件工程专家"))),
                "supporting": ["视觉识别策略专家"] if brand else (["软件架构专家", "测试验证专家"] if fullstack else (["UI/UX 信息架构专家", "软件产品设计专家"] if ui else ([] if direct else ["测试验证专家"]))),
                "reason": "fake task understanding from current prompt and parent context",
            }
            return {
                "interpreted_request": interpreted[:500],
                "request_type": "direct_answer" if direct else ("implementation" if followup or "实现" in current else "planning"),
                "is_followup": followup,
                "parent_trace_id": parent_trace if followup else "",
                "continuity_confidence": 0.9 if followup and parent_request else 0.0,
                "skill_needed": False if direct else True,
                "domain": "general" if direct else ("brand_design" if brand else "software_engineering"),
                "task_type": "brand_logo_design" if brand else ("fullstack_integration_change" if fullstack else ("frontend_ui_redesign" if ui else ("direct_answer" if direct else "software_engineering_change"))),
                "surfaces": ["design"] if brand else (["backend", "frontend", "testing"] if fullstack else (["frontend", "ui", "ux"] if ui else ([] if direct else ["backend", "testing"]))),
                "role_profile": role,
                "implementation_scope": ["明确品牌语境和视觉方向", "提炼 logo 约束与候选方向"] if brand else (["实现 API 与前端联动", "验证后端和前端行为"] if fullstack else (["重构页面信息层级和布局", "实现前端交互与状态", "保持功能来源可追溯"] if ui else ([] if direct else ["检查上下文", "实施变更", "验证结果"]))),
                "out_of_scope": ["不直接生成未确认品牌信息的最终 logo"] if brand else (["不新增无需求依据的按钮、卡片或筛选项"] if ui else []),
                "acceptance_criteria": ["先澄清品牌名称、行业、受众和偏好"] if brand else (["后端测试、前端 typecheck 和浏览器验证覆盖改动"] if fullstack else (["新增 UI 元素能对应需求依据", "Chrome 验证无明显布局重叠", "运行前端 typecheck"] if ui else ([] if direct else ["运行相关验证"]))),
                "clarification_required": bool(followup and not parent_request),
                "uncertainties": ["missing parent task"] if followup and not parent_request else [],
            }
        if "generate a runtime skill" in lowered:
            target = lowered.split("user request:", 1)[-1]
            if any(signal in target for signal in ("logo", "标志", "品牌", "视觉识别")):
                return {
                    "name": "brand_logo_design_intake",
                    "applies_to": "brand logo and visual identity design requests",
                    "goal": "Clarify the brand brief before any logo generation.",
                    "memory_basis_ids": _fake_memory_ids(prompt),
                    "seed_skill_ids": _fake_seed_skill_ids(prompt),
                    "strategy": [
                        "Do not generate the logo immediately.",
                        "Ask for brand name, industry or product, target audience, logo type, colors, and forbidden elements.",
                        "Use the supplied memory basis to keep the direction minimal, professional, and restrained.",
                    ],
                    "first_action": {
                        "type": "ask_clarifying_questions",
                        "questions": ["品牌名称是什么？", "面向什么行业或产品？", "目标客户是谁？", "希望文字标、图形标还是组合标？"],
                    },
                    "avoid": ["Do not invent organization positioning.", "Do not use noisy gradients or cartoon style unless requested."],
                    "confidence": 0.86,
                }
            if any(
                signal in target
                for signal in (
                    "修复",
                    "实现",
                    "代码",
                    "bug",
                    "页面",
                    "滚动条",
                    "滚动",
                    "布局",
                    "样式",
                    "按钮",
                    "下拉",
                    "列表",
                    "表格",
                    "筛选",
                    "placeholder",
                    "ui",
                    "layout",
                    "scrollbar",
                    "scroll",
                    "button",
                    "select",
                    "dropdown",
                    "panel",
                    "table",
                    "filter",
                    "fix",
                    "implement",
                    "debug",
                    "test",
                )
            ):
                return {
                    "name": "software_change_guarded_workflow",
                    "applies_to": "bug fixes, code changes, refactors, and implementation tasks",
                    "goal": "Complete the engineering task through inspection, minimal change, and verification evidence.",
                    "memory_basis_ids": _fake_memory_ids(prompt),
                    "seed_skill_ids": _fake_seed_skill_ids(prompt),
                    "strategy": [
                        "Inspect the relevant repository context before editing.",
                        "Make the smallest focused change that satisfies the task.",
                        "Run the most relevant test, build, or lint command and report the result honestly.",
                    ],
                    "first_action": {"type": "inspect_repository", "questions": []},
                    "avoid": ["Do not edit before inspecting relevant files.", "Do not claim completion without verification evidence."],
                    "confidence": 0.84,
                }
            return {
                "name": "memory_grounded_task_strategy",
                "applies_to": "multi-step task requiring remembered preferences or project context",
                "goal": "Use clean memory basis to choose a task-specific strategy before acting.",
                "memory_basis_ids": _fake_memory_ids(prompt),
                "seed_skill_ids": _fake_seed_skill_ids(prompt),
                "strategy": ["Use only the supplied memory basis.", "Ask for clarification when required information is missing."],
                "first_action": {"type": "proceed_or_clarify", "questions": []},
                "avoid": ["Do not invent missing facts."],
                "confidence": 0.74,
            }
        if "classify runtime skill feedback" in lowered:
            if "模板" in lowered or "seed" in lowered:
                return {"outcome": "negative", "feedback_target": "seed_skill", "confidence": 0.82, "reason": "fake seed feedback"}
            if "长期技能" in lowered or "durable" in lowered:
                return {"outcome": "negative", "feedback_target": "durable_skill", "confidence": 0.82, "reason": "fake durable feedback"}
            if "方向对" in lowered and "问题太多" in lowered:
                return {"outcome": "mixed", "feedback_target": "skill_strategy", "confidence": 0.76, "reason": "fake mixed strategy and first action feedback"}
            if "提问" in lowered or "问题" in lowered:
                return {"outcome": "positive", "feedback_target": "first_action", "confidence": 0.84, "reason": "fake first action feedback"}
            if "方向" in lowered or "策略" in lowered:
                return {"outcome": "positive", "feedback_target": "skill_strategy", "confidence": 0.84, "reason": "fake strategy feedback"}
            return {"outcome": "positive", "feedback_target": "final_result", "confidence": 0.8, "reason": "fake final result feedback"}
        if "consolidate memory cluster" in lowered:
            if "dynamic_cross_project" in lowered:
                return {
                    "content": "性能优化通用经验：跨多个项目反复出现的性能预算、加载链路和调试手段，应沉淀为可复用的工程检查清单。",
                    "triggers": ["性能优化", "性能预算", "加载链路", "调试"],
                    "reason": "fake dynamic consolidation abstraction",
                }
            if "project_type_cross_project" in lowered:
                return {
                    "content": "项目类型经验：管理平台、门户、小程序等不同交付形态要分别明确入口、权限、端侧约束和发布流程，再抽象出可复用的交付检查清单。",
                    "triggers": ["项目类型", "管理平台", "门户", "小程序", "权限", "发布流程"],
                    "reason": "fake consolidation abstraction",
                }
            return {
                "content": "前端通用经验：跨 Vue、React、jQuery 项目反复出现的组件边界、状态管理、接口封装和构建调试问题，应抽象成框架无关的工程原则。",
                "triggers": ["前端", "Vue", "React", "jQuery", "组件边界", "接口封装"],
                "reason": "fake consolidation abstraction",
            }
        return {
            "candidates": [
                {
                    "content": "用户偏好默认使用中文回答。",
                    "type": "user_preference",
                    "proposed_action": "store",
                    "confidence": 0.93,
                    "importance": 0.8,
                    "ttl": "long",
                    "scope": "global",
                    "evidence": [{"source": "user_message", "quote": "默认使用中文回答"}],
                    "reason": "明确、稳定的交互偏好。",
                }
            ]
        }


def _safe_error(text: str) -> str:
    redacted = []
    for line in str(redact_secrets(text)).splitlines()[:8]:
        redacted.append(line[:300])
    return "\n".join(redacted) or "model call failed"


def _fake_memory_ids(prompt: str) -> list[str]:
    import re

    return list(dict.fromkeys(re.findall(r"mem_[a-zA-Z0-9]+", prompt)))[:8]


def _fake_seed_skill_ids(prompt: str) -> list[str]:
    import re

    return list(dict.fromkeys(re.findall(r"agency-agents:[A-Za-z0-9_./-]+", prompt)))[:8]


def _fake_section(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    chunk = text.split(start, 1)[1]
    if end in chunk:
        chunk = chunk.split(end, 1)[0]
    return chunk.strip()


def _fake_after(text: str, marker: str) -> str:
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].split("\n", 1)[0].strip()
