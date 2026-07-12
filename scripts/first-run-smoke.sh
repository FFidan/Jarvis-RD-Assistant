#!/usr/bin/env bash
# scripts/first-run-smoke.sh — clean-machine first-run smoke test.
#
# Proves a brand-new user can go from a fresh checkout to a healthy stack by
# following the README's documented first-run command. Catches "works on my
# machine" / undocumented-manual-step regressions before public launch.
#
# It runs the EXACT documented non-interactive bootstrap from the README:
#
#     ./setup.sh --non-interactive --profile=dev   # local dev / CI smoke test
#
# (README "Non-interactive (CI / cloud-init)" section). setup.sh generates
# secrets, brings the stack up, and waits for every mandatory service to become
# healthy (postgres, ollama, litellm, paper_ingestion, learning_engine,
# dashboard); it exits non-zero if any service fails to become healthy. This
# smoke additionally verifies the dashboard responds on http://localhost:3001.
#
# ISOLATION (so this can NEVER wipe a real deployment):
#   * Runs under a dedicated compose project name (jarvis-firstrun-smoke) via
#     COMPOSE_PROJECT_NAME, which docker compose honours natively — setup.sh's
#     own `docker compose` calls and this script's teardown all target that
#     isolated project (separate containers, volumes, and network).
#   * Refuses to run if that smoke project already has containers/volumes
#     (unless --force), and refuses if a `.env` already exists in the repo
#     (the documented first run starts with NO .env, and this smoke regenerates
#     it). The real deployment's project + volumes are never touched.
#   * ALWAYS tears down on exit (trap): `docker compose -p ... down -v` plus
#     removal of the bootstrap-generated working-tree artifacts, leaving no
#     residue (intended for an ephemeral CI checkout).
#
# Usage:
#   bash scripts/first-run-smoke.sh [--force] [--timeout SECONDS] [--build-local]
#                                   [--integration] [--rerun] [--wrapper] [--help]
#
#   --force            Tear down a pre-existing smoke project before starting,
#                      instead of refusing. (Never affects the real deployment;
#                      only the isolated jarvis-firstrun-smoke project.)
#   --timeout SECONDS  Overall budget for `setup.sh` to finish (default 3600).
#                      The first run pulls 7-11 GB of model data, so a clean
#                      machine legitimately needs 20-60 min.
#   --build-local      Pass --build-local to setup.sh so the app images are
#                      BUILT from this checkout instead of pulled from GHCR —
#                      how CI proves the branch's own code boots (the pull
#                      path only exercises previously-published images).
#                      Incompatible with --wrapper: scripts/jarvis-setup.sh
#                      takes no bootstrap-mode flag, so there is nothing to
#                      forward to it.
#   --integration      After the stack is healthy, run the gated integration
#                      suite against it (sets SMOKE_INTEGRATION=1; requires uv).
#   --rerun            After the first bootstrap succeeds, run it again with
#                      the kept .env and assert the re-run also succeeds.
#   --wrapper          Bootstrap via scripts/jarvis-setup.sh instead of
#                      ./setup.sh --non-interactive --profile=dev.
#   --help             Show this help and exit.
set -euo pipefail

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
# Dedicated, isolated compose project — never collides with the real deployment.
readonly SMOKE_PROJECT="jarvis-firstrun-smoke"
export COMPOSE_PROJECT_NAME="$SMOKE_PROJECT"

# Isolation: distinct subnet + dashboard port so a smoke run never collides with
# a live deploy on the same host (which uses 10.137.241.0/24 and port 3001).
# Override via env before calling this script if the defaults conflict.
: "${JARVIS_NET_SUBNET:=10.137.242.0/24}"
: "${DASHBOARD_HOST_PORT:=13001}"
: "${LITELLM_HOST_PORT:=14000}"
: "${POSTGRES_HOST_PORT:=15432}"
: "${PAPER_INGESTION_HOST_PORT:=18010}"
: "${LEARNING_ENGINE_HOST_PORT:=18011}"
: "${QDRANT_HOST_PORT:=16333}"
: "${OLLAMA_HOST_PORT:=11444}"
export JARVIS_NET_SUBNET DASHBOARD_HOST_PORT LITELLM_HOST_PORT POSTGRES_HOST_PORT
export PAPER_INGESTION_HOST_PORT LEARNING_ENGINE_HOST_PORT QDRANT_HOST_PORT OLLAMA_HOST_PORT

readonly DASHBOARD_URL="http://localhost:${DASHBOARD_HOST_PORT}"

