#!/usr/bin/env bash
set -euo pipefail

HOOK_NAME="${1:?Usage: codex-cognitive-runtime-hook.sh <hook-name>}"
PLUGIN_ROOT="${PLUGIN_ROOT:-${CODEX_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
LOG_DIR="${CODEX_COGNITIVE_RUNTIME_LOG_DIR:-${HOME}/.codex-cognitive-runtime/logs}"
LOG_FILE="${LOG_DIR}/hooks.log"
export CODEX_COGNITIVE_RUNTIME_LOG_LEVEL="${CODEX_COGNITIVE_RUNTIME_LOG_LEVEL:-DEBUG}"

mkdir -p "$LOG_DIR"
if [[ "${CODEX_COGNITIVE_RUNTIME_INTERNAL_CALL:-}" == "1" || "${CODEX_COGNITIVE_RUNTIME_HOOK_DEPTH:-0}" != "0" ]]; then
  {
    printf '[%s] hook=%s skipped=internal_call plugin_root=%s cwd=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$HOOK_NAME" "$PLUGIN_ROOT" "$(pwd)"
  } >> "$LOG_FILE" 2>/dev/null || true
  printf '{}\n'
  exit 0
fi

{
  printf '[%s] hook=%s plugin_root=%s cwd=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$HOOK_NAME" "$PLUGIN_ROOT" "$(pwd)"
} >> "$LOG_FILE" 2>/dev/null || true

export CODEX_COGNITIVE_RUNTIME_HOOK_DEPTH=1
export PYTHONPATH="${PLUGIN_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
python3 -m codex_cognitive_runtime.hooks "$HOOK_NAME"
