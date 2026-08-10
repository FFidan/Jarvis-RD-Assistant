#!/usr/bin/env bash
# scripts/first-run-smoke.sh — clean-machine first-run smoke test.
#
# Proves a brand-new user can go from a fresh checkout to a healthy stack by
# following the README's documented first-run command. Catches "works on my
# machine" / undocumented-manual-step regressions before public launch.
#
# It runs the documented non-interactive bootstrap from the README, adding only
# the explicit project identity that isolates this smoke:
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
#   * setup.sh receives a dedicated persisted project name
#     (`jarvis-firstrun-<checkout-key>`). Teardown is explicitly scoped with
#     `-p`.
#   * Refuses to run if that smoke project already has containers, volumes, or
#     networks
#     (unless --force), and refuses if a `.env` already exists in the repo
#     (the documented first run starts with NO .env, and this smoke regenerates
#     it). The real deployment's project + volumes are never touched.
#   * ALWAYS tears down on exit (trap): `docker compose -p ... down -v` plus
#     removal of the bootstrap-generated working-tree artifacts, leaving no
#     residue (intended for an ephemeral CI checkout).
#
# Usage:
#   bash scripts/first-run-smoke.sh [--force] [--timeout SECONDS] [--build-local]
#                                   [--image-tag TAG] [--integration] [--rerun]
#                                   [--help]
#
#   --force            Remove pre-existing resources carrying the exact smoke
#                      project label before starting, instead of refusing.
#                      Never affects the real deployment.
#   --timeout SECONDS  Overall budget for `setup.sh` to finish (default 3600).
#                      The first run pulls 7-11 GB of model data, so a clean
#                      machine legitimately needs 20-60 min.
#   --build-local      Pass --build-local to setup.sh so the app images are
#                      BUILT from this checkout instead of pulled from GHCR —
#                      how CI proves the branch's own code boots (the pull
#                      path only exercises previously-published images).
#   --image-tag TAG    Pull stable, prerelease, or lowercase 40-hex commit-tagged
#                      application images.
#   --integration      After the stack is healthy, run the gated integration
#                      suite against it (sets SMOKE_INTEGRATION=1; requires uv).
#   --rerun            After the first bootstrap succeeds, run it again with
#                      the kept .env and assert the re-run also succeeds.
#   --help             Show this help and exit.
set -euo pipefail

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
# The dedicated Compose project is derived from the canonical checkout after
# setup_lib.sh is loaded. Separate checkouts therefore never share teardown.

# Isolation: distinct subnet + dashboard ports so a smoke run never collides
# with a live deploy on the same host (which uses 10.137.241.0/24 and ports
# 3001/3003).
# Override via env before calling this script if the defaults conflict.
: "${JARVIS_NET_SUBNET:=10.137.242.0/24}"
: "${DASHBOARD_HOST_PORT:=13001}"
: "${DASHBOARD_TRUSTED_HOST_PORT:=13003}"
: "${LITELLM_HOST_PORT:=14000}"
: "${POSTGRES_HOST_PORT:=15432}"
: "${PAPER_INGESTION_HOST_PORT:=18010}"
: "${LEARNING_ENGINE_HOST_PORT:=18011}"
: "${QDRANT_HOST_PORT:=16333}"
: "${OLLAMA_HOST_PORT:=11444}"
export JARVIS_NET_SUBNET DASHBOARD_HOST_PORT DASHBOARD_TRUSTED_HOST_PORT
export LITELLM_HOST_PORT POSTGRES_HOST_PORT
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
IMAGE_TAG=""
IMAGE_TAG_EXPLICIT=0
INTEGRATION=0
RERUN=0
SMOKE_OWNS_PROJECT=0

# -----------------------------------------------------------------------------
# Shared helpers (this script lives in <root>/scripts). Loaded before argument
# parsing because the parser reports its own failures through err().
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=setup_lib.sh
source "${SCRIPT_DIR}/setup_lib.sh"

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
    --image-tag)
      if [ "$#" -lt 2 ] || [[ "$2" == -* ]]; then
        err "--image-tag requires a value."
        exit 2
      fi
      IMAGE_TAG="$2"; IMAGE_TAG_EXPLICIT=1; shift 2 ;;
    --image-tag=*) IMAGE_TAG="${1#*=}"; IMAGE_TAG_EXPLICIT=1; shift ;;
    --integration) INTEGRATION=1; shift ;;
    --rerun)   RERUN=1; shift ;;
    -h|--help) show_help; exit 0 ;;
    *) err "Unknown argument: $1"; echo; show_help; exit 2 ;;
  esac