# Disk-budget ratchet: app images plus build cache this run may add, in SI GB.
# Keyed to the bootstrap mode so each is ratcheted at its OWN figure — budgeting the
# pull path at the build ceiling would let a pull-size regression of several GB pass
# unnoticed. Both mirror scripts/setup_lib.sh::_image_budget_gb (cpu-build 9 /
# cpu-pull 6); a pull acquires strictly less than a build, which also fills the
# build cache. Resolved after argument parsing, once the mode is known.
DISK_BUDGET_GB=6

FORCE=0
TIMEOUT_SECONDS=3600
BUILD_LOCAL=0
INTEGRATION=0
RERUN=0
WRAPPER=0

# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_RESET=""
fi
info() { printf '%s[INFO]%s  %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[ERROR]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }

show_help() {
  # Print the leading comment block (drop the shebang) as the help text.
  sed -n '2,/^set -euo/{ /^set -euo/d; s/^# \{0,1\}//; s/^#$//; p; }' "$0"
}

# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --force)   FORCE=1; shift ;;
    --timeout) TIMEOUT_SECONDS="$2"; shift 2 ;;
    --timeout=*) TIMEOUT_SECONDS="${1#*=}"; shift ;;
    --build-local) BUILD_LOCAL=1; shift ;;
    --integration) INTEGRATION=1; shift ;;
    --rerun)   RERUN=1; shift ;;
    --wrapper) WRAPPER=1; shift ;;
    -h|--help) show_help; exit 0 ;;
    *) err "Unknown argument: $1"; echo; show_help; exit 2 ;;
  esac
done
case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*) err "--timeout must be a positive integer (got: $TIMEOUT_SECONDS)"; exit 2 ;;
esac
if [ "$BUILD_LOCAL" -eq 1 ] && [ "$WRAPPER" -eq 1 ]; then
  err "--build-local cannot be combined with --wrapper: scripts/jarvis-setup.sh takes no"
  err "bootstrap-mode flag, so there is nothing to forward to it."
  exit 2
fi
# A --build-local run also fills the Docker build cache, so it is held to the larger
# cpu-build ceiling; the pull path keeps the tighter cpu-pull figure set above.
[ "$BUILD_LOCAL" -eq 1 ] && DISK_BUDGET_GB=9
readonly DISK_BUDGET_GB

# -----------------------------------------------------------------------------
# Resolve repo root (this script lives in <root>/scripts)
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

# Whether a .env already existed BEFORE this run — drives teardown (only remove
# the .env if WE generated it, never an operator's existing config).
ENV_PREEXISTED=0
[ -f "$REPO_ROOT/.env" ] && ENV_PREEXISTED=1

# -----------------------------------------------------------------------------
# Teardown — ALWAYS runs (trap EXIT). Leaves no residue.
# -----------------------------------------------------------------------------
teardown() {
  local rc=$?
  printf '\n'
  info "Teardown: removing the isolated '${SMOKE_PROJECT}' project (containers + volumes)..."
  # -v removes the named volumes for THIS project only; --remove-orphans clears
  # any one-off (ollama-bootstrap / *-db-init) containers. Scoped to the
  # isolated project via COMPOSE_PROJECT_NAME, so the real deployment is safe.
  docker compose -p "$SMOKE_PROJECT" down -v --remove-orphans 2>/dev/null || true

  # Remove bootstrap-generated working-tree artifacts so the checkout is clean.
  # Only delete the .env if THIS run created it (never an operator's existing one).
  if [ "$ENV_PREEXISTED" -eq 0 ]; then
    rm -f "$REPO_ROOT/.env" "$REPO_ROOT"/docker-compose.override.yml.bak.* 2>/dev/null || true
  fi
  ok "Teardown complete."

  if [ "$rc" -eq 0 ]; then
    printf '\n%s================ FIRST-RUN SMOKE: PASS ================%s\n' "$C_GREEN" "$C_RESET"
  else
    printf '\n%s================ FIRST-RUN SMOKE: FAIL ================%s\n' "$C_RED" "$C_RESET"
  fi
  exit "$rc"
}
trap teardown EXIT

# -----------------------------------------------------------------------------
# Preconditions (cold-box discipline: assert, fail fast, leave no residue)
# -----------------------------------------------------------------------------
info "Checking preconditions..."

command -v docker >/dev/null 2>&1 \
  || { err "docker not found — install Docker Engine 24+ with Compose v2."; exit 1; }
docker compose version >/dev/null 2>&1 \
  || { err "'docker compose' (v2 plugin) not found."; exit 1; }
docker info >/dev/null 2>&1 \
  || { err "Docker daemon unreachable ('docker info' failed)."; exit 1; }
[ -x "$REPO_ROOT/setup.sh" ] \
  || { err "setup.sh not found or not executable at repo root ($REPO_ROOT)."; exit 1; }
