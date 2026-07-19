#!/usr/bin/env bash
# test_update_coverage.sh — behavioral tests for update.sh and the shared release
# helpers in scripts/setup_lib.sh. No docker daemon or network is needed: docker
# and git are stubbed on a private PATH (the pattern established by
# scripts/tests/test_setup_lib_helpers.sh's fake_docker), and update.sh itself is
# run against a throwaway fixture repo whose versions.env pins differ from what
# the docker stub reports running.
#
# Coverage:
#   * print_split_recovery prints only the half (versions.env vs JARVIS_VERSION)
#     that matches the failed set, and never names a third-party pin in the
#     JARVIS_VERSION rollback line;
#   * update.sh --yes runs promptless; all image pulls complete before any
#     container is recreated; a pull failure prints the split recovery and exits
#     1 with nothing recreated; a no-healthcheck service is reported, not silently
#     counted as verified;
#   * latest_stable_tag excludes pre-releases and sorts versionally;
#   * install_cli_shim is idempotent and prepend-dedups the installs registry;
#   * verify_release_manifests strips the tag's v-prefix from image refs, passes
#     only when every inspected image is present, and skips local-build images.
#
# Run: bash scripts/tests/test_update_coverage.sh   (exit 0 = pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
UPDATE_SCRIPT="${REPO_ROOT}/update.sh"
LIB="${REPO_ROOT}/scripts/setup_lib.sh"

fail=0
pass_n=0
pass() { pass_n=$((pass_n + 1)); printf 'PASS: %s\n' "$1"; }
check_fail() { printf 'FAIL: %s\n' "$1" >&2; fail=1; }

# The shared helpers under test, plus _env_key_in_list which print_split_recovery
# consumes to classify a failed service.
# shellcheck source=../setup_lib.sh
# shellcheck disable=SC1091
source "$LIB"

# =============================================================================
# print_split_recovery — extracted from update.sh and evaluated here, so the one
# implementation is what is tested (a private copy would drift).
# =============================================================================
# C_BOLD/C_RESET are consumed by the eval'd print_split_recovery, invisibly to shellcheck.
# shellcheck disable=SC2034
C_BOLD=""
# shellcheck disable=SC2034
C_RESET=""
psr_src="$(sed -n '/^print_split_recovery() {/,/^}/p' "$UPDATE_SCRIPT")"
if [ -z "$psr_src" ]; then
  printf 'FAIL: could not sed-extract print_split_recovery from %s\n' "$UPDATE_SCRIPT" >&2
  exit 1
fi
eval "$psr_src"

# The full third-party set update.sh builds for a default run (the always-on
# services plus the DR backup sidecar).
TP_SET="postgres ollama qdrant litellm cloudflared postgres-backup"
run_recovery() { THIRD_PARTY_SET="$TP_SET" print_split_recovery "$@"; }
has()  { printf '%s' "$1" | grep -q "$2"; }
want() { if has "$1" "$2"; then pass "$3"; else check_fail "$3 ($1)"; fi; }
lack() { if has "$1" "$2"; then check_fail "$3 ($1)"; else pass "$3"; fi; }

# --- app-only: FAILED=(dashboard) --------------------------------------------
out="$(run_recovery dashboard)"
lack "$out" 'Third-party services' "app-only: no third-party/versions.env block"
want "$out" 'Application services'  "app-only: prints the application/JARVIS_VERSION block"
want "$out" 'JARVIS_VERSION=<previous-version> docker compose pull dashboard' \
  "app-only: JARVIS_VERSION line names the app service"
want "$out" 'docker compose logs --tail=200 dashboard' "app-only: trailing logs line lists the full set"

# --- third-party-only: FAILED=(postgres) -------------------------------------
out="$(run_recovery postgres)"
want "$out" 'Third-party services' "third-party-only: prints the third-party/versions.env block"
lack "$out" 'Application services' "third-party-only: no application/JARVIS_VERSION block"
lack "$out" 'JARVIS_VERSION='      "third-party-only: no JARVIS_VERSION line at all"

