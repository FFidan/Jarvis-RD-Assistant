#!/usr/bin/env bash
# test_jarvis_research_cli.sh — behavioral tests for scripts/jarvis-research.sh,
# the lifecycle CLI that fronts a managed JARVIS install. No docker daemon,
# network, or real git repo is needed: git, docker, and `docker compose` are
# stubbed on a private PATH that LOGS every invocation (the pattern established
# by test_update_coverage.sh / test_setup_lib_helpers.sh), and the CLI runs
# against a throwaway fixture repo. State dirs, backup dirs, and the CLI's poll
# budget are redirected to mktemp fixtures via the CLI's env overrides.
#
# The refusal matrix and the transaction ordering checks are the specification:
# every `update` refusal must exit 1 and leave the stub log free of a mutating
# git verb (merge/checkout/reset) or a compose mutation (pull/up/build), and the
# pending-transaction file must be on disk before the fast-forward merge runs.
#
# Run: bash scripts/tests/test_jarvis_research_cli.sh   (exit 0 = pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CLI="${REPO_ROOT}/scripts/jarvis-research.sh"
LIB="${REPO_ROOT}/scripts/setup_lib.sh"
UPDATE_SCRIPT="${REPO_ROOT}/update.sh"

fail=0
pass_n=0
pass() { pass_n=$((pass_n + 1)); printf 'PASS: %s\n' "$1"; }
check_fail() { printf 'FAIL: %s\n' "$1" >&2; fail=1; }
has()  { printf '%s' "$1" | grep -q -- "$2"; }
want() { if has "$1" "$2"; then pass "$3"; else check_fail "$3 :: missing /$2/ in <<<$1>>>"; fi; }
lack() { if has "$1" "$2"; then check_fail "$3 :: unexpected /$2/ in <<<$1>>>"; else pass "$3"; fi; }

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
STUB="$ROOT/stub"
mkdir -p "$STUB"

# =============================================================================
# Stubs: git + docker + docker compose, logging every call to $STUB_LOG.
# =============================================================================
cat > "$STUB/git" <<'GIT'
#!/usr/bin/env bash
log() { [ -n "${STUB_LOG:-}" ] && printf 'git %s\n' "$*" >> "$STUB_LOG"; }
case "${1:-} ${2:-}" in
  "symbolic-ref --short") printf '%s\n' "${STUB_BRANCH:-main}"; exit 0 ;;
  "status --porcelain")   printf '%s' "${STUB_DIRTY:-}"; [ -n "${STUB_DIRTY:-}" ] && printf '\n'; exit 0 ;;
  "remote get-url")       printf '%s\n' "${STUB_REMOTE:-git@github.com:limitcycle-oss/jarvis-rd-assistant.git}"; exit 0 ;;
esac
case "${1:-}" in
  rev-parse)
    # rev-parse HEAD -> head sha; rev-parse <ref> -> a per-ref sha.
    case "${2:-}" in
      HEAD) printf 'sha-head\n' ;;
      *)    printf 'sha-%s\n' "${2:-}" ;;
    esac
    exit 0 ;;
  fetch) log "$*"; exit 0 ;;
  ls-remote)
    # latest_stable_tag reads this.
    printf 'sha refs/tags/%s\n' "${STUB_TAGS:-v1.1.3}"
    exit 0 ;;
  merge-base)
    # merge-base --is-ancestor HEAD <ref>
    log "$*"
    exit "${STUB_ANCESTOR:-0}" ;;
  diff)
    # diff --name-only HEAD..<ref> -- db/migrations/
    [ -n "${STUB_MIGRATIONS:-}" ] && printf '%s\n' "${STUB_MIGRATIONS}"
    exit 0 ;;
  show)
    # show <ref>:<path>
    case "${2:-}" in
      *:versions.env)
        cat <<'VE'
