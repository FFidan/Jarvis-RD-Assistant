#!/usr/bin/env bash
# test_uninstall.sh — behavioral tests for scripts/uninstall.sh, the tiered,
# contained teardown of a managed JARVIS install. No docker daemon, network, or
# real git repo is needed: docker (and `docker compose`) are stubbed on a private
# PATH that LOGS every invocation (the pattern from test_update_coverage.sh /
# test_jarvis_research_cli.sh), and the script runs against throwaway fixture
# clones under an isolated HOME.
#
# The containment matrix is the specification: every refusal must exit non-zero
# and leave the docker stub log free of a mutating verb (down/rmi), the on-disk
# secrets/ untouched, and the destructive typed gates (data project name, purge
# phrase, per-image confirmations) must stay mandatory even under --yes/--all.
#
# Run: bash scripts/tests/test_uninstall.sh   (exit 0 = pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UNINSTALL="${REPO_ROOT}/scripts/uninstall.sh"
LIB="${REPO_ROOT}/scripts/setup_lib.sh"
PURGE_PHRASE="I-UNDERSTAND-BACKUPS-BECOME-UNRECOVERABLE"

fail=0
pass_n=0
pass() { pass_n=$((pass_n + 1)); printf 'PASS: %s\n' "$1"; }
check_fail() { printf 'FAIL: %s\n' "$1" >&2; fail=1; }
has()  { printf '%s' "$1" | grep -q -- "$2"; }
want() { if has "$1" "$2"; then pass "$3"; else check_fail "$3 :: missing /$2/ in <<<$1>>>"; fi; }
lack() { if has "$1" "$2"; then check_fail "$3 :: unexpected /$2/ in <<<$1>>>"; else pass "$3"; fi; }

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT" 2>/dev/null || true' EXIT
STUB="$ROOT/stub"
mkdir -p "$STUB"
HOMEDIR="$ROOT/home"          # isolated HOME (its parent, $ROOT, is an ancestor)
mkdir -p "$HOMEDIR"

# =============================================================================
# docker stub: logs every mutating call to $DOCKER_LOG. `info` reports the daemon
# reachable unless STUB_NO_DAEMON=1; `compose ps -q` reports the stack down.
# =============================================================================
cat > "$STUB/docker" <<'DOCKER'
#!/usr/bin/env bash
log() { [ -n "${DOCKER_LOG:-}" ] && printf 'docker %s\n' "$*" >> "$DOCKER_LOG"; }
case "${1:-}" in
  info) [ "${STUB_NO_DAEMON:-0}" = 1 ] && exit 1; exit 0 ;;
  rmi)
    log "$*"
    # STUB_FAIL_RMI=<ref> makes rmi of exactly that ref fail (image absent).
    if [ -n "${STUB_FAIL_RMI:-}" ]; then
      for a in "$@"; do [ "$a" = "${STUB_FAIL_RMI}" ] && exit 1; done
    fi
    exit 0 ;;
  compose)
    shift
    case "${1:-}" in
      ps) exit 0 ;;                 # -q with no running service -> stack down
      down) log "compose $*"; exit 0 ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
DOCKER
chmod +x "$STUB/docker"

# =============================================================================
# Fixture clone builder: a valid JARVIS install with the real volumes/networks
# blocks, image pins, runtime files, and a backup encryption key.
# =============================================================================
make_clone() {
  local dir="$1"
  mkdir -p "$dir/scripts" "$dir/secrets" "$dir/shared"
  ln -sf "$LIB" "$dir/scripts/setup_lib.sh"
  cat > "$dir/versions.env" <<'VE'
POSTGRES_IMAGE=postgres:16.8
OLLAMA_IMAGE=ollama/ollama:0.31.2
QDRANT_IMAGE=qdrant/qdrant:v1.13.2
CADDY_IMAGE=caddy:2.9-alpine
VE
  cat > "$dir/docker-compose.yml" <<'YML'
services:
  dashboard:
    image: x
volumes:
  postgres_data:
  ollama_data:
  qdrant_data:
  postgres_backups:
  backup_trigger:
  restore_staging:  # staged snapshots
  restore_inbox:  # cross-host DR drop zone
  hf_cache:
  caddy_data:
  caddy_config:
  langfuse_postgres_data:
  vector_data:
networks:
  jarvis:
    driver: bridge
YML
  printf 'JARVIS_VERSION=1.1.3\nTORCH_VARIANT=cuda\nTORCH_VARIANT_SUFFIX=-cuda\n' > "$dir/.env"
  printf 'SUPERSECRETKEY\n' > "$dir/secrets/backup_encrypt_key.txt"
  printf 'shared-data\n' > "$dir/shared/marker.txt"
}