# --- reconciled third-party (postgres-backup) classifies as third-party -------
out="$(run_recovery postgres-backup)"
if has "$out" 'Third-party services' && ! has "$out" 'Application services'; then
  pass "postgres-backup classifies as third-party, not application"
else
  check_fail "postgres-backup misclassified ($out)"
fi

# --- mixed: FAILED=(dashboard postgres) --------------------------------------
out="$(run_recovery dashboard postgres)"
if has "$out" 'Third-party services' && has "$out" 'Application services'; then
  pass "mixed: prints both blocks"
else
  check_fail "mixed: missing one block ($out)"
fi
version_lines="$(printf '%s\n' "$out" | grep 'JARVIS_VERSION=')"
if [ -n "$version_lines" ] && ! printf '%s\n' "$version_lines" \
    | grep -qE 'postgres|ollama|qdrant|litellm|cloudflared'; then
  pass "mixed: JARVIS_VERSION lines never name a third-party service"
else
  check_fail "mixed: JARVIS_VERSION line named a third-party service ($version_lines)"
fi
want "$out" 'docker compose logs --tail=200 dashboard postgres' \
  "mixed: trailing logs line lists the full (both) failed set"

# fail_with_recovery exits 1 after printing the split recovery.
fwr_src="$(sed -n '/^fail_with_recovery() {/,/^}/p' "$UPDATE_SCRIPT")"
# shellcheck disable=SC2329  # err is called indirectly by the eval'd fail_with_recovery
( eval "$fwr_src"; err() { :; }; THIRD_PARTY_SET="$TP_SET" \
    fail_with_recovery "boom" "hint" dashboard >/dev/null 2>&1 ); rc=$?
if [ "$rc" -eq 1 ]; then pass "fail_with_recovery exits 1"; else check_fail "fail_with_recovery did not exit 1 (rc=$rc)"; fi

# =============================================================================
# update.sh full-run harness (stubbed docker + throwaway fixture repo).
# =============================================================================
FX="$(mktemp -d)"
STUB="$(mktemp -d)"
trap 'rm -rf "$FX" "$STUB"' EXIT

ln -s "$UPDATE_SCRIPT" "$FX/update.sh"
mkdir -p "$FX/scripts"
ln -s "$LIB" "$FX/scripts/setup_lib.sh"
# Pins deliberately unequal to the stub's reported running image, so every base
# third-party service diffs as "update available".
cat > "$FX/versions.env" <<'VERS'
POSTGRES_IMAGE=postgres:test-new
OLLAMA_IMAGE=ollama/ollama:test-new
QDRANT_IMAGE=qdrant/qdrant:test-new
LITELLM_IMAGE=litellm:test-new
CLOUDFLARED_IMAGE=cloudflare/cloudflared:test-new
CADDY_IMAGE=caddy:test-new
VECTOR_IMAGE=timberio/vector:test-new
LANGFUSE_POSTGRES_IMAGE=postgres:test-new-alpine
VERS
# TORCH_VARIANT present so the backfill is a no-op; no telegram token.
printf 'TORCH_VARIANT=cpu\nTORCH_VARIANT_SUFFIX=\n' > "$FX/.env"

# docker stub: logs pull/up/build to $DOCKER_LOG; reports the base third-party
# and application services as running with a stale image; optional services are
# absent (not deployed). STUB_HEALTH / STUB_RUN_STATE / STUB_FAIL_PULL tune it.
cat > "$STUB/docker" <<'DOCKER'
#!/usr/bin/env bash
log() { printf '%s\n' "$*" >> "$DOCKER_LOG"; }
running_cid() {
  case "$1" in
    postgres|ollama|qdrant|litellm|cloudflared|postgres-backup) printf 'cid-%s\n' "$1" ;;
    paper_ingestion|learning_engine|dashboard|restore-uploader|telegram_bot) printf 'cid-%s\n' "$1" ;;
    *) : ;;
  esac
}
if [ "${1:-}" = "inspect" ]; then
  shift; fmt=""
  while [ $# -gt 0 ]; do
    case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac
  done
  case "$fmt" in
    *Config.Image*) printf 'oldimage:running\n' ;;
    *State.Health*) printf '%s\n' "${STUB_HEALTH-healthy}" ;;
    *State.Status*) printf '%s\n' "${STUB_RUN_STATE-running}" ;;
  esac
  exit 0
