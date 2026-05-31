# Codex Cognitive Runtime

Codex Cognitive Runtime turns clean long-term memory and trusted seed skills into task-specific Runtime Skills for Codex, then observes outcomes to improve future skill selection. For engineering work, it also guards workflows against missing inspection, missing verification, and false completion claims.

This is a local developer beta for local Codex use. It is intended for developers who can inspect local Codex configuration and recover their own environment. It does not guarantee compatibility across Codex CLI versions and is not recommended for sensitive production environments.

## Runtime

- `memory-engine`: extracts, classifies, and ranks memory candidates with `gpt-5.4-mini`.
- `memory-review`: validates schema, evidence, confidence, TTL, duplicate risk, and secret-like content.
- `memory-ledger`: local SQLite audit trail at `~/.codex-cognitive-runtime/ledger.sqlite3`.
- `runtime-skill`: decides whether the current request needs a task-specific skill, retrieves clean active memories, active durable skills, and trusted seed skills, then injects a short action strategy.
- `cognitive-runtime`: observes `UserPromptSubmit`, `PostToolUse`, and `Stop` events to maintain workflow state and inject next-step control signals.
- `workflow-guard`: detects engineering workflow violations such as code changes without verification evidence.
- `skill-synthesizer`: turns successful observed workflows and related experience memories into reusable dynamic skills.
- `runtime-trace`: records a local full-flow trace from user prompt through Runtime Skill generation, injection, workflow observation, Stop audit, feedback, and skill governance updates.
- `mcp`: exposes local memory, Runtime Skill, seed skill, dynamic skill, and runtime governance tools.

The local SQLite Ledger is the only runtime store and source of truth.

The runtime observes Codex tool use; it does not execute shell commands, edit files, or run tests by itself.

Current Runtime Skill beta supports runtime skill generation from clean long-term memory, active durable skills, seed skill cold start, runtime skill injection audit, observed engineering workflows, task start, turn-bound workflow matching, repository inspection, code change detection, verification detection, Stop-time violation checks, next-turn control injection, verification recipe learning, dynamic skill candidate synthesis, and verification recipe reuse feedback. Legacy `workflow-execute` remains as a deprecated alias for experimental `workflow-simulate`; neither command is the runtime execution path.

The runtime observer is enabled by default. Disable it with `CODEX_COGNITIVE_RUNTIME_ENABLE_RUNTIME_OBSERVER=0` if you only want reviewed memory storage without workflow guard behavior.

Runtime Skill lifecycle:

1. Decide whether the request needs a task-specific skill.
2. Retrieve reviewed clean memories, active durable skills, and trusted seed skills.
3. Generate a temporary Runtime Skill.
4. Review the Runtime Skill for allowed basis ids, missing clarification, secret-like content, and unsupported user or organization claims.
5. Inject the reviewed skill and record a local audit event.
6. Observe workflow or natural feedback.
7. Feed success/failure dimensions back into seed skill strength and dynamic skill candidates.
8. Promote reviewed dynamic skill candidates into active durable skills when they are useful.

## Commands

```bash
./scripts/codex-cognitive-runtime status
./scripts/codex-cognitive-runtime runtime-status
./scripts/codex-cognitive-runtime runtime-status --pretty
./scripts/codex-cognitive-runtime doctor
./scripts/codex-cognitive-runtime ingest "默认使用中文回答"
./scripts/codex-cognitive-runtime search "中文回答偏好"
./scripts/codex-cognitive-runtime queue --status quarantined
./scripts/codex-cognitive-runtime seed-skills --dry-run
./scripts/codex-cognitive-runtime seed-skills list
./scripts/codex-cognitive-runtime runtime-skills list
./scripts/codex-cognitive-runtime dynamic-skills list --status candidate
./scripts/codex-cognitive-runtime dynamic-skills stats
./scripts/codex-cognitive-runtime traces list
./scripts/codex-cognitive-runtime traces summary <trace_id>
./scripts/codex-cognitive-runtime runtime-benchmark
./scripts/codex-cognitive-runtime runtime-benchmark --fail-under-defaults
./scripts/codex-cognitive-runtime export --output ~/codex-cognitive-runtime-export.json
./scripts/codex-cognitive-runtime prune-runtime
```

