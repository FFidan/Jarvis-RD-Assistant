#!/usr/bin/env bash
# jarvis-research — the lifecycle CLI for a managed JARVIS install.
#
# This repository script contains the lifecycle logic. The installed
# `jarvis-research` launcher resolves the selected installation and executes this
# file with `--repo <dir> "$@"`, so application updates include matching CLI
# behavior.
#
# Subcommands:
#   update [--to <tag>] [--resume <tag>] [--yes]   transactional, DB-safe upgrade
#   status | start | stop | restart | logs         day-to-day container control
#   owner status | owner set <email>               owner inspection and recovery
#   restore status                                 report restore progress
#   restore legacy <timestamp>                     accept an unsigned same-host set
#   restore request <timestamp>                    print the off-host restore steps
#   restore acknowledge <restore-id>               release off-host quarantine
#   doctor                                          read-only health + preflight
#   repair                                          bounded, non-destructive recovery
#   register                                        record this repo as an install
#   uninstall [--dry-run] [--tier N] [--all] ...     tiered, contained teardown
#   version | help
#
# Exit codes: 0 ok · 1 refused/failed · 2 usage · 3 environment (no docker).
# The only operation that advances the branch is a fast-forward merge to an
# approved release tag; the branch is never force-rewritten.
set -euo pipefail

LIFECYCLE_CODE_DIR="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P
)" || {
  printf '[ERROR] Cannot resolve the lifecycle command directory.\n' >&2
  exit 1
}

# -----------------------------------------------------------------------------
# Output and error helpers.
# -----------------------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi
info() { printf '%s[INFO]%s  %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[ERROR]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }

# die MSG NEXT — a refused/failed operation. Two lines: what happened + what to
# run next. Every failure path also points at `jarvis-research doctor`.
die() {
  err "$1"
  printf '        %s%s%s\n' "$C_YELLOW" "$2" "$C_RESET" >&2
  exit 1
}
# usage_error MSG [NEXT] — a misuse. Same two-line shape as die: what was wrong +
# the correct invocation. Callers with no single correct invocation to name fall
# back to the general help pointer.
usage_error() {
  err "$1"
  printf '        %s%s%s\n' "$C_YELLOW" "${2:-Run: jarvis-research help}" "$C_RESET" >&2
  exit 2
}
env_die() {
  err "$1"
  printf '        %s%s%s\n' "$C_YELLOW" "$2" "$C_RESET" >&2
  exit 3
}

# -----------------------------------------------------------------------------
# CLI state dir (deliberately NOT ~/.config/jarvis, which is chmod 700 and holds
# the API key — CLI bookkeeping must not live in a secret-bearing directory).
# -----------------------------------------------------------------------------
STATE_DIR="${JARVIS_CLI_CONFIG_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/jarvis-research}"
INSTALLS_FILE="${STATE_DIR}/installs"
LEGACY_PENDING_FILE_PATH="${STATE_DIR}/pending-update.json"
PENDING_FILE_PATH=""
STAGED_SIDECAR_MARKER_PATH=""

_acquire_update_lock() {
  local rc=0
  claim_host_lifecycle_lock "$REPO" || rc=$?
  case "$rc" in
    0) ;;
    3) die "Another lifecycle operation is already running for this JARVIS install." \
         "Wait for it to finish, then run: jarvis-research update" ;;
    *) die "The per-install lifecycle lock is missing or unsafe; refusing the update." \
         "No services were changed. Run: jarvis-research doctor" ;;
  esac
}

_control_lifecycle_exit() {
  local rc=$?
  trap - EXIT
  set +e
  finish_lifecycle_operation "$REPO" control
  exit "$rc"
}

_acquire_control_lifecycle() {
  local rc=0
  claim_host_lifecycle_lock "$REPO" || rc=$?
  case "$rc" in
    0) ;;
    3) die "Another lifecycle operation is already running for this JARVIS install." \
         "No services were changed. Wait for it to finish, then retry." ;;
    *) die "The per-install lifecycle lock is unavailable or unsafe." \
         "No services were changed. Run: jarvis-research doctor" ;;
  esac
  rc=0
  claim_lifecycle_operation "$REPO" control || rc=$?
  case "$rc" in
    0) trap _control_lifecycle_exit EXIT ;;
    3|4) die "Another lifecycle operation is active or needs recovery." \
           "No services were changed. Finish it, then retry." ;;
    *) die "The private lifecycle volume is unavailable or unsafe." \
         "No services were changed. Run: jarvis-research doctor" ;;
  esac
}

_finish_control_lifecycle() {
  finish_lifecycle_operation "$REPO" control \
    || die "The command finished, but its lifecycle state could not be cleared." \
      "Run: jarvis-research doctor"
  trap - EXIT
}

# -----------------------------------------------------------------------------
# Repository resolution.
# -----------------------------------------------------------------------------
_valid_repo() { [ -f "$1/docker-compose.yml" ] && [ -f "$1/versions.env" ]; }

# _find_repo_upwards START — the nearest ancestor of START (inclusive) that is a
# JARVIS checkout, or non-zero when none is.
_find_repo_upwards() {
  local d="$1"
  while [ -n "$d" ] && [ "$d" != "/" ]; do
    _valid_repo "$d" && { printf '%s' "$d"; return 0; }
    d="$(dirname "$d")"
  done
  _valid_repo "/" && { printf '/'; return 0; }
  return 1
}

# resolve_repo — echo the managed repo. Order: --repo / JARVIS_RESEARCH_HOME,
# then cwd-or-ancestor, then the first live line of the installs registry (stale
# lines skipped with a warning). Fails with actionable guidance otherwise.
resolve_repo() {
  local cand line
  if [ -n "${REPO_OVERRIDE:-}" ]; then
    _valid_repo "$REPO_OVERRIDE" && { printf '%s' "$REPO_OVERRIDE"; return 0; }
    die "--repo ${REPO_OVERRIDE} is not a JARVIS install (no docker-compose.yml + versions.env)." \
        "Point --repo at the install directory, or run: jarvis-research register"
  fi
  if [ -n "${JARVIS_RESEARCH_HOME:-}" ]; then
    _valid_repo "$JARVIS_RESEARCH_HOME" && { printf '%s' "$JARVIS_RESEARCH_HOME"; return 0; }
    die "JARVIS_RESEARCH_HOME (${JARVIS_RESEARCH_HOME}) is not a JARVIS install." \
        "Unset it or point it at the install directory; then run: jarvis-research doctor"
  fi
  cand="$(_find_repo_upwards "$PWD" 2>/dev/null || true)"
  [ -n "$cand" ] && { printf '%s' "$cand"; return 0; }
  if [ -f "$INSTALLS_FILE" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      if _valid_repo "$line"; then printf '%s' "$line"; return 0; fi
      warn "Skipping stale install path (no longer a JARVIS checkout): ${line}"
    done < "$INSTALLS_FILE"
  fi
  die "No JARVIS install found (not in a checkout, and none registered)." \
      "cd into your JARVIS directory and run: jarvis-research register"
}

# -----------------------------------------------------------------------------
# Pending-transaction file (written to the state dir before any branch advance).
# -----------------------------------------------------------------------------
TXN_SCHEMA_VERSION=1
TXN_FROM_SHA=""; TXN_FROM_VERSION=""; TXN_TARGET=""; TXN_TARGET_SHA=""
TXN_TARGET_VERSION=""; TXN_PHASE=""; TXN_STARTED_AT=""; TXN_BACKUP_ID=""
TXN_BACKUP_RUN_ID=""; TXN_LEGACY_RECOVERY="false"

_init_pending_file_path() {
  local lock_path install_key
  lock_path="$(host_lifecycle_lock_path "$REPO" 2>/dev/null || true)"
  install_key="${lock_path##*/}"
  install_key="${install_key%.lock}"
  printf '%s' "$install_key" | grep -Eq '^[0-9a-f]{64}$' || return 1
  PENDING_FILE_PATH="${STATE_DIR}/pending-update-${install_key}.json"
  STAGED_SIDECAR_MARKER_PATH="${STATE_DIR}/pending-update-${install_key}.backup-sidecar-quiesced"
}

_txn_file_field() {
  local path="$1" field="$2"
  [ -f "$path" ] || return 0
  grep -oE "\"${field}\":\"[^\"]*\"" "$path" 2>/dev/null \
    | head -1 | sed -E "s/\"${field}\":\"([^\"]*)\"/\1/"
}

_txn_field() {
  _txn_file_field "$PENDING_FILE_PATH" "$1"
}

_txn_state_shape() {
  local path="$1" state
  [ -f "$path" ] && [ ! -L "$path" ] || return 1
  state="$(cat "$path" 2>/dev/null || true)"
  [ "$(wc -l < "$path" 2>/dev/null || echo 0)" -eq 1 ] 2>/dev/null || return 1
  [ "${#state}" -le 4096 ] || return 1
  if printf '%s' "$state" | grep -Eq '^\{"schema_version":1,"from_sha":"[0-9a-f]{40}","from_version":"(unknown|[A-Za-z0-9][A-Za-z0-9._+-]*)","target":"[A-Za-z0-9][A-Za-z0-9._/-]*","target_sha":"[0-9a-f]{40}","target_version":"[A-Za-z0-9][A-Za-z0-9._+/-]*","phase":"(merge_pending|pull|health|committed)","started_at":"[0-9]+","backup_id":"([0-9]{8}_[0-9]{6})?","backup_run_id":"([0-9a-f]{32})?","legacy_recovery":(true|false)\}$'; then
    printf 'current'
    return 0
  fi
  if printf '%s' "$state" | grep -Eq '^\{"from_sha":"[0-9a-f]{40}","from_version":"(unknown|[A-Za-z0-9][A-Za-z0-9._+-]*)","target":"[A-Za-z0-9][A-Za-z0-9._/-]*","target_version":"[A-Za-z0-9][A-Za-z0-9._+/-]*","phase":"(staging|merged)","started_at":"[0-9]+","backup_id":"([0-9]{8}_[0-9]{6})?"\}$'; then
    printf 'legacy'
    return 0
  fi
  return 1
}

_pending_state_matches_repo() {
  local path="$1" repo="$2" shape target target_version target_sha from_sha phase
  local head resolved
  shape="$(_txn_state_shape "$path" 2>/dev/null || true)"
  [ -n "$shape" ] || return 2
  target="$(_txn_file_field "$path" target)"
  target_version="$(_txn_file_field "$path" target_version)"
  from_sha="$(_txn_file_field "$path" from_sha)"
  phase="$(_txn_file_field "$path" phase)"
  [ "$target_version" = "${target#v}" ] || return 2
  head="$(git -C "$repo" rev-parse HEAD 2>/dev/null || true)"
  resolved="$(git -C "$repo" rev-parse "${target}^{commit}" 2>/dev/null || true)"
  printf '%s' "$head" | grep -Eq '^[0-9a-f]{40}$' || return 3
  printf '%s' "$resolved" | grep -Eq '^[0-9a-f]{40}$' || return 3

  if [ "$shape" = current ]; then
    target_sha="$(_txn_file_field "$path" target_sha)"
    [ "$resolved" = "$target_sha" ] || return 1
    case "$phase" in
      merge_pending) [ "$head" = "$from_sha" ] || [ "$head" = "$target_sha" ] ;;
      pull|health|committed) [ "$head" = "$target_sha" ] ;;
      *) return 2 ;;
    esac
    return
  fi

  case "$phase" in staging|merged) [ "$head" = "$resolved" ] ;; *) return 2 ;; esac
}

_migrate_legacy_pending_for_repo() {
  local selected candidate canon seen="" candidates matches=0 matched_repo="" match_rc
  [ -e "$LEGACY_PENDING_FILE_PATH" ] || return 0
  if [ ! -f "$LEGACY_PENDING_FILE_PATH" ] || [ -L "$LEGACY_PENDING_FILE_PATH" ]; then
    err "The old shared update journal is not a safe regular file."
    return 1
  fi
  [ ! -e "$PENDING_FILE_PATH" ] || return 0
  mkdir -p "$STATE_DIR" || return 1
  _txn_state_shape "$LEGACY_PENDING_FILE_PATH" >/dev/null 2>&1 || {
    err "The old shared update journal is malformed; it was left untouched."
    return 1
  }
  selected="$(canonical_path_portable "$REPO" 2>/dev/null || true)"
  [ -n "$selected" ] || return 1
  candidates="$selected"$'\n'
  if [ -f "$INSTALLS_FILE" ]; then
    candidates="${candidates}$(cat "$INSTALLS_FILE")"$'\n'
  fi
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    canon="$(canonical_path_portable "$candidate" 2>/dev/null || true)"
    if [ -z "$canon" ] || ! _valid_repo "$canon"; then
      continue
    fi
    printf '%s\n' "$seen" | grep -qxF "$canon" && continue
    seen="${seen}${canon}"$'\n'
    match_rc=0
    _pending_state_matches_repo "$LEGACY_PENDING_FILE_PATH" "$canon" || match_rc=$?
    case "$match_rc" in
      0)
        matches=$((matches + 1))
        matched_repo="$canon" ;;
      1) : ;;
      2)
        err "The old shared update journal is malformed; it was left untouched."
        return 1 ;;
      *)
        err "A registered install could not be checked against the old shared update journal."
        return 1 ;;
    esac
  done <<< "$candidates"
  if [ "$matches" -ne 1 ] || [ "$matched_repo" != "$selected" ]; then
    err "The old shared update journal cannot be attributed uniquely to this install; it was left untouched."
    return 1
  fi
  [ ! -e "$PENDING_FILE_PATH" ] && [ -f "$LEGACY_PENDING_FILE_PATH" ] \
    || return 1
  mv "$LEGACY_PENDING_FILE_PATH" "$PENDING_FILE_PATH" || return 1
  info "Moved this install's interrupted update record to its per-install journal."
}

_txn_persist() {
  local tmp
  mkdir -p "$STATE_DIR"
  tmp="$(mktemp "${STATE_DIR}/.pending-update.XXXXXX")" || return 1
  if ! printf '{"schema_version":%s,"from_sha":"%s","from_version":"%s","target":"%s","target_sha":"%s","target_version":"%s","phase":"%s","started_at":"%s","backup_id":"%s","backup_run_id":"%s","legacy_recovery":%s}\n' \
      "$TXN_SCHEMA_VERSION" "$TXN_FROM_SHA" "$TXN_FROM_VERSION" "$TXN_TARGET" \
      "$TXN_TARGET_SHA" "$TXN_TARGET_VERSION" "$TXN_PHASE" "$TXN_STARTED_AT" \
      "$TXN_BACKUP_ID" "$TXN_BACKUP_RUN_ID" "$TXN_LEGACY_RECOVERY" > "$tmp" \
     || ! chmod 600 "$tmp" 2>/dev/null \
     || ! mv -f "$tmp" "$PENDING_FILE_PATH"; then
    rm -f "$tmp"
    return 1
  fi
}

