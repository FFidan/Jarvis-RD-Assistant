#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || true)
if [[ -z "${ROOT_DIR}" ]]; then
  ROOT_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
fi

PROMPT_FILE="${RALPH_DESLOPPIFY_PROMPT_FILE:-${ROOT_DIR}/.ralph/desloppify-loop.md}"
TASKS_FILE="${RALPH_DESLOPPIFY_TASKS_FILE:-${ROOT_DIR}/.ralph/ralph-tasks.md}"
MIN_ITERATIONS="${RALPH_DESLOPPIFY_MIN_ITERATIONS:-5}"
MAX_ITERATIONS="${RALPH_DESLOPPIFY_MAX_ITERATIONS:-}"
COMPLETION_PROMISE="${RALPH_DESLOPPIFY_COMPLETION_PROMISE:-REACHED_90_PLUS_AND_QUEUE_DRAINED}"
ABORT_PROMISE="${RALPH_DESLOPPIFY_ABORT_PROMISE:-RALPH_ABORT_HARD_BLOCKER}"
TASK_PROMISE="${RALPH_DESLOPPIFY_TASK_PROMISE:-READY_FOR_NEXT_TASK}"
AGENT_NAME="${RALPH_DESLOPPIFY_AGENT:-codex}"
COMMIT_MODE="${RALPH_DESLOPPIFY_COMMIT_MODE:-off}"
TARGET_OVERALL_SCORE="${RALPH_DESLOPPIFY_TARGET_OVERALL_SCORE:-90}"
TASK_SYNC_COUNT="${RALPH_DESLOPPIFY_TASK_SYNC_COUNT:-12}"
MAX_SESSIONS="${RALPH_DESLOPPIFY_MAX_SESSIONS:-25}"

if [[ -d "${HOME}/.bun/bin" ]]; then
  PATH="${HOME}/.bun/bin:${PATH}"
  export PATH
fi

find_desloppify() {
  local candidates=(
    "${ROOT_DIR}/.venv/bin/desloppify"
    "${ROOT_DIR}/venv/bin/desloppify"
    "${ROOT_DIR}/bin/desloppify"
  )

  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  if command -v desloppify >/dev/null 2>&1; then
    command -v desloppify
    return 0
  fi

  return 1
}

find_ralph() {
  if command -v ralph >/dev/null 2>&1; then
    command -v ralph
    return 0
  fi

  return 1
}

require_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "Missing required file: ${path}" >&2
    exit 1
  fi
}

is_control_mode() {
  local arg
  for arg in "$@"; do
    case "${arg}" in
      --version|-v|--status|--add-context|--clear-context|--list-tasks|--add-task|--remove-task)
        return 0
        ;;
    esac
  done
  return 1
}

DESLOPPIFY_BIN=$(find_desloppify) || {
  echo "Missing required command: desloppify" >&2
  echo "Install it in a repo-local environment or make it available on PATH." >&2
  exit 1
}

RALPH_BIN=$(find_ralph) || {
  echo "Missing required command: ralph" >&2
  echo "Install Ralph and ensure Bun is available on PATH." >&2
  exit 1
}

cd "${ROOT_DIR}"

