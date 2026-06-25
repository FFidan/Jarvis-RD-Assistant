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
#   bash scripts/first-run-smoke.sh [--force] [--timeout SECONDS] [--help]
#
#   --force            Tear down a pre-existing smoke project before starting,
#                      instead of refusing. (Never affects the real deployment;
#                      only the isolated jarvis-firstrun-smoke project.)
#   --timeout SECONDS  Overall budget for `setup.sh` to finish (default 3600).
#                      The first run pulls 7-11 GB of model data, so a clean
#                      machine legitimately needs 20-60 min.
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
export JARVIS_NET_SUBNET DASHBOARD_HOST_PORT

readonly DASHBOARD_URL="http://localhost:${DASHBOARD_HOST_PORT}"

FORCE=0
TIMEOUT_SECONDS=3600

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
    -h|--help) show_help; exit 0 ;;
    *) err "Unknown argument: $1"; echo; show_help; exit 2 ;;
  esac
done
case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*) err "--timeout must be a positive integer (got: $TIMEOUT_SECONDS)"; exit 2 ;;
esac

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
#   ./setup.sh --non-interactive --profile=dev
# Under COMPOSE_PROJECT_NAME=jarvis-firstrun-smoke, so every container/volume
# setup.sh creates lands in the isolated project.
# -----------------------------------------------------------------------------
info "Running documented first-run bootstrap (project: ${SMOKE_PROJECT}):"
info "  ./setup.sh --non-interactive --profile=dev"
info "First run pulls 7-11 GB of model data — this can take 20-60 minutes."

setup_rc=0
timeout "$TIMEOUT_SECONDS" ./setup.sh --non-interactive --profile=dev || setup_rc=$?

if [ "$setup_rc" -eq 124 ]; then
  err "setup.sh exceeded the ${TIMEOUT_SECONDS}s budget (timed out)."
elif [ "$setup_rc" -ne 0 ]; then
  err "setup.sh exited non-zero (rc=${setup_rc}) — stack did not come up cleanly."
fi

if [ "$setup_rc" -ne 0 ]; then
  err "Diagnostics: docker compose ps + last logs of every service:"
  docker compose -p "$SMOKE_PROJECT" ps >&2 || true
  docker compose -p "$SMOKE_PROJECT" logs --tail 80 >&2 || true
  exit 1
fi
ok "setup.sh completed — all mandatory services reported healthy."

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

# Final compose state for the log record (informational).
docker compose -p "$SMOKE_PROJECT" ps || true

ok "Clean-machine first run succeeded end to end."
# trap teardown runs on exit and prints the final PASS banner.