_txn_write_new() {
  local phase="$1" target="$2" version="$3" target_sha="$4"
  local backup_id="${5:-}" backup_run_id="${6:-}" from_version
  TXN_FROM_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
  from_version="$(sed -n 's/^JARVIS_IMAGE_TAG=//p' .env 2>/dev/null | head -1)"
  if [ -z "$from_version" ]; then
    from_version="$(sed -n 's/^JARVIS_VERSION=//p' .env 2>/dev/null | head -1)"
  fi
  if ! printf '%s' "${from_version:-unknown}" | grep -Eq '^(unknown|[A-Za-z0-9][A-Za-z0-9._+-]*)$'; then
    from_version="unknown"
  fi
  TXN_FROM_VERSION="${from_version:-unknown}"
  TXN_TARGET="$target"; TXN_TARGET_SHA="$target_sha"; TXN_TARGET_VERSION="$version"
  TXN_PHASE="$phase"; TXN_STARTED_AT="${UPDATE_START_EPOCH:-0}"
  TXN_BACKUP_ID="$backup_id"; TXN_BACKUP_RUN_ID="$backup_run_id"
  TXN_LEGACY_RECOVERY="false"
  # Refuse to persist any field the state reader would reject: a record that
  # cannot be loaded back strands the update it is supposed to make resumable.
  if ! [[ "$TXN_FROM_SHA" =~ ^[0-9a-f]{40}$ ]] \
     || [ "$TXN_PHASE" != "merge_pending" ] \
     || ! [[ "$TXN_BACKUP_ID" =~ ^([0-9]{8}_[0-9]{6})?$ ]] \
     || ! [[ "$TXN_BACKUP_RUN_ID" =~ ^([0-9a-f]{32})?$ ]] \
     || { [ -n "$TXN_BACKUP_ID" ] && [ -z "$TXN_BACKUP_RUN_ID" ]; } \
     || { [ -z "$TXN_BACKUP_ID" ] && [ -n "$TXN_BACKUP_RUN_ID" ]; }; then
    return 1
  fi
  _txn_persist
}

_txn_load_current_shape() {
  local state
  [ "$(_txn_state_shape "$PENDING_FILE_PATH" 2>/dev/null || true)" = current ] || return 1
  state="$(cat "$PENDING_FILE_PATH" 2>/dev/null || true)"
  TXN_SCHEMA_VERSION=1
  TXN_FROM_SHA="$(_txn_field from_sha)"; TXN_FROM_VERSION="$(_txn_field from_version)"
  TXN_TARGET="$(_txn_field target)"; TXN_TARGET_SHA="$(_txn_field target_sha)"
  TXN_TARGET_VERSION="$(_txn_field target_version)"; TXN_PHASE="$(_txn_field phase)"
  TXN_STARTED_AT="$(_txn_field started_at)"; TXN_BACKUP_ID="$(_txn_field backup_id)"
  TXN_BACKUP_RUN_ID="$(_txn_field backup_run_id)"
  TXN_LEGACY_RECOVERY="$(printf '%s' "$state" | grep -oE '"legacy_recovery":(true|false)' | cut -d: -f2)"
  if [ "$TXN_LEGACY_RECOVERY" = "false" ]; then
    if { [ -n "$TXN_BACKUP_ID" ] && [ -z "$TXN_BACKUP_RUN_ID" ]; } \
       || { [ -z "$TXN_BACKUP_ID" ] && [ -n "$TXN_BACKUP_RUN_ID" ]; }; then
      return 1
    fi
  elif [ -n "$TXN_BACKUP_RUN_ID" ]; then
    return 1
  fi
  return 0
}

_txn_load_legacy_staging() {
  local state target_sha head verified
  [ "$(_txn_state_shape "$PENDING_FILE_PATH" 2>/dev/null || true)" = legacy ] || return 1
  state="$(cat "$PENDING_FILE_PATH" 2>/dev/null || true)"
  TXN_TARGET="$(_txn_field target)"; TXN_TARGET_VERSION="$(_txn_field target_version)"
  [ "$TXN_TARGET_VERSION" = "${TXN_TARGET#v}" ] || return 1
  target_sha="$(git rev-parse "${TXN_TARGET}^{commit}" 2>/dev/null || true)"
  head="$(git rev-parse HEAD 2>/dev/null || true)"
  [ -n "$target_sha" ] && [ "$head" = "$target_sha" ] || return 1
  TXN_SCHEMA_VERSION=1
  TXN_FROM_SHA="$(_txn_field from_sha)"; TXN_FROM_VERSION="$(_txn_field from_version)"
  TXN_TARGET_SHA="$target_sha"
  TXN_PHASE="pull"; TXN_STARTED_AT="$(_txn_field started_at)"
  TXN_BACKUP_ID="$(_txn_field backup_id)"; TXN_BACKUP_RUN_ID=""
  if [ -n "$TXN_BACKUP_ID" ]; then
    verified="$(_backup_volume_helper verify "$TXN_BACKUP_ID" "" legacy 2>/dev/null)" \
      || return 1
    [ "$verified" = "${TXN_BACKUP_ID}|" ] || return 1
  fi
  TXN_LEGACY_RECOVERY="true"
  if [ -n "$TXN_BACKUP_ID" ]; then
    _write_update_backup_pin "$TXN_BACKUP_ID" "" true || return 1
  fi
  _txn_persist
}

_txn_validate_context() {
  local resolved_target head
  resolved_target="$(git rev-parse "${TXN_TARGET}^{commit}" 2>/dev/null || true)"
  head="$(git rev-parse HEAD 2>/dev/null || true)"
  if [ -z "$resolved_target" ] || [ "$resolved_target" != "$TXN_TARGET_SHA" ]; then
    err "Pending update target '${TXN_TARGET}' no longer resolves to its recorded commit; refusing to continue."
    return 1
  fi
  if [ "$TXN_TARGET_VERSION" != "${TXN_TARGET#v}" ]; then
    err "Pending update version metadata is inconsistent; refusing to continue."
    return 1
  fi
  case "$TXN_PHASE" in
    merge_pending)
      if [ "$head" != "$TXN_FROM_SHA" ] && [ "$head" != "$TXN_TARGET_SHA" ]; then
        err "Checkout HEAD is unrelated to the pending update's source and target commits."
        return 1
      fi ;;
    pull|health|committed)
      if [ "$head" != "$TXN_TARGET_SHA" ]; then
        err "Checkout HEAD does not match the pending update target '${TXN_TARGET}'."
        return 1
      fi ;;
  esac
  return 0
}

_txn_load() {
  local shape
  [ -f "$PENDING_FILE_PATH" ] || return 1
  shape="$(_txn_state_shape "$PENDING_FILE_PATH" 2>/dev/null || true)"
  case "$shape" in
    current) _txn_load_current_shape && { _txn_validate_context; return; } ;;
    legacy) _txn_load_legacy_staging && { _txn_validate_context; return; } ;;
  esac
  err "Pending update state is missing, truncated, unknown, or internally inconsistent."
  return 1
}

_txn_load_or_die() {
  _txn_load || die "Cannot safely resume the pending update transaction." \
    "No services were changed. Inspect ${PENDING_FILE_PATH}, then run: jarvis-research doctor"
}

_txn_update_phase() {
  local next="$1"
  case "$next" in merge_pending|pull|health|committed) ;; *) return 1 ;; esac
  _txn_load || return 1
  TXN_PHASE="$next"
  _txn_persist
}

# _txn_phase_or_die PHASE — record a phase the update must not continue past.
# The five update routes share one wording so the journal advice cannot drift
# between them under a later edit. The two failure-path calls in
# _resume_transaction deliberately warn instead: dying there would skip the
# epilogue that tells the operator how to roll back.
_txn_phase_or_die() {
  _txn_update_phase "$1" \
    || die "Could not record the update's progress in its journal." \
      "Check ${PENDING_FILE_PATH}, then run: jarvis-research doctor"
}

# -----------------------------------------------------------------------------
# Update-flow guards.
# -----------------------------------------------------------------------------
# (1) Only a registered, managed checkout may be updated by this tool.
_require_managed_install() {
  local registered=0 origin want
  if [ -n "${REPO_OVERRIDE:-}" ]; then
    registered=1
  elif [ -f "$INSTALLS_FILE" ] && grep -qxF "$REPO" "$INSTALLS_FILE"; then
    registered=1
  fi
  if [ "$registered" -ne 1 ]; then
    die "This JARVIS install is not registered with jarvis-research." \
        "Run: jarvis-research register   (then: jarvis-research update)"
  fi
  want="${JARVIS_RESEARCH_REMOTE:-limitcycle-oss/jarvis-rd-assistant}"
  origin="$(git remote get-url origin 2>/dev/null || true)"
  if ! printf '%s' "$origin" | tr '[:upper:]' '[:lower:]' | grep -qF "$(printf '%s' "$want" | tr '[:upper:]' '[:lower:]')"; then
    die "Origin (${origin:-none}) is not the managed JARVIS repository." \
        "Set JARVIS_RESEARCH_REMOTE for a fork, or update this checkout the way you installed it."
  fi
}

# (2) The Docker daemon, and a clean, on-main, non-detached checkout.
_require_docker_daemon() {
  docker info >/dev/null 2>&1 \
    || env_die "The Docker daemon is not reachable." "Start Docker, then re-run: jarvis-research doctor"
}
_require_clean_main_checkout() {
  local branch git_dir op hidden marker_rel tracked_marker dirt
  branch="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
  if [ -z "$branch" ]; then
    die "HEAD is detached; jarvis-research updates only a normal 'main' checkout." \
        "Run: git checkout main   (then: jarvis-research update)"
  fi
  if [ "$branch" != "main" ]; then
    die "You are on branch '${branch}', not 'main'; refusing to update a working branch." \
        "Run: git checkout main   (then: jarvis-research update)"
  fi

  # A clean tree is not the same as an updatable tree. An interrupted rebase,
  # merge, cherry-pick or revert leaves the porcelain status empty but makes the
  # release fast-forward fail once the update transaction is already open.
  # Refuse here, while nothing is at stake.
  # Git before 2.13 does not know --absolute-git-dir. rev-parse echoes an unknown
  # flag back and exits 0, so a successful call is not yet a usable directory and
  # the loop below would silently test paths that cannot exist.
  git_dir="$(git rev-parse --absolute-git-dir 2>/dev/null)" \
    || die "Could not locate this installation's Git directory." \
        "Check the checkout, then re-run: jarvis-research doctor"
  [ -d "$git_dir" ] \
    || die "Could not locate this installation's Git directory." \
        "Upgrade Git to 2.13 or newer, then re-run: jarvis-research doctor"
  for op in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG; do
    if [ -e "${git_dir}/${op}" ]; then
      die "A Git operation is already in progress in this checkout; refusing to update." \
          "Finish or abort it (for example: git rebase --abort), then re-run: jarvis-research update"
    fi
  done

  # Tracked files flagged skip-worktree (tag 'S') or assume-unchanged (lowercase
  # tag) hide real modifications from every status query, including the one below.
  hidden="$(git ls-files -v 2>/dev/null | sed -n 's/^[a-zS] //p')" \
    || die "Could not inspect this installation's index flags." \
        "Check the Git installation, then re-run: jarvis-research doctor"
  if [ -n "$hidden" ]; then
    printf '%s\n' "$hidden" | head -20 >&2
    die "Some tracked files are flagged to hide local changes; refusing to update." \
        "Clear them with: git update-index --no-skip-worktree --no-assume-unchanged <path>"
  fi

  # The exemption is for one product-managed regular file. A directory or symlink
  # at that path is not it: the pathspec below excludes a prefix, so without this
  # fence any content beneath it would be laundered.
  # Transitional: tolerates the pre-relocation marker in secrets/; deletable when no supported update source predates the durable state directory.
  marker_rel="secrets/manifest-hmac-required"
  if { [ -e "$marker_rel" ] || [ -L "$marker_rel" ]; } \
     && { [ ! -f "$marker_rel" ] || [ -L "$marker_rel" ]; }; then
    die "The signed-manifest marker is not a regular file; refusing to update." \
        "Inspect ${marker_rel}, then re-run: jarvis-research doctor"
  fi
  # It is machine-local and must never be tracked; a tracked copy would let the
  # exclusion hide a real modification to a committed file.
  tracked_marker="$(git ls-files -- "$marker_rel" 2>/dev/null)" \
    || die "Could not inspect this installation's index." \
        "Check the Git installation, then re-run: jarvis-research doctor"
  if [ -n "$tracked_marker" ]; then
    die "The signed-manifest marker is tracked in this checkout; refusing to update." \
        "Remove it from version control, then re-run: jarvis-research update"
  fi
  # One repo-wide status. Declared then assigned: `local x="$(...)"` returns
  # local's status and would silently defeat the fail-closed branch.
  dirt="$(git status --porcelain -- ':(top)' ":(top,exclude)${marker_rel}" 2>/dev/null)" \
    || die "Could not inspect this installation's working tree." \
        "Check the Git installation, then re-run: jarvis-research doctor"
  if [ -n "$dirt" ]; then
    printf '%s\n' "$dirt" | head -20 >&2
    [ "$(printf '%s\n' "$dirt" | wc -l)" -le 20 ] || printf '        ... and more\n' >&2
    die "Your working tree has uncommitted changes; refusing to update." \
        "Restore or move the paths listed above, then re-run: jarvis-research update. Leave ${marker_rel} in place; it is managed by the backup service."
  fi
}

# -----------------------------------------------------------------------------
# Migration classification + the mandatory backup gate.
# -----------------------------------------------------------------------------
_active_profiles() {
  sed -n 's/^COMPOSE_PROFILES=//p' .env 2>/dev/null | head -1 | tr ',' ' '
}

# _new_migrations TARGET_REF — the migration files added between HEAD and the tag.
_new_migrations() {
  git diff --name-only "HEAD..$1" -- db/migrations/ 2>/dev/null || true
}