sync_ralph_tasks() {
  local next_json
  next_json=$("${DESLOPPIFY_BIN}" next --count "${TASK_SYNC_COUNT}" --format json)
  mkdir -p "$(dirname "${TASKS_FILE}")"

  NEXT_JSON="${next_json}" python3 - "${TASKS_FILE}" <<'PY'
import json
import os
import sys
from pathlib import Path

tasks_path = Path(sys.argv[1])
payload = json.loads(os.environ["NEXT_JSON"])
items = payload.get("items", [])

lines = [
    "# Ralph Tasks",
    "",
    "Auto-generated from the live desloppify queue. Do not treat this as an independent source of truth.",
    "",
]

if not items:
    lines.append("- [ ] Completion verification against live desloppify queue, score, and review state")
else:
    for item in items:
        summary = item.get("summary") or item.get("id") or "Unnamed desloppify task"
        cluster = item.get("plan_cluster", {}).get("name")
        if cluster:
            summary = f"{summary} [{cluster}]"
        lines.append(f"- [ ] {summary}")

tasks_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

verify_completion_gate() {
  local status_json queue_output review_output status_text overall queue_count

  status_json=$("${DESLOPPIFY_BIN}" status --json)
  queue_output=$("${DESLOPPIFY_BIN}" plan queue)
  review_output=$("${DESLOPPIFY_BIN}" show review --status open --top 200 --no-budget)
  status_text=$("${DESLOPPIFY_BIN}" status)

  overall=$(STATUS_JSON="${status_json}" python3 - <<'PY'
import json
import os
payload = json.loads(os.environ["STATUS_JSON"])
print(payload.get("overall_score", "0"))
PY
)

  queue_count=$(QUEUE_OUTPUT="${queue_output}" python3 - <<'PY'
import os
import re
text = os.environ["QUEUE_OUTPUT"]
match = re.search(r"Queue:\s+(\d+)\s+items", text)
print(match.group(1) if match else "0")
PY
)

  if python3 - "${overall}" "${TARGET_OVERALL_SCORE}" <<'PY'
import sys
overall = float(sys.argv[1])
target = float(sys.argv[2])
raise SystemExit(0 if overall >= target else 1)
PY
  then
    :
  else
    return 1
  fi

  if [[ "${queue_count}" != "0" ]]; then
    return 1
  fi

  if grep -q "TRIAGE PENDING" <<<"${status_text}"; then
    return 1
  fi

  if grep -q "Incomplete scan" <<<"${status_text}"; then
    return 1
  fi

  if grep -Eq '^[[:space:]]*[○*][[:space:]]+T1[[:space:]]+\[high\]' <<<"${review_output}"; then
    return 1
  fi

  return 0
}

run_ralph_once() {
  local -a args
  args=(
    --agent "${AGENT_NAME}"
    --tasks
    --min-iterations "${MIN_ITERATIONS}"
    --completion-promise "${COMPLETION_PROMISE}"
    --abort-promise "${ABORT_PROMISE}"
    --task-promise "${TASK_PROMISE}"
    --allow-all
    --no-questions
    --prompt-file "${PROMPT_FILE}"
  )

  if [[ -n "${MAX_ITERATIONS}" ]]; then
    args+=(--max-iterations "${MAX_ITERATIONS}")
  fi

  if [[ "${COMMIT_MODE}" != "on" ]]; then
    args+=(--no-commit)
  fi

  "${RALPH_BIN}" "${args[@]}" "$@"
}

echo "Repo root: ${ROOT_DIR}"
echo "Prompt file: ${PROMPT_FILE}"
echo "Tasks file: ${TASKS_FILE}"
echo "desloppify: ${DESLOPPIFY_BIN}"
echo "ralph: ${RALPH_BIN}"
echo "agent: ${AGENT_NAME}"
echo "commit mode: ${COMMIT_MODE}"
echo "target overall score: ${TARGET_OVERALL_SCORE}"

if is_control_mode "$@"; then
  exec "${RALPH_BIN}" "$@"
fi

if [[ "${1:-}" == "--sync-tasks-only" ]]; then
  require_file "${PROMPT_FILE}"
  sync_ralph_tasks
  echo "Synced ${TASKS_FILE} from live desloppify queue."
  exit 0
fi

if [[ "${1:-}" == "--verify-gate" ]]; then
  require_file "${PROMPT_FILE}"
  if verify_completion_gate; then
    echo "Live completion gate satisfied."
    exit 0
  fi
  echo "Live completion gate not satisfied."
  exit 1
fi

require_file "${PROMPT_FILE}"

if ! "${DESLOPPIFY_BIN}" status >/dev/null 2>&1; then
  echo "desloppify preflight failed: '${DESLOPPIFY_BIN} status' did not succeed." >&2
  exit 1
fi

if ! "${DESLOPPIFY_BIN}" next --count 1 >/dev/null 2>&1; then
  echo "desloppify preflight failed: '${DESLOPPIFY_BIN} next --count 1' did not succeed." >&2
  exit 1
fi

session=1
while true; do
  if (( session > MAX_SESSIONS )); then
    echo "Reached launcher max sessions (${MAX_SESSIONS}) without satisfying the completion gate." >&2
    exit 2
  fi

  sync_ralph_tasks
  echo "Starting Ralph session ${session}/${MAX_SESSIONS}..."

  if ! run_ralph_once "$@"; then
    exit $?
  fi

  if verify_completion_gate; then
    echo "Verified completion gate against live desloppify state."
    exit 0
  fi

  echo "Ralph stopped without satisfying the live desloppify completion gate; syncing tasks and continuing."
  session=$((session + 1))
done
