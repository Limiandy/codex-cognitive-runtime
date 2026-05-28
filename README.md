# Codex Memory

Codex Memory is a local cognitive runtime for Codex. It observes real Codex work through hooks, maintains task workflow state, detects missing verification or process violations, and turns reviewed outcomes into reusable memory, knowledge, and skills.

This is a local developer alpha. It is intended for developers who can inspect local Codex configuration and recover their own environment. It does not guarantee compatibility across Codex CLI versions and is not recommended for sensitive production environments.

## Runtime

- `memory-engine`: extracts, classifies, and ranks memory candidates with `gpt-5.4-mini`.
- `memory-review`: validates schema, evidence, confidence, TTL, duplicate risk, and secret-like content.
- `memory-ledger`: local SQLite audit trail at `~/.codex-memory/ledger.sqlite3`.
- `runtime-skill`: decides whether the current request needs a task-specific skill, retrieves clean active memories, and injects a short action strategy.
- `cognitive-runtime`: observes `UserPromptSubmit`, `PostToolUse`, and `Stop` events to maintain workflow state and inject next-step control signals.
- `workflow-guard`: detects engineering workflow violations such as code changes without verification evidence.
- `skill-synthesizer`: turns successful observed workflows and related experience memories into reusable dynamic skills.
- `mcp`: exposes `codex_memory_status`, `codex_memory_search`, `codex_memory_ingest`, and `codex_memory_queue`.

The local SQLite Ledger is the only runtime store and source of truth.

The runtime observes Codex tool use; it does not execute shell commands, edit files, or run tests by itself.

Current Runtime MVP supports runtime skill generation from clean long-term memory, seed skill cold start, runtime skill injection audit, observed engineering workflows, task start, turn-bound workflow matching, repository inspection, code change detection, verification detection, Stop-time violation checks, next-turn control injection, verification recipe learning, dynamic skill candidate synthesis, and verification recipe reuse feedback. Legacy `workflow-execute` remains as a deprecated alias for experimental `workflow-simulate`; neither command is the runtime execution path.

The runtime observer is enabled by default. Disable it with `CODEX_MEMORY_ENABLE_RUNTIME_OBSERVER=0` if you only want reviewed memory storage without workflow guard behavior.

Runtime Skill lifecycle:

1. Decide whether the request needs a task-specific skill.
2. Retrieve reviewed clean memories and trusted seed skills.
3. Generate a temporary Runtime Skill.
4. Review the Runtime Skill for allowed basis ids, missing clarification, secret-like content, and unsupported user or organization claims.
5. Inject the reviewed skill and record a local audit event.
6. Observe workflow or natural feedback.
7. Feed success/failure back into seed skill strength and dynamic skill candidates.

## Commands

```bash
./scripts/codex-memory status
./scripts/codex-memory runtime-status
./scripts/codex-memory runtime-status --pretty
./scripts/codex-memory doctor
./scripts/codex-memory ingest "默认使用中文回答"
./scripts/codex-memory search "中文回答偏好"
./scripts/codex-memory queue --status quarantined
./scripts/codex-memory seed-skills --dry-run
./scripts/codex-memory export --output ~/codex-memory-export.json
./scripts/codex-memory prune-runtime
```

Set `CODEX_MEMORY_MODEL` to override the default model. The default is `gpt-5.4-mini`.

## Support Matrix

This alpha is tested for local developer use with:

- Python 3.9 or newer.
- SQLite through Python's standard `sqlite3` module.
- Codex CLI installed and logged in locally.
- macOS as the primary tested platform.

Linux may work when Codex CLI, Python, SQLite, and filesystem permissions match the same assumptions. Windows is not currently supported.

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

Review local privacy state:

```bash
./scripts/codex-memory doctor --privacy
```

Check that hooks and MCP are wired:

```bash
./scripts/codex-memory status
./scripts/codex-memory runtime-status --pretty
./scripts/codex-memory ingest "默认使用中文回答"
./scripts/codex-memory search "中文回答"
```

Observed runtime smoke path:

```text
UserPromptSubmit: "修复这个 bug，并跑测试验证"
PostToolUse: rg/search/list/read command -> inspect_repository
PostToolUse: apply_patch/edit/write tool -> execute_change
PostToolUse: pytest/unittest/npm test/build/lint command -> execute_and_verify
Stop: final answer with verification evidence -> audit_outcome
```

If code was changed without verification, the next turn receives a Runtime control warning. If a learned verification recipe is recommended and then reused, the recipe records reuse, success/failure, command source, exit code, and strength adjustment.

Project-specific workflow detection can be tuned with environment variables or a local `.codex-memory.json` file:

```bash
CODEX_MEMORY_VERIFY_COMMANDS="make verify,tox,pnpm check" ./scripts/codex-memory runtime-status
CODEX_MEMORY_INSPECT_COMMANDS="fd ,git show" ./scripts/codex-memory runtime-status
CODEX_MEMORY_EDIT_COMMANDS="apply_patch,write_file" ./scripts/codex-memory runtime-status
```

Example `.codex-memory.json`:

```json
{
  "runtime_observer": {
    "verify_commands": ["make verify", "tox", "pnpm check"],
    "inspect_commands": ["fd ", "git show"],
    "edit_commands": ["apply_patch", "write_file"]
  }
}
```

Seed skills can be imported to provide a cold-start skill basis before the local Ledger has enough user-specific memories:

```bash
./scripts/codex-memory seed-skills --dry-run
./scripts/codex-memory seed-skills
```

By default this imports agent skill markdown from [`msitarzewski/agency-agents`](https://github.com/msitarzewski/agency-agents) on demand and records each entry as a local `seed_skill` cognitive record with source path, commit, content hash, trust level, feedback counters, and MIT license metadata. The source content is not vendored into this repository. Use `--source /path/to/agency-agents` for an already cloned checkout, `--category design` to import one category, and `--limit N` for a smaller trial import.

Seed skills are a bootstrap layer, not a replacement for personal memory. Runtime Skill generation can use them when long-term memories are still empty; as reviewed memories, successful workflows, and user feedback accumulate, user-specific memories and durable skills should become the stronger basis. Seed skills with repeated failures are automatically suppressed from future Runtime Skill basis retrieval.

Runtime Skill injections are recorded as local audit records with the generated skill JSON, memory basis ids, seed skill ids, session/turn metadata, and a redacted prompt preview. Successful workflows can synthesize `dynamic_skill` candidates, but those candidates are not recommended until they are promoted to active.

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

You can also export, prune processed event payloads, or wipe the Ledger through CLI:

```bash
./scripts/codex-memory export --output ~/codex-memory-export.json
./scripts/codex-memory prune-events --older-than-days 30
./scripts/codex-memory prune-runtime
./scripts/codex-memory wipe --yes
```

`prune-events` only deletes processed rows from the `events` table. It does not remove cognitive runtime observations, workflow violations, learned recipes, or reviewed memories. Use `prune-runtime` to remove runtime audit records such as workflow observations and recipe reuse events; it also clears observation copies embedded in observed workflow metadata. Learned verification recipes are kept unless you pass `--include-recipes`. Use `wipe --yes` to clear the local Ledger completely.

## Privacy

Codex Memory stores events in `~/.codex-memory/ledger.sqlite3`. By default, event payloads are sanitized before storage: allowed fields are retained, long strings are truncated, and secret-like values are redacted. Stored event payloads include `_raw_payload_stored: false`.

Reviewed memory content and evidence can still be stored when they pass review gates. Use `./scripts/codex-memory queue`, `promote`, `reject`, and `delete` to inspect and manage memory records.

Runtime observation is a separate privacy surface from event payload storage. When the observer is enabled, Codex Memory may store structured workflow observations in the local Ledger, including redacted tool command strings, changed file paths, exit codes, source field names, failure flags, and stdout/stderr hashes and lengths. By default, stdout/stderr text previews are not stored in runtime observations or verification recipes.

User opt-out phrases such as "不要记忆" or "do not remember" skip durable memory candidate extraction. They do not mean "do not write any local audit event": sanitized hook events may still be stored so the local workflow guard and audit trail can function. Disable the runtime observer or prune/wipe local data if you need stricter local retention.

To store stdout/stderr previews for local debugging, opt in explicitly:

```bash
CODEX_MEMORY_STORE_RUNTIME_OBSERVATION_PREVIEWS=1 ./scripts/codex-memory doctor --privacy
```

When preview storage is enabled, runtime observations and learned verification recipes may include truncated stdout/stderr text. Do not enable it for sensitive projects.

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

The public alpha command surface is focused on local memory, runtime skills, and observed runtime guardrails: `status`, `runtime-status`, `doctor`, `ingest`, `search`, `queue`, `seed-skills`, `promote`, `reject`, `delete`, `recall-feedback`, `expire`, `audit`, `export`, `prune-events`, `prune-runtime`, `wipe`, `plugin`, `govern`, and `govern-periodic`.

Experimental cognitive, knowledge, skill, and workflow commands are hidden behind an explicit environment switch:

```bash
CODEX_MEMORY_ENABLE_EXPERIMENTAL_CLI=1 ./scripts/codex-memory workflow-plan "plan this task"
```