POSTGRES_IMAGE=postgres:16.8
OLLAMA_IMAGE=ollama/ollama:0.31.2
QDRANT_IMAGE=qdrant/qdrant:v1.13.2
LITELLM_IMAGE=ghcr.io/berriai/litellm:main-stable
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2025.1.0
CADDY_IMAGE=caddy:2.9-alpine
VE
        ;;
      *) printf '%s\n' "${STUB_MIG_CONTENT:-}" ;;
    esac
    exit 0 ;;
  merge)
    # merge --ff-only <ref>  — the ONLY branch advance.
    [ -n "${PENDING_FILE:-}" ] && [ -f "$PENDING_FILE" ] && printf 'PENDING_EXISTS_AT_MERGE\n' >> "$STUB_LOG"
    log "$*"
    exit "${STUB_MERGE_RC:-0}" ;;
  *) log "$*"; exit 0 ;;
esac
GIT
chmod +x "$STUB/git"

cat > "$STUB/docker" <<'DOCKER'
#!/usr/bin/env bash
log() { [ -n "${STUB_LOG:-}" ] && printf 'docker %s\n' "$*" >> "$STUB_LOG"; }
running_svc() {
  case "$1" in
    postgres|ollama|qdrant|litellm|cloudflared|postgres-backup) return 0 ;;
    paper_ingestion|learning_engine|dashboard|restore-uploader|telegram_bot) return 0 ;;
    *) return 1 ;;
  esac
}
case "${1:-}" in
  info) [ "${STUB_NO_DAEMON:-0}" = 1 ] && exit 1; exit 0 ;;
  manifest)
    # manifest inspect <ref>
    ref="${3:-}"
    log "manifest inspect $ref"
    if [ -n "${MANIFEST_MISS:-}" ] && printf '%s' "$ref" | grep -q -- "$MANIFEST_MISS"; then exit 1; fi
    exit 0 ;;
  inspect)
    shift; fmt=""
    while [ $# -gt 0 ]; do case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac; done
    case "$fmt" in
      *Config.Image*) printf 'oldimage:running\n' ;;
      *State.Health*) printf '%s\n' "${STUB_HEALTH-healthy}" ;;
      *State.Status*) printf '%s\n' "${STUB_RUN_STATE-running}" ;;
      *) : ;;
    esac
    exit 0 ;;
  restart) log "restart ${*:2}"; exit 0 ;;
  compose)
    shift; args=()
    while [ $# -gt 0 ]; do case "$1" in --profile) shift 2 ;; --env-file) shift 2 ;; *) args+=("$1"); shift ;; esac; done
    set -- "${args[@]:-}"
    case "${1:-}" in
      version) exit 0 ;;
      ps)
        if [ "${2:-}" = "-q" ]; then running_svc "${3:-}" && printf 'cid-%s\n' "${3:-}"; exit 0; fi
        # bare `ps` (status/doctor table)
        printf 'NAME                 STATUS\n'
        printf 'jarvis-dashboard-1   Up 3 minutes (healthy)\n'
        exit 0 ;;
      pull)  log "compose pull ${*:2}"; [ "${STUB_FAIL_STAGE_PULL:-0}" = 1 ] && exit 1; exit 0 ;;
      up)    log "compose up ${*:2}"; exit 0 ;;
      build) log "compose build ${*:2}"; exit 0 ;;
      stop|restart|logs) log "compose $*"; exit 0 ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
DOCKER
chmod +x "$STUB/docker"

# A minimal setup.sh stub for `doctor` (shells `./setup.sh --check`) is NOT used;
# the fixture symlinks the real setup.sh only where needed. doctor tests below use
# a fixture setup.sh that just prints a PASS line.

# =============================================================================
# Fixture repo builder.
# =============================================================================
make_repo() {
  local dir="$1"
  mkdir -p "$dir/scripts/tests" "$dir/db/migrations"
  ln -sf "$CLI" "$dir/scripts/jarvis-research.sh"
  ln -sf "$LIB" "$dir/scripts/setup_lib.sh"
  ln -sf "$UPDATE_SCRIPT" "$dir/update.sh"
  cat > "$dir/versions.env" <<'VE'
POSTGRES_IMAGE=postgres:16.8
OLLAMA_IMAGE=ollama/ollama:0.31.2
QDRANT_IMAGE=qdrant/qdrant:v1.13.2
LITELLM_IMAGE=ghcr.io/berriai/litellm:main-stable
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2025.1.0
CADDY_IMAGE=caddy:2.9-alpine
VE
  printf 'services:\n  dashboard:\n    image: x\n' > "$dir/docker-compose.yml"
  printf 'JARVIS_VERSION=1.1.2\nTORCH_VARIANT=cpu\nTORCH_VARIANT_SUFFIX=\n' > "$dir/.env"
  # A setup.sh that doctor can shell for `--check`.
  cat > "$dir/setup.sh" <<'SETUP'
#!/usr/bin/env bash
[ "${1:-}" = "--check" ] && { printf 'PREFLIGHT: PASS\n'; exit 0; }
exit 0
SETUP
  chmod +x "$dir/setup.sh"
}

