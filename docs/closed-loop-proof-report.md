# Closed Loop Proof Report

Generated: 2026-06-02 19:42 Asia/Shanghai

## Result

The product loop is now closed across task understanding, recall, seed scoring, fragment selection, final context, execution guard, acceptance coverage, outcome attribution, feedback calibration, and regression proof.

## Implemented Loop

1. Task understanding produces a validated task contract with request type, role profile, implementation scope, acceptance criteria, and corrected direct-answer boundaries.
2. Recall and seed retrieval record `seed_skill_selection_scores` with rank, selected state, base score, calibration metadata, and final score.
3. Runtime skill distillation records selected fragments with source skill id, source field, reason, score, risk, and hash.
4. Runtime skill injection maps fragments to final task rules: `implementation_scope`, `acceptance_criteria`, and `final_context.task_rules`, with `final_rule_hash` and `final_context_sha256`.
5. Workflow observation and Stop compute `acceptance_coverage` for every acceptance criterion as `covered`, `missing`, or `failed`.
6. Missing or failed acceptance criteria emit `acceptance_missing` / `acceptance_failed` signals and violations.
7. Outcome attribution persists six layer records: `task_understanding`, `recall`, `seed_scoring`, `fragment_selection`, `final_context`, `execution_guard`.
8. Runtime/user feedback is traced and attributed back to seed scoring and fragment mappings.
9. Seed feedback applies profile-aware calibration by `task_type/domain/surfaces/project_type`, rather than globally suppressing the whole seed skill.
10. Regression proof harness repeats the before -> feedback -> after loop and writes machine-readable JSON plus Markdown.

## Proof Harness

Command:

```bash
./scripts/codex-cognitive-runtime-regression-proof --state-dir /private/tmp/codex-cognitive-runtime-regression-proof-state-main --clear-before --json-report /private/tmp/codex-cognitive-runtime-regression-proof-main.json --markdown-report /private/tmp/codex-cognitive-runtime-regression-proof-main.md
```

Result: `5/5` scenarios passed.

Covered scenarios:

- `brand_logo_task`: Brand Guardian ranks first and is selected.
- `wechat_mini_program_ui`: WeChat Mini Program Developer ranks first; generic UI exclusion no longer blocks 小程序 UI.
- `generic_frontend_ui`: UI Designer ranks first; 小程序, Filament, Roblox, and marketing cross-domain seeds are absent from selected set.
- `memory_statement`: memory statement does not trigger runtime skill scoring.
- `wrong_sort_feedback_calibration`: bad generic frontend template starts rank 1, receives negative seed-skill feedback, records profile penalty, and drops out of the selected top set while good UI skills remain selected.

Artifacts:

- JSON: `/private/tmp/codex-cognitive-runtime-regression-proof-main.json`
- Markdown: `/private/tmp/codex-cognitive-runtime-regression-proof-main.md`

## Test Evidence

Commands run:

```bash
python3 -m compileall -q src/codex_cognitive_runtime
PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest tests.test_runtime_trace tests.test_runtime_observer tests.test_runtime_skill tests.test_regression_proof
PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest discover -s tests
```

Results:

- Core loop tests: `Ran 99 tests in 7.876s`, `OK`.
- Full suite: `Ran 270 tests in 562.929s`, `OK`.

## Current Status

The scoring mechanism is not just present; it is observable and acts on feedback. The closed loop now has durable evidence at each boundary: score records, fragment-rule mappings, acceptance coverage, attribution records, calibration events, and reproducible proof artifacts.