# _migrations_need_backup TARGET_REF MIGRATIONS — true when any new migration
# carries a data-changing statement. Read each migration's CONTENT from the tag
# (`git show`), NEVER the working tree — those files do not exist locally yet.
# Broad matcher over non-comment lines: fail toward taking a backup.
_migrations_need_backup() {
  local target_ref="$1" migs="$2" path content
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    content="$(git show "${target_ref}:${path}" 2>/dev/null || true)"
    if printf '%s\n' "$content" \
        | grep -vE '^[[:space:]]*(--|$)' \
        | grep -qiE 'DELETE[[:space:]]+FROM|DROP[[:space:]]|TRUNCATE|UPDATE[[:space:]]|ALTER[[:space:]]+TABLE.*(DROP|RENAME|ALTER[[:space:]]+COLUMN|SET[[:space:]]+DATA[[:space:]]+TYPE|TYPE[[:space:]])'; then
      return 0
    fi
  done <<< "$migs"
  return 1
}

BACKUP_COMPOSE_INITIALIZED=0
BACKUP_COMPOSE_VERIFIED=0
BACKUP_COMPOSE_PROJECT=""
BACKUP_COMPOSE_CONFIG_LABEL=""
declare -a BACKUP_COMPOSE_FILES=()
UPDATE_VOLUME_GUARD_ID=""
UPDATE_VOLUME_GUARD_ACTIVE=0
STAGED_BACKUP_SIDECAR_QUIESCED=0

