# Split Ledger Closure Report

Generated: 2026-06-03

## Objective

Implement the baseline/user/team ledger split so public baseline knowledge can be distributed with the product while private personalization keeps learning locally and never leaks into the GitHub-safe baseline.

## Architecture

- `baseline` ledger: distributable public defaults and bundled agency seed skills. Runtime feedback, recall telemetry, traces, private preferences, and user-created data do not write here.
- `user` ledger: default write target for preferences, runtime traces, feedback, overlays, dynamic skills, governance state, and personal learning.
- `team` ledger: optional read layer. When configured it participates in retrieval between user and baseline; default writes still go to user.
- `LayeredLedgerView`: read-through view with `user > team > baseline` precedence and source tagging via `_ledger_layer` / `metadata_json.ledger_layer`.

## Closed Loop

- Cold start imports bundled defaults and bundled seed skills into baseline, not user.
- Retrieval reads all configured layers and preserves source layer metadata.
- Runtime selection of a baseline/team seed creates a user overlay before feedback can calibrate it.
- Natural feedback updates the user overlay and leaves baseline metadata unchanged.
- Export defaults to `target=user`; `target=baseline` is GitHub-safe and strips runtime/private surfaces.
- Wipe still affects only the user ledger.

## Verification

- `python3 -m compileall -q src/codex_cognitive_runtime`
- `PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m unittest discover -s tests`
  - Result: `Ran 277 tests in 592.625s`, `OK`
- `PYTHONPATH=src CODEX_COGNITIVE_RUNTIME_FAKE_MODEL=1 python3 -m codex_cognitive_runtime.real_full_chain_proof --state-dir /private/tmp/ccr-partition-proof-state --clear-before --sample-filter partition --json-report /private/tmp/ccr-partition-proof.json --markdown-report /private/tmp/ccr-partition-proof.md --progress`
  - Result: `partition_split_ledger_closure passed=True`

## Guardrails

- Baseline export does not include user sentinel data.
- User feedback calibration is proven to land on user overlay records.
- Seed ranking keeps relevance first and uses ledger layer as a tie-break, avoiding both cross-domain UI suppression and baseline pollution.
- API and CLI export accept explicit targets: `user`, `team`, `baseline`, `all`.