# Fresh per-test environment: a repo, a CLI state dir, a backup dir + trigger dir,
# a clean stub log. Echoes nothing; exports globals the run_cli helper consumes.
new_env() {
  # Reset every stub tunable so a value set by one test can never leak into the
  # next through run_cli's ${VAR:-default} passthrough.
  unset STUB_MIGRATIONS STUB_MIG_CONTENT MANIFEST_MISS STUB_ANCESTOR STUB_MERGE_RC \
        STUB_DIRTY STUB_BRANCH STUB_REMOTE STUB_HEALTH STUB_FAIL_STAGE_PULL \
        STUB_NO_DAEMON STUB_TAGS STUB_RUN_STATE 2>/dev/null || true
  REPO="$ROOT/repo.$RANDOM.$RANDOM"
  CFG="$ROOT/cfg.$RANDOM.$RANDOM"
  BK="$ROOT/backups.$RANDOM.$RANDOM"
  TRIG="$ROOT/trigger.$RANDOM.$RANDOM"
  mkdir -p "$CFG" "$BK" "$TRIG"
  make_repo "$REPO"
  STUB_LOG="$ROOT/log.$RANDOM"
  : > "$STUB_LOG"
  PENDING_FILE="$CFG/pending-update.json"
}

# run_cli [--norepo] <args...>  — invoke the CLI with the stub PATH and the
# fixture env overrides. Default passes --repo "$REPO"; --norepo omits it and
# cd's into the repo (exercising cwd-ancestor resolution + registry).
run_cli() {
  local norepo=0
  if [ "${1:-}" = "--norepo" ]; then norepo=1; shift; fi
  local -a pre=(env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" "PENDING_FILE=$PENDING_FILE"
    "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin"
    "JARVIS_BACKUP_DIR=$BK" "JARVIS_BACKUP_TRIGGER_DIR=$TRIG"
    "JARVIS_BACKUP_POLL_TIMEOUT=1" "JARVIS_BACKUP_POLL_INTERVAL=1"
    "STUB_BRANCH=${STUB_BRANCH:-main}" "STUB_DIRTY=${STUB_DIRTY:-}"
    "STUB_REMOTE=${STUB_REMOTE:-git@github.com:limitcycle-oss/jarvis-rd-assistant.git}"
    "STUB_ANCESTOR=${STUB_ANCESTOR:-0}" "STUB_MERGE_RC=${STUB_MERGE_RC:-0}"
    "STUB_MIGRATIONS=${STUB_MIGRATIONS:-}" "STUB_MIG_CONTENT=${STUB_MIG_CONTENT:-}"
    "MANIFEST_MISS=${MANIFEST_MISS:-}" "STUB_HEALTH=${STUB_HEALTH-healthy}"
    "STUB_FAIL_STAGE_PULL=${STUB_FAIL_STAGE_PULL:-0}" "STUB_TAGS=${STUB_TAGS:-v1.1.3}"
    "STUB_NO_DAEMON=${STUB_NO_DAEMON:-0}")
  if [ "$norepo" -eq 1 ]; then
    ( cd "$REPO" && "${pre[@]}" bash "$REPO/scripts/jarvis-research.sh" "$@" ) </dev/null 2>&1
  else
    "${pre[@]}" bash "$REPO/scripts/jarvis-research.sh" --repo "$REPO" "$@" </dev/null 2>&1
  fi
}

# register REPO in the state file (so the managed-install guard's (a) leg passes).
register_repo() { printf '%s\n' "$REPO" > "$CFG/installs"; }