Set `CODEX_COGNITIVE_RUNTIME_MODEL` to override the default model. The default is `gpt-5.4-mini`.
Runtime Skill classification and synthesis use a shorter model timeout than durable memory extraction and fall back to deterministic skills when the model is slow or unavailable.

## Support Matrix

This beta is tested for local developer use with:

- Python 3.9 or newer.
- SQLite through Python's standard `sqlite3` module.
- Codex CLI installed and logged in locally.
- macOS as the primary tested platform.

Linux may work when Codex CLI, Python, SQLite, and filesystem permissions match the same assumptions. Windows is not currently supported.

## Install

Install from a local checkout:

```bash
git clone https://github.com/Limiandy/codex-cognitive-runtime.git ~/plugins/codex-cognitive-runtime
cd ~/plugins/codex-cognitive-runtime
PYTHONPATH=src python3 -m unittest discover -s tests -v
./scripts/codex-cognitive-runtime plugin install --source "$PWD"
./scripts/codex-cognitive-runtime doctor
```

The installer copies the plugin to `~/plugins/codex-cognitive-runtime`, registers it in `~/.agents/plugins/marketplace.json`, and enables it in `~/.codex/config.toml`. Existing Codex config is backed up before writing.

Preview install changes without writing files:

```bash
./scripts/codex-cognitive-runtime plugin install --source "$PWD" --dry-run --diff
```

## Verify

Run doctor after install:

```bash
./scripts/codex-cognitive-runtime doctor
```

Doctor returns JSON with `fatal`, `warn`, and `info` checks. `fatal` failures block the core local memory path. `warn` items need attention but do not block startup. `info` items are optional or skipped checks, such as the default model smoke test.

Run a model smoke test only when you want to verify the local `codex exec` model path:

```bash
./scripts/codex-cognitive-runtime doctor --model-check
```

Review local privacy state:

```bash
./scripts/codex-cognitive-runtime doctor --privacy
```

Check that hooks and MCP are wired:

```bash
./scripts/codex-cognitive-runtime status
./scripts/codex-cognitive-runtime runtime-status --pretty
./scripts/codex-cognitive-runtime ingest "默认使用中文回答"
./scripts/codex-cognitive-runtime search "中文回答"
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

Project-specific workflow detection can be tuned with environment variables or a local `.codex-cognitive-runtime.json` file:

```bash
CODEX_COGNITIVE_RUNTIME_VERIFY_COMMANDS="make verify,tox,pnpm check" ./scripts/codex-cognitive-runtime runtime-status
CODEX_COGNITIVE_RUNTIME_INSPECT_COMMANDS="fd ,git show" ./scripts/codex-cognitive-runtime runtime-status
CODEX_COGNITIVE_RUNTIME_EDIT_COMMANDS="apply_patch,write_file" ./scripts/codex-cognitive-runtime runtime-status
```

Example `.codex-cognitive-runtime.json`:

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
./scripts/codex-cognitive-runtime seed-skills --dry-run
./scripts/codex-cognitive-runtime seed-skills
```

