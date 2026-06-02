import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _write_seed_source(root: Path):
    (root / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (root / "design").mkdir(parents=True)
    (root / "design" / "design-brand-guardian.md").write_text(
        """---
name: Brand Guardian
description: Expert brand strategist for cohesive visual identity, logos, and brand systems.
---
# Brand Guardian

Use brand context before producing logo directions.
""",
        encoding="utf-8",
    )
    (root / "engineering").mkdir(parents=True)
    (root / "engineering" / "engineering-code-reviewer.md").write_text(
        """---
name: Code Reviewer
description: Reviews code changes with attention to tests and maintainability.
---
# Code Reviewer

Inspect code changes and verification gaps.
""",
        encoding="utf-8",
    )


class CliDataManagementTest(unittest.TestCase):
    def test_export_prune_and_wipe_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            export_path = Path(tmp) / "export.json"
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp, "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1"}
            ingest = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "ingest", "默认使用中文回答"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(ingest.returncode, 0, ingest.stderr)
            runtime_status = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "runtime-status"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(runtime_status.returncode, 0, runtime_status.stderr)
            self.assertIn("active_workflow", json.loads(runtime_status.stdout))
            runtime_status_pretty = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "runtime-status", "--pretty"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(runtime_status_pretty.returncode, 0, runtime_status_pretty.stderr)
            self.assertIn("Codex Cognitive Runtime Runtime Status", runtime_status_pretty.stdout)
            export = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "export", "--output", str(export_path)],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(export.returncode, 0, export.stderr)
            self.assertTrue(export_path.exists())
            self.assertIn("memories", json.loads(export_path.read_text(encoding="utf-8")))
            prune = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "prune-events"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(prune.returncode, 0, prune.stderr)
            prune_runtime = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "prune-runtime"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(prune_runtime.returncode, 0, prune_runtime.stderr)
            self.assertIn("pruned_runtime_records", json.loads(prune_runtime.stdout))
            wipe_without_confirm = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "wipe"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(wipe_without_confirm.returncode, 2)
            wipe = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "wipe", "--yes"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(wipe.returncode, 0, wipe.stderr)
            self.assertIn("wiped", json.loads(wipe.stdout))

    def test_seed_skills_command_supports_dry_run_and_local_source(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp, "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1"}

            dry_run = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "seed-skills", "--source", str(source_path), "--limit", "1", "--dry-run"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            dry_payload = json.loads(dry_run.stdout)
            self.assertTrue(dry_payload["dry_run"])
            self.assertEqual(dry_payload["skill_count"], 1)

            seed = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "seed-skills", "--source", str(source_path), "--category", "design"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(seed.returncode, 0, seed.stderr)
            payload = json.loads(seed.stdout)
            self.assertFalse(payload["dry_run"])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["skill_count"], 1)
            self.assertEqual(payload["created"][0]["id"], "agency-agents:design/design-brand-guardian.md")
            listed = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "seed-skills", "list"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)[0]["status"], "candidate")

    def test_seed_skills_rejects_non_mit_source(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            (source_path / "LICENSE").write_text("Proprietary\n", encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp, "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1"}

            seed = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "seed-skills", "--source", str(source_path)],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(seed.returncode, 0, seed.stderr)
            payload = json.loads(seed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "unsupported_seed_skill_license")
            self.assertEqual(payload["skill_count"], 0)

    def test_runtime_seed_dynamic_skill_governance_commands(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_COGNITIVE_RUNTIME_STATE_DIR": tmp, "CODEX_COGNITIVE_RUNTIME_FAKE_MODEL": "1"}

            seed = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "seed-skills", "--source", str(source_path), "--category", "design", "--activate"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(seed.returncode, 0, seed.stderr)
            seed_id = json.loads(seed.stdout)["created"][0]["id"]

            seed_list = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "seed-skills", "list"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(seed_list.returncode, 0, seed_list.stderr)
            self.assertEqual(json.loads(seed_list.stdout)[0]["id"], seed_id)

            disable = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "seed-skills", "disable", seed_id],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(disable.returncode, 0, disable.stderr)
            disabled_payload = json.loads(disable.stdout)
            self.assertEqual(disabled_payload["metadata_json"]["trust_state"], "disabled")
            self.assertEqual(disabled_payload["status"], "deprecated")

            restore = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "seed-skills", "restore", seed_id],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(restore.returncode, 0, restore.stderr)
            restored_payload = json.loads(restore.stdout)
            self.assertEqual(restored_payload["metadata_json"]["trust_state"], "unverified")
            self.assertEqual(restored_payload["status"], "candidate")

            context = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "search", "帮我画一个品牌 logo"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(context.returncode, 0, context.stderr)

            benchmark = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "runtime-benchmark"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(benchmark.returncode, 0, benchmark.stderr)
            benchmark_payload = json.loads(benchmark.stdout)
            self.assertIn("skill_trigger_recall", benchmark_payload)
            self.assertNotEqual(benchmark_payload["source"], "synthetic")
            self.assertTrue(benchmark_payload["passed_thresholds"])
            self.assertEqual(
                benchmark_payload["categories"],
                {
                    "direct_answer": 62,
                    "creative_design": 60,
                    "planning_business": 60,
                    "engineering": 60,
                    "ui_engineering": 3,
                    "ambiguous": 40,
                    "feedback": 80,
                },
            )
            fixture_path = Path(benchmark_payload["source"])
            self.assertFalse(any('"repeat"' in line for line in fixture_path.read_text(encoding="utf-8").splitlines()))

            synthetic = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "runtime-benchmark", "--synthetic"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(synthetic.returncode, 0, synthetic.stderr)
            self.assertEqual(json.loads(synthetic.stdout)["source"], "synthetic")
            synthetic_threshold = subprocess.run(
                [sys.executable, "-m", "codex_cognitive_runtime.cli", "runtime-benchmark", "--synthetic", "--fail-under-defaults"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(synthetic_threshold.returncode, 0, synthetic_threshold.stderr)
            synthetic_threshold_payload = json.loads(synthetic_threshold.stdout)
            self.assertEqual(synthetic_threshold_payload["source"], "synthetic")
            self.assertTrue(synthetic_threshold_payload["passed_thresholds"])


if __name__ == "__main__":
    unittest.main()