fi
if [ "${1:-}" = "compose" ]; then
  shift; args=()
  while [ $# -gt 0 ]; do
    case "$1" in --profile) shift 2 ;; *) args+=("$1"); shift ;; esac
  done
  set -- "${args[@]:-}"
  case "${1:-}" in
    version) exit 0 ;;
    ps)      running_cid "${3:-}"; exit 0 ;;
    pull)    log "pull ${*:2}"; [ "${STUB_FAIL_PULL:-0}" = 1 ] && exit 1; exit 0 ;;
    up)      log "up ${*:2}"; exit 0 ;;
    build)   log "build ${*:2}"; exit 0 ;;
    *)       exit 0 ;;
  esac
fi
exit 0
DOCKER
chmod +x "$STUB/docker"

run_update() {
  : > "$FX/docker.log"
  mkdir -p "$FX/home"
  DOCKER_LOG="$FX/docker.log" HOME="$FX/home" XDG_CONFIG_HOME="$FX/home/.config" \
    PATH="$STUB:$PATH" bash "$FX/update.sh" "$@" </dev/null 2>&1
}

# --- update_yes_runs_promptless ----------------------------------------------
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] && grep -q '^pull ' "$FX/docker.log"; then
  pass "update_yes_runs_promptless: --yes proceeds past prompts on closed stdin"
else
  check_fail "update_yes_runs_promptless: rc=$rc, log=$(cat "$FX/docker.log")"
fi
# Contrast: without --yes, a closed stdin answers no -> nothing pulled.
noyes_log="$FX/docker.log"; out="$(run_update)"; rc=$?
if [ "$rc" -eq 0 ] && ! grep -q '^pull ' "$noyes_log"; then
  pass "no --yes on closed stdin: nothing is pulled (prompts default to no)"
else
  check_fail "no-yes contrast: rc=$rc, log=$(cat "$noyes_log")"
fi

# --- pulls_complete_before_any_recreate --------------------------------------
out="$(run_update --yes)"
last_pull="$(grep -n '^pull ' "$FX/docker.log" | tail -1 | cut -d: -f1)"
first_up="$(grep -n '^up '   "$FX/docker.log" | head -1 | cut -d: -f1)"
if [ -n "$last_pull" ] && [ -n "$first_up" ] && [ "$last_pull" -lt "$first_up" ]; then
  pass "pulls_complete_before_any_recreate: last pull precedes first up"
else
  check_fail "pulls_complete_before_any_recreate: last_pull=$last_pull first_up=$first_up log=$(cat "$FX/docker.log")"
fi

# --- no_healthcheck_reported_not_silent --------------------------------------
out="$(STUB_HEALTH="" run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && printf '%s' "$out" | grep -q 'running (no healthcheck)' \
   && printf '%s' "$out" | grep -q 'not health-verified' \
   && ! printf '%s' "$out" | grep -q 'dashboard: healthy'; then
  pass "no_healthcheck_reported_not_silent: reported as running-not-verified, never healthy"
else
  check_fail "no_healthcheck_reported_not_silent: rc=$rc out=$out"
fi

# --- update_success_installs_shim --------------------------------------------
rm -rf "$FX/home"
out="$(run_update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && [ -x "$FX/home/.local/bin/jarvis-research" ] \
   && grep -qxF "$FX" "$FX/home/.config/jarvis-research/installs"; then
  pass "update_success_installs_shim: launcher installed and path registered on success"
else
  check_fail "update_success_installs_shim: rc=$rc shim=$(ls -l "$FX/home/.local/bin" 2>/dev/null) installs=$(cat "$FX/home/.config/jarvis-research/installs" 2>/dev/null)"
fi
# Failure runs must not install it.
rm -rf "$FX/home"
out="$(STUB_FAIL_PULL=1 run_update --yes)" || true
if [ ! -e "$FX/home/.local/bin/jarvis-research" ]; then
  pass "update_failure_skips_shim_install: failed update leaves no launcher behind"