log_lacks_mutations() {  # $1 = description
  local l; l="$(cat "$STUB_LOG" 2>/dev/null || true)"
  if printf '%s' "$l" | grep -qE 'merge |checkout |reset |compose (pull|up|build)'; then
    check_fail "$1 :: stub log has a mutation: $(printf '%s' "$l" | grep -E 'merge |checkout |reset |compose (pull|up|build)' | tr '\n' ';')"
  else
    pass "$1"
  fi
}

# =============================================================================
# 1. Resolution / registration.
# =============================================================================
new_env
out="$(run_cli version)"; rc=$?
want "$out" 'jarvis-research' "resolve_repo_env_override: --repo resolves and version runs (rc=$rc)"

# cwd/ancestor resolution: run from a SUBDIR of the repo, no --repo, registered.
new_env; register_repo
mkdir -p "$REPO/db/migrations/nested"
out="$( cd "$REPO/db/migrations/nested" && env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
  "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
  bash "$REPO/scripts/jarvis-research.sh" version </dev/null 2>&1 )"; rc=$?
want "$out" 'JARVIS_VERSION=1.1.2\|1.1.2' "resolve_repo_cwd_ancestor: finds compose+versions.env walking up (rc=$rc)"

# state-file default: no --repo, not in repo tree; first registry line is the repo.
new_env; register_repo
out="$( cd "$ROOT" && env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
  "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
  bash "$CLI" version </dev/null 2>&1 )"; rc=$?
want "$out" '1.1.2' "resolve_repo_state_file_default: first registry line is the default install (rc=$rc)"

# stale registry lines are skipped with a warning.
new_env
printf '%s\n%s\n' "$ROOT/does-not-exist-$RANDOM" "$REPO" > "$CFG/installs"
out="$( cd "$ROOT" && env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
  "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
  bash "$CLI" version </dev/null 2>&1 )"; rc=$?
if has "$out" '1.1.2' && has "$out" 'skip\|stale\|ignor'; then
  pass "resolve_repo_skips_stale_lines: stale first line skipped with a warning, valid one used"
else
  check_fail "resolve_repo_skips_stale_lines: <<<$out>>>"
fi

# register prepends + de-dups.
new_env
run_cli register >/dev/null 2>&1
run_cli register >/dev/null 2>&1
if [ "$(grep -cxF "$REPO" "$CFG/installs" 2>/dev/null)" = 1 ] && [ "$(head -n1 "$CFG/installs")" = "$REPO" ]; then
  pass "register_prepends_and_dedups: repo at top exactly once after two registers"
else
  check_fail "register_prepends_and_dedups: installs=$(tr '\n' ',' < "$CFG/installs" 2>/dev/null)"
fi

# unknown command -> exit 2.
new_env
out="$(run_cli frobnicate)"; rc=$?
if [ "$rc" -eq 2 ]; then pass "unknown_command_exits_2: usage exit code"; else check_fail "unknown_command_exits_2: rc=$rc out=$out"; fi

# shim (from setup_lib install_cli_shim) execs the repo CLI with --repo. The
# installed shim resolves the registry at ${XDG_CONFIG_HOME}/jarvis-research/installs,
# so wire XDG_CONFIG_HOME to a fixture whose jarvis-research/ dir install_cli_shim
# populates, then run the shim with that same XDG_CONFIG_HOME.
new_env
XDGH="$ROOT/xdg.$RANDOM"; mkdir -p "$XDGH/jarvis-research"
# shellcheck source=../setup_lib.sh
# shellcheck disable=SC1091
( source "$LIB"; JARVIS_CLI_BIN_DIR="$CFG/bin" JARVIS_CLI_CONFIG_DIR="$XDGH/jarvis-research" \
    install_cli_shim "$REPO" >/dev/null )