_init_backup_volume_compose() {
  local raw item candidate canon seen="" joined="" name
  local -a requested=()
  [ "$BACKUP_COMPOSE_INITIALIZED" -eq 0 ] || return 0
  name="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' .env 2>/dev/null | head -1)"
  case "$name" in
    \"*\") name="${name#\"}"; name="${name%\"}" ;;
    \'*\') name="${name#\'}"; name="${name%\'}" ;;
  esac
  [ -n "$name" ] || name="$(basename "$REPO" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  printf '%s' "$name" | grep -Eq '^[a-z0-9][a-z0-9_-]*$' || return 1
  BACKUP_COMPOSE_PROJECT="$name"
  raw="$(sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null | head -1)"
  case "$raw" in
    \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
    \'*\') raw="${raw#\'}"; raw="${raw%\'}" ;;
  esac
  if [ -z "$raw" ]; then
    raw=docker-compose.yml
    [ ! -f "$REPO/docker-compose.override.yml" ] || raw="${raw}:docker-compose.override.yml"
  fi
  IFS=: read -r -a requested <<< "$raw"
  for item in "${requested[@]}"; do
    [ -n "$item" ] || return 1
    case "$item" in /*) candidate="$item" ;; *) candidate="$REPO/$item" ;; esac
    canon="$(canonical_path_portable "$candidate" 2>/dev/null || true)"
    [ -n "$canon" ] && [ -f "$canon" ] && _lifecycle_path_inside_repo "$canon" "$REPO" || return 1
    printf '%s\n' "$seen" | grep -qxF "$canon" && return 1
    BACKUP_COMPOSE_FILES+=("$canon")
    seen="${seen}${canon}"$'\n'
    joined="${joined:+${joined},}${canon}"
  done
  [ "${BACKUP_COMPOSE_FILES[0]:-}" = "$REPO/docker-compose.yml" ] || return 1
  [ -f "$LIFECYCLE_CODE_DIR/backup-lifecycle.sh" ] \
    && [ ! -L "$LIFECYCLE_CODE_DIR/backup-lifecycle.sh" ] || return 1
  BACKUP_COMPOSE_CONFIG_LABEL="$joined"
  BACKUP_COMPOSE_INITIALIZED=1
}

_raw_backup_volume_compose() {
  local -a cmd=(docker compose --project-directory "$REPO" --env-file "$REPO/.env" -p "$BACKUP_COMPOSE_PROJECT")
  local file timeout_seconds="${BACKUP_COMPOSE_TIMEOUT_SECONDS:-}"
  for file in "${BACKUP_COMPOSE_FILES[@]}"; do cmd+=(-f "$file"); done
  if [ -n "$timeout_seconds" ]; then
    timeout --kill-after=5s "${timeout_seconds}s" \
      env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
        -u COMPOSE_PATH_SEPARATOR -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE \
        "${cmd[@]}" "$@" 8>&-
  else
    env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
        -u COMPOSE_PATH_SEPARATOR -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE \
        "${cmd[@]}" "$@" 8>&-
  fi
}

_verify_backup_volume_compose_owner() {
  local cid labels project workdir configs
  [ "$BACKUP_COMPOSE_VERIFIED" -eq 0 ] || return 0
  # -a, not a running-only probe: the recovery commands exist for the disaster
  # where Postgres will not start, and a stopped container carries the same
  # compose labels this check reads. Requiring a RUNNING one made break-glass
  # unreachable in the case it was written for.
  cid="$(_raw_backup_volume_compose ps -a -q postgres 2>/dev/null | head -1 || true)"
  if [ -z "$cid" ]; then
    err "This install has no Postgres container, so the backup service's ownership cannot be verified."
    err "Start the stack once so its containers exist, then retry: jarvis-research start"
    return 1
  fi
  labels="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}|{{ index .Config.Labels "com.docker.compose.project.working_dir" }}|{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$cid" 2>/dev/null || true)"
  IFS='|' read -r project workdir configs <<< "$labels"
  if [ "$project" != "$BACKUP_COMPOSE_PROJECT" ] \
     || [ "$workdir" != "$REPO" ] \
     || [ "$configs" != "$BACKUP_COMPOSE_CONFIG_LABEL" ]; then
    err "Compose ownership does not match this managed JARVIS install."
    return 1
  fi
  BACKUP_COMPOSE_VERIFIED=1
}

_backup_volume_compose() {
  _init_backup_volume_compose || return 1
  _verify_backup_volume_compose_owner || return 1
  _raw_backup_volume_compose "$@"
}

_backup_volume_helper() {
  _backup_volume_compose run --rm --no-deps --entrypoint bash \
    --volume "$LIFECYCLE_CODE_DIR/backup-lifecycle.sh:/tmp/backup-lifecycle.sh:ro" \
    postgres-backup /tmp/backup-lifecycle.sh "$@"
}

_backup_sidecar_runtime_state() {
  local cid state
  cid="$(_backup_volume_compose ps -q postgres-backup 2>/dev/null | head -1)" \
    || return 1
  if [ -z "$cid" ]; then
    printf 'absent|||'
    return 0
  fi
  printf '%s' "$cid" | grep -Eq '^[0-9a-f]{64}$' || return 1
  state="$(docker inspect --format \
    '{{.State.Paused}}|{{.State.Running}}|{{.State.Pid}}' "$cid" 2>/dev/null)" \
    || return 1
  printf '%s' "$state" \
    | grep -Eq '^(true|false)\|(true|false)\|[0-9]+$' || return 1
  printf '%s|%s' "$cid" "$state"
}

_staged_sidecar_marker_present() {
  [ -f "$STAGED_SIDECAR_MARKER_PATH" ] \
    && [ ! -L "$STAGED_SIDECAR_MARKER_PATH" ] \
    && [ "$(cat "$STAGED_SIDECAR_MARKER_PATH" 2>/dev/null || true)" = quiesced ]
}

_write_staged_sidecar_marker() {
  local tmp
  mkdir -p "$STATE_DIR" || return 1
  [ ! -L "$STAGED_SIDECAR_MARKER_PATH" ] || return 1
  tmp="$(mktemp "${STATE_DIR}/.backup-sidecar-quiesced.XXXXXX")" || return 1
  if ! printf 'quiesced\n' > "$tmp" \
     || ! chmod 600 "$tmp" 2>/dev/null \
     || ! mv -f "$tmp" "$STAGED_SIDECAR_MARKER_PATH"; then
    rm -f "$tmp"
    return 1
  fi
}

_quiesce_staged_backup_sidecar() {
  local state cid paused running init_pid processes marker_existed=0 paused_here=0
  _lifecycle_runtime_is_staged || return 0
  if [ -e "$STAGED_SIDECAR_MARKER_PATH" ] || [ -L "$STAGED_SIDECAR_MARKER_PATH" ]; then
    _staged_sidecar_marker_present \
      || { err "The backup sidecar handoff record is invalid."; return 1; }
    marker_existed=1
  fi
  state="$(_backup_sidecar_runtime_state)" \
    || { err "The installed backup sidecar state could not be verified."; return 1; }
  IFS='|' read -r cid paused running init_pid <<< "$state"
  if [ "$cid" = absent ] || [ "$running" != true ]; then
    [ "$marker_existed" -eq 0 ] || STAGED_BACKUP_SIDECAR_QUIESCED=1
    return 0
  fi
  if [ "$paused" = true ] && [ "$marker_existed" -eq 0 ]; then
    err "The installed backup sidecar is already paused outside this update."
    return 1
  fi

  if [ "$marker_existed" -eq 0 ]; then
    _write_staged_sidecar_marker \
      || { err "The backup sidecar handoff could not be recorded."; return 1; }
  fi
  if [ "$paused" = false ]; then
    if ! _backup_volume_compose pause postgres-backup >/dev/null; then
      [ "$marker_existed" -eq 1 ] || rm -f "$STAGED_SIDECAR_MARKER_PATH"
      err "The installed backup sidecar could not be paused safely."
      return 1
    fi
    paused_here=1
    state="$(_backup_sidecar_runtime_state)" || state=""
    IFS='|' read -r cid paused running init_pid <<< "$state"
  fi
  if [ "$paused" != true ] || [ "$running" != true ] \
     || ! printf '%s' "$init_pid" | grep -Eq '^[1-9][0-9]*$'; then
    [ "$paused_here" -eq 0 ] \
      || _backup_volume_compose unpause postgres-backup >/dev/null 2>&1
    [ "$marker_existed" -eq 1 ] || rm -f "$STAGED_SIDECAR_MARKER_PATH"
    err "The installed backup sidecar did not reach a verified paused state."
    return 1
  fi

  processes="$(docker top "$cid" -eo pid,args 2>/dev/null)" || {
    [ "$paused_here" -eq 0 ] \
      || _backup_volume_compose unpause postgres-backup >/dev/null 2>&1
    [ "$marker_existed" -eq 1 ] || rm -f "$STAGED_SIDECAR_MARKER_PATH"
    err "The installed backup sidecar process state could not be verified."
    return 1
  }
  if printf '%s\n' "$processes" \
      | awk -v init="$init_pid" 'NR > 1 && $1 != init { $1 = ""; print }' \
      | grep -Eq '/usr/local/bin/(backup|restore|prune)\.sh([[:space:]]|$)'; then
    if [ ! -f "$PENDING_FILE_PATH" ]; then
      _backup_volume_compose unpause postgres-backup >/dev/null 2>&1 || true
      rm -f "$STAGED_SIDECAR_MARKER_PATH"
    fi
    err "A backup, restore, or prune operation is already active."
    printf '        Let it finish, then re-run: jarvis-research update\n' >&2
    return 1
  fi
  STAGED_BACKUP_SIDECAR_QUIESCED=1
}

_release_staged_backup_sidecar() {
  local state cid paused running init_pid
  [ "$STAGED_BACKUP_SIDECAR_QUIESCED" -eq 1 ] || return 0
  [ ! -f "$PENDING_FILE_PATH" ] || return 0
  _staged_sidecar_marker_present || return 1
  state="$(_backup_sidecar_runtime_state)" || return 1
  IFS='|' read -r cid paused running init_pid <<< "$state"
  if [ "$cid" = absent ] || [ "$running" = false ]; then
    _backup_volume_compose up -d --no-deps postgres-backup >/dev/null || return 1
  elif [ "$paused" = true ] && [ "$running" = true ]; then
    _backup_volume_compose unpause postgres-backup >/dev/null || return 1
  elif [ "$paused" != false ]; then
    return 1
  fi
  rm -f "$STAGED_SIDECAR_MARKER_PATH"
  STAGED_BACKUP_SIDECAR_QUIESCED=0
}

_activate_selected_backup_sidecar() {
  local state cid paused running init_pid refreshed
  _staged_sidecar_marker_present || return 0
  state="$(_backup_sidecar_runtime_state)" \
    || { err "The paused backup sidecar state could not be verified."; return 1; }
  IFS='|' read -r cid paused running init_pid <<< "$state"
  if [ "$cid" != absent ]; then
    { [ "$paused" = true ] && [ "$running" = true ]; } || [ "$running" = false ] \
      || { err "The recorded backup sidecar is not safely paused."; return 1; }
    docker rm -f -- "$cid" >/dev/null \
      || { err "The paused backup sidecar could not be replaced."; return 1; }
  fi
  rm -f "$STAGED_SIDECAR_MARKER_PATH"
  STAGED_BACKUP_SIDECAR_QUIESCED=0
  _backup_volume_compose up -d --no-deps --force-recreate postgres-backup >/dev/null \
    || { err "The selected release's backup sidecar could not be started."; return 1; }
  refreshed="$(_backup_sidecar_runtime_state)" || return 1
  IFS='|' read -r cid paused running init_pid <<< "$refreshed"
  [ "$cid" != absent ] && [ "$paused" = false ] && [ "$running" = true ] \
    || { err "The selected release's backup sidecar is not running."; return 1; }
}

_release_update_volume_guard() {
  local attempt=0 action=clear
  [ "$UPDATE_VOLUME_GUARD_ACTIVE" -eq 1 ] || return 0
  [ ! -f "$PENDING_FILE_PATH" ] || action=retain
  _backup_volume_helper release-update "$UPDATE_VOLUME_GUARD_ID" "$action" >/dev/null || return 1
  while [ "$attempt" -lt 30 ]; do
    if ! _backup_volume_helper update-status "$UPDATE_VOLUME_GUARD_ID" >/dev/null 2>&1; then
      UPDATE_VOLUME_GUARD_ACTIVE=0
      return 0
    fi
    sleep 0.1
    attempt=$((attempt + 1))
  done
  return 1
}

_promote_update_volume_guard() {
  [ "$UPDATE_VOLUME_GUARD_ACTIVE" -eq 1 ] || return 1
  _backup_volume_helper promote-update "$UPDATE_VOLUME_GUARD_ID" >/dev/null
}

_begin_update_mutation() {
  _quiesce_staged_backup_sidecar || return 1
  _promote_update_volume_guard
}

_update_volume_guard_exit() {
  local rc=$?
  trap - EXIT
  set +e
  _release_update_volume_guard
  _release_staged_backup_sidecar
  exit "$rc"
}

_acquire_update_volume_guard() {
  local inherited="${JARVIS_UPDATE_VOLUME_GUARD_ID:-}" existing reservation_action
  local timeout attempts interval helper_container="" helper_state=""
  local inspect_output="" inspect_rc=0 wait_output="" wait_output_file="" wait_warning=0
  timeout="${JARVIS_UPDATE_GUARD_TIMEOUT:-21600}"
  attempts="${JARVIS_UPDATE_GUARD_READY_ATTEMPTS:-100}"
  interval="${JARVIS_UPDATE_GUARD_READY_INTERVAL:-0.1}"
  printf '%s' "$timeout" | grep -Eq '^[1-9][0-9]{0,5}$' \
    || die "JARVIS_UPDATE_GUARD_TIMEOUT must be a positive integer." \
      "No restore point was changed. Run: jarvis-research doctor"
  printf '%s' "$attempts" | grep -Eq '^[1-9][0-9]{0,5}$' \
    || die "JARVIS_UPDATE_GUARD_READY_ATTEMPTS must be a positive integer." \
      "No restore point was changed. Run: jarvis-research doctor"
  printf '%s' "$interval" \
    | grep -Eq '^(0\.[0-9]*[1-9][0-9]*|[1-9][0-9]*(\.[0-9]+)?)$' \
    || die "JARVIS_UPDATE_GUARD_READY_INTERVAL must be a positive number." \
      "No restore point was changed. Run: jarvis-research doctor"

  if [ -n "$inherited" ]; then
    if ! printf '%s' "$inherited" | grep -Eq '^[0-9a-f]{32}$'; then
      die "The inherited backup lifecycle guard is missing or invalid." \
        "No restore point was changed. Run: jarvis-research doctor"
    fi
    UPDATE_VOLUME_GUARD_ID="$inherited"
    if _backup_volume_helper update-status "$inherited" >/dev/null 2>&1; then
      UPDATE_VOLUME_GUARD_ACTIVE=1
      trap _update_volume_guard_exit EXIT
      return 0
    fi
    existing="$(_backup_volume_helper current-update-reservation 2>/dev/null || true)"
    if [ "$existing" != "$inherited" ]; then
      die "The inherited backup lifecycle guard is missing or invalid." \
        "No restore point was changed. Run: jarvis-research doctor"
    fi
  fi
  existing="$(_backup_volume_helper current-update 2>/dev/null || true)"
  if printf '%s' "$existing" | grep -Eq '^[0-9a-f]{32}$'; then
    UPDATE_VOLUME_GUARD_ID="$existing"
    UPDATE_VOLUME_GUARD_ACTIVE=1
    JARVIS_UPDATE_VOLUME_GUARD_ID="$existing"
    export JARVIS_UPDATE_VOLUME_GUARD_ID
    trap _update_volume_guard_exit EXIT
    return 0
  fi
  if [ -z "$UPDATE_VOLUME_GUARD_ID" ]; then
    existing="$(_backup_volume_helper current-update-reservation 2>/dev/null || true)"
    if printf '%s' "$existing" | grep -Eq '^[0-9a-f]{32}$'; then
      UPDATE_VOLUME_GUARD_ID="$existing"
    else
      UPDATE_VOLUME_GUARD_ID="$(openssl rand -hex 16 2>/dev/null || true)"
    fi
  fi
  printf '%s' "$UPDATE_VOLUME_GUARD_ID" | grep -Eq '^[0-9a-f]{32}$' \
    || die "Could not generate the backup lifecycle guard identity." \
      "No restore point was changed. Run: jarvis-research doctor"
  reservation_action="$(_backup_volume_helper reserve-update "$UPDATE_VOLUME_GUARD_ID")" \
    || die "Could not reserve the backup lifecycle guard identity." \
      "No restore point was changed. Run: jarvis-research doctor"
  case "$reservation_action" in
    adopt)
      info "Adopting the pending backup lifecycle guard."
      ;;
    launch)
      helper_container="$(_backup_volume_compose run --rm --no-deps -d --entrypoint bash \
        --volume "$LIFECYCLE_CODE_DIR/backup-lifecycle.sh:/tmp/backup-lifecycle.sh:ro" \
        postgres-backup /tmp/backup-lifecycle.sh hold-update \
        "$UPDATE_VOLUME_GUARD_ID" "$timeout" 8>&-)" \
        || die "Could not start the backup lifecycle guard." \
          "No restore point was changed. Run: jarvis-research doctor"
      ;;
    *)
      die "The backup lifecycle reservation returned an unknown state." \
        "No restore point was changed. Run: jarvis-research doctor"
      ;;
  esac

  info "Waiting for the backup lifecycle guard (up to ${timeout} seconds)."
  wait_output_file="$(mktemp)" \
    || die "Could not create backup lifecycle monitor state." \
      "No restore point was changed. Run: jarvis-research doctor"
  while true; do
    if _backup_volume_helper wait-update \
        "$UPDATE_VOLUME_GUARD_ID" "$attempts" "$interval" \
        >"$wait_output_file" 2>&1; then
      rm -f "$wait_output_file"
      UPDATE_VOLUME_GUARD_ACTIVE=1
      JARVIS_UPDATE_VOLUME_GUARD_ID="$UPDATE_VOLUME_GUARD_ID"
      export JARVIS_UPDATE_VOLUME_GUARD_ID
      trap _update_volume_guard_exit EXIT
      return 0
    fi
    wait_output="$(cat "$wait_output_file" 2>/dev/null || true)"
    : > "$wait_output_file"
    if printf '%s\n' "$wait_output" \
        | grep -qF 'ERROR: update reservation owner stopped before guard activation'; then
      rm -f "$wait_output_file"
      die "The backup lifecycle guard timed out before activation." \
        "No restore point was changed. Re-run: jarvis-research update"
    fi
    if printf '%s\n' "$wait_output" \
        | grep -qF 'ERROR: update reservation owner was not observed before guard activation'; then
      if [ -n "$helper_container" ] \
         && printf '%s' "$helper_container" | grep -Eq '^[0-9a-f]{12,64}$'; then
        inspect_rc=0
        inspect_output="$(docker inspect --format '{{.State.Status}}' \
          "$helper_container" 2>&1 8>&-)" || inspect_rc=$?
        if [ "$inspect_rc" -eq 0 ]; then
          helper_state="$inspect_output"
          case "$helper_state" in
            exited|dead)
              rm -f "$wait_output_file"
              die "The backup lifecycle guard helper stopped before activation." \
                "No restore point was changed. Re-run: jarvis-research update"
              ;;
            *) sleep 1; continue ;;
          esac
        fi
        if printf '%s\n' "$inspect_output" | grep -q 'No such'; then
          rm -f "$wait_output_file"
          die "The backup lifecycle guard helper stopped before activation." \
            "No restore point was changed. Re-run: jarvis-research update"
        fi
        sleep 1
        continue
      fi
      if [ "$reservation_action" = adopt ]; then
        rm -f "$wait_output_file"
        die "The pending backup lifecycle guard stopped before activation." \
          "No restore point was changed. Re-run: jarvis-research update"
      fi
    fi
    if [ "$wait_warning" -eq 0 ]; then
      warn "Waiting for Docker to resume backup lifecycle monitoring..."
      wait_warning=1
    fi
    sleep 1
  done
}

_update_backup_pin_path() { printf '%s' 'postgres-backup:/backups/.lifecycle/update-backup-pin.json'; }

_write_update_backup_pin() {
  [ "$UPDATE_VOLUME_GUARD_ACTIVE" -eq 1 ] || return 1
  _backup_volume_helper write-pin "$1" "$2" "${3:-false}" >/dev/null
}

_update_backup_pin_matches() {
  _backup_volume_helper pin-matches "$1" "$2" "${3:-false}" >/dev/null 2>&1
}

_clear_update_backup_pin() {
  [ -n "$TXN_BACKUP_ID" ] || return 0
  [ "$UPDATE_VOLUME_GUARD_ACTIVE" -eq 1 ] || return 1
  _backup_volume_helper clear-pin \
    "$TXN_BACKUP_ID" "$TXN_BACKUP_RUN_ID" "$TXN_LEGACY_RECOVERY" >/dev/null
}

_verify_recorded_update_backup_archives() {
  local shape verified
  [ -n "$TXN_BACKUP_ID" ] || return 0
  if [ "$TXN_LEGACY_RECOVERY" = "true" ]; then
    [ -z "$TXN_BACKUP_RUN_ID" ] || return 1
    shape=legacy
  else
    [ -n "$TXN_BACKUP_RUN_ID" ] || return 1
    shape=current
  fi
  verified="$(_backup_volume_helper verify \
    "$TXN_BACKUP_ID" "$TXN_BACKUP_RUN_ID" "$shape" 2>/dev/null)" || return 1
  [ "$verified" = "${TXN_BACKUP_ID}|${TXN_BACKUP_RUN_ID}" ]
}

_verify_recorded_update_backup() {
  [ -n "$TXN_BACKUP_ID" ] || return 0
  _verify_recorded_update_backup_archives || return 1
  if ! _update_backup_pin_matches \
      "$TXN_BACKUP_ID" "$TXN_BACKUP_RUN_ID" "$TXN_LEGACY_RECOVERY"; then
    err "The pending update's rollback backup has no matching durable retention pin."
    return 1
  fi
}

_lifecycle_runtime_is_staged() {
  local installed
  installed="$(cd -- "$REPO/scripts" 2>/dev/null && pwd -P)" || return 1
  [ "$LIFECYCLE_CODE_DIR" != "$installed" ]
}

_run_staged_backup_producer() {
  local request_id="$1" timeout_seconds="$2" producer pdf_dir repo_dir
  producer="$LIFECYCLE_CODE_DIR/backup.sh"
  [ -f "$producer" ] && [ ! -L "$producer" ] \
    || { err "The selected release's backup producer is unavailable or unsafe."; return 1; }
  repo_dir="$(cd -- "$REPO" 2>/dev/null && pwd -P)" || return 1
  pdf_dir="$(cd -- "$REPO/shared/pdf_storage" 2>/dev/null && pwd -P)" \
    || { err "The installation's PDF storage directory is unavailable."; return 1; }
  [ "$pdf_dir" = "$repo_dir/shared/pdf_storage" ] \
    && [ -d "$pdf_dir" ] && [ ! -L "$REPO/shared/pdf_storage" ] \
    || { err "The installation's PDF storage directory is unsafe."; return 1; }

  BACKUP_COMPOSE_TIMEOUT_SECONDS="$timeout_seconds" \
    _backup_volume_compose run --rm --no-deps --entrypoint bash \
    --env "BACKUP_RUN_ID=$request_id" \
    --volume "$producer:/tmp/jarvis-target-backup.sh:ro" \
    --volume "$pdf_dir:/pdf-storage:rw" \
    postgres-backup /tmp/jarvis-target-backup.sh
}

_require_staged_runtime_backup() {
  local request_id="$1" timeout="$2" interval="$3"
  local started now verified rc=0 remaining delay
  started="$(date +%s)"
  while true; do
    now="$(date +%s)"
    remaining=$((timeout - (now - started)))
    if [ "$remaining" -le 0 ]; then
      err "The selected release's backup producer exceeded the ${timeout}s limit."
      return 1
    fi
    rc=0
    # The producer narrates progress on stdout, but this function's stdout is
    # the verified payload its caller captures; route the narration to stderr.
    _run_staged_backup_producer "$request_id" "$remaining" >&2 || rc=$?
    if [ "$rc" -ne 0 ]; then
      if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        err "The selected release's backup producer exceeded the ${timeout}s limit."
      else
        err "The selected release's backup producer failed."
        printf '        Review: docker compose logs postgres-backup\n' >&2
      fi
      return 1
    fi
    if verified="$(_backup_volume_helper wait-verify "$request_id" 1 1)"; then
      printf '%s' "$verified"
      return 0
    else
      rc=$?
    fi
    if [ "$rc" -ne 75 ]; then
      err "The selected release's backup could not be authenticated."
      return 1
    fi
    now="$(date +%s)"
    remaining=$((timeout - (now - started)))
    if [ "$remaining" -le 0 ]; then
      err "No authenticated backup for this update appeared within ${timeout}s."
      printf '        Review: docker compose logs postgres-backup\n' >&2
      return 1
    fi
    delay="$interval"
    [ "$delay" -le "$remaining" ] || delay="$remaining"
    sleep "$delay"
  done
}

# _require_fresh_backup — generate a per-request ID and accept only the signed
# manifest that echoes that exact ID. An in-checkout command asks the running
# sidecar; a staged target runtime uses that target's producer directly.
# Wall-clock mtimes are deliberately irrelevant and cannot make an old backup pass.
_require_fresh_backup() {
  local _start_epoch="${1:-}" timeout interval request_id verified
  request_id="$(openssl rand -hex 16 2>/dev/null || true)"
  printf '%s' "$request_id" | grep -Eq '^[0-9a-f]{32}$' || { err "Could not create a secure backup request ID."; return 1; }
  timeout="${JARVIS_BACKUP_POLL_TIMEOUT:-300}"; interval="${JARVIS_BACKUP_POLL_INTERVAL:-3}"
  printf '%s' "$timeout" | grep -Eq '^[1-9][0-9]{0,5}$' \
    && printf '%s' "$interval" | grep -Eq '^[1-9][0-9]{0,3}$' \
    || { err "Backup wait settings must be positive integers."; return 1; }
  if _lifecycle_runtime_is_staged; then
    info "Creating a restore point with the selected release's backup format..."
    _quiesce_staged_backup_sidecar || return 1
    verified="$(_require_staged_runtime_backup "$request_id" "$timeout" "$interval")" \
      || return 1
  else
    if ! _backup_volume_helper publish-request "$request_id" >/dev/null; then
      err "Cannot publish the backup request."
      return 1
    fi
    info "Requested an on-demand backup; waiting for a fresh, verified restore point..."
    verified="$(_backup_volume_helper wait-verify "$request_id" "$timeout" "$interval")" \
      || return 1
  fi
  VERIFIED_BACKUP_TS="${verified%%|*}"
  VERIFIED_BACKUP_RUN_ID="${verified#*|}"
  # grep matches per line, so a multi-line value with one clean line would
  # pass; the whole-string match rejects anything but the bare timestamp.
  [[ "$VERIFIED_BACKUP_TS" =~ ^[0-9]{8}_[0-9]{6}$ ]] \
    && [ "$VERIFIED_BACKUP_RUN_ID" = "$request_id" ]
}

# -----------------------------------------------------------------------------
# Stage-first pull + the fast-forward advance.
# -----------------------------------------------------------------------------
# _stage_target_cohort TARGET_REF TARGET_VERSION — render the selected topology
# from the target ref, then pull every exact registry-backed image before the
# branch advances. Services marked pull_policy: build are intentionally omitted:
# their images exist only on this host and are not registry pull targets.
_stage_target_cohort() {
  local target_ref="$1" target_version="$2" tmp_root raw item target_path canon
  local seen="" config_json images_file ref rc=0 profile active_profiles line key value
  local -a requested=() compose_args=() profile_args=()
  tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/jarvis-target-compose.XXXXXX")" \
    || die "Could not create a temporary target-release workspace." \
      "No branch change was made. Check temporary-directory permissions, then retry."
  config_json="$tmp_root/compose.json"
  images_file="$tmp_root/images"

  if ! git show "${target_ref}:versions.env" > "$tmp_root/versions.env" 2>/dev/null; then
    rm -rf -- "$tmp_root"
    die "The target release has no readable versions.env." \
      "No branch change was made. Check the release tag, then retry."
  fi

  raw="$(sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null | head -1)"
  case "$raw" in
    \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
    \'*\') raw="${raw#\'}"; raw="${raw%\'}" ;;
  esac
  if [ -n "$raw" ]; then
    IFS=: read -r -a requested <<< "$raw"
  else
    requested=(docker-compose.yml)
    mkdir -p "$tmp_root/.probe"
    if git show "${target_ref}:docker-compose.override.yml" \
        > "$tmp_root/.probe/docker-compose.override.yml" 2>/dev/null \
       && [ -s "$tmp_root/.probe/docker-compose.override.yml" ]; then
      requested+=(docker-compose.override.yml)
    fi
  fi

  [ "${requested[0]:-}" = docker-compose.yml ] || {
    rm -rf -- "$tmp_root"
    die "This install's Compose file list does not start with docker-compose.yml." \
      "No branch change was made. Fix COMPOSE_FILE in .env, then retry."
  }
  for item in "${requested[@]}"; do
    if ! printf '%s' "$item" | grep -Eq '^[A-Za-z0-9._/-]+$'; then
      rm -rf -- "$tmp_root"
      die "COMPOSE_FILE contains a path that cannot be staged safely (${item:-empty})." \
        "No branch change was made. Use relative paths inside the managed checkout."
    fi
    case "$item" in /*|..|../*|*/..|*/../*)
      rm -rf -- "$tmp_root"
      die "COMPOSE_FILE points outside the managed checkout (${item})." \
        "No branch change was made. Use relative paths inside the managed checkout." ;;
    esac
    printf '%s\n' "$seen" | grep -qxF "$item" && {
      rm -rf -- "$tmp_root"
      die "COMPOSE_FILE lists ${item} more than once." \
        "No branch change was made. Remove the duplicate entry, then retry."
    }
    seen="${seen}${item}"$'\n'
    target_path="$tmp_root/$item"
    mkdir -p "$(dirname "$target_path")"
    if [ "$item" = docker-compose.override.yml ] \
       && [ -s "$tmp_root/.probe/docker-compose.override.yml" ]; then
      cp "$tmp_root/.probe/docker-compose.override.yml" "$target_path"
    elif ! git show "${target_ref}:${item}" > "$target_path" 2>/dev/null; then
      rm -rf -- "$tmp_root"
      die "The target release does not contain the configured Compose file ${item}." \
        "No branch change was made. Fix COMPOSE_FILE or choose a compatible release."
    fi
    canon="$(canonical_path_portable "$target_path" 2>/dev/null || true)"
    case "$canon/" in "$tmp_root"/*) : ;; *)
      rm -rf -- "$tmp_root"
      die "A target Compose file resolved outside its temporary workspace." \
        "No branch change was made. Inspect ${item}, then retry." ;;
    esac
    compose_args+=(-f "$target_path")
  done

  active_profiles="$(_active_profiles)"
  if grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' .env 2>/dev/null; then
    case " $active_profiles " in *' telegram '*) : ;; *) active_profiles="${active_profiles:+$active_profiles }telegram" ;; esac
  fi
  for profile in $active_profiles; do
    printf '%s' "$profile" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]*$' || {
      rm -rf -- "$tmp_root"
      die "COMPOSE_PROFILES contains an invalid profile name." \
        "No branch change was made. Fix COMPOSE_PROFILES in .env, then retry."
    }
    profile_args+=(--profile "$profile")
  done

  info "Staging images for ${target_ref} before advancing..."
  if ! (
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|'#'*) continue ;; esac
      key="${line%%=*}"; value="${line#*=}"
      printf '%s' "$key" | grep -Eq '^[A-Z][A-Z0-9_]*$' || exit 1
      export "$key=$value"
    done < "$tmp_root/versions.env"
    export JARVIS_IMAGE_TAG="$target_version"
    env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
      -u COMPOSE_PATH_SEPARATOR -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE \
      JARVIS_TARGET_COHORT_RENDER=1 docker compose \
      --project-directory "$tmp_root" --env-file "$REPO/.env" \
      ${profile_args[@]+"${profile_args[@]}"} \
      ${compose_args[@]+"${compose_args[@]}"} config --format json > "$config_json"
  ); then
    rm -rf -- "$tmp_root"
    die "The target release's active Compose topology could not be resolved." \
      "No branch change was made. Inspect COMPOSE_FILE and COMPOSE_PROFILES, then retry."
  fi

  if ! python3 - "$config_json" > "$images_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)

images = set()
services = config.get("services")
if not isinstance(services, dict):
    raise SystemExit(1)
for service in services.values():
    if not isinstance(service, dict):
        raise SystemExit(1)
    image = service.get("image")
    if not image or service.get("pull_policy") in {"build", "never"}:
        continue
    if (
        not isinstance(image, str)
        or not image
        or image.startswith("-")
        or len(image) > 512
        or any(ch.isspace() or ord(ch) < 32 for ch in image)
    ):
        raise SystemExit(1)
    images.add(image)
if not images:
    raise SystemExit(1)
for image in sorted(images):
    print(image)
PY
  then
    rm -rf -- "$tmp_root"
    die "The target release did not resolve to a safe registry image set." \
      "No branch change was made. Inspect the target Compose files, then retry."
  fi

  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    if ! docker pull "$ref"; then rc=1; break; fi
  done < "$images_file"
  rm -rf -- "$tmp_root"
  [ "$rc" -eq 0 ] || die "Staging images for ${target_ref} failed; nothing was changed." \
    "Check registry access and free disk space, then re-run: jarvis-research update"
}

_resume_pending_merge() {
  local active_profiles head
  info "Resuming the pending update to ${TXN_TARGET} before the branch advance."
  active_profiles="$(_active_profiles)"
  # shellcheck disable=SC2086  # active_profiles is an intentional word list
  if ! verify_release_manifests "$TXN_TARGET" $active_profiles >/dev/null 2>&1; then
    die "Published images for the pending target ${TXN_TARGET} are not all available." \
      "No branch change was made. Check the registry, then re-run: jarvis-research update"
  fi
  if [ -n "$TXN_BACKUP_ID" ]; then
    if ! _verify_recorded_update_backup_archives; then
      die "The pending update's recovery backup is no longer authenticated and complete." \
        "No branch change was made. Repair or replace that restore point, then start a new update."
    fi
    if ! _update_backup_pin_matches "$TXN_BACKUP_ID" "$TXN_BACKUP_RUN_ID" false; then
      head="$(git rev-parse HEAD 2>/dev/null || true)"
      if [ "$head" != "$TXN_FROM_SHA" ] \
         || ! _write_update_backup_pin "$TXN_BACKUP_ID" "$TXN_BACKUP_RUN_ID" false; then
        die "The pending update's recovery backup has no valid retention pin." \
          "No branch change was made. Repair the pin or start a new update."
      fi
    fi
  fi
  _stage_target_cohort "$TXN_TARGET" "$TXN_TARGET_VERSION"
  info "Advancing the checkout to ${TXN_TARGET} (fast-forward only)..."
  if ! git merge --ff-only "$TXN_TARGET"; then
    die "Fast-forward to ${TXN_TARGET} failed; the checkout was not advanced." \
      "Reconcile by hand, then run: jarvis-research doctor"
  fi
  head="$(git rev-parse HEAD 2>/dev/null || true)"
  [ "$head" = "$TXN_TARGET_SHA" ] || die "Git returned success but HEAD is not the recorded update target." \
    "Stop here and run: jarvis-research doctor"
  _txn_phase_or_die pull
  exec bash "${REPO}/scripts/jarvis-research.sh" --repo "$REPO" update --resume "$TXN_TARGET" --yes
}

# -----------------------------------------------------------------------------
# Update — the transactional entry point.
# -----------------------------------------------------------------------------
cmd_update() {
  local to_ref="" resume_ref="" head
  while [ $# -gt 0 ]; do
    case "$1" in
      --to)       to_ref="${2:-}"; shift 2 ;;
      --to=*)     to_ref="${1#--to=}"; shift ;;
      --resume)   resume_ref="${2:-}"; shift 2 ;;
      --resume=*) resume_ref="${1#--resume=}"; shift ;;
      --yes|-y)   shift ;;   # accepted for symmetry; the flow is non-interactive
      *)          usage_error "update: unknown option '$1'" \
                    "Run: jarvis-research update [--to <tag>] [--resume <tag>] [--yes]" ;;
    esac
  done

  _init_pending_file_path \
    || die "Could not derive this install's update journal path." \
      "No services were changed. Run: jarvis-research doctor"
  _acquire_update_lock
  _require_managed_install
  _require_docker_daemon
  _require_clean_main_checkout
  _migrate_legacy_pending_for_repo \
    || die "Cannot safely assign the old shared update journal to this install." \
      "No services were changed. Keep ${LEGACY_PENDING_FILE_PATH} and run: jarvis-research doctor"
  _acquire_update_volume_guard

  # Explicit resume is valid only for a schema-checked durable transaction whose
  # recorded target is the current HEAD. It can never invent recovery state.
  if [ -n "$resume_ref" ]; then
    [ -f "$PENDING_FILE_PATH" ] || die "--resume requires a pending update transaction, but none exists." \
      "Run: jarvis-research update   (or jarvis-research doctor)"
    _txn_load_or_die
    if [ "$TXN_TARGET" != "$resume_ref" ]; then
      die "--resume ${resume_ref} does not match the pending update target (${TXN_TARGET})." \
        "Re-run: jarvis-research update --resume ${TXN_TARGET}"
    fi
    _begin_update_mutation \
      || die "Exclusive lifecycle ownership could not be established for this update." \
        "No update changes were applied. Let the restore finish, then re-run: jarvis-research update"
    case "$TXN_PHASE" in
      committed)
        _clear_update_backup_pin || die "The update completed, but its retention pin could not be cleared safely." \
          "Inspect $(_update_backup_pin_path 2>/dev/null || printf '<backups>/.lifecycle/update-backup-pin.json'), then run: jarvis-research update"
        rm -f "$PENDING_FILE_PATH"
        ok "Update to ${TXN_TARGET} was already completed and health-verified."
        return 0 ;;
      merge_pending)
        head="$(git rev-parse HEAD 2>/dev/null || true)"
        if [ "$head" = "$TXN_TARGET_SHA" ]; then
          _txn_phase_or_die pull
        else
          die "The pending update has not advanced to ${TXN_TARGET} yet; explicit post-merge resume is unsafe." \
            "Run without --resume: jarvis-research update"
        fi ;;
      pull|health) : ;;
    esac
    _resume_transaction "$TXN_TARGET"
    return
  fi

  # A pending transaction takes precedence over tag discovery and the ordinary
  # up-to-date shortcut. It either resumes from its exact source/target pair or
  # fails closed; no newly published tag can silently replace it.
  if [ -f "$PENDING_FILE_PATH" ]; then
    _txn_load_or_die
    _begin_update_mutation \
      || die "The retained update cannot reacquire exclusive lifecycle ownership." \
        "No new update changes were applied. Finish the active recovery, then re-run: jarvis-research update"
    case "$TXN_PHASE" in
      committed)
        _clear_update_backup_pin || die "The update completed, but its retention pin could not be cleared safely." \
          "Inspect $(_update_backup_pin_path 2>/dev/null || printf '<backups>/.lifecycle/update-backup-pin.json'), then run: jarvis-research update"
        rm -f "$PENDING_FILE_PATH"
        ok "Update to ${TXN_TARGET} was already completed and health-verified."
        return 0 ;;
      merge_pending)
        head="$(git rev-parse HEAD 2>/dev/null || true)"
        if [ "$head" = "$TXN_TARGET_SHA" ]; then
          info "The checkout reached ${TXN_TARGET}; resuming its pending service update."
          _txn_phase_or_die pull
          _resume_transaction "$TXN_TARGET"
          return
        fi
        _resume_pending_merge ;;
      pull|health)
        info "Resuming an interrupted update at phase '${TXN_PHASE}'."
        _resume_transaction "$TXN_TARGET"
        return ;;
    esac
  fi

  UPDATE_START_EPOCH="$(date +%s)"
  MIGRATIONS_RAN=0

  info "Fetching tags from origin..."                          # (3)
  git fetch --tags origin >/dev/null 2>&1 \
    || env_die "Could not fetch from origin." "Check network access, then re-run: jarvis-research update"

  local target_ref target_version target_sha
  if [ -n "$to_ref" ]; then
    target_ref="$to_ref"
  else
    target_ref="$(latest_stable_tag origin)"
    [ -n "$target_ref" ] || die "No stable release tag found on origin." "Run: jarvis-research doctor"
  fi
  target_version="${target_ref#v}"
  target_sha="$(git rev-parse "${target_ref}^{commit}" 2>/dev/null || true)"
  if ! printf '%s' "$target_ref" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/-]*$' \
     || ! printf '%s' "$target_version" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._+/-]*$' \
     || ! printf '%s' "$target_sha" | grep -Eq '^[0-9a-f]{40}$'; then
    die "The selected release reference does not resolve to a safe, immutable commit identity." \
      "Run: jarvis-research doctor"
  fi

  if ! git merge-base --is-ancestor HEAD "$target_ref" 2>/dev/null; then
    die "Your checkout has diverged from ${target_ref}; a fast-forward update is not possible." \
        "Reinstall this release into a fresh checkout, then run: jarvis-research doctor"
  fi
  # Release tags are annotated, so the tag name resolves to the tag object, not
  # the commit it points at; peel it or this never matches.
  if [ "$(git rev-parse HEAD)" = "$(git rev-parse "${target_ref}^{commit}")" ]; then
    ok "Already up to date (${target_ref})."
    return 0
  fi

  local active_profiles; active_profiles="$(_active_profiles)"
  info "Verifying that every published image for ${target_ref} exists..."   # (4)
  # shellcheck disable=SC2086  # active_profiles is an intentional word list
  if ! verify_release_manifests "$target_ref" $active_profiles >/dev/null 2>&1; then
    die "Some images for ${target_ref} are not published yet — a visible tag is not release-readiness." \
        "Wait for the release to finish publishing, then re-run: jarvis-research update"
  fi

  local backup_id="" backup_run_id=""                           # (5)
  local migrations; migrations="$(_new_migrations "$target_ref")"
  if [ -n "$migrations" ]; then
    if _migrations_need_backup "$target_ref" "$migrations"; then
      warn "${target_ref} includes a data-changing migration; a fresh verified backup is required."
      if _require_fresh_backup "$UPDATE_START_EPOCH"; then
        backup_id="${VERIFIED_BACKUP_TS:-}"
        backup_run_id="${VERIFIED_BACKUP_RUN_ID:-}"
        MIGRATIONS_RAN=1
        ok "Verified backup ${backup_id} present; continuing."
      else
        die "No fresh, verified backup exists — refusing to apply a data-changing migration." \
            "Take a backup (WebUI Backup panel or the backup sidecar), then re-run: jarvis-research update"
      fi
    else
      info "New additive migration(s) will apply on restart:"
      printf '%s\n' "$migrations" | sed 's/^/    /'
      info "Open the WebUI Backup panel first if you want a restore point."
    fi
  fi

  _begin_update_mutation \
    || die "Exclusive lifecycle ownership could not be established for this update." \
      "No update changes were applied. Let the restore finish, then re-run: jarvis-research update"
  if ! _txn_write_new merge_pending "$target_ref" "$target_version" "$target_sha" \
      "$backup_id" "$backup_run_id"; then                                  # (6)
    die "Could not write a valid pending-update transaction; refusing to advance the checkout." \
      "No branch change was made. Check ${STATE_DIR}, then run: jarvis-research doctor"
  fi
  if [ -n "$backup_id" ] \
     && ! _write_update_backup_pin "$backup_id" "$backup_run_id" false; then
    die "Could not persist the recovery backup's retention pin; refusing to advance the checkout." \
      "No branch change was made. Check the backup-trigger volume, then run: jarvis-research update"
  fi
  _stage_target_cohort "$target_ref" "$target_version"                      # (7)

  info "Advancing the checkout to ${target_ref} (fast-forward only)..."     # (8)
  if ! git merge --ff-only "$target_ref"; then
    err "Fast-forward to ${target_ref} failed; the checkout was not advanced."
    printf '        %s%s%s\n' "$C_YELLOW" "Reconcile by hand, then run: jarvis-research doctor" "$C_RESET" >&2
    exit 1
  fi
  head="$(git rev-parse HEAD 2>/dev/null || true)"
  [ "$head" = "$target_sha" ] || die "Git returned success but HEAD is not the recorded update target." \
    "Stop here and run: jarvis-research doctor"
  _txn_phase_or_die pull
  ensure_state_dir "$REPO" || warn "Could not record the durable state directory (non-fatal)."
  local -a resume_cmd=(bash "${REPO}/scripts/jarvis-research.sh" --repo "$REPO" update --resume "$target_ref" --yes)
  exec "${resume_cmd[@]}"
}

# _resume_transaction TARGET_REF — the post-merge half (phases 9-12). Never
# fetches, guards-mutates, merges, or re-execs.
_resume_transaction() {
  # First, and above all before the sidecar is recreated below: compose reads
  # JARVIS_STATE_DIR from .env to bind-mount the durable state directory, and every
  # resume path reaches here without passing the fresh-update call site.
  ensure_state_dir "$REPO" || warn "Could not record the durable state directory (non-fatal)."
  local target_ref="$1"
  MIGRATIONS_RAN="${MIGRATIONS_RAN:-0}"
  if [ -f "$PENDING_FILE_PATH" ] && [ -n "$(_txn_field backup_id)" ]; then
    MIGRATIONS_RAN=1
  fi

  install_cli_shim "$REPO" >/dev/null 2>&1 || true             # (9)
  _txn_phase_or_die pull

  info "Applying ${target_ref} — pulling images and recreating services..."  # (10)
  if ! _verify_recorded_update_backup; then
    die "The pending update's recovery backup is no longer authenticated, complete, and pinned." \
      "No services were recreated. Repair the recorded restore point, then re-run: jarvis-research update --resume ${target_ref}"
  fi
  # The journal write is best-effort HERE and only here: this path is already
  # failing, and the epilogue that follows is what tells the operator how to roll
  # back. An unguarded call would abort the script under `set -e` the moment the
  # journal became unwritable, taking the epilogue with it.
  if ! _activate_selected_backup_sidecar; then
    _txn_update_phase health || warn "The update's journal could not be marked as failed; ${PENDING_FILE_PATH} may name an earlier phase."
    _failure_epilogue "$target_ref"
    exit 1
  fi
  if ! _run_update_sh; then
    _txn_update_phase health || warn "The update's journal could not be marked as failed; ${PENDING_FILE_PATH} may name an earlier phase."
    _failure_epilogue "$target_ref"
    exit 1
  fi

  _txn_update_phase committed \
    || die "Could not record the update's completion in its journal." \
      "Check ${PENDING_FILE_PATH}, then run: jarvis-research doctor"   # (11)
  if ! _clear_update_backup_pin; then
    die "The update completed, but its recovery-backup retention pin could not be cleared safely." \
      "The transaction remains recorded as committed. Inspect the backup-trigger volume, then re-run: jarvis-research update"
  fi
  rm -f "$PENDING_FILE_PATH"
  ok "Update to ${target_ref} complete and health-verified."
  _success_epilogue "$target_ref"                              # (12)
}

# _run_update_sh — hand off to update.sh (warm pulls become no-ops) and let it
# own the recreate + health wait; its exit code is the health verdict.
_run_update_sh() {
  (
    cd "$REPO"
    JARVIS_TRANSACTIONAL_UPDATE=1 \
      bash "${REPO}/update.sh" --yes --image-tag "$TXN_TARGET_VERSION"
  )
}

# _rollback_pin_lines FROM_VERSION — bounded application-image recovery commands
# for the exact recorded version and the install's active Compose profiles.
_rollback_pin_lines() {
  local fv="$1" active_profiles profile
  local -a svcs=("${PUBLISHED_SERVICES_BASE[@]}") profile_args=()
  [ "$fv" != unknown ] \
    && printf '%s' "$fv" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._+-]*$' \
    || return 1

  active_profiles="$(_active_profiles)"
  if grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' .env 2>/dev/null; then
    case " $active_profiles " in
      *' telegram '*) : ;;
      *) active_profiles="${active_profiles:+$active_profiles }telegram" ;;
    esac
  fi
  for profile in $active_profiles; do
    printf '%s' "$profile" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.-]*$' || return 1
    profile_args+=(--profile "$profile")
    if [ "$profile" = telegram ] \
       && ! _env_key_in_list "$PUBLISHED_SERVICE_TELEGRAM" "${svcs[*]}"; then
      svcs+=("$PUBLISHED_SERVICE_TELEGRAM")
    fi
  done

  printf '    cd %q\n' "$REPO"
  printf '    JARVIS_IMAGE_TAG=%q docker compose' "$fv"
  if [ "${#profile_args[@]}" -gt 0 ]; then
    printf ' %q' "${profile_args[@]}"
  fi
  printf ' pull'
  printf ' %q' "${svcs[@]}"
  printf '\n'
  printf '    JARVIS_IMAGE_TAG=%q docker compose' "$fv"
  if [ "${#profile_args[@]}" -gt 0 ]; then
    printf ' %q' "${profile_args[@]}"
  fi
  printf ' up -d --no-build'
  printf ' %q' "${svcs[@]}"
  printf '\n'
}
# _schema_not_safe_notice — a backup means a data-changing migration was in the
# update cohort, not proof that it completed. Put data recovery before images.
_schema_not_safe_notice() {
  printf '\n%sA data-changing migration may have run.%s Restore data before recovering images:\n' "$C_YELLOW" "$C_RESET"
  printf '  1. If the dashboard is reachable, open Admin > Backups and restore the pre-update backup.\n'
  printf '  2. Wait for that data restore to finish, then use the application-image recovery below.\n'
}

_failure_epilogue() {
  local target_ref="$1" fv="${TXN_FROM_VERSION:-}"
  printf '\n'
  err "Update to ${target_ref} did not finish; the transaction remains pending."
  printf 'Repository: %s\n' "$REPO"
  [ "${MIGRATIONS_RAN:-0}" -eq 1 ] && _schema_not_safe_notice
  printf '\n%sApplication-image recovery (not a full release rollback):%s\n' "$C_BOLD" "$C_RESET"
  printf '  These commands replace application containers only; they do not move the Git checkout or restore stored data.\n'
  if ! _rollback_pin_lines "$fv"; then
    printf '  The recorded previous application image tag or active profile list is unsafe; no image command was printed.\n'
  fi
  printf '\nDiagnose first: cd %q && jarvis-research doctor\n' "$REPO"
  printf 'Resume pending update: cd %q && jarvis-research update --resume %q\n' "$REPO" "$target_ref"
}

_success_epilogue() {
  local target_ref="$1" app_version image_tag
  app_version="$(sed -n 's/^JARVIS_VERSION=//p' .env 2>/dev/null | head -1)"
  image_tag="$(sed -n 's/^JARVIS_IMAGE_TAG=//p' .env 2>/dev/null | head -1)"
  image_tag="${image_tag:-$app_version}"
  printf '\n'
  cmd_doctor || true
  printf '\nNow running %s (version=%s, image tag=%s).\n' \
    "$target_ref" "${app_version:-unknown}" "${image_tag:-unknown}"
}

# -----------------------------------------------------------------------------
# Instance-owner inspection and on-host recovery.
# -----------------------------------------------------------------------------
_owner_require_services() {
  local service container_id
  for service in paper_ingestion postgres; do
    container_id="$(docker compose ps -q "$service" 2>/dev/null | head -1 || true)"
    [ -n "$container_id" ] \
      || die "The ${service} service is not running; owner state cannot be verified." \
        "Start JARVIS, then run: jarvis-research owner status"
  done
}

_owner_effective_environment() {
  docker compose exec -T paper_ingestion \
    sh -c 'printf "%s" "${OWNER_USER_ID-}"'
}

_owner_status_sql() {
  cat <<'SQL'
-- jarvis-owner-status
WITH raw_owner AS (
    SELECT
        CASE
            WHEN NULLIF(btrim(:'owner_env'), '') IS NOT NULL THEN 'environment'
            WHEN EXISTS (
                SELECT 1 FROM user_config
                WHERE user_id IS NULL AND key = 'owner.user_id'
            ) THEN 'database'
            ELSE 'none'
        END AS source,
        CASE
            WHEN NULLIF(btrim(:'owner_env'), '') IS NOT NULL THEN btrim(:'owner_env')
            ELSE (
                SELECT value #>> '{}'
                FROM user_config
                WHERE user_id IS NULL AND key = 'owner.user_id'
                LIMIT 1
            )
        END AS raw_value
), parsed_owner AS (
    SELECT
        source,
        CASE
            WHEN raw_value ~ '^[1-9][0-9]{0,17}$' THEN raw_value::bigint
            ELSE NULL
        END AS user_id
    FROM raw_owner
), resolved_owner AS (
    SELECT parsed_owner.source, parsed_owner.user_id,
           users.email, users.role, users.deleted_at
    FROM parsed_owner
    LEFT JOIN users ON users.id = parsed_owner.user_id
)
SELECT
    source,
    CASE
        WHEN source = 'none' THEN 'missing'
        WHEN user_id IS NULL THEN 'invalid_value'
        WHEN email IS NULL OR deleted_at IS NOT NULL THEN 'missing_or_deleted_user'
        WHEN role <> 'admin' THEN 'non_admin_user'
        ELSE 'valid'
    END,
    COALESCE(user_id::text, ''),
    COALESCE(email, '')
FROM resolved_owner;
SQL
}

_owner_set_sql() {
  cat <<'SQL'
-- jarvis-owner-set
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('admin_role_mutation'));
SELECT
    set_config('jarvis.owner_target_email', :'target_email', true) AS target_setting,
    set_config('jarvis.owner_env', :'owner_env', true) AS environment_setting
\gset
DO $owner$
DECLARE
    target_email text := current_setting('jarvis.owner_target_email');
    effective_environment text := NULLIF(
        btrim(current_setting('jarvis.owner_env', true)), ''
    );
    target_count bigint;
    target_user_id bigint;
    owner_row_exists boolean;
    current_raw text;
    current_user_id bigint;
    current_owner_is_valid boolean := false;
    previous_state text;
BEGIN
    IF effective_environment IS NOT NULL THEN
        RAISE EXCEPTION 'environment_owner_is_authoritative';
    END IF;

    SELECT count(*), min(id)
    INTO target_count, target_user_id
    FROM users
    WHERE lower(email) = lower(target_email)
      AND role = 'admin'
      AND deleted_at IS NULL;

    IF target_count <> 1 THEN
        RAISE EXCEPTION 'target_must_be_one_live_admin';
    END IF;

    SELECT
        EXISTS (
            SELECT 1 FROM user_config
            WHERE user_id IS NULL AND key = 'owner.user_id'
        ),
        (
            SELECT value #>> '{}'
            FROM user_config
            WHERE user_id IS NULL AND key = 'owner.user_id'
            LIMIT 1
        )
    INTO owner_row_exists, current_raw;

    IF current_raw ~ '^[1-9][0-9]{0,17}$' THEN
        current_user_id := current_raw::bigint;
        SELECT EXISTS (
            SELECT 1 FROM users
            WHERE id = current_user_id
              AND role = 'admin'
              AND deleted_at IS NULL
        ) INTO current_owner_is_valid;
    END IF;

    IF current_owner_is_valid THEN
        RAISE EXCEPTION 'current_owner_is_valid_use_admin_users_transfer';
    END IF;

    previous_state := CASE
        WHEN NOT owner_row_exists THEN 'missing'
        WHEN current_user_id IS NULL THEN 'invalid_value'
        ELSE 'missing_deleted_or_non_admin_user'
    END;

    IF owner_row_exists THEN
        UPDATE user_config
        SET value = to_jsonb(target_user_id), updated_at = NOW()
        WHERE user_id IS NULL AND key = 'owner.user_id';
    ELSE
        INSERT INTO user_config (user_id, key, value)
        VALUES (NULL, 'owner.user_id', to_jsonb(target_user_id));
    END IF;

    INSERT INTO audit_log (user_id, action, resource, metadata)
    VALUES (
        NULL,
        'admin.owner.repair',
        'owner.user_id',
        jsonb_build_object(
            'source', 'host_cli',
            'previous_state', previous_state,
            'new_owner_user_id', target_user_id
        )
    );
END
$owner$;
COMMIT;
SQL
}

_owner_read_environment_or_die() {
  local environment_value
  if ! environment_value="$(_owner_effective_environment)"; then
    die "The running service environment could not be inspected safely." \
      "No owner state was changed. Run: jarvis-research doctor"
  fi
  printf '%s' "$environment_value"
}

cmd_owner_status() {
  local environment_value row source state user_id email
  _require_docker_daemon
  _owner_require_services
  environment_value="$(_owner_read_environment_or_die)"
  if ! row="$(_owner_status_sql | docker compose exec -T postgres sh -c \
      'exec psql --no-psqlrc -v ON_ERROR_STOP=1 -At -F "|" --username="${POSTGRES_USER:-jarvis}" --dbname="${POSTGRES_DB:-jarvis}" -v owner_env="$1"' \
      sh "$environment_value")"; then
    die "The instance-owner record could not be read safely." \
      "No owner state was changed. Run: jarvis-research doctor"
  fi

  IFS='|' read -r source state user_id email <<< "$row"
  case "$source:$state" in
    environment:valid|database:valid|none:missing|database:missing|\
    environment:invalid_value|environment:missing_or_deleted_user|\
    environment:non_admin_user|database:invalid_value|\
    database:missing_or_deleted_user|database:non_admin_user) ;;
    *) die "The instance-owner query returned an unexpected result." \
         "No owner state was changed. Run: jarvis-research doctor" ;;
  esac

  printf 'Instance owner\n'
  printf '  Source: %s\n' "$source"
  printf '  State: %s\n' "$state"
  if [ "$state" = valid ]; then
    printf '  User: %s (id %s)\n' "$email" "$user_id"
  elif [ "$source" = environment ]; then
    printf '  Next: correct OWNER_USER_ID on the host, then restart JARVIS.\n'
  else
    printf '  Next: run jarvis-research owner set <admin-email> on this host.\n'
  fi
}

cmd_owner_set() {
  [ "$#" -eq 1 ] \
    || usage_error "owner set takes exactly one email address." \
      "Run: jarvis-research owner set <email>"
  local email="$1" environment_value confirmation
  if [ "${#email}" -gt 320 ] \
     || ! printf '%s' "$email" | grep -Eq '^[^[:space:]@]+@[^[:space:]@]+$'; then
    usage_error "owner set requires one ordinary email address." \
      "Run: jarvis-research owner set <email>"
  fi

  _require_docker_daemon
  _owner_require_services
  environment_value="$(_owner_read_environment_or_die)"
  if [ -n "${environment_value//[[:space:]]/}" ]; then
    die "Ownership is managed by OWNER_USER_ID in the running service." \
      "Change OWNER_USER_ID on the host and restart JARVIS; the database was not changed."
  fi

  printf 'Type %s to confirm the new instance owner: ' "$email"
  if ! IFS= read -r confirmation; then
    die "No confirmation was received; owner repair was cancelled." \
      "No owner state was changed. Run: jarvis-research owner status"
  fi
  [ "$confirmation" = "$email" ] \
    || die "Confirmation did not match; owner repair was cancelled." \
      "No owner state was changed. Run: jarvis-research owner status"

  _acquire_control_lifecycle
  if ! _owner_set_sql | docker compose exec -T postgres sh -c \
      'exec psql --no-psqlrc --quiet -v ON_ERROR_STOP=1 --username="${POSTGRES_USER:-jarvis}" --dbname="${POSTGRES_DB:-jarvis}" -v target_email="$1" -v owner_env="$2"' \
      sh "$email" "$environment_value"; then
    die "Owner repair was refused or could not be committed." \
      "No partial change was kept. Run: jarvis-research owner status"
  fi
  _finish_control_lifecycle
  ok "Instance owner repaired for ${email}."
}

cmd_owner() {
  local owner_command="${1:-}"
  if [ "$#" -gt 0 ]; then
    shift
  fi
  case "$owner_command" in
    status)
      [ "$#" -eq 0 ] || usage_error "owner status takes no arguments." \
        "Run: jarvis-research owner status"
      cmd_owner_status ;;
    set) cmd_owner_set "$@" ;;
    *) usage_error "owner: unknown subcommand '${owner_command}'." \
         "Run: jarvis-research owner status   (or: jarvis-research owner set <email>)" ;;
  esac
}

# -----------------------------------------------------------------------------
# Off-host restore acknowledgement on this installation.
# -----------------------------------------------------------------------------
cmd_restore_acknowledge() {
  [ "$#" -eq 1 ] \
    || usage_error "restore acknowledge takes exactly one restore ID." \
      "Run: jarvis-research restore acknowledge <restore-id>"
  local restore_id="$1" confirmation
  printf '%s' "$restore_id" | grep -Eq '^[0-9a-f]{32}$' \
    || usage_error "restore acknowledge requires one lowercase 32-hex restore ID." \
      "Run: jarvis-research restore acknowledge <restore-id>"

  _require_docker_daemon
  if ! _backup_volume_helper inspect-quarantine "$restore_id" >/dev/null 2>&1; then
    die "Restore review state is unavailable or does not match ${restore_id}." \
      "Quarantine remains active. Check the exact restore ID, then retry."
  fi

  printf 'Type %s to confirm credential review and release outbound access: ' "$restore_id"
  if ! IFS= read -r confirmation; then
    die "No confirmation was received; restore acknowledgement was cancelled." \
      "Quarantine remains active. Re-run with the exact restore ID when review is complete."
  fi
  [ "$confirmation" = "$restore_id" ] \
    || die "Confirmation did not match; restore acknowledgement was cancelled." \
      "Quarantine remains active. Re-run with the exact restore ID when review is complete."

  _acquire_control_lifecycle
  if ! _backup_volume_helper acknowledge-quarantine "$restore_id" >/dev/null 2>&1; then
    die "Restore review state changed before acknowledgement; quarantine remains active." \
      "Re-check the current restore ID, then retry: jarvis-research restore acknowledge ${restore_id}"
  fi
  _finish_control_lifecycle
  ok "Acknowledged off-host restore ${restore_id}; outbound access is released."
}

# -----------------------------------------------------------------------------
# Recovery: same-host break-glass restore, restore progress, and the off-host
# request an operator submits by hand.
#
# The two commands that TOUCH the backup service (legacy, status) reach it
# through _backup_volume_compose, whose ownership check is what stops a
# caller-supplied .env from pointing them at a sibling project. `request` makes
# no compose call at all — it only prints a procedure — so it cannot rely on
# that fence and instead prints commands already scoped to this install.
# -----------------------------------------------------------------------------
# Paths inside the backup service's trigger volume (see docker-compose.yml).
RESTORE_REQUEST_PATH="/backup-trigger/.restore_request.json"
RESTORE_STATUS_PATH="/backup-trigger/.restore_status.json"

_restore_timestamp_or_usage() {
  local subcommand="$1" timestamp="$2"
  printf '%s' "$timestamp" | grep -Eq '^[0-9]{8}_[0-9]{6}$' \
    || usage_error "restore ${subcommand} requires one backup timestamp in YYYYMMDD_HHMMSS form." \
      "Run: jarvis-research restore ${subcommand} <timestamp>"
}

_restore_new_request_id() {
  local id
  id="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')" \
    && printf '%s' "$id" | grep -Eq '^[0-9a-f]{32}$' \
    || return 1
  printf '%s' "$id"
}

_restore_request_json() {
  # $5 (optional): when "1", record the operator's unknown-schema acknowledgement so a
  # restore point that carries no usable schema version is accepted. restore.sh reads it
  # back from the request; the field is omitted otherwise, leaving a normal request byte
  # for byte unchanged.
  local ack=""
  [ "${5:-}" = "1" ] && ack=',"allow_unknown_schema":true'
  printf '{"source":"%s","timestamp":"%s","restore_id":"%s","requested_at":"%s"%s}' \
    "$1" "$2" "$3" "$4" "$ack"
}

# _restore_status_field JSON FIELD — the value of a flat top-level string field,
# empty when the field is absent or JSON null.
_restore_status_field() {
  printf '%s' "$1" | grep -oE "\"${2}\":\"[^\"]*\"" 2>/dev/null | head -1 \
    | sed -E "s/\"${2}\":\"([^\"]*)\"/\1/" || true
}

# _restore_status_flag JSON FIELD — whether a flat top-level boolean field is true.
_restore_status_flag() {
  printf '%s' "$1" | grep -qE "\"${2}\":true"
}

_restore_legacy_resume_sidecar() {
  local rc=$?
  trap - EXIT
  set +e
  # A run that died before restore.sh consumed the request leaves it in the
  # trigger volume, and the service resumed below would pick it up and fail it
  # non-interactively — an outcome the operator never asked for and would then
  # find in `restore status`. restore.sh unlinks the request itself, so after
  # any later failure this is already a no-op.
  if [ "$rc" -ne 0 ]; then
    _backup_volume_compose run --rm --no-deps -T --entrypoint sh postgres-backup \
      -c "rm -f ${RESTORE_REQUEST_PATH}" >/dev/null 2>&1 \
      || warn "An unconsumed restore request may remain. Check: jarvis-research restore status"
  fi
  _backup_volume_compose start postgres-backup >/dev/null 2>&1 \
    || warn "The backup service did not restart. Run: jarvis-research start"
  exit "$rc"
}

cmd_restore_legacy() {
  local allow_unknown_schema=0 timestamp=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --allow-unknown-schema) allow_unknown_schema=1 ;;
      -*) usage_error "restore legacy: unknown option '$1'." \
            "Run: jarvis-research restore legacy <timestamp> [--allow-unknown-schema]" ;;
      *)
        [ -z "$timestamp" ] \
          || usage_error "restore legacy takes exactly one backup timestamp." \
            "Run: jarvis-research restore legacy <timestamp> [--allow-unknown-schema]"
        timestamp="$1"
        ;;
    esac
    shift
  done
  [ -n "$timestamp" ] \
    || usage_error "restore legacy takes exactly one backup timestamp." \
      "Run: jarvis-research restore legacy <timestamp> [--allow-unknown-schema]"
  local restore_id requested_at
  _restore_timestamp_or_usage legacy "$timestamp"

  _require_docker_daemon
  # A restore replays into a RUNNING database: restore.sh drives psql against the
  # postgres service, and --no-deps below deliberately starts nothing. Checked
  # here so a stopped stack is named at the start, instead of surfacing as the
  # safety backup failing several steps later with no mention of the cause.
  # Note this is the running-only probe on purpose; the ownership check that
  # gates it accepts a stopped container, because `restore status` and
  # `restore request` genuinely do work while the stack is down.
  # Ownership is verified first and separately: its refusals are about the WRONG
  # install, not a stopped one, and folding them into the message below would
  # answer a mismatch with "start the database".
  _backup_volume_compose ps -q postgres >/dev/null \
    || die "This install's backup service could not be verified, so no restore was started." \
      "Nothing was changed. Run: jarvis-research doctor"
  [ -n "$(_raw_backup_volume_compose ps -q postgres 2>/dev/null | head -1)" ] \
    || die "The database is not running, so there is nothing to restore into." \
      "Start it first, then retry: jarvis-research start"
  info "Restoring backup ${timestamp} on this host without manifest authentication."
  info "This set carries no signature, so it cannot be checked for tampering: its archive checksums are self-reported."
  info "The restore asks you to type the acceptance phrase before it changes anything."

  restore_id="$(_restore_new_request_id)" \
    || die "A restore identifier could not be generated." \
      "Nothing was changed. Run: jarvis-research doctor"
  requested_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    || die "The current time could not be read." \
      "Nothing was changed. Run: jarvis-research doctor"

  # The backup service polls the trigger volume every five seconds and consumes a
  # restore request before anything else, so it is stopped BEFORE the request is
  # written. Writing first hands the set to a non-interactive service that refuses
  # it and deletes the request out from under this run.
  _backup_volume_compose stop postgres-backup >/dev/null \
    || die "The backup service could not be stopped, so no restore was started." \
      "Nothing was changed. Run: jarvis-research doctor"
  trap _restore_legacy_resume_sidecar EXIT

  _restore_request_json local "$timestamp" "$restore_id" "$requested_at" "$allow_unknown_schema" \
    | _backup_volume_compose run --rm --no-deps -T --entrypoint sh postgres-backup \
        -c "cat > ${RESTORE_REQUEST_PATH}" \
    || die "The restore request could not be written." \
      "Nothing was changed. Run: jarvis-research doctor"

  # No Compose flag forces a pseudo-terminal, so this run deliberately relies on
  # stdin auto-detection; a -T here would make the acceptance prompt unreachable
  # and refuse every unsigned restore. The compose timeout is held unset so
  # nothing can kill the prompt while the operator is reading it.
  BACKUP_COMPOSE_TIMEOUT_SECONDS="" \
    _backup_volume_compose run --rm --no-deps -i \
      -e JARVIS_RESTORE_ALLOW_LEGACY=1 \
      --entrypoint /usr/local/bin/restore.sh postgres-backup \
    || die "The restore did not complete." \
      "Check what it reported: jarvis-research restore status"
  ok "The restore of backup ${timestamp} finished."
  info "Review the outcome with: jarvis-research restore status"
}

cmd_restore_status() {
  [ "$#" -eq 0 ] \
    || usage_error "restore status takes no arguments." \
      "Run: jarvis-research restore status"
  _require_docker_daemon
  local report state step error safety
  report="$(_backup_volume_compose run --rm --no-deps -T --entrypoint sh postgres-backup \
    -c "cat ${RESTORE_STATUS_PATH} 2>/dev/null || echo '{}'")" \
    || die "The stack is not running, so the restore status file cannot be read." \
      "Start it with: jarvis-research start"

  state="$(_restore_status_field "$report" state)"
  if [ -z "$state" ]; then
    info "No restore has been recorded on this installation."
    return 0
  fi
  step="$(_restore_status_field "$report" current_step)"
  error="$(_restore_status_field "$report" error)"
  safety="$(_restore_status_field "$report" safety_backup_ts)"

  case "$state" in
    running) info "A restore is in progress." ;;
    done)    ok "The last restore completed." ;;
    failed)  err "The last restore failed." ;;
    *)       info "The backup service reports restore state '${state}'." ;;
  esac
  [ -z "$step" ]  || printf 'Current step:  %s\n' "$step"
  [ -z "$error" ] || printf 'Error:         %s\n' "$error"
  if _restore_status_flag "$report" manual_steps_required; then
    printf 'Manual steps:  required — the restore stopped after it had started changing data.\n'
    [ -z "$safety" ] \
      || printf 'Safety backup: %s — restore this point to return to the pre-restore state.\n' "$safety"
  else
    printf 'Manual steps:  none.\n'
    [ -z "$safety" ] || printf 'Safety backup: %s\n' "$safety"
  fi
}

cmd_restore_request() {
  [ "$#" -eq 1 ] \
    || usage_error "restore request takes exactly one backup timestamp." \
      "Run: jarvis-research restore request <timestamp>"
  local timestamp="$1" restore_id requested_at request compose_prefix file
  local -a compose_argv=()
  _restore_timestamp_or_usage request "$timestamp"
  # The operator runs these by hand in their own shell, where COMPOSE_PROJECT_NAME
  # or COMPOSE_FILE may point somewhere else entirely. A bare `docker compose` in
  # printed disaster instructions would silently address whatever that shell is
  # pointing at, so every printed command carries this install's own scope.
  # No ownership check and no compose call: this command only prints, and the
  # scope it prints is read from THIS install's .env rather than the environment,
  # so an exported COMPOSE_PROJECT_NAME cannot redirect the printed procedure at
  # a sibling install. Keeping it call-free also means it still works for an
  # operator whose Docker daemon is down, which is when the procedure is needed.
  _init_backup_volume_compose \
    || die "This install's compose project could not be resolved, so no procedure was printed." \
      "Run this from the installation directory, then retry: jarvis-research doctor"
  compose_argv=(docker compose --project-directory "$REPO" --env-file "$REPO/.env" -p "$BACKUP_COMPOSE_PROJECT")
  for file in "${BACKUP_COMPOSE_FILES[@]}"; do compose_argv+=(-f "$file"); done
  compose_prefix="$(printf '%q ' "${compose_argv[@]}")"
  compose_prefix="${compose_prefix% }"
  restore_id="$(_restore_new_request_id)" \
    || die "A restore identifier could not be generated." \
      "Nothing was changed. Run: jarvis-research doctor"
  requested_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    || die "The current time could not be read." \
      "Nothing was changed. Run: jarvis-research doctor"
  request="$(_restore_request_json inbox "$timestamp" "$restore_id" "$requested_at")"

  cat <<REQUEST
Recovering this host from another installation's backup set is a manual,
ordered procedure. This command only prints it: nothing was submitted, no file
was written, and no archive was moved.

Each command below is already scoped to this installation. Run them as printed,
from any directory.

1. Copy the complete archive set into the restore inbox:

     ${compose_prefix} cp ./offsite/. postgres-backup:/restore-inbox/

2. Copy the matching encryption key under its required one-time name:

     ${compose_prefix} cp /path/to/backup_encrypt_key.txt postgres-backup:/restore-inbox/operator_key

3. Only once both copies are in place, submit the request below. The backup
   service acts on it within seconds, and an empty inbox fails the restore:

     printf '%s' '${request}' | ${compose_prefix} exec -T postgres-backup sh -c 'cat > ${RESTORE_REQUEST_PATH}'

Keep the restore identifier ${restore_id}. After the restore you type it back to
release outbound quarantine:

     jarvis-research restore acknowledge ${restore_id}
REQUEST
}

cmd_restore() {
  local restore_command="${1:-}"
  if [ "$#" -gt 0 ]; then
    shift
  fi
  case "$restore_command" in
    acknowledge) cmd_restore_acknowledge "$@" ;;
    legacy)      cmd_restore_legacy "$@" ;;
    status)      cmd_restore_status "$@" ;;
    request)     cmd_restore_request "$@" ;;
    *) usage_error "restore: unknown subcommand '${restore_command}'." \
         "Run: jarvis-research restore status   (or: restore legacy|request <timestamp>, restore acknowledge <restore-id>)" ;;
  esac
}

# -----------------------------------------------------------------------------
# Day-to-day container control (start/repair are always --no-build).
# -----------------------------------------------------------------------------
cmd_status()  { _require_docker_daemon; docker compose ps "$@"; }
cmd_start()   { _require_docker_daemon; _acquire_control_lifecycle; info "Starting services (no build)..."; docker compose up -d --no-build; _finish_control_lifecycle; }
cmd_stop()    { _require_docker_daemon; _acquire_control_lifecycle; info "Stopping services..."; docker compose stop; _finish_control_lifecycle; }
cmd_restart() { _require_docker_daemon; _acquire_control_lifecycle; info "Restarting services..."; docker compose restart; _finish_control_lifecycle; }
cmd_logs()    { _require_docker_daemon; docker compose logs "$@"; }

# repair — bounded, never destructive: recreate stopped containers (no build/pull),
# restart any unhealthy mandatory service, wait, then summarise via doctor.
cmd_repair() {
  _require_docker_daemon
  _acquire_control_lifecycle
  info "Repairing: recreating stopped containers (no build, no pull)..."
  docker compose up -d --no-build || warn "Some services did not come up cleanly."
  local svc cid h
  for svc in $MANDATORY_HEALTH_BASE; do
    cid="$(docker compose ps -q "$svc" 2>/dev/null | head -1 || true)"
    [ -n "$cid" ] || continue
    h="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    if [ "$h" = "unhealthy" ]; then
      info "Restarting unhealthy service: ${svc}"
      docker restart "$cid" >/dev/null 2>&1 || true
    fi
  done
  info "Repair finished; running a doctor summary."
  cmd_doctor || true
  _finish_control_lifecycle
}

# -----------------------------------------------------------------------------
# doctor — read-only health, disk, registration, update-availability, and
# host-preflight probes (each a WARN line, never a hard fail). Exit 0/1.
# -----------------------------------------------------------------------------
cmd_doctor() {
  local rc=0
  printf '%s=== jarvis-research doctor ===%s\n' "$C_BOLD" "$C_RESET"
  if [ -x "${REPO}/setup.sh" ]; then
    ( cd "$REPO" && ./setup.sh --check ) || rc=1
  fi
  printf '\n%s-- containers --%s\n' "$C_BOLD" "$C_RESET"
  docker compose ps 2>/dev/null \
    || warn "Could not query container status (recover with: jarvis-research repair)."
  local disk; disk="$(preflight_disk_lib 1 2>/dev/null || true)"
  printf '\n%s-- disk --%s  free ~%s GB on the Docker data root\n' "$C_BOLD" "$C_RESET" "${disk%% *}"
  if [ -f "$INSTALLS_FILE" ] && grep -qxF "$REPO" "$INSTALLS_FILE"; then
    ok "This install is registered with jarvis-research."
  else
    warn "This install is not registered (run: jarvis-research register)."
  fi
  # Both update refusals point here, so doctor must be able to answer the
  # question they raise. _require_clean_main_checkout exits on refusal, so the
  # command substitution both isolates that exit and captures the explanation.
  local readiness
  printf '\n%s-- update readiness --%s\n' "$C_BOLD" "$C_RESET"
  if readiness="$(_require_clean_main_checkout 2>&1)"; then
    ok "The checkout is ready to update."
  else
    printf '%s\n' "$readiness" | sed 's/^/  /'
    rc=1
  fi
  local latest cur; latest="$(latest_stable_tag origin 2>/dev/null || true)"
  cur="$(sed -n 's/^JARVIS_VERSION=//p' .env 2>/dev/null | head -1)"
  [ -n "$latest" ] && info "Latest published release: ${latest} (installed: ${cur:-unknown})."
  _doctor_host_probes
  return "$rc"
}

_doctor_host_probes() {
  local compose_file torch cv dri
  compose_file="$(sed -n 's/^COMPOSE_FILE=//p' .env 2>/dev/null | head -1)"
  if printf '%s' "$compose_file" | grep -qE 'docker-compose\.(gpu|rocm|vulkan)\.yml'; then
    dri="${JARVIS_DRI_DIR:-/dev/dri}"
    local -a nodes=("$dri"/renderD*)
    if [ ! -e "${nodes[0]}" ]; then
      warn "A GPU overlay is configured but no ${dri}/renderD* render node is present; the accelerator will be unavailable."
    else
      grep -qE '^JARVIS_RENDER_GID=[0-9]+' .env 2>/dev/null || warn "GPU overlay configured but JARVIS_RENDER_GID is not numeric in .env."
      grep -qE '^JARVIS_VIDEO_GID=[0-9]+'  .env 2>/dev/null || warn "GPU overlay configured but JARVIS_VIDEO_GID is not numeric in .env."
    fi
  fi
  torch="$(sed -n 's/^TORCH_VARIANT=//p' .env 2>/dev/null | head -1)"
  if [ "$torch" = "cuda" ]; then
    docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"' \
      || warn "TORCH_VARIANT=cuda but Docker does not report the nvidia runtime; inference may fall back to CPU."
  fi
  cv="$(docker compose version --short 2>/dev/null || true)"
  if [ -n "$cv" ] && ! compose_meets_floor "$cv" 2.24.4; then
    warn "Docker Compose ${cv} is older than the tested floor 2.24.4; overlay merges may misbehave."
  fi
}

# -----------------------------------------------------------------------------
# register / uninstall / version / help.
# -----------------------------------------------------------------------------
cmd_register() {
  install_cli_shim "$REPO"
  ok "Registered ${REPO} as the default jarvis-research install."
}

cmd_uninstall() {
  [ -x "${REPO}/scripts/uninstall.sh" ] \
    || die "The uninstall helper is missing from this checkout." "Restore it with: git checkout scripts/uninstall.sh"
  exec bash "${REPO}/scripts/uninstall.sh" --repo "$REPO" "$@"
}

cmd_version() {
  local v; v="$(sed -n 's/^JARVIS_VERSION=//p' "${REPO}/.env" 2>/dev/null | head -1)"
  printf 'jarvis-research — JARVIS lifecycle CLI\n'
  printf 'Installed JARVIS_VERSION=%s\n' "${v:-unknown}"
  printf 'Repository: %s\n' "$REPO"
}

cmd_help() {
  cat <<'HELP'
jarvis-research — lifecycle CLI for a managed JARVIS install

Usage: jarvis-research [--repo <dir>] <command> [options]

Commands:
  update [--to <tag>] [--resume <tag>] [--yes]
                     Transactional, DB-safe upgrade to the latest stable release
                     (or --to a specific tag). Refuses on a diverged/dirty/non-main
                     checkout, when target images are not yet published, and applies
                     a data-changing migration only after a fresh verified backup.
  status             Show container status.
  start | stop | restart
                     Start (no build), stop, or restart the stack.
  logs [args]        Tail service logs (passes args through to docker compose logs).
  doctor             Read-only health, disk, registration, and update check.
  repair             Bounded, non-destructive recovery (recreate + restart unhealthy).
  owner status       Show the effective instance-owner source and validity.
  owner set <email>  Repair a missing or invalid database owner on this host.
                     A valid owner transfers in Admin Users; OWNER_USER_ID stays
                     host-managed. The email must be typed again to confirm.
  restore status     Report what the backup service's last or current restore
                     did, including whether manual follow-up is required.
  restore legacy <timestamp>
                     Restore a same-host backup taken before manifest signing.
                     The set cannot be checked for tampering, so the acceptance
                     phrase must be typed at the prompt. Off-host sets are never
                     eligible.
  restore request <timestamp>
                     Print the ordered steps and the ready-made request for
                     recovering this host from another installation's backup
                     set. It submits nothing.
  restore acknowledge <restore-id>
                     After reviewing restored credentials, release outbound
                     quarantine for that exact off-host restore. The restore ID
                     must be typed again to confirm.
  register           Record this checkout as a managed install.
  uninstall [--dry-run] [--tier N] [--keep-data] [--all] [--yes]
                     Tiered, contained teardown: stop (1), remove app images (2),
                     delete data volumes (3, typed confirmation), or full purge (4,
                     third-party images + files + the clone). Lead with --dry-run.
  version | help     Print version / this help.

Exit codes: 0 ok · 1 refused/failed · 2 usage · 3 environment (Docker missing).
HELP
}

# -----------------------------------------------------------------------------
# Argument parsing, resolution, and dispatch.
# -----------------------------------------------------------------------------
REPO_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)   REPO_OVERRIDE="${2:-}"; shift 2 ;;
    --repo=*) REPO_OVERRIDE="${1#--repo=}"; shift ;;
    *)        break ;;
  esac
done

SUBCMD="${1:-help}"
if [ "$#" -gt 0 ]; then
  shift
fi

REPO="$(resolve_repo)"
cd "$REPO"
# shellcheck source=scripts/setup_lib.sh
# shellcheck disable=SC1091  # selected lifecycle runtime, verified by its caller
. "$LIFECYCLE_CODE_DIR/setup_lib.sh"
# The managed repo, not the caller's shell, owns Compose file/project/profile
# selection. Compose reloads the install's persisted selectors from its .env.
sanitize_compose_environment

case "$SUBCMD" in
  update)                cmd_update "$@" ;;
  status)                cmd_status "$@" ;;
  start)                 cmd_start ;;
  stop)                  cmd_stop ;;
  restart)               cmd_restart ;;
  logs)                  cmd_logs "$@" ;;
  doctor)                cmd_doctor ;;
  repair)                cmd_repair ;;
  owner)                 cmd_owner "$@" ;;
  restore)               cmd_restore "$@" ;;
  register)              cmd_register ;;
  uninstall)             cmd_uninstall "$@" ;;
  version|--version|-v)  cmd_version ;;
  help|-h|--help)        cmd_help ;;
  *)                     err "Unknown command: ${SUBCMD}"; cmd_help >&2; exit 2 ;;
esac