# The application image refs uninstall.sh must derive from this fixture (.env
# pins JARVIS_VERSION=1.1.3, TORCH_VARIANT_SUFFIX=-cuda; no telegram token).
NS="ghcr.io/limitcycle-oss/jarvis-"
APP_REFS=(
  "${NS}paper-ingestion:1.1.3-cuda"
  "${NS}learning-engine:1.1.3"
  "${NS}dashboard:1.1.3"
  "${NS}restore-uploader:1.1.3"
)
TP_REFS=(postgres:16.8 ollama/ollama:0.31.2 qdrant/qdrant:v1.13.2 caddy:2.9-alpine)

# new_env — a fresh fixture clone, state dir, bin dir, and stub log.
new_env() {
  CLONE="$ROOT/clone.$RANDOM.$RANDOM"
  CFG="$ROOT/cfg.$RANDOM.$RANDOM"
  BIN="$ROOT/bin.$RANDOM.$RANDOM"
  mkdir -p "$CFG" "$BIN"
  make_clone "$CLONE"
  DOCKER_LOG="$ROOT/dlog.$RANDOM"
  : > "$DOCKER_LOG"
}

# run_un [--stdin <data>] <args...> — invoke uninstall.sh with the stub PATH and
# the fixture env overrides. Default feeds an empty stdin (closed).
run_un() {
  local stdin_data=""
  if [ "${1:-}" = "--stdin" ]; then stdin_data="$2"; shift 2; fi
  env "PATH=$STUB:$PATH" "DOCKER_LOG=$DOCKER_LOG" "STUB_NO_DAEMON=${STUB_NO_DAEMON:-0}" \
    "STUB_FAIL_RMI=${STUB_FAIL_RMI:-}" \
    "HOME=$HOMEDIR" "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$BIN" \
    bash "$UNINSTALL" "$@" <<<"$stdin_data" 2>&1
}

log_has()  { grep -q -- "$1" "$DOCKER_LOG" 2>/dev/null; }
mutation_log_empty() {  # $1 = description
  if grep -qE 'compose down|rmi ' "$DOCKER_LOG" 2>/dev/null; then
    check_fail "$1 :: mutation in docker log: $(tr '\n' ';' < "$DOCKER_LOG")"
  else
    pass "$1"
  fi
}

# =============================================================================
# 1. Mechanical: banned bulk-prune primitives never appear in the script text.
# =============================================================================
if grep -qE '(system|image|volume|network|builder) prune' "$UNINSTALL"; then
  check_fail "banned_primitives_absent: a bulk 'prune' primitive is present in the script"
else
  pass "banned_primitives_absent: no system/image/volume/network/builder prune in the script"
fi

# =============================================================================
# 2. Containment: dangerous / unmanaged targets refuse before any mutation.
# =============================================================================
new_env
out="$(run_un --repo / --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ]; then pass "refuses_root: filesystem root -> nonzero exit"; else check_fail "refuses_root: rc=$rc out=$out"; fi
mutation_log_empty "refuses_root: empty mutation log"

new_env
out="$(run_un --repo "$HOMEDIR" --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ]; then pass "refuses_home: \$HOME -> nonzero exit"; else check_fail "refuses_home: rc=$rc out=$out"; fi
mutation_log_empty "refuses_home: empty mutation log"

new_env
out="$(run_un --repo "$ROOT" --tier 4 --yes)"; rc=$?   # $ROOT is an ancestor of HOME
if [ "$rc" -ne 0 ]; then pass "refuses_ancestor_of_home: ancestor path -> nonzero exit"; else check_fail "refuses_ancestor_of_home: rc=$rc out=$out"; fi
mutation_log_empty "refuses_ancestor_of_home: empty mutation log"

# symlink is canonicalized BEFORE the identity check; the refusal names the
# canonical (resolved) path, not the link.
new_env
mkdir -p "$ROOT/nonproject"
ln -sf "$ROOT/nonproject" "$ROOT/aliaslink"
out="$(run_un --repo "$ROOT/aliaslink" --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && has "$out" "$ROOT/nonproject"; then
  pass "resolves_symlink_before_identity_check: refusal names the canonical path"
