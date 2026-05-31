import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex-cognitive-runtime-real-chain-stress"


def load_stress_module():
    loader = importlib.machinery.SourceFileLoader("codex_cognitive_runtime_real_chain_stress", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RealChainStressTest(unittest.TestCase):
    def test_coverage_skips_previously_passed_scenarios(self):
        mod = load_stress_module()
        scenarios = [
            ("stable_pref", "请记住稳定偏好。", "use", "/tmp/a"),
            ("bad_secret", "请长期记住 token sk-nope。", "ignore", "/tmp/b"),
        ]
        first_id = mod.scenario_id(*scenarios[0])
        with tempfile.TemporaryDirectory() as tmp:
            coverage_path = Path(tmp) / "coverage.jsonl"
            coverage_path.write_text(json.dumps({"scenario_id": first_id, "status": "passed"}) + "\n", encoding="utf-8")
            coverage = mod.load_coverage(coverage_path)
            args = Namespace(sample_mode="sequential", random_seed=1, start_offset=0)

            self.assertEqual(mod.scenario_order(args, scenarios, coverage, skip_passed=True), [1])
            self.assertEqual(mod.scenario_order(args, scenarios, coverage, skip_passed=False), [0, 1])

    def test_selection_does_not_repeat_when_rounds_exceed_remaining_scenarios(self):
        mod = load_stress_module()
        scenarios = [
            ("one", "one", "use", "/tmp/a"),
            ("two", "two", "ignore", "/tmp/b"),
            ("three", "three", "neutral", "/tmp/c"),
        ]
        args = Namespace(sample_mode="sequential", random_seed=1, start_offset=0, rounds=10)

        self.assertEqual(mod.selected_scenario_indices(args, scenarios, {}, skip_passed=True), [0, 1, 2])

    def test_real_chain_fixture_pool_is_broad_and_explicit(self):
        files = sorted((ROOT / "benchmarks" / "real_chain").glob("*.jsonl"))
        total = 0
        modes = {"use": 0, "ignore": 0, "neutral": 0}
        for path in files:
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    self.assertIn("kind", item, f"{path}:{line_number}")
                    self.assertIn("text", item, f"{path}:{line_number}")
                    self.assertIn("assistant_mode", item, f"{path}:{line_number}")
                    self.assertIn("cwd", item, f"{path}:{line_number}")
                    self.assertNotIn("repeat", item, f"{path}:{line_number}")
                    modes[item["assistant_mode"]] = modes.get(item["assistant_mode"], 0) + 1
                    total += 1

        self.assertGreaterEqual(total, 3000)
        self.assertGreaterEqual(modes["use"], 300)
        self.assertGreaterEqual(modes["ignore"], 300)
        self.assertGreaterEqual(modes["neutral"], 100)


if __name__ == "__main__":
    unittest.main()