done
case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*) err "--timeout must be a positive integer (got: $TIMEOUT_SECONDS)"; exit 2 ;;
esac
# Adjust the budget to the path this run actually takes. The default above (6) is
# cpu-pull; --build-local fills the build cache (cpu-build 9); and a GPU host
# pulls/builds the much larger CUDA image (cuda-pull == cuda-build == 17). Mirrors
# scripts/setup_lib.sh::_image_budget_gb. The smoke never passes --gpu, so the
# accelerator matches setup.sh's auto path: the Docker nvidia runtime.
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
  DISK_BUDGET_GB=17
elif [ "$BUILD_LOCAL" -eq 1 ]; then
  DISK_BUDGET_GB=9
fi
readonly DISK_BUDGET_GB

# -----------------------------------------------------------------------------
# Resolve repo root
# -----------------------------------------------------------------------------
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"
if [ "$IMAGE_TAG_EXPLICIT" -eq 1 ] && ! image_tag_is_valid "$IMAGE_TAG"; then
  err "--image-tag must be X.Y.Z, X.Y.Z-prerelease, or a lowercase 40-hex commit."
  exit 2
fi

_smoke_lock_path="$(host_lifecycle_lock_path "$REPO_ROOT")" \
  || { err "Could not derive the checkout's isolated project identity."; exit 1; }
_smoke_project_key="${_smoke_lock_path##*/}"
_smoke_project_key="${_smoke_project_key%.lock}"
case "$_smoke_project_key" in
  ''|*[!0-9a-f]*)
    err "The checkout's isolated project identity is invalid."
    exit 1
    ;;
esac
[ "${#_smoke_project_key}" -eq 64 ] \
  || { err "The checkout's isolated project identity has the wrong length."; exit 1; }
readonly SMOKE_PROJECT="jarvis-firstrun-${_smoke_project_key:0:16}"
unset _smoke_lock_path _smoke_project_key

# Whether a .env already existed BEFORE this run — drives teardown (only remove
# the .env if WE generated it, never an operator's existing config).
ENV_PREEXISTED=0
[ -f "$REPO_ROOT/.env" ] && ENV_PREEXISTED=1

# project_resource_ids PROJECT KIND — list exact Compose-owned resources plus
# detached lifecycle guards carrying the separate ownership label.
project_resource_ids() {
  local project="$1"
  case "$2" in
    containers)
      {
        docker ps -aq --filter "label=com.docker.compose.project=${project}"
        docker ps -aq \
          --filter "label=${JARVIS_LIFECYCLE_PROJECT_LABEL}=${project}"
      } | sort -u ;;
    volumes)
      docker volume ls -q --filter "label=com.docker.compose.project=${project}" ;;
    networks)
      docker network ls -q --filter "label=com.docker.compose.project=${project}" ;;
    *) return 2 ;;
  esac
}

# project_has_resources PROJECT — return 0 when any owned resource exists.
project_has_resources() {
  local project="$1" kind ids
  for kind in containers volumes networks; do
    ids="$(project_resource_ids "$project" "$kind")" || return 2
    [ -z "$ids" ] || return 0
  done
  return 1
}

# remove_smoke_project_resources PROJECT — remove only exact project-labeled
# resources, retrying briefly for one-off Compose containers that are still
# exiting after an interrupted bootstrap.
remove_smoke_project_resources() {
  local project="$1" attempt kind resources resource state
  for attempt in 1 2 3; do
    for kind in containers volumes networks; do
      resources="$(project_resource_ids "$project" "$kind")" || return 2
      while IFS= read -r resource; do
        [ -n "$resource" ] || continue
        case "$kind" in
          containers) docker rm -f "$resource" >/dev/null 2>&1 || true ;;
          volumes) docker volume rm "$resource" >/dev/null 2>&1 || true ;;
          networks) docker network rm "$resource" >/dev/null 2>&1 || true ;;
        esac
      done <<< "$resources"
    done

    state=0
    project_has_resources "$project" || state=$?
    case "$state" in
      1) return 0 ;;
      2) return 2 ;;
    esac
    sleep 1
  done
  return 1
}

# require_project_resource_labels PROJECT KIND — require non-empty exact labels.
require_project_resource_labels() {
  local project="$1" kind="$2" ids id label
  ids="$(project_resource_ids "$project" "$kind")" || return 1
  [ -n "$ids" ] || {
    err "The smoke project owns no ${kind} after bootstrap."
    return 1
  }
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    case "$kind" in
      containers)
        label="$(docker inspect --format \
          '{{ index .Config.Labels "com.docker.compose.project" }}|{{ index .Config.Labels "dev.limitcycle.jarvis.lifecycle-project" }}' "$id")"
        case "$label" in
          "$project|"|"|$project"|"$project|$project") continue ;;
        esac
        ;;
      volumes)
        label="$(docker volume inspect --format \
          '{{ index .Labels "com.docker.compose.project" }}' "$id")" ;;
      networks)
        label="$(docker network inspect --format \
          '{{ index .Labels "com.docker.compose.project" }}' "$id")" ;;
    esac
    if [ "$label" != "$project" ]; then
      err "${kind%?} ${id} is labeled for '${label:-none}', not '${project}'."
      return 1
    fi
  done <<< "$ids"
}