else
  check_fail "resolves_symlink_before_identity_check: rc=$rc out=<<<$out>>>"
fi
mutation_log_empty "resolves_symlink_before_identity_check: empty mutation log"

new_env
mkdir -p "$ROOT/plaindir"
out="$(run_un --repo "$ROOT/plaindir" --tier 2 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && has "$out" 'not a JARVIS install'; then
  pass "refuses_unmanaged_dir: a non-JARVIS directory -> refusal"
else
  check_fail "refuses_unmanaged_dir: rc=$rc out=<<<$out>>>"
fi
mutation_log_empty "refuses_unmanaged_dir: empty mutation log"

# =============================================================================
# 3. Docker daemon absent: all tiers refuse (exit 3) + print an orphan inventory.
# =============================================================================
new_env; STUB_NO_DAEMON=1
out="$(run_un --repo "$CLONE" --tier 2 --yes)"; rc=$?
STUB_NO_DAEMON=0
if [ "$rc" -eq 3 ] && has "$out" 'postgres_data' && has "$out" 'compose project'; then
  pass "docker_absent_all_tiers_refuse_exit3_with_inventory: exit 3 + orphan inventory"
else
  check_fail "docker_absent_all_tiers_refuse_exit3_with_inventory: rc=$rc out=<<<$out>>>"
fi
mutation_log_empty "docker_absent_all_tiers_refuse_exit3_with_inventory: empty mutation log"

# =============================================================================
# 4. Dry run mutates nothing, and enumerates the same set it would remove.
# =============================================================================
new_env
out="$(run_un --repo "$CLONE" --dry-run --tier 4 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && has "$out" '^PLAN ' && [ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && [ -f "$CLONE/.env" ]; then
  pass "dry_run_mutates_nothing: PLAN lines printed, secrets/ and .env intact"
else
  check_fail "dry_run_mutates_nothing: rc=$rc secrets=$([ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && echo yes) out=<<<$out>>>"
fi
mutation_log_empty "dry_run_mutates_nothing: empty docker mutation log"

# dry-run parity at tier 2 (no conditional gates): PLAN set == real DONE set.
new_env
plan="$(run_un --repo "$CLONE" --dry-run --tier 2 --yes | grep '^PLAN ' | sed 's/^PLAN //' | sort)"
new_env
done_set="$(run_un --repo "$CLONE" --tier 2 --yes | grep '^DONE ' | sed 's/^DONE //' | sort)"
if [ -n "$plan" ] && [ "$plan" = "$done_set" ]; then
  pass "dry_run_parity: tier-2 enumeration equals the real mutation set"
else
  check_fail "dry_run_parity: plan=<<<$plan>>> done=<<<$done_set>>>"
fi

# =============================================================================
# 5. Tier semantics.
# =============================================================================
new_env
out="$(run_un --repo "$CLONE" --tier 1 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && log_has 'compose down' && ! log_has 'rmi ' && ! log_has '--volumes'; then
  pass "tier1_only_compose_down: containers/network only, no image or volume removal"
else
  check_fail "tier1_only_compose_down: rc=$rc log=$(cat "$DOCKER_LOG")"
fi

new_env
run_un --repo "$CLONE" --tier 2 --yes >/dev/null; rc=$?
rmi_lines="$(grep 'rmi ' "$DOCKER_LOG" 2>/dev/null)"
ok_all=1
for r in "${APP_REFS[@]}"; do log_has "rmi -f $r" || ok_all=0; done
n_rmi="$(printf '%s\n' "$rmi_lines" | grep -c 'rmi ')"
if [ "$ok_all" -eq 1 ] && [ "$n_rmi" -eq "${#APP_REFS[@]}" ] \
   && ! printf '%s' "$rmi_lines" | grep -qE 'postgres|ollama|qdrant|caddy'; then
  pass "tier2_removes_exactly_the_ghcr_images: the four app refs and nothing else"
else
  check_fail "tier2_removes_exactly_the_ghcr_images: n=$n_rmi lines=<<<$rmi_lines>>>"
fi

# tier 3 requires typing the compose project name; a wrong name refuses.
new_env
out="$(run_un --stdin 'wrong-name' --repo "$CLONE" --tier 3 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && ! log_has '--volumes'; then
  pass "tier3_requires_typed_project_name: wrong name -> refuse, no --volumes"