# Replace the repo CLI with a probe that echoes its args, then run the shim.
# rm the symlink FIRST so the write never clobbers the real CLI source.
rm -f "$REPO/scripts/jarvis-research.sh"
cat > "$REPO/scripts/jarvis-research.sh" <<'PROBE'
#!/usr/bin/env bash
printf 'PROBE-ARGS: %s\n' "$*"
PROBE
chmod +x "$REPO/scripts/jarvis-research.sh"
shim_out="$(XDG_CONFIG_HOME="$XDGH" bash "$CFG/bin/jarvis-research" version 2>&1)"
if has "$shim_out" "PROBE-ARGS: --repo $REPO version"; then
  pass "shim_execs_repo_script: shim execs \$repo/scripts/jarvis-research.sh --repo \$repo \"\$@\""
else
  check_fail "shim_execs_repo_script: <<<$shim_out>>>"
fi
make_repo "$REPO"   # restore the real symlink for later tests

# =============================================================================
# 2. Refusal matrix — each exits 1 and mutates nothing.
# =============================================================================
new_env   # not registered, no --repo -> unregistered
out="$( cd "$REPO" && env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
  "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
  bash "$REPO/scripts/jarvis-research.sh" update --yes </dev/null 2>&1 )"; rc=$?
if [ "$rc" -eq 1 ]; then pass "update_refuses_unregistered_repo: exit 1"; else check_fail "update_refuses_unregistered_repo: rc=$rc out=$out"; fi
want "$out" 'register' "update_refuses_unregistered_repo: names jarvis-research register"
log_lacks_mutations "update_refuses_unregistered_repo: no mutation"

new_env; register_repo; STUB_REMOTE="git@github.com:someone-else/other.git"
out="$(run_cli update --yes)"; rc=$?
unset STUB_REMOTE
if [ "$rc" -eq 1 ]; then pass "update_refuses_wrong_remote: exit 1"; else check_fail "update_refuses_wrong_remote: rc=$rc out=$out"; fi
log_lacks_mutations "update_refuses_wrong_remote: no mutation"

new_env; register_repo; STUB_DIRTY=" M setup.sh"
out="$(run_cli update --yes)"; rc=$?
unset STUB_DIRTY
if [ "$rc" -eq 1 ]; then pass "update_refuses_dirty_tree: exit 1"; else check_fail "update_refuses_dirty_tree: rc=$rc out=$out"; fi
log_lacks_mutations "update_refuses_dirty_tree: no mutation"

new_env; register_repo; STUB_BRANCH="feature/x"
out="$(run_cli update --yes)"; rc=$?
unset STUB_BRANCH
if [ "$rc" -eq 1 ]; then pass "update_refuses_nonmain_branch: exit 1"; else check_fail "update_refuses_nonmain_branch: rc=$rc out=$out"; fi
log_lacks_mutations "update_refuses_nonmain_branch: no mutation"

# diverged (a): merge-base --is-ancestor fails -> abort BEFORE any side effect.
new_env; register_repo; STUB_ANCESTOR=1
out="$(run_cli update --yes)"; rc=$?
unset STUB_ANCESTOR
if [ "$rc" -eq 1 ]; then pass "update_refuses_diverged_main: exit 1"; else check_fail "update_refuses_diverged_main: rc=$rc out=$out"; fi
log_lacks_mutations "update_refuses_diverged_main: no compose/branch mutation"
if [ ! -f "$PENDING_FILE" ] && [ -z "$(ls -A "$TRIG" 2>/dev/null)" ]; then
  pass "update_refuses_diverged_main: no pending state file, empty backup trigger dir"
else
  check_fail "update_refuses_diverged_main: side effect (pending=$([ -f "$PENDING_FILE" ] && echo yes) trig=$(ls -A "$TRIG"))"
fi

# diverged (b): precheck green but merge --ff-only red -> terminal guard aborts.
new_env; register_repo; STUB_MERGE_RC=1
out="$(run_cli update --yes)"; rc=$?
unset STUB_MERGE_RC
if [ "$rc" -ne 0 ]; then pass "update_refuses_diverged_main(b): ff-merge failure aborts nonzero"; else check_fail "update_refuses_diverged_main(b): rc=$rc out=$out"; fi
# The merge WAS attempted (terminal guard), but nothing beyond it ran.
if has "$(cat "$STUB_LOG")" 'merge --ff-only' && ! grep -q 'compose up' "$STUB_LOG"; then
  pass "update_refuses_diverged_main(b): merge attempted, no recreate followed"