# -----------------------------------------------------------------------------
# Teardown — ALWAYS runs (trap EXIT). Leaves no residue.
# -----------------------------------------------------------------------------
teardown() {
  local rc=$? kind leftovers cleanup_ok=1
  printf '\n'
  if [ "$SMOKE_OWNS_PROJECT" -eq 1 ]; then
    info "Teardown: removing the isolated '${SMOKE_PROJECT}' project..."
    # -v removes this project's named volumes; --remove-orphans clears its
    # one-off bootstrap containers. The explicit project keeps teardown scoped.
    docker compose -p "$SMOKE_PROJECT" down -v --remove-orphans 2>/dev/null \
      || warn "Compose teardown returned non-zero; checking owned resources."
    remove_smoke_project_resources "$SMOKE_PROJECT" \
      || warn "Exact project cleanup returned non-zero; verifying owned resources."
    for kind in containers volumes networks; do
      if ! leftovers="$(project_resource_ids "$SMOKE_PROJECT" "$kind")"; then
        err "Could not verify ${kind} cleanup for '${SMOKE_PROJECT}'."
        cleanup_ok=0
      elif [ -n "$leftovers" ]; then
        err "Teardown left project-owned ${kind}: ${leftovers//$'\n'/ }"
        cleanup_ok=0
      fi
    done
  else
    info "Teardown: the smoke project was not acquired; no Docker state removed."
  fi

  # Remove bootstrap-generated working-tree artifacts so the checkout is clean.
  # Only delete the .env if THIS run created it (never an operator's existing one).
  if [ "$ENV_PREEXISTED" -eq 0 ]; then
    rm -f "$REPO_ROOT/.env" "$REPO_ROOT"/docker-compose.override.yml.bak.* 2>/dev/null || true
    if ! find "$REPO_ROOT/secrets" -maxdepth 1 -name '*.txt' -exec rm -f -- {} +; then
      err "Could not remove secret files generated by the isolated first-run check."
      cleanup_ok=0
    elif [ -n "$(find "$REPO_ROOT/secrets" -maxdepth 1 -name '*.txt' -print -quit)" ]; then
      err "Teardown left secret files generated by the isolated first-run check."
      cleanup_ok=0
    fi
  fi
  if [ "$cleanup_ok" -eq 1 ]; then
    ok "Teardown complete; no project-owned containers, volumes, or networks remain."
  else
    rc=1
    err "Teardown could not prove complete project cleanup."
  fi

  if [ "$rc" -eq 0 ]; then
    printf '\n%s================ FIRST-RUN SMOKE: PASS ================%s\n' "$C_GREEN" "$C_RESET"
  else
    printf '\n%s================ FIRST-RUN SMOKE: FAIL ================%s\n' "$C_RED" "$C_RESET"
  fi
  exit "$rc"
}
trap 'exit 143' TERM
trap 'exit 130' INT
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
if [ "$INTEGRATION" -eq 1 ]; then
  command -v uv >/dev/null 2>&1 \
    || { err "--integration requires uv (https://docs.astral.sh/uv/)."; exit 1; }
fi
ok "docker + compose present; setup.sh found."

# The documented first run starts with no .env. Refuse before any forced
# cleanup so an operator's Compose model can never influence smoke deletion.
if [ "$ENV_PREEXISTED" -eq 1 ]; then
  err "A .env already exists at $REPO_ROOT/.env — this is not a clean checkout."
  err "Run the first-run smoke on a fresh checkout (CI) where no .env is present."
  exit 1
fi
existing_secret_file="$(find "$REPO_ROOT/secrets" -maxdepth 1 -name '*.txt' -print -quit)"
if [ -n "$existing_secret_file" ]; then
  err "Generated secret files already exist — this is not a clean checkout."
  err "Run the first-run smoke on a fresh checkout with no secrets/*.txt files."
  exit 1
fi
unset existing_secret_file

# Hold the checkout lifecycle lock before project admission and ownership. The
# setup subprocess inherits the authenticated descriptor; a concurrent smoke
# from this checkout fails before its EXIT trap may remove Docker state.
_smoke_lock_rc=0
claim_host_lifecycle_lock "$REPO_ROOT" || _smoke_lock_rc=$?
case "$_smoke_lock_rc" in
  0) ;;
  3)
    err "Another setup or first-run check is already using this checkout."
    exit 1
    ;;
  *)
    err "The checkout lifecycle lock is unavailable or unsafe."
    exit 1
    ;;