else
  check_fail "tier3_requires_typed_project_name: rc=$rc log=$(cat "$DOCKER_LOG")"
fi
# correct name proceeds to a volume-removing down.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
out="$(run_un --stdin "$proj" --repo "$CLONE" --tier 3 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && log_has 'compose down' && log_has '--volumes'; then
  pass "tier3_correct_name_removes_volumes: typed project name -> down --volumes"
else
  check_fail "tier3_correct_name_removes_volumes: rc=$rc log=$(cat "$DOCKER_LOG")"
fi

# --keep-data caps at tier 2 even when tier 4 is requested.
new_env
out="$(run_un --repo "$CLONE" --tier 4 --keep-data --yes)"; rc=$?
if [ "$rc" -eq 0 ] && ! log_has '--volumes' && log_has "rmi -f ${APP_REFS[0]}" \
   && [ -d "$CLONE/secrets" ] && [ -f "$CLONE/.env" ]; then
  pass "keep_data_caps_at_tier2: no volume/purge action, data + files intact"
else
  check_fail "keep_data_caps_at_tier2: rc=$rc secrets=$([ -d "$CLONE/secrets" ] && echo yes) log=$(cat "$DOCKER_LOG")"
fi

# --yes without an explicit --tier is a usage error.
new_env
out="$(run_un --repo "$CLONE" --yes)"; rc=$?
if [ "$rc" -eq 2 ]; then pass "yes_requires_tier: --yes with no --tier -> usage exit 2"; else check_fail "yes_requires_tier: rc=$rc out=$out"; fi
mutation_log_empty "yes_requires_tier: empty mutation log"

# =============================================================================
# 6. Purge-tier destructive gates (mandatory even under --yes/--all).
# =============================================================================
# no export path + wrong phrase -> refuse, secrets/ untouched.
new_env
out="$(run_un --stdin "$(printf '%s\n\nnope' "$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')")" \
  --repo "$CLONE" --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && [ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && ! log_has 'rmi -f postgres'; then
  pass "purge_requires_key_export_or_typed_phrase: declined export + wrong phrase -> refuse, secrets intact"
else
  check_fail "purge_requires_key_export_or_typed_phrase(refuse): rc=$rc secrets=$([ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && echo yes)"
fi

# export path given -> the key copy PRECEDES any file removal in the action log.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORT="$ROOT/exported.$RANDOM.key"
# stdin: tier-3 project name, key export path, then Y to every third-party image.
out="$(run_un --stdin "$(printf '%s\n%s\ny\ny\ny\ny' "$proj" "$EXPORT")" --repo "$CLONE" --tier 4 --yes)"; rc=$?
key_idx="$(printf '%s\n' "$out" | grep -n 'DONE key-export' | head -1 | cut -d: -f1)"
rm_idx="$(printf '%s\n' "$out" | grep -n 'DONE file ' | head -1 | cut -d: -f1)"
if [ -f "$EXPORT" ] && [ -n "$key_idx" ] && [ -n "$rm_idx" ] && [ "$key_idx" -lt "$rm_idx" ]; then
  pass "purge_key_export_precedes_rm: key copied out before any file removal"
else
  check_fail "purge_key_export_precedes_rm: export=$([ -f "$EXPORT" ] && echo yes) key_idx=$key_idx rm_idx=$rm_idx out=<<<$out>>>"
fi

# export path inside the clone (or secrets/) is refused; no key copy lands there.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
INSIDE="$CLONE/secrets/exported.key"
out="$(run_un --stdin "$(printf '%s\n%s' "$proj" "$INSIDE")" --repo "$CLONE" --tier 4 --yes)"; rc=$?
if [ "$rc" -ne 0 ] && [ ! -f "$INSIDE" ] && has "$out" 'inside the clone'; then
  pass "purge_key_export_refuses_in_clone_path: in-clone export path re-prompted/refused, no copy there"
else
  check_fail "purge_key_export_refuses_in_clone_path: rc=$rc inside=$([ -f "$INSIDE" ] && echo yes) out=<<<$out>>>"
fi