By default this imports agent skill markdown from [`msitarzewski/agency-agents`](https://github.com/msitarzewski/agency-agents) on demand and records each entry as a local `seed_skill` cognitive record with source path, commit, content hash, trust level, feedback counters, and MIT license metadata. The source content is not vendored into this repository. Use `--source /path/to/agency-agents` for an already cloned checkout, `--category design` to import one category, `--limit N` for a smaller trial import, and `--activate` when you intentionally want a local non-git source to be immediately eligible as Runtime Skill basis.

Seed skills are a bootstrap layer, not a replacement for personal memory. Runtime Skill generation can use them when long-term memories are still empty; as reviewed memories, successful workflows, and user feedback accumulate, user-specific memories and durable skills should become the stronger basis. Seed skills stay active for cold start but carry `trust_level`, `trust_state`, source hash, license metadata, and feedback counters; repeated failures, `seed-skills disable`, or `seed-skills suppress` remove them from future Runtime Skill basis retrieval. Disabled and suppressed seed skills also update their record status, so `status` and `trust_state` do not disagree.

Runtime Skill injections are recorded as local runtime records with the generated skill JSON, memory basis ids, durable skill ids, seed skill ids, session/turn metadata, and a redacted prompt preview unless strict privacy mode replaces it with a hash. Feedback is associated with the same turn when available, or with the latest same-session injection within a short recent window. Successful workflows can synthesize `dynamic_skill` candidates, but those candidates are not recommended until they are promoted to active.

Runtime Skill injection and feedback records are stored as local `runtime_skill` cognitive records. Older alpha Ledgers may still contain legacy audit-layer Runtime Skill records; the Ledger runs an idempotent `runtime_skill_governance_shape` migration to normalize those records, add shape metadata, and reconcile seed skill status/trust state. `doctor` reports the migration state.

Dynamic skill governance:

```bash
./scripts/codex-cognitive-runtime dynamic-skills list --status candidate
./scripts/codex-cognitive-runtime dynamic-skills show <skill_id>
./scripts/codex-cognitive-runtime dynamic-skills promote <skill_id> --note "validated"
./scripts/codex-cognitive-runtime dynamic-skills reject <skill_id> --note "too narrow"
./scripts/codex-cognitive-runtime dynamic-skills deprecate <skill_id>
./scripts/codex-cognitive-runtime dynamic-skills stats
```

Runtime and seed skill governance:

```bash
./scripts/codex-cognitive-runtime runtime-skills list
./scripts/codex-cognitive-runtime runtime-skills feedback <injection_id> --outcome positive --target skill_strategy
./scripts/codex-cognitive-runtime seed-skills list
./scripts/codex-cognitive-runtime seed-skills disable <seed_skill_id>
./scripts/codex-cognitive-runtime seed-skills restore <seed_skill_id>
./scripts/codex-cognitive-runtime seed-skills stats
```

Runtime Skill feedback attribution is rule-first. Ambiguous or multi-target feedback can use a short model check; set `CODEX_COGNITIVE_RUNTIME_FEEDBACK_MODEL=0` to keep attribution purely deterministic. `runtime-benchmark` reads the maintained explicit benchmark fixture by default and supports `--synthetic` for the generated regression set.

Runtime Trace flow monitor:

```bash
./scripts/codex-cognitive-runtime traces list --session-id <session_id>
./scripts/codex-cognitive-runtime traces show <trace_id>
./scripts/codex-cognitive-runtime traces events <trace_id>
./scripts/codex-cognitive-runtime traces summary <trace_id>
./scripts/codex-cognitive-runtime traces audit
./scripts/codex-cognitive-runtime traces export <trace_id>
./scripts/codex-cognitive-runtime traces prune --older-than-days 30
```

Trace records are stored in dedicated local tables (`runtime_traces`, `runtime_trace_spans`, `runtime_trace_events`, and `runtime_trace_links`). They link user prompts to skill need decisions, recall skips, memory/durable/seed basis retrieval, Runtime Skill synthesis and review, injections, tool observations, Stop audits, feedback records, and seed/durable skill adjustments. `doctor` reports trace table migration state, trace counts by status, stale/open/failed trace health, and whether live trace logging is enabled.

Live trace logging is disabled by default. Enable it only for local observation:

```bash
export CODEX_COGNITIVE_RUNTIME_TRACE_LIVE_LOG=1
# Then use Codex normally; hook-created trace events are printed to stderr.
```

When enabled for a process that creates trace events, trace events are emitted to `stderr` as JSONL and are not written to a separate log file.

## Uninstall

Disable the plugin but keep files:

```bash
./scripts/codex-cognitive-runtime plugin uninstall
```

Remove the installed plugin files too:

```bash
./scripts/codex-cognitive-runtime plugin uninstall --delete-files
```

Preview uninstall changes:

```bash
./scripts/codex-cognitive-runtime plugin uninstall --dry-run --diff
```

To remove local memory data, stop active Codex sessions using the plugin and delete the state directory:

```bash
rm -rf ~/.codex-cognitive-runtime
```

You can also export, prune processed event payloads, or wipe the Ledger through CLI:

```bash
./scripts/codex-cognitive-runtime export --output ~/codex-cognitive-runtime-export.json
./scripts/codex-cognitive-runtime prune-events --older-than-days 30
./scripts/codex-cognitive-runtime prune-runtime
./scripts/codex-cognitive-runtime wipe --yes
```

`prune-events` only deletes processed rows from the `events` table. It does not remove cognitive runtime observations, workflow violations, learned recipes, or reviewed memories. Use `prune-runtime` to remove runtime audit records such as workflow observations, Runtime Skill injection/feedback records, and recipe reuse events; it also clears observation copies embedded in observed workflow metadata. Learned verification recipes are kept unless you pass `--include-recipes`; dynamic skills are kept unless you pass `--include-skills`. Use `wipe --yes` to clear the local Ledger completely.

## Privacy

Codex Cognitive Runtime stores events in `~/.codex-cognitive-runtime/ledger.sqlite3`. By default, event payloads are sanitized before storage: allowed fields are retained, long strings are truncated, and secret-like values are redacted. Stored event payloads include `_raw_payload_stored: false`.

Reviewed memory content and evidence can still be stored when they pass review gates. Use `./scripts/codex-cognitive-runtime queue`, `promote`, `reject`, and `delete` to inspect and manage memory records.

Runtime observation is a separate privacy surface from event payload storage. When the observer is enabled, Codex Cognitive Runtime may store structured workflow observations in the local Ledger, including redacted tool command strings, changed file paths, exit codes, source field names, failure flags, and stdout/stderr hashes and lengths. By default, stdout/stderr text previews are not stored in runtime observations or verification recipes.

User opt-out phrases such as "不要记忆" or "do not remember" skip durable memory candidate extraction. They do not mean "do not write any local audit event": sanitized hook events may still be stored so the local workflow guard and audit trail can function. Disable the runtime observer or prune/wipe local data if you need stricter local retention.

To store stdout/stderr previews for local debugging, opt in explicitly:

```bash
CODEX_COGNITIVE_RUNTIME_STORE_RUNTIME_OBSERVATION_PREVIEWS=1 ./scripts/codex-cognitive-runtime doctor --privacy
```

When preview storage is enabled, runtime observations and learned verification recipes may include truncated stdout/stderr text. Do not enable it for sensitive projects.

Strict privacy mode further minimizes local runtime data:

```bash
CODEX_COGNITIVE_RUNTIME_STRICT_PRIVACY=1 ./scripts/codex-cognitive-runtime doctor --privacy
```

In strict privacy mode, prompt previews are replaced with hashes, trace records omit raw cwd/project keys, runtime observation commands and changed paths are hashed, Runtime Skill injection records keep only compact skill metadata and basis ids, feedback prompt evidence is hash/count only, stdout/stderr previews stay disabled, live trace logs use the same minimized metadata, and exports omit seed skill content.

Raw event storage is opt-in and should only be used for local debugging:

```bash
CODEX_COGNITIVE_RUNTIME_STORE_RAW_EVENTS=1 ./scripts/codex-cognitive-runtime ingest "debug text"
```

When raw event storage is enabled, original event payloads are written to the local Ledger with `_raw_payload_stored: true`. `status` and `doctor` report that raw event storage is enabled.

## MCP Permissions

MCP defaults to read-only tools. Mutating tools require explicit opt-in:

- `CODEX_COGNITIVE_RUNTIME_ENABLE_MCP_WRITE_TOOLS=1`: allows ingest, recall feedback, and expiration.
- `CODEX_COGNITIVE_RUNTIME_ENABLE_MCP_REVIEW_TOOLS=1`: allows promote and reject.
- `CODEX_COGNITIVE_RUNTIME_ENABLE_MCP_ADMIN_TOOLS=1`: allows delete, reconcile, consolidate, and `govern apply`.

The legacy `CODEX_COGNITIVE_RUNTIME_ENABLE_DANGEROUS_MCP_TOOLS=1` enables all three groups for compatibility, but the narrower switches are preferred.

## Experimental CLI

The public beta command surface is focused on local memory, runtime skills, traces, and observed runtime guardrails: `status`, `runtime-status`, `runtime-benchmark`, `doctor`, `ingest`, `search`, `queue`, `runtime-skills`, `seed-skills`, `dynamic-skills`, `traces`, `promote`, `reject`, `delete`, `recall-feedback`, `expire`, `audit`, `export`, `prune-events`, `prune-runtime`, `wipe`, `plugin`, `govern`, and `govern-periodic`.

Experimental cognitive, knowledge, skill, and workflow commands are hidden behind an explicit environment switch:

```bash
CODEX_COGNITIVE_RUNTIME_ENABLE_EXPERIMENTAL_CLI=1 ./scripts/codex-cognitive-runtime workflow-plan "plan this task"
```
