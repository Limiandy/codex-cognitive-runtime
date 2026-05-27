# Codex Memory

Codex Memory is a local Codex plugin that uses GPT-5.4-Mini as a memory decision model, reviews candidates through deterministic gates, and stores approved long-term memories in a local SQLite Ledger.

This is a local developer alpha. It is intended for developers who can inspect local Codex configuration and recover their own environment. It does not guarantee compatibility across Codex CLI versions and is not recommended for sensitive production environments.

## Runtime

- `memory-engine`: extracts, classifies, and ranks memory candidates with `gpt-5.4-mini`.
- `memory-review`: validates schema, evidence, confidence, TTL, duplicate risk, and secret-like content.
- `memory-ledger`: local SQLite audit trail at `~/.codex-memory/ledger.sqlite3`.
- `mcp`: exposes `codex_memory_status`, `codex_memory_search`, `codex_memory_ingest`, and `codex_memory_queue`.

The local SQLite Ledger is the only runtime store and source of truth.

## Commands

```bash
./scripts/codex-memory status
./scripts/codex-memory doctor
./scripts/codex-memory ingest "默认使用中文回答"
./scripts/codex-memory search "中文回答偏好"
./scripts/codex-memory queue --status quarantined
```

Set `CODEX_MEMORY_MODEL` to override the default model. The default is `gpt-5.4-mini`.

## Install

Install from a local checkout:

```bash
git clone https://github.com/Limiandy/codex-memory.git ~/plugins/codex-memory
cd ~/plugins/codex-memory
PYTHONPATH=src python3 -m unittest discover -s tests -v
./scripts/codex-memory plugin install --source "$PWD"
./scripts/codex-memory doctor
```

The installer copies the plugin to `~/plugins/codex-memory`, registers it in `~/.agents/plugins/marketplace.json`, and enables it in `~/.codex/config.toml`. Existing Codex config is backed up before writing.

Preview install changes without writing files:

```bash
./scripts/codex-memory plugin install --source "$PWD" --dry-run --diff
```

## Verify

Run doctor after install:

```bash
./scripts/codex-memory doctor
```

Doctor returns JSON with `fatal`, `warn`, and `info` checks. `fatal` failures block the core local memory path. `warn` items need attention but do not block startup. `info` items are optional or skipped checks, such as the default model smoke test.

Run a model smoke test only when you want to verify the local `codex exec` model path:

```bash
./scripts/codex-memory doctor --model-check
```

Check that hooks and MCP are wired:

```bash
./scripts/codex-memory status
./scripts/codex-memory ingest "默认使用中文回答"
./scripts/codex-memory search "中文回答"
```

## Uninstall

Disable the plugin but keep files:

```bash
./scripts/codex-memory plugin uninstall
```

Remove the installed plugin files too:

```bash
./scripts/codex-memory plugin uninstall --delete-files
```

Preview uninstall changes:

```bash
./scripts/codex-memory plugin uninstall --dry-run --diff
```

To remove local memory data, stop active Codex sessions using the plugin and delete the state directory:

```bash
rm -rf ~/.codex-memory
```

## Privacy

Codex Memory stores events in `~/.codex-memory/ledger.sqlite3`. By default, event payloads are sanitized before storage: allowed fields are retained, long strings are truncated, and secret-like values are redacted. Stored event payloads include `_raw_payload_stored: false`.

Reviewed memory content and evidence can still be stored when they pass review gates. Use `./scripts/codex-memory queue`, `promote`, `reject`, and `delete` to inspect and manage memory records.

Raw event storage is opt-in and should only be used for local debugging:

```bash
CODEX_MEMORY_STORE_RAW_EVENTS=1 ./scripts/codex-memory ingest "debug text"
```

When raw event storage is enabled, original event payloads are written to the local Ledger with `_raw_payload_stored: true`. `status` and `doctor` report that raw event storage is enabled.

## MCP Permissions

MCP defaults to read-only tools. Mutating tools require explicit opt-in:

- `CODEX_MEMORY_ENABLE_MCP_WRITE_TOOLS=1`: allows ingest, recall feedback, and expiration.
- `CODEX_MEMORY_ENABLE_MCP_REVIEW_TOOLS=1`: allows promote and reject.
- `CODEX_MEMORY_ENABLE_MCP_ADMIN_TOOLS=1`: allows delete, reconcile, consolidate, and `govern apply`.

The legacy `CODEX_MEMORY_ENABLE_DANGEROUS_MCP_TOOLS=1` enables all three groups for compatibility, but the narrower switches are preferred.

## Experimental CLI

The public alpha command surface is focused on local memory: `status`, `doctor`, `ingest`, `search`, `queue`, `promote`, `reject`, `delete`, `recall-feedback`, `expire`, `audit`, `plugin`, `govern`, and `govern-periodic`.

Experimental cognitive, knowledge, skill, and workflow commands are hidden behind an explicit environment switch:

```bash
CODEX_MEMORY_ENABLE_EXPERIMENTAL_CLI=1 ./scripts/codex-memory workflow-plan "plan this task"
```
