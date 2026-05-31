import os
import tempfile
import unittest
from pathlib import Path

from codex_cognitive_runtime.config import Config
from codex_cognitive_runtime.schema import Evidence, MemoryCandidate
from codex_cognitive_runtime.service import MemoryService


def _service(tmp):
    config = Config(
        model="gpt-5.4-mini",
        state_dir=Path(tmp),
        ledger_path=Path(tmp) / "ledger.sqlite3",
        min_active_confidence=0.82,
        min_quarantine_confidence=0.62,
        duplicate_threshold=0.9,
        max_evidence_quote_chars=500,
    )
    return MemoryService(config)


def _candidate(content):
    return MemoryCandidate(
        content=content,
        memory_type="experience",
        proposed_action="store",
        confidence=0.95,
        importance=0.86,
        ttl="long",
        scope="global",
        domain="software_engineering",
        category="lesson",
        subcategory="testing",
        abstraction_level="principle",
        triggers=["测试", "质量"],
        evidence=[Evidence(source="user_message", quote=content)],
        reason="global governance test",
    )


class GlobalGovernanceTest(unittest.TestCase):
    def setUp(self):
        os.environ["CODEX_COGNITIVE_RUNTIME_FAKE_MODEL"] = "1"

    def tearDown(self):
        os.environ.pop("CODEX_COGNITIVE_RUNTIME_FAKE_MODEL", None)

    def test_governance_lowers_often_injected_unused_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                memory_id = service.ledger.add_candidate(
                    _candidate("经验：测试质量要结合覆盖率、边界用例和真实运行报告。"),
                    "active",
                    {"status": "active"},
                )
                for index in range(5):
                    service.ledger.record_recall(
                        f"测试质量怎么判断 {index}",
                        {"domain": "software_engineering"},
                        [service.ledger.get_memory(memory_id)],
                        session_id="s",
                        turn_id=f"t{index}",
                    )
                before = service.ledger.get_memory(memory_id)["strength"]
                result = service.govern_memories(apply=True)
                after = service.ledger.get_memory(memory_id)["strength"]
                self.assertLess(after, before)
                self.assertTrue(result["applied_actions"])
                self.assertEqual(service.ledger.get_memory(memory_id)["status"], "active")
            finally:
                service.close()

    def test_governance_quarantines_negative_feedback_dominant_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                memory_id = service.ledger.add_candidate(
                    _candidate("经验：所有项目都必须注入这条很宽泛的测试建议。"),
                    "active",
                    {"status": "active"},
                )
                service.recall_feedback(memory_id, "negative", "误导")
                service.recall_feedback(memory_id, "negative", "无效")
                result = service.govern_memories(apply=True)
                memory = service.ledger.get_memory(memory_id)
                self.assertEqual(memory["status"], "quarantined")
                self.assertTrue(any(action["action"] == "quarantine" for action in result["applied_actions"]))
            finally:
                service.close()

    def test_governance_repairs_future_admission_with_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                bad_id = service.ledger.add_candidate(
                    _candidate("经验：所有项目都必须注入这条很宽泛的测试建议。"),
                    "active",
                    {"status": "active"},
                )
                service.recall_feedback(bad_id, "negative", "误导")
                service.recall_feedback(bad_id, "negative", "无效")
                result = service.govern_memories(apply=True)
                self.assertTrue(any(action["action"] == "create_policy" for action in result["applied_actions"]))

                policy_decision = service.ledger.candidate_policy_decision(
                    _candidate("经验：测试质量必须注入所有项目，且测试建议要一直加入上下文。")
                )
                self.assertIsNotNone(policy_decision)
                self.assertEqual(policy_decision["action"], "quarantine")
            finally:
                service.close()

    def test_governance_repairs_similar_existing_active_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                bad_id = service.ledger.add_candidate(
                    _candidate("经验：所有项目都必须注入这条很宽泛的测试建议。"),
                    "active",
                    {"status": "active"},
                )
                similar_id = service.ledger.add_candidate(
                    _candidate("经验：所有项目都必须注入这条测试建议，并且保持长期提示。"),
                    "active",
                    {"status": "active"},
                )
                service.recall_feedback(bad_id, "negative", "误导")
                service.recall_feedback(bad_id, "negative", "无效")
                result = service.govern_memories(apply=True)
                self.assertTrue(any(action["action"] == "quarantine_similar" for action in result["applied_actions"]))
                self.assertEqual(service.ledger.get_memory(similar_id)["status"], "quarantined")
            finally:
                service.close()

    def test_governance_reports_injection_pressure(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                ids = []
                for index in range(5):
                    ids.append(service.ledger.add_candidate(_candidate(f"经验：测试质量规则 {index}。"), "active", {"status": "active"}))
                memories = [service.ledger.get_memory(memory_id) for memory_id in ids]
                service.ledger.record_recall("测试质量", {"domain": "software_engineering"}, memories)
                result = service.govern_memories(apply=False)
                self.assertEqual(result["report"]["injection_pressure"], "high")
                self.assertGreaterEqual(result["report"]["avg_injected_memories"], 5)
            finally:
                service.close()

    def test_governance_supersedes_near_duplicate_active_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.ledger.add_candidate(
                    _candidate("经验：hook 内部调用 codex exec 必须设置 internal 标记，否则会递归触发。"),
                    "active",
                    {"status": "active"},
                )
                second = service.ledger.add_candidate(
                    _candidate("在 hook 内部调用 `codex exec` 时，必须设置 `internal` 标记，否则会递归触发。"),
                    "active",
                    {"status": "active"},
                )
                result = service.govern_memories(apply=True)
                statuses = {memory_id: service.ledger.get_memory(memory_id)["status"] for memory_id in (first, second)}
                self.assertIn("superseded", statuses.values())
                self.assertIn("active", statuses.values())
                self.assertTrue(any(action["action"] == "supersede_duplicates" for action in result["applied_actions"]))
            finally:
                service.close()

    def test_governance_repairs_future_duplicate_admission_with_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.ledger.add_candidate(
                    _candidate("经验：hook 内部调用 codex exec 必须设置 internal 标记，否则会递归触发。"),
                    "active",
                    {"status": "active"},
                )
                second = service.ledger.add_candidate(
                    _candidate("在 hook 内部调用 `codex exec` 时，必须设置 `internal` 标记，否则会递归触发。"),
                    "active",
                    {"status": "active"},
                )
                result = service.govern_memories(apply=True)
                self.assertTrue(
                    any(
                        action["action"] == "create_policy"
                        and action.get("policy_type") == "candidate_gate"
                        and action.get("policy_action") == "supersede"
                        for action in result["applied_actions"]
                    )
                )
                decision = service.ledger.candidate_policy_decision(
                    _candidate("hook 调用 codex exec 必须设置 internal，否则会递归触发。")
                )
                self.assertIsNotNone(decision)
                self.assertEqual(decision["action"], "supersede")
                self.assertIn(decision["source_memory_id"], {first, second})
            finally:
                service.close()

    def test_governance_repairs_future_recall_with_suppression_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                memory_id = service.ledger.add_candidate(
                    _candidate("经验：所有项目都应该反复注入这条测试质量提示。"),
                    "active",
                    {"status": "active"},
                )
                memory = service.ledger.get_memory(memory_id)
                for index in range(5):
                    service.ledger.record_recall(f"测试质量 {index}", {"domain": "software_engineering"}, [memory])
                result = service.govern_memories(apply=True)
                self.assertTrue(
                    any(
                        action["action"] == "create_policy"
                        and action.get("policy_type") == "recall_gate"
                        and action.get("policy_action") == "suppress"
                        for action in result["applied_actions"]
                    )
                )
                recallable = service.ledger.list_recallable_memories(limit=20)
                self.assertNotIn(memory_id, {item["id"] for item in recallable})
            finally:
                service.close()

    def test_natural_language_negative_feedback_is_attributed_to_recent_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                memory_id = service.ledger.add_candidate(
                    _candidate("经验：测试质量要结合覆盖率、边界用例和真实运行报告。"),
                    "active",
                    {"status": "active"},
                )
                service.ledger.record_recall(
                    "测试质量怎么判断",
                    {"domain": "software_engineering"},
                    [service.ledger.get_memory(memory_id)],
                    session_id="s1",
                    turn_id="t1",
                )
                result = service.apply_natural_feedback("不对，这条记忆误导了回答", session_id="s1")
                memory = service.ledger.get_memory(memory_id)
                self.assertEqual(result["updated"], 1)
                self.assertEqual(memory["negative_recall_count"], 1)
            finally:
                service.close()

    def test_periodic_governance_runs_once_per_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                first = service.periodic_governance(interval_minutes=60)
                second = service.periodic_governance(interval_minutes=60)
                self.assertIn("report", first)
                self.assertEqual(second["skipped"], "not_due")
            finally:
                service.close()

    def test_dynamic_budget_reduces_short_prompt_under_pressure(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                ids = []
                for index in range(5):
                    ids.append(service.ledger.add_candidate(_candidate(f"经验：测试质量规则 {index}。"), "active", {"status": "active"}))
                memories = [service.ledger.get_memory(memory_id) for memory_id in ids]
                for index in range(4):
                    service.ledger.record_recall(f"测试 {index}", {"domain": "software_engineering"}, memories)
                context = service.prompt_context("测试", limit=6)
                self.assertLessEqual(context.count("\n- "), 2)
            finally:
                service.close()

    def test_policy_hit_count_and_expiry_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                policy_id = service.ledger.add_governance_policy(
                    "candidate_gate",
                    {
                        "memory_type": "experience",
                        "scope": "global",
                        "domain": "software_engineering",
                        "category": "lesson",
                        "subcategory": "testing",
                        "terms": ["测试", "质量"],
                    },
                    "quarantine",
                    "test policy",
                    ttl_days=1,
                )
                decision = service.ledger.candidate_policy_decision(_candidate("经验：测试质量要长期注入。"))
                policy = [item for item in service.ledger.list_governance_policies() if item["id"] == policy_id][0]
                self.assertIsNotNone(decision)
                self.assertEqual(policy["hit_count"], 1)
            finally:
                service.close()

    def test_ledger_only_has_no_external_reconcile_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            try:
                memory_id = service.ledger.add_candidate(
                    _candidate("经验：测试质量要结合覆盖率、边界用例和真实运行报告。"),
                    "active",
                    {"status": "active"},
                )
                memory = service.ledger.get_memory(memory_id)
                self.assertFalse(hasattr(service, "reconcile_" + "mempalace"))
                self.assertEqual(memory["status"], "active")
            finally:
                service.close()