if [ "$WRAPPER" -eq 1 ]; then
  [ -f "$REPO_ROOT/scripts/jarvis-setup.sh" ] \
    || { err "--wrapper: scripts/jarvis-setup.sh not found."; exit 1; }
fi
if [ "$INTEGRATION" -eq 1 ]; then
  command -v uv >/dev/null 2>&1 \
    || { err "--integration requires uv (https://docs.astral.sh/uv/)."; exit 1; }
fi
ok "docker + compose present; setup.sh found."

# Guard: never silently clobber a real deployment. Refuse if the SMOKE project
# already has containers or volumes, unless --force tears them down first.
existing_containers="$(docker compose -p "$SMOKE_PROJECT" ps -aq 2>/dev/null || true)"
existing_volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=${SMOKE_PROJECT}" 2>/dev/null || true)"
if [ -n "$existing_containers" ] || [ -n "$existing_volumes" ]; then
  if [ "$FORCE" -eq 1 ]; then
    warn "Pre-existing '${SMOKE_PROJECT}' state found — removing it (--force)."
    docker compose -p "$SMOKE_PROJECT" down -v --remove-orphans 2>/dev/null || true
  else
    err "An isolated '${SMOKE_PROJECT}' project already exists (containers/volumes)."
    err "Re-run with --force to remove it first. (The real deployment is never touched.)"
    exit 1
  fi
fi

# Guard: the documented first run starts with NO .env. A pre-existing .env means
# this is not a clean machine; refuse rather than overwrite the operator's config.
if [ "$ENV_PREEXISTED" -eq 1 ]; then
  err "A .env already exists at $REPO_ROOT/.env — this is not a clean checkout."
  err "Run the first-run smoke on a fresh checkout (CI) where no .env is present."
  exit 1
fi
ok "Preconditions met — clean checkout, no conflicting deployment."

# -----------------------------------------------------------------------------
# Run the documented first-run bootstrap (README: "Non-interactive (CI/cloud-init)")
#   ./setup.sh --non-interactive --profile=dev   (or scripts/jarvis-setup.sh
#   with --wrapper)
# Under COMPOSE_PROJECT_NAME=jarvis-firstrun-smoke, so every container/volume
# the bootstrap creates lands in the isolated project.
# -----------------------------------------------------------------------------
BOOTSTRAP_CMD=(./setup.sh --non-interactive --profile=dev)
[ "$BUILD_LOCAL" -eq 1 ] && BOOTSTRAP_CMD+=(--build-local)
[ "$WRAPPER" -eq 1 ] && BOOTSTRAP_CMD=(bash scripts/jarvis-setup.sh)

# App images plus Docker build cache, in bytes — the disk the install acquires.
# Measured as a delta against this pre-run baseline so the ratchet stays honest on
# machines with unrelated images or cache. BOTH namespaces are counted: the
# published images now land under ghcr.io/limitcycle-oss/jarvis-*, and matching only
# the pre-1.1 `jarvis/*` names would silently measure zero app-image growth (the
# unpublished langfuse fork still uses `jarvis/*`).
smoke_footprint_bytes() {
  local images cache
  images="$(docker images \
      --filter 'reference=ghcr.io/limitcycle-oss/jarvis-*' \
      --filter 'reference=jarvis/*' \
      --format '{{.ID}}' \
    | sort -u \
    | xargs -r docker image inspect --format '{{.Size}}' \
    | awk '{s+=$1} END {printf "%.0f", s}')"
  cache="$(docker system df --format '{{.Type}}\t{{.Size}}' 2>/dev/null \
    | awk -F'\t' '$1 == "Build Cache" {print $2}' \
    | awk '/TB$/ {printf "%.0f", $1 * 1e12; next}
           /GB$/ {printf "%.0f", $1 * 1e9;  next}
           /MB$/ {printf "%.0f", $1 * 1e6;  next}
           /kB$/ {printf "%.0f", $1 * 1e3;  next}
                 {printf "%.0f", $1 + 0}')"
  echo $(( ${images:-0} + ${cache:-0} ))
}

info "Running documented first-run bootstrap (project: ${SMOKE_PROJECT}):"
info "  ${BOOTSTRAP_CMD[*]}"
info "First run pulls 7-11 GB of model data — this can take 20-60 minutes."

disk_baseline_bytes="$(smoke_footprint_bytes)"

setup_rc=0
timeout "$TIMEOUT_SECONDS" "${BOOTSTRAP_CMD[@]}" || setup_rc=$?

if [ "$setup_rc" -eq 124 ]; then
  err "Bootstrap exceeded the ${TIMEOUT_SECONDS}s budget (timed out)."