else
  check_fail "update_failure_skips_shim_install: launcher appeared on a failed update"
fi

# --- die_paths_print_recovery (pull failure) ---------------------------------
out="$(STUB_FAIL_PULL=1 run_update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && printf '%s' "$out" | grep -q 'Recovery — the two image sets roll back differently' \
   && printf '%s' "$out" | grep -q 'Third-party services' \
   && ! grep -q '^up ' "$FX/docker.log"; then
  pass "die_paths_print_recovery: pull failure prints split recovery, exits 1, recreates nothing"
else
  check_fail "die_paths_print_recovery: rc=$rc out=$out log=$(cat "$FX/docker.log")"
fi

# =============================================================================
# Pure release helpers (source setup_lib.sh, stub git/docker on PATH).
# =============================================================================
mkstub() { mkdir -p "$1"; cat > "$1/$2"; chmod +x "$1/$2"; }

# --- latest_stable_tag: excludes pre-releases, sorts versionally -------------
GITSTUB="$(mktemp -d)"
mkstub "$GITSTUB" git <<'GIT'
#!/usr/bin/env bash
if [ "${1:-}" = "ls-remote" ]; then
  cat <<'TAGS'
sha refs/tags/v0.9.9
sha refs/tags/v1.9.0
sha refs/tags/v1.10.0
sha refs/tags/v1.10.0-rc2
sha refs/tags/v2.0.0-rc1
TAGS
  exit 0
fi
exit 0
GIT
got="$(PATH="$GITSTUB:$PATH" latest_stable_tag origin)"
if [ "$got" = "v1.10.0" ]; then
  pass "latest_stable_tag_sorts_versionally: v1.10.0 > v1.9.0"
else
  check_fail "latest_stable_tag_sorts_versionally: got '$got' (want v1.10.0)"
fi
case "$got" in
  *-rc*) check_fail "latest_stable_tag_excludes_rc: returned a pre-release '$got'" ;;
  *)     pass "latest_stable_tag_excludes_rc: no -rc tag selected" ;;
esac
rm -rf "$GITSTUB"

# --- install_cli_shim: idempotent + prepend-dedup ----------------------------
CLIROOT="$(mktemp -d)"
export JARVIS_CLI_BIN_DIR="$CLIROOT/bin"
export JARVIS_CLI_CONFIG_DIR="$CLIROOT/cfg"
repoA="$CLIROOT/repoA"; repoB="$CLIROOT/repoB"
out1="$(install_cli_shim "$repoA")"
out2="$(install_cli_shim "$repoA")"
shim="$JARVIS_CLI_BIN_DIR/jarvis-research"
installs="$JARVIS_CLI_CONFIG_DIR/installs"
if [ -n "$out1" ] && [ -z "$out2" ]; then
  pass "install_cli_shim_idempotent: prints on first install, silent on re-run"
else
  check_fail "install_cli_shim_idempotent: out1='$out1' out2='$out2'"
fi
if [ -x "$shim" ] && grep -q 'scripts/jarvis-research.sh' "$shim" && grep -q -- '--repo' "$shim"; then
  pass "install_cli_shim: launcher is executable and execs the repo CLI with --repo"
else
  check_fail "install_cli_shim: launcher missing/wrong"
fi
if [ "$(head -n 1 "$installs")" = "$repoA" ]; then
  pass "install_cli_shim: repo registered at the top of the registry"
else
  check_fail "install_cli_shim: registry top is '$(head -n 1 "$installs")'"
fi
install_cli_shim "$repoB" >/dev/null
if [ "$(head -n 1 "$installs")" = "$repoB" ] && grep -qxF "$repoA" "$installs" \
   && [ "$(grep -cxF "$repoA" "$installs")" = 1 ]; then
  pass "install_cli_shim: a second repo prepends and de-dups the registry"
else
  check_fail "install_cli_shim: dedup/prepend wrong ($(tr '\n' ',' < "$installs"))"
fi
unset JARVIS_CLI_BIN_DIR JARVIS_CLI_CONFIG_DIR
rm -rf "$CLIROOT"