else
  check_fail "update_refuses_diverged_main(b): log=$(cat "$STUB_LOG")"
fi

# missing manifest -> refusal BEFORE any pull/merge.
new_env; register_repo; MANIFEST_MISS='jarvis-dashboard'
out="$(run_cli update --yes)"; rc=$?
unset MANIFEST_MISS
if [ "$rc" -eq 1 ]; then pass "update_refuses_missing_manifest: exit 1"; else check_fail "update_refuses_missing_manifest: rc=$rc out=$out"; fi
if ! grep -qE 'merge --ff-only|compose pull|compose up' "$STUB_LOG"; then
  pass "update_refuses_missing_manifest: no pull/merge before the gate"
else
  check_fail "update_refuses_missing_manifest: log=$(cat "$STUB_LOG")"
fi

# destructive migration + no fresh backup -> refusal; with a fresh backup -> proceeds.
new_env; register_repo
STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
STUB_MIG_CONTENT="-- purge
DELETE FROM telegram_user_pairings WHERE chat_id < 0;"
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && has "$out" 'backup'; then
  pass "update_requires_backup_on_destructive_migration: no fresh backup -> refuse"
else
  check_fail "update_requires_backup_on_destructive_migration(no-backup): rc=$rc out=$out"
fi
log_lacks_mutations "update_requires_backup_on_destructive_migration(no-backup): no mutation"

# =============================================================================
# Backup fixture helper: write a fresh (future-dated) manifest + archives.
#   $1=backup_dir  $2=mode (good|bad_db)
# =============================================================================
seed_fresh_backup() {
  local dir="$1" mode="$2" ts="20991231_235959"
  local jf="jarvis_${ts}.sql.gz.enc" sf="secrets_${ts}.tar.gz.enc" qf="qdrant_papers_${ts}.snapshot.enc"
  printf 'JARVISDBDATA' > "$dir/$jf"
  printf 'SECRETSDATA'  > "$dir/$sf"
  printf 'QDRANTSNAP'   > "$dir/$qf"
  local jsha ssha qsha jsz ssz qsz
  jsha="$(sha256sum "$dir/$jf" | cut -d' ' -f1)"; jsz="$(stat -c%s "$dir/$jf")"
  ssha="$(sha256sum "$dir/$sf" | cut -d' ' -f1)"; ssz="$(stat -c%s "$dir/$sf")"
  qsha="$(sha256sum "$dir/$qf" | cut -d' ' -f1)"; qsz="$(stat -c%s "$dir/$qf")"
  if [ "$mode" = "bad_db" ]; then jsha="deadbeef"; fi   # DB hash mismatch
  printf '{"timestamp":"%s","app_version":"1.1.3","schema_version":200,"created_at":"2099-12-31T23:59:59+00:00","archives":[{"filename":"%s","sha256":"%s","size_bytes":%s},{"filename":"%s","sha256":"%s","size_bytes":%s},{"filename":"%s","sha256":"%s","size_bytes":%s}]}' \
    "$ts" "$jf" "$jsha" "$jsz" "$sf" "$ssha" "$ssz" "$qf" "$qsha" "$qsz" > "$dir/manifest_${ts}.json"
  touch -d '2099-12-31 23:59:59' "$dir/manifest_${ts}.json"
}

new_env; register_repo
STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
STUB_MIG_CONTENT="DELETE FROM telegram_user_pairings WHERE chat_id < 0;"
seed_fresh_backup "$BK" good
out="$(run_cli update --yes)"; rc=$?
if has "$(cat "$STUB_LOG")" 'merge --ff-only'; then
  pass "update_requires_backup_on_destructive_migration: fresh verified backup -> proceeds to merge"
else
  check_fail "update_requires_backup_on_destructive_migration(with-backup): rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

# archive-set gate: fresh manifest but DB archive hash-mismatched -> refuse.
new_env; register_repo
STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
STUB_MIG_CONTENT="DROP TABLE old_stuff;"
seed_fresh_backup "$BK" bad_db
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && ! grep -q 'merge --ff-only' "$STUB_LOG"; then
  pass "update_backup_gate_requires_archive_set: mismatched DB archive -> refuse, never merges"