elif [ "$setup_rc" -ne 0 ]; then
  err "Bootstrap exited non-zero (rc=${setup_rc}) — stack did not come up cleanly."
fi

if [ "$setup_rc" -ne 0 ]; then
  err "Diagnostics: docker compose ps + last logs of every service:"
  docker compose -p "$SMOKE_PROJECT" ps >&2 || true
  docker compose -p "$SMOKE_PROJECT" logs --tail 80 >&2 || true
  exit 1
fi
ok "Bootstrap completed: ${BOOTSTRAP_CMD[*]}"

# -----------------------------------------------------------------------------
# Disk-budget ratchet: what this run added in app images + build cache must fit
# the measured CPU-path budget. Catches image-bloat regressions (e.g. the CUDA
# torch stack sneaking back into the default build).
# -----------------------------------------------------------------------------
docker system df || true
disk_after_bytes="$(smoke_footprint_bytes)"
disk_added_centi_gb=$(( (disk_after_bytes - disk_baseline_bytes) / 10000000 ))
[ "$disk_added_centi_gb" -lt 0 ] && disk_added_centi_gb=0
disk_added_human="$((disk_added_centi_gb / 100)).$(printf '%02d' $((disk_added_centi_gb % 100)))"
info "Disk added by this run (app images + build cache): ${disk_added_human} GB (budget: ${DISK_BUDGET_GB} GB)"
if [ "$disk_added_centi_gb" -gt $((DISK_BUDGET_GB * 100)) ]; then
  err "Disk budget exceeded: ${disk_added_human} GB > ${DISK_BUDGET_GB} GB (DISK_BUDGET_GB)."
  err "An image or build-cache regression made the install heavier — inspect 'docker system df -v'."
  exit 1
fi
ok "Disk footprint within budget (${disk_added_human} GB <= ${DISK_BUDGET_GB} GB)."

# -----------------------------------------------------------------------------
# Optional idempotency check (--rerun): the bootstrap must also succeed against
# the .env it just generated.
# -----------------------------------------------------------------------------
if [ "$RERUN" -eq 1 ]; then
  info "Re-running the bootstrap against the kept .env (idempotency check)..."
  env_sha_before="$(sha256sum "$REPO_ROOT/.env" | cut -d' ' -f1)"
  rerun_rc=0
  timeout "$TIMEOUT_SECONDS" "${BOOTSTRAP_CMD[@]}" || rerun_rc=$?
  if [ "$rerun_rc" -ne 0 ]; then
    err "Bootstrap re-run with an existing .env failed (rc=${rerun_rc})."
    exit 1
  fi
  if [ "$(sha256sum "$REPO_ROOT/.env" | cut -d' ' -f1)" != "$env_sha_before" ]; then
    warn "The re-run modified .env — review whether that mutation is intentional."
  fi
  ok "Bootstrap re-run with the kept .env succeeded."
fi

# -----------------------------------------------------------------------------
# Belt-and-suspenders: confirm the dashboard actually serves HTTP on :3001
# (README states the dashboard opens at http://localhost:3001 after setup).
# -----------------------------------------------------------------------------
info "Verifying the dashboard responds at ${DASHBOARD_URL} ..."
dash_ok=0
for _ in $(seq 1 20); do
  if curl -fs --max-time 5 "$DASHBOARD_URL" >/dev/null 2>&1; then
    dash_ok=1
    break
  fi
  sleep 3
done
if [ "$dash_ok" -ne 1 ]; then
  err "Dashboard did not respond at ${DASHBOARD_URL} after setup.sh reported healthy."
  docker compose -p "$SMOKE_PROJECT" ps >&2 || true
  docker compose -p "$SMOKE_PROJECT" logs --tail 80 dashboard >&2 || true
  exit 1
fi
ok "Dashboard is serving HTTP at ${DASHBOARD_URL}."

# -----------------------------------------------------------------------------
# Optional gated integration suite (--integration): tests/integration is NOT in
# testpaths and skipif-passes without SMOKE_INTEGRATION=1 — set the gate so the
# suite genuinely runs against the live smoke stack.
# -----------------------------------------------------------------------------
if [ "$INTEGRATION" -eq 1 ]; then
  info "Running the gated integration suite against the live stack..."
  if ! SMOKE_INTEGRATION=1 \
       PAPER_INGESTION_BASE="http://localhost:${PAPER_INGESTION_HOST_PORT}" \
       uv run pytest tests/integration -q; then
    err "Integration tests failed against the live stack."
    exit 1
  fi
  ok "Integration tests passed."
fi

# Final compose state for the log record (informational).
docker compose -p "$SMOKE_PROJECT" ps || true

ok "Clean-machine first run succeeded end to end."
# trap teardown runs on exit and prints the final PASS banner.
