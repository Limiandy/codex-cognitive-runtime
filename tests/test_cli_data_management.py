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
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_MEMORY_STATE_DIR": tmp, "CODEX_MEMORY_FAKE_MODEL": "1"}
            ingest = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "ingest", "默认使用中文回答"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(ingest.returncode, 0, ingest.stderr)
            runtime_status = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "runtime-status"],
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
                [sys.executable, "-m", "codex_memory.cli", "runtime-status", "--pretty"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(runtime_status_pretty.returncode, 0, runtime_status_pretty.stderr)
            self.assertIn("Codex Memory Runtime Status", runtime_status_pretty.stdout)
            export = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "export", "--output", str(export_path)],
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
                [sys.executable, "-m", "codex_memory.cli", "prune-events"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(prune.returncode, 0, prune.stderr)
            prune_runtime = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "prune-runtime"],
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
                [sys.executable, "-m", "codex_memory.cli", "wipe"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(wipe_without_confirm.returncode, 2)
            wipe = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "wipe", "--yes"],
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
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_MEMORY_STATE_DIR": tmp, "CODEX_MEMORY_FAKE_MODEL": "1"}

            dry_run = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "seed-skills", "--source", str(source_path), "--limit", "1", "--dry-run"],
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
                [sys.executable, "-m", "codex_memory.cli", "seed-skills", "--source", str(source_path), "--category", "design"],
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

    def test_seed_skills_rejects_non_mit_source(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as source:
            source_path = Path(source)
            _write_seed_source(source_path)
            (source_path / "LICENSE").write_text("Proprietary\n", encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_MEMORY_STATE_DIR": tmp, "CODEX_MEMORY_FAKE_MODEL": "1"}

            seed = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "seed-skills", "--source", str(source_path)],
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
            env = {**os.environ, "PYTHONPATH": "src", "CODEX_MEMORY_STATE_DIR": tmp, "CODEX_MEMORY_FAKE_MODEL": "1"}

            seed = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "seed-skills", "--source", str(source_path), "--category", "design"],
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
                [sys.executable, "-m", "codex_memory.cli", "seed-skills", "list"],
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
                [sys.executable, "-m", "codex_memory.cli", "seed-skills", "disable", seed_id],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(disable.returncode, 0, disable.stderr)
            self.assertEqual(json.loads(disable.stdout)["metadata_json"]["trust_state"], "disabled")

            restore = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "seed-skills", "restore", seed_id],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(restore.returncode, 0, restore.stderr)
            self.assertEqual(json.loads(restore.stdout)["metadata_json"]["trust_state"], "unverified")

            context = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "search", "帮我画一个品牌 logo"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(context.returncode, 0, context.stderr)

            benchmark = subprocess.run(
                [sys.executable, "-m", "codex_memory.cli", "runtime-benchmark"],
                cwd=".",
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(benchmark.returncode, 0, benchmark.stderr)
            self.assertIn("skill_trigger_recall", json.loads(benchmark.stdout))


if __name__ == "__main__":
    unittest.main()