# --- verify_release_manifests ------------------------------------------------
# git stub emits the target ref's versions.env; docker stub logs every inspected
# ref and passes/fails per MANIFEST_MISS.
VSTUB="$(mktemp -d)"
mkstub "$VSTUB" git <<'GIT'
#!/usr/bin/env bash
if [ "${1:-}" = "show" ]; then
  cat <<'VE'
POSTGRES_IMAGE=postgres:16.8@sha256:aaa
OLLAMA_IMAGE=ollama/ollama:0.31.2
QDRANT_IMAGE=qdrant/qdrant:v1.13.2
LITELLM_IMAGE=litellm@sha256:bbb
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2025.1.0
CADDY_IMAGE=caddy:2.9-alpine
VE
  exit 0
fi
exit 0
GIT
mkstub "$VSTUB" docker <<'DOCKER'
#!/usr/bin/env bash
# docker manifest inspect <ref>
if [ "${1:-}" = "manifest" ] && [ "${2:-}" = "inspect" ]; then
  ref="$3"
  [ -n "${MANIFEST_LOG:-}" ] && printf '%s\n' "$ref" >> "$MANIFEST_LOG"
  if [ -n "${MANIFEST_MISS:-}" ] && printf '%s' "$ref" | grep -q "$MANIFEST_MISS"; then
    exit 1
  fi
  exit 0
fi
exit 0
DOCKER
export TORCH_VARIANT_SUFFIX=""

# all present -> rc 0
out="$( PATH="$VSTUB:$PATH" MANIFEST_LOG="" verify_release_manifests v1.1.3 )"; rc=$?
if [ "$rc" -eq 0 ] \
   && printf '%s' "$out" | grep -q 'PRESENT ghcr.io/limitcycle-oss/jarvis-dashboard:1.1.3' \
   && ! printf '%s' "$out" | grep -q 'MISSING'; then
  pass "manifest_verify_all_present_rc0: every image present -> rc 0"
else
  check_fail "manifest_verify_all_present_rc0: rc=$rc out=$out"
fi

# one missing -> rc 1
out="$( PATH="$VSTUB:$PATH" MANIFEST_MISS='jarvis-dashboard:1.1.3' verify_release_manifests v1.1.3 )"; rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'MISSING ghcr.io/limitcycle-oss/jarvis-dashboard:1.1.3'; then
  pass "manifest_verify_missing_rc1: a missing image -> rc 1 + MISSING line"
else
  check_fail "manifest_verify_missing_rc1: rc=$rc out=$out"
fi

# app image refs never carry the tag's v-prefix
MLOG="$(mktemp)"
out="$( PATH="$VSTUB:$PATH" MANIFEST_LOG="$MLOG" verify_release_manifests v1.1.3 )"
if grep -q ':1\.1\.3' "$MLOG" && ! grep -q ':v1\.1\.3' "$MLOG"; then
  pass "manifest_names_never_carry_v_prefix: image tags are :1.1.3, never :v1.1.3"
else
  check_fail "manifest_names_never_carry_v_prefix: log=$(tr '\n' ' ' < "$MLOG")"
fi
rm -f "$MLOG"

# observability active: langfuse (local-build) skipped, gate still passes
MLOG="$(mktemp)"
out="$( PATH="$VSTUB:$PATH" MANIFEST_LOG="$MLOG" verify_release_manifests v1.1.3 observability )"; rc=$?
if [ "$rc" -eq 0 ] \
   && printf '%s' "$out" | grep -q 'SKIPPED jarvis/langfuse-hardened:1.1.3' \
   && ! grep -q 'langfuse-hardened' "$MLOG"; then
  pass "manifest_gate_skips_local_build_images: langfuse SKIPPED, never inspected, gate passes"
else
  check_fail "manifest_gate_skips_local_build_images: rc=$rc out=$out log=$(tr '\n' ' ' < "$MLOG")"
fi
rm -f "$MLOG"
unset TORCH_VARIANT_SUFFIX
rm -rf "$VSTUB"

# =============================================================================
if [ "$fail" -ne 0 ]; then
  printf '\nupdate.sh coverage: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nupdate.sh coverage: all %s checks passed\n' "$pass_n"