else
  check_fail "update_backup_gate_requires_archive_set: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

# =============================================================================
# 3. Transaction set.
# =============================================================================
# additive-only migration: no backup needed, full happy path completes.
new_env; register_repo
out="$(run_cli update --yes)"; rc=$?
if has "$(cat "$STUB_LOG")" 'PENDING_EXISTS_AT_MERGE'; then
  pass "update_writes_pending_txn_before_merge: pending file exists when merge runs"
else
  check_fail "update_writes_pending_txn_before_merge: rc=$rc log=$(cat "$STUB_LOG")"
fi

# pins the UNPREFIXED version + v-less image refs.
new_env; register_repo
out="$(run_cli update --yes)"; rc=$?
if grep -q '^JARVIS_VERSION=1.1.3$' "$REPO/.env"; then
  pass "update_pins_unprefixed_version: .env JARVIS_VERSION=1.1.3 (no v-prefix)"
else
  check_fail "update_pins_unprefixed_version: .env=$(grep JARVIS_VERSION "$REPO/.env")"
fi
if ! grep -q ':v1\.1\.3' "$STUB_LOG" && ! grep -q 'JARVIS_VERSION=v1.1.3' "$STUB_LOG"; then
  pass "update_pins_unprefixed_version: no v-prefixed image ref or pin in the stub log"
else
  check_fail "update_pins_unprefixed_version: v-prefix leaked: $(grep -E ':v1|=v1' "$STUB_LOG")"
fi

# commits only after health: failed health -> transaction stays pending.
new_env; register_repo; STUB_HEALTH="unhealthy"
out="$(run_cli update --yes)"; rc=$?
unset STUB_HEALTH
if [ "$rc" -ne 0 ] && [ -f "$PENDING_FILE" ] && ! grep -q '"committed"' "$PENDING_FILE" 2>/dev/null; then
  pass "update_commits_txn_only_after_health: failed health leaves txn pending, not committed"
else
  check_fail "update_commits_txn_only_after_health: rc=$rc pending=$([ -f "$PENDING_FILE" ] && cat "$PENDING_FILE")"
fi

# --to targets an rc tag with the same gates.
new_env; register_repo
out="$(run_cli update --to v1.1.4-rc.1 --yes)"; rc=$?
if grep -q 'git merge --ff-only v1.1.4-rc.1' "$STUB_LOG" && grep -q '^JARVIS_VERSION=1.1.4-rc.1$' "$REPO/.env"; then
  pass "update_to_flag_targets_rc_with_same_gates: rc tag merged + pinned v-less"
else
  check_fail "update_to_flag_targets_rc_with_same_gates: log=$(grep merge "$STUB_LOG") env=$(grep JARVIS_VERSION "$REPO/.env")"
fi

# resume re-enters recorded phase: a pending file at phase=pull resumes pulls, never re-merges.
new_env; register_repo
printf '{"from_sha":"sha-head","from_version":"1.1.2","target":"v1.1.3","phase":"pull","started_at":"1"}' > "$PENDING_FILE"
out="$(run_cli update --yes)"; rc=$?
if grep -q 'compose pull' "$STUB_LOG" && ! grep -q 'merge --ff-only' "$STUB_LOG"; then
  pass "update_resume_reenters_recorded_phase: pending phase=pull resumes pulls, no re-merge"
else
  check_fail "update_resume_reenters_recorded_phase: log=$(cat "$STUB_LOG")"
fi

# explicit --resume skips merge and does not re-exec.
new_env; register_repo
printf '{"from_sha":"sha-head","from_version":"1.1.2","target":"v1.1.3","phase":"pull","started_at":"1"}' > "$PENDING_FILE"
out="$(run_cli update --resume v1.1.3 --yes)"; rc=$?
if ! grep -q 'merge --ff-only' "$STUB_LOG"; then
  pass "update_resume_skips_merge_and_does_not_reexec: --resume performs no merge"
else
  check_fail "update_resume_skips_merge_and_does_not_reexec: log=$(cat "$STUB_LOG")"
fi

