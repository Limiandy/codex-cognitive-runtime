# Real Full Chain Test Report

Generated: 2026-06-02

## Result

The real-mode full-chain harness passed.

- Real full-chain samples: 47/47 passed
- Industry depth matrix: 12/12 industries passed, 36/36 industry samples passed
- Depth axes: backend API/data 10/10, governance/privacy/compliance 17/17, product UI experience 9/9
- Workflow guard samples: 3/3 passed
- Feedback calibration sample: 1/1 passed
- Fake model environment: false

Primary artifacts:

- JSON: `/private/tmp/codex-real-full-v1.json`
- Markdown: `/private/tmp/codex-real-full-v1.md`

## Coverage

Industries covered:

- healthcare
- finance
- education
- ecommerce
- manufacturing
- logistics
- legal
- hr
- real_estate
- gaming
- data_ml
- developer_tools

Loop segments covered:

- task_understanding
- recall
- seed_scoring
- fragment_selection
- final_context
- outcome_attribution
- runtime_workflow
- tool_observation
- acceptance_coverage
- execution_guard
- runtime_feedback
- profile_calibration
- rerank_after_feedback

## Failure Classification During Testing

Failures were not treated as one class.

- Code/product contract gaps fixed:
  - Privacy execution requests containing `检查`/脱敏/闭环 must trigger a runtime skill.
  - Chinese dashboard UI requests using `看板`/`仪表盘` must be classified as UI/product experience work.
  - Negated cross-domain mentions such as "不要误选 Roblox" and "不要把营销 seed 错注入" must not become positive domain signals.
  - WeChat mini-program seed remains valid for explicit mini-program UI tasks, but is penalized for generic/non-WeChat UI tasks.
- Scoring mismatch fixed:
  - Generic UI tasks no longer select marketing, Roblox, or WeChat mini-program seeds only because those words appear in a guard phrase or generic UI context.
- Test fixture issues fixed:
  - Feedback calibration proof fixture now creates eligible external seed records with isolated tokens so the calibration path is actually exercised.
  - Project-boundary and deleted-memory tests no longer assert against brittle normalized prompt text or bundled default policy text.
- Audit/coverage degradation:
  - No unresolved audit degradation or coverage gap remained in the final report.

## Verification Commands

Passed:

```bash
python3 -m compileall -q src/codex_cognitive_runtime
```

```bash
PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest tests.test_runtime_skill tests.test_regression_proof
```

```bash
PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest tests.test_runtime_skill tests.test_regression_proof tests.test_memory_loop
```

```bash
PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest tests.test_recall
```

```bash
PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest discover -s tests
```

Final full unit result:

- Ran 274 tests in 566.323s
- OK

Real full-chain command:

```bash
./scripts/codex-cognitive-runtime-real-full-chain-proof --state-dir /private/tmp/codex-real-full-v1-state --clear-before --json-report /private/tmp/codex-real-full-v1.json --markdown-report /private/tmp/codex-real-full-v1.md --progress
```

Final real-chain result:

- 47/47 passed
- failed: none

## Conclusion

For the implemented product chain, the loop is now closed and regression-tested end to end:

task understanding -> recall -> seed scoring -> fragment selection -> final context -> execution/workflow observation -> acceptance coverage -> outcome attribution -> feedback calibration -> rerank verification.

The result is not a claim that all future arbitrary inputs are impossible to break. It is a concrete proof that the current full product chain, with broad multi-industry and deep per-industry samples, is executable, observable, classified by failure type, and protected by regression tests.