esac

# Guard: never silently clobber a prior smoke run. Refuse if its exact project
# labels own any resource; --force may remove only that already-identified state.
existing_containers="$(project_resource_ids "$SMOKE_PROJECT" containers)" \
  || { err "Could not inspect existing smoke containers."; exit 1; }
existing_volumes="$(project_resource_ids "$SMOKE_PROJECT" volumes)" \
  || { err "Could not inspect existing smoke volumes."; exit 1; }
existing_networks="$(project_resource_ids "$SMOKE_PROJECT" networks)" \
  || { err "Could not inspect existing smoke networks."; exit 1; }
if [ -n "$existing_containers" ] || [ -n "$existing_volumes" ] \
   || [ -n "$existing_networks" ]; then
  if [ "$FORCE" -eq 1 ]; then
    warn "Pre-existing '${SMOKE_PROJECT}' state found — removing it (--force)."
    remove_smoke_project_resources "$SMOKE_PROJECT" \
      || { err "--force could not remove and verify '${SMOKE_PROJECT}'."; exit 1; }
  else
    err "An isolated '${SMOKE_PROJECT}' project already exists (containers, volumes, or networks)."
    err "Re-run with --force to remove it first. (The real deployment is never touched.)"
    exit 1
  fi
fi
SMOKE_OWNS_PROJECT=1

CHECKOUT_PROJECT="$(_lifecycle_compose_project_name "$REPO_ROOT")" \
  || { err "The checkout directory does not resolve to a valid Compose project."; exit 1; }
ok "Preconditions met — clean checkout, no conflicting deployment."

# -----------------------------------------------------------------------------
# Run the documented first-run bootstrap (README: "Non-interactive (CI/cloud-init)")
#   ./setup.sh --non-interactive --profile=dev
# setup.sh receives its persisted project explicitly.
# -----------------------------------------------------------------------------
BOOTSTRAP_CMD=(
  ./setup.sh --non-interactive --profile=dev
  --compose-project-name "$SMOKE_PROJECT"
)
[ "$BUILD_LOCAL" -eq 1 ] && BOOTSTRAP_CMD+=(--build-local)
[ "$IMAGE_TAG_EXPLICIT" -eq 0 ] || BOOTSTRAP_CMD+=(--image-tag "$IMAGE_TAG")

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

# The bootstrap must own a real, labeled project and must not have fallen back
# to the checkout directory after setup sanitized its caller environment.
for _kind in containers volumes networks; do
  require_project_resource_labels "$SMOKE_PROJECT" "$_kind" || exit 1
done
if [ "$CHECKOUT_PROJECT" != "$SMOKE_PROJECT" ]; then
  _checkout_rc=0
  project_has_resources "$CHECKOUT_PROJECT" || _checkout_rc=$?
  case "$_checkout_rc" in
    1) ;;
    0)
      err "Bootstrap also created resources under checkout-derived project '${CHECKOUT_PROJECT}'."
      exit 1
      ;;
    *)
      err "Could not verify the checkout-derived project '${CHECKOUT_PROJECT}'."
      exit 1
      ;;
  esac
fi
ok "Bootstrap resources are non-empty, correctly labeled, and isolated."

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
  # Compared as a SORTED line set, not as file bytes. The keep path re-runs
  # scripts/gen-langfuse-keys.sh, which rewrites its two keys by deleting and
  # re-appending them — so the line ORDER legitimately shifts on every re-run
  # while every key and value stays put. Sorting isolates the property that
  # actually matters: a re-run must not rotate a secret, drop a key, or add one.
  env_before="$(mktemp)"
  sort "$REPO_ROOT/.env" > "$env_before"
  rerun_rc=0
  timeout "$TIMEOUT_SECONDS" "${BOOTSTRAP_CMD[@]}" || rerun_rc=$?
  if [ "$rerun_rc" -ne 0 ]; then
    rm -f "$env_before"
    err "Bootstrap re-run with an existing .env failed (rc=${rerun_rc})."
    exit 1
  fi
  if ! sort "$REPO_ROOT/.env" | diff -u "$env_before" - > "${env_before}.diff"; then
    err "The re-run changed .env — a keep-path re-run must preserve every key and value."
    err "Rotating a secret here silently invalidates the running deployment's data."
    sed -n '1,40p' "${env_before}.diff" >&2
    rm -f "$env_before" "${env_before}.diff"
    exit 1
  fi
  rm -f "$env_before" "${env_before}.diff"
  ok "Bootstrap re-run with the kept .env succeeded and preserved every value."
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