# an explicit --resume tag that disagrees with the pending target is refused
# before any pull/merge (a mistyped tag must not re-pin .env to the wrong version).
new_env; register_repo
printf '{"from_sha":"sha-head","from_version":"1.1.2","target":"v1.1.3","phase":"pull","started_at":"1"}' > "$PENDING_FILE"
out="$(run_cli update --resume v9.9.9 --yes)"; rc=$?
if [ "$rc" -eq 1 ] && has "$out" 'does not match' \
   && ! grep -qE 'compose pull|merge --ff-only' "$STUB_LOG"; then
  pass "resume_tag_must_match_pending: mismatched --resume tag refused, no pull/merge"
else
  check_fail "resume_tag_must_match_pending: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

# =============================================================================
# 4. Mechanical + misc.
# =============================================================================
if ! grep -qE 'checkout[[:space:]]+-B|reset[[:space:]]+--hard' "$CLI"; then
  pass "update_script_never_contains_checkout_dash_B: no 'checkout -B' / 'reset --hard' in the script"
else
  check_fail "update_script_never_contains_checkout_dash_B: found a banned branch-rewrite verb"
fi

# uninstall dispatches to scripts/uninstall.sh, vouching for the repo with --repo
# and passing the operator's flags through unchanged.
new_env; register_repo
cat > "$REPO/scripts/uninstall.sh" <<'PROBE'
#!/usr/bin/env bash
printf 'UNINSTALL-ARGS: %s\n' "$*"
PROBE
chmod +x "$REPO/scripts/uninstall.sh"
out="$(run_cli uninstall --dry-run --tier 1)"; rc=$?
if has "$out" "UNINSTALL-ARGS: --repo $REPO --dry-run --tier 1"; then
  pass "uninstall_dispatches_to_script: CLI execs scripts/uninstall.sh --repo \$REPO with the flags"
else
  check_fail "uninstall_dispatches_to_script: <<<$out>>>"
fi

# status happy path against stubbed `docker compose ps`.
new_env; register_repo
out="$(run_cli status)"; rc=$?
if [ "$rc" -eq 0 ] && has "$out" 'dashboard'; then
  pass "status_happy_path: renders the compose ps table, exit 0"
else
  check_fail "status_happy_path: rc=$rc out=$out"
fi

# doctor warns on a GPU overlay with no DRI render node, exit unchanged.
new_env; register_repo
printf 'JARVIS_VERSION=1.1.2\nTORCH_VARIANT=cpu\nCOMPOSE_FILE=docker-compose.yml:docker-compose.vulkan.yml\n' > "$REPO/.env"
EMPTY_DRI="$ROOT/dri.$RANDOM"; mkdir -p "$EMPTY_DRI"
out="$( run_cli_dri() { env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
  "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
  "JARVIS_DRI_DIR=$EMPTY_DRI" \
  bash "$REPO/scripts/jarvis-research.sh" --repo "$REPO" doctor </dev/null 2>&1; }; run_cli_dri )"; rc=$?
if has "$out" 'render node\|/dev/dri\|render' && [ "$rc" -ne 2 ]; then
  pass "doctor_warns_overlay_without_dri: render-node WARN emitted, exit unchanged"
else
  check_fail "doctor_warns_overlay_without_dri: rc=$rc out=<<<$out>>>"
fi

# rollback honesty: a migration-bearing update that fails health prints BOTH the
# image-pin rollback AND the not-schema-safe warning + restore pointer.
new_env; register_repo; STUB_HEALTH="unhealthy"
STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
STUB_MIG_CONTENT="DELETE FROM telegram_user_pairings WHERE chat_id < 0;"
seed_fresh_backup "$BK" good
out="$(run_cli update --yes)"; rc=$?
unset STUB_HEALTH
if has "$out" 'JARVIS_VERSION=' && has "$out" 'schema' && has "$out" 'restore'; then
  pass "rollback_honesty: epilogue has image-pin rollback + not-schema-safe + restore pointer"
else
  check_fail "rollback_honesty: rc=$rc out=<<<$out>>>"
fi

# =============================================================================
if [ "$fail" -ne 0 ]; then
  printf '\njarvis-research CLI: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\njarvis-research CLI: all %s checks passed\n' "$pass_n"