# --all with a closed stdin cannot satisfy the typed purge gate -> refuse.
new_env
out="$(run_un --repo "$CLONE" --all)"; rc=$?   # empty stdin
if [ "$rc" -ne 0 ] && [ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && ! log_has 'rmi -f postgres'; then
  pass "yes_all_still_requires_typed_purge_confirmation: closed stdin -> refuse, secrets intact"
else
  check_fail "yes_all_still_requires_typed_purge_confirmation: rc=$rc secrets=$([ -f "$CLONE/secrets/backup_encrypt_key.txt" ] && echo yes)"
fi

# --all never auto-confirms the per-image third-party removals: with the typed
# gates satisfied but the confirms hitting EOF, no third-party image is removed.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORT2="$ROOT/exported2.$RANDOM.key"
out="$(run_un --stdin "$(printf '%s\n%s' "$proj" "$EXPORT2")" --repo "$CLONE" --all)"; rc=$?
if ! printf '%s\n' "$(cat "$DOCKER_LOG")" | grep -qE 'rmi -f (postgres|ollama|qdrant|caddy)' \
   && has "$out" 'Keeping shared third-party'; then
  pass "yes_all_never_skips_third_party_image_confirms: unconfirmed third-party images kept, named"
else
  check_fail "yes_all_never_skips_third_party_image_confirms: log=$(grep rmi "$DOCKER_LOG"); out=<<<$out>>>"
fi

# a missing image during rmi (partial prior run, or a declared-but-never-pulled
# variant) must be skipped, not abort teardown after the volumes are already gone.
new_env
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORTF="$ROOT/exportfail.$RANDOM.key"
out="$(STUB_FAIL_RMI="${APP_REFS[0]}" run_un --stdin "$(printf '%s\n%s\ny\ny\ny\ny' "$proj" "$EXPORTF")" --repo "$CLONE" --tier 4 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && has "$out" "not present, skipping: ${APP_REFS[0]}" && [ ! -d "$CLONE" ]; then
  pass "rmi_missing_image_completes_teardown: absent image skipped, teardown finishes (clone removed), exit 0"
else
  check_fail "rmi_missing_image_completes_teardown: rc=$rc clone=$([ -d "$CLONE" ] && echo present) out=<<<$out>>>"
fi

# =============================================================================
# 7. Registry line + shim removed only when this is the last install.
# =============================================================================
# last install: installs holds only this clone -> line removed AND shim removed.
new_env
CANON="$(realpath "$CLONE")"
printf '%s\n' "$CANON" > "$CFG/installs"
printf '#shim\n' > "$BIN/jarvis-research"; chmod +x "$BIN/jarvis-research"
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORT3="$ROOT/exported3.$RANDOM.key"
run_un --stdin "$(printf '%s\n%s\nn\nn\nn\nn' "$proj" "$EXPORT3")" --repo "$CLONE" --tier 4 --yes >/dev/null 2>&1
if ! grep -qxF "$CANON" "$CFG/installs" 2>/dev/null && [ ! -e "$BIN/jarvis-research" ]; then
  pass "tier4_removes_state_line_and_shim_when_last: registry line gone, shim gone"
else
  check_fail "tier4_removes_state_line_and_shim_when_last: installs=$(cat "$CFG/installs" 2>/dev/null) shim=$([ -e "$BIN/jarvis-research" ] && echo present)"
fi

# not last: another install remains -> line removed but the shim stays.
new_env
CANON="$(realpath "$CLONE")"
OTHER="$ROOT/otherinstall.$RANDOM"; mkdir -p "$OTHER"
printf '%s\n%s\n' "$CANON" "$OTHER" > "$CFG/installs"
printf '#shim\n' > "$BIN/jarvis-research"; chmod +x "$BIN/jarvis-research"
proj="$(basename "$CLONE" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
EXPORT4="$ROOT/exported4.$RANDOM.key"
run_un --stdin "$(printf '%s\n%s\nn\nn\nn\nn' "$proj" "$EXPORT4")" --repo "$CLONE" --tier 4 --yes >/dev/null 2>&1
if ! grep -qxF "$CANON" "$CFG/installs" 2>/dev/null && grep -qxF "$OTHER" "$CFG/installs" 2>/dev/null \
   && [ -e "$BIN/jarvis-research" ]; then
  pass "tier4_keeps_shim_when_another_install_remains: line removed, other kept, shim retained"
else
  check_fail "tier4_keeps_shim_when_another_install_remains: installs=$(cat "$CFG/installs" 2>/dev/null) shim=$([ -e "$BIN/jarvis-research" ] && echo present)"
fi

# =============================================================================
if [ "$fail" -ne 0 ]; then
  printf '\nuninstall: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nuninstall: all %s checks passed\n' "$pass_n"
