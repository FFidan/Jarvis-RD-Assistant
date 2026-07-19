#!/usr/bin/env bash
# scripts/lifecycle-smoke.sh — release smoke for the lifecycle paths the
# cold-install smoke never reaches.
#
# scripts/first-run-smoke.sh proves ONE path: a clean machine reaching a healthy
# stack over plain HTTP on localhost. Three lifecycle surfaces ship untested by
# it, and each is a path an operator takes on a bad day:
#
#   tls        The TLS profile. `--profile=local-https` fronts the dashboard with
#              Caddy on https://localhost:3443 using mkcert-issued certs. This
#              leg installs that profile non-interactively and proves the edge
#              serves a real backend route over a cert that validates against the
#              mkcert root CA — not merely that something answers on 3443.
#
#   update     An interrupted update. `jarvis-research update` writes a pending
#              transaction BEFORE it stages images, precisely so a pull that dies
#              mid-flight leaves a resumable record instead of a half-updated
#              checkout. This leg injects a pull failure at that exact phase,
#              asserts the transaction survives with the checkout unadvanced,
#              then clears the injection and asserts the retry runs to
#              completion and clears the transaction.
#
#   uninstall  The uninstall dry run. Tier 3 is the first tier that deletes data
#              volumes, so what an operator needs from the dry run is an accurate
#              inventory of what a real run would destroy. This leg asserts the
#              CONTENT of the emitted plan: that it carries the volume-removing
#              teardown rather than the volume-sparing one, that it names every
#              application image the deployment is actually running, and that it
#              stops short of the tier-4 purge steps. A plan that quietly stopped
#              enumerating any of that fails the leg.
#
# ISOLATION: every leg runs under its own ephemeral compose project name
# (jarvis-lifecycle-<leg>-<pid>) and tears that project down on exit. No leg ever
# touches the default project, so a developer's running stack is never at risk.
#
# Usage:
#   bash scripts/lifecycle-smoke.sh [--leg tls|update|uninstall]... [--timeout N]
#
#   --leg NAME       Run only this leg. Repeatable. Default: all three.
#   --timeout N      Per-install budget in seconds (default 3600). A cold install
#                    pulls several GB, so a clean machine legitimately needs
#                    20-60 minutes.
#   --help           Show this help and exit.
set -euo pipefail

# -----------------------------------------------------------------------------
# Output helpers (mirrors scripts/first-run-smoke.sh)
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
  sed -n '2,/^set -euo/{ /^set -euo/d; s/^# \{0,1\}//; s/^#$//; p; }' "$0"
}

# -----------------------------------------------------------------------------
# Arguments
# -----------------------------------------------------------------------------
LEGS=()
TIMEOUT_SECONDS=3600
while [ $# -gt 0 ]; do
  case "$1" in
    --leg)     LEGS+=("$2"); shift 2 ;;
    --leg=*)   LEGS+=("${1#*=}"); shift ;;
    --timeout) TIMEOUT_SECONDS="$2"; shift 2 ;;
    --timeout=*) TIMEOUT_SECONDS="${1#*=}"; shift ;;
    -h|--help) show_help; exit 0 ;;
    *) err "Unknown argument: $1"; echo; show_help; exit 2 ;;
  esac
done
[ "${#LEGS[@]}" -eq 0 ] && LEGS=(tls update uninstall)
case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*) err "--timeout must be a positive integer (got: $TIMEOUT_SECONDS)"; exit 2 ;;
esac
for leg in "${LEGS[@]}"; do
  case "$leg" in
    tls|update|uninstall) ;;
    *) err "Unknown leg '${leg}' (expected: tls, update, or uninstall)"; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

# The real docker binary, resolved BEFORE any leg prepends a fault-injection
# shim to PATH, so the shim can delegate to it.
REAL_DOCKER="$(command -v docker 2>/dev/null || true)"
[ -n "$REAL_DOCKER" ] || { err "docker not found — install Docker Engine 24+ with Compose v2."; exit 1; }
docker compose version >/dev/null 2>&1 \
  || { err "'docker compose' (v2 plugin) not found."; exit 1; }
docker info >/dev/null 2>&1 \
  || { err "Docker daemon unreachable ('docker info' failed)."; exit 1; }

# -----------------------------------------------------------------------------
# Teardown — every project this run created, plus every scratch directory.
# Registered before any leg starts, so an abort still cleans up.
# -----------------------------------------------------------------------------
CREATED_PROJECTS=()
SCRATCH_DIRS=()
FAILED_LEGS=()
PROJECT=""
SCRATCH=""

# The tls leg is the one leg that installs into REPO_ROOT itself, so it is the
# one leg that can leave the checkout dirty and refuse its own next run. These
# track exactly what it generated; the cleanup is idempotent and runs on every
# exit path, including an abort part-way through the install.
TLS_ARTIFACTS_ACTIVE=0
TLS_CERTS_PREEXISTING=0

_tls_cleanup() {
  [ "$TLS_ARTIFACTS_ACTIVE" -eq 1 ] || return 0
  TLS_ARTIFACTS_ACTIVE=0
  rm -f "${REPO_ROOT}/.env"
  [ "$TLS_CERTS_PREEXISTING" -eq 1 ] || rm -rf "${REPO_ROOT}/certs"
}

teardown() {
  local rc=$? project dir
  printf '\n'
  _tls_cleanup
  for project in ${CREATED_PROJECTS[@]+"${CREATED_PROJECTS[@]}"}; do
    info "Teardown: removing project '${project}' (containers + volumes)..."
    "$REAL_DOCKER" compose -p "$project" down -v --remove-orphans >/dev/null 2>&1 || true
  done
  for dir in ${SCRATCH_DIRS[@]+"${SCRATCH_DIRS[@]}"}; do
    rm -rf "$dir" 2>/dev/null || true
  done
  if [ "$rc" -eq 0 ] && [ "${#FAILED_LEGS[@]}" -eq 0 ]; then
    printf '\n%s================ LIFECYCLE SMOKE: PASS ================%s\n' "$C_GREEN" "$C_RESET"
  else
    printf '\n%s================ LIFECYCLE SMOKE: FAIL ================%s\n' "$C_RED" "$C_RESET"
    [ "$rc" -eq 0 ] && rc=1
  fi
  exit "$rc"
}
trap teardown EXIT

# new_project LEG — set PROJECT to an ephemeral project name and register it for
# teardown. Both helpers ASSIGN rather than echo: a `$(...)` reader would run the
# registration in a subshell, losing it, and nothing would ever be torn down.
new_project() {
  PROJECT="jarvis-lifecycle-${1}-$$"
  CREATED_PROJECTS+=("$PROJECT")
}

# new_scratch — set SCRATCH to a temp dir and register it for teardown.
new_scratch() {
  SCRATCH="$(mktemp -d)"
  SCRATCH_DIRS+=("$SCRATCH")
}

# -----------------------------------------------------------------------------
# Leg: tls
# -----------------------------------------------------------------------------
run_leg_tls() {
  local rc=0
  _run_leg_tls_body || rc=$?
  _tls_cleanup
  return "$rc"
}

_run_leg_tls_body() {
  local project caroot rc=0
  new_project tls; project="$PROJECT"

  command -v mkcert >/dev/null 2>&1 || {
    err "tls leg needs mkcert (https://github.com/FiloSottile/mkcert#installation)."
    return 1
  }
  # caddy_local pins container_name jarvis_caddy_local and host port 3443, neither
  # of which an ephemeral project name re-scopes. Refuse rather than fight a
  # stack that already owns them.
  if "$REAL_DOCKER" ps -a --format '{{.Names}}' | grep -qx 'jarvis_caddy_local'; then
    err "A 'jarvis_caddy_local' container already exists; the TLS edge cannot be isolated from it."
    return 1
  fi
  if [ -f "$REPO_ROOT/.env" ]; then
    err "A .env already exists at ${REPO_ROOT}/.env — the tls leg installs from a clean checkout."
    return 1
  fi

  if [ -e "${REPO_ROOT}/certs" ]; then TLS_CERTS_PREEXISTING=1; fi
  TLS_ARTIFACTS_ACTIVE=1

  info "Generating locally-trusted certs (mkcert)..."
  bash scripts/init-mkcert.sh || { err "mkcert cert generation failed."; return 1; }
  caroot="$(mkcert -CAROOT)/rootCA.pem"
  [ -r "$caroot" ] || { err "mkcert root CA not readable at ${caroot}."; return 1; }

  info "Installing the TLS profile (project: ${project})..."
  COMPOSE_PROJECT_NAME="$project" \
    timeout "$TIMEOUT_SECONDS" ./setup.sh --non-interactive --profile=local-https || rc=$?
  if [ "$rc" -ne 0 ]; then
    err "TLS-profile install exited non-zero (rc=${rc})."
    "$REAL_DOCKER" compose -p "$project" logs --tail 80 caddy_local >&2 || true
    return 1
  fi

  # The assertion that matters: a REAL backend route served through the TLS edge,
  # validated against the mkcert root CA. --cacert (never -k) is what makes this a
  # cert assertion; /health/paper_ingestion (never /health, which the dashboard
  # does not route and the SPA fallback would answer 200 for regardless) is what
  # makes it a liveness assertion.
  local url="https://localhost:3443/health/paper_ingestion"
  info "Verifying ${url} over the mkcert chain..."
  local served=0
  for _ in $(seq 1 20); do
    if curl -fsS --cacert "$caroot" --max-time 10 "$url" >/dev/null 2>&1; then
      served=1; break
    fi
    sleep 3
  done
  if [ "$served" -ne 1 ]; then
    err "${url} did not serve over the mkcert chain after the install reported healthy."
    curl -sS --cacert "$caroot" --max-time 10 -o /dev/null -w 'curl exit/status: %{http_code}\n' "$url" >&2 || true
    "$REAL_DOCKER" compose -p "$project" ps >&2 || true
    "$REAL_DOCKER" compose -p "$project" logs --tail 80 caddy_local dashboard >&2 || true
    return 1
  fi
  ok "TLS edge serves a live backend route on a cert that validates against the mkcert root CA."
  return 0
}

# -----------------------------------------------------------------------------
# Leg: update
# -----------------------------------------------------------------------------
# stable_tags -> origin's vX.Y.Z release tags, oldest first.
stable_tags() {
  git ls-remote --tags --refs origin 2>/dev/null \
    | sed -n 's#.*refs/tags/##p' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    | sort -V
}


# install_pull_failure_shim DIR — a `docker` that fails every `compose ... pull`
# and delegates everything else to the real binary. This is the fault injection:
# it lands inside _stage_target_cohort (the phase AFTER the pending transaction
# is written) without touching the host's network, so the leg stays hermetic and
# the failure point is exact rather than incidental.
install_pull_failure_shim() {
  local dir="$1"
  cat > "${dir}/docker" <<SHIM
#!/usr/bin/env bash
if [ "\${1:-}" = "compose" ]; then
  for arg in "\$@"; do
    if [ "\$arg" = "pull" ]; then
      printf 'lifecycle-smoke: image pull blocked by fault injection\n' >&2
      exit 1
    fi
  done
fi
exec "${REAL_DOCKER}" "\$@"
SHIM
  chmod +x "${dir}/docker"
}

run_leg_update() {
  local project scratch clone state shim previous latest rc=0
  new_project update; project="$PROJECT"
  new_scratch; scratch="$SCRATCH"
  clone="${scratch}/install"
  state="${scratch}/cli-state"
  shim="${scratch}/shim"
  mkdir -p "$state" "$shim"

  latest="$(stable_tags | tail -n 1)"
  previous="$(stable_tags | tail -n 2 | head -n 1)"
  if [ -z "$previous" ] || [ -z "$latest" ] || [ "$previous" = "$latest" ]; then
    err "update leg needs two published stable tags on origin (found previous='${previous}' latest='${latest}')."
    return 1
  fi
  info "Update leg: ${previous} -> ${latest}"

  info "Cold-installing ${previous} (project: ${project})..."
  git clone --quiet --branch main "$(git remote get-url origin)" "$clone" \
    || { err "Could not clone the managed repository."; return 1; }
  git -C "$clone" checkout --quiet -B main "$previous" \
    || { err "Could not place the clone on ${previous}."; return 1; }
  ( cd "$clone" && COMPOSE_PROJECT_NAME="$project" \
      timeout "$TIMEOUT_SECONDS" ./setup.sh --non-interactive --profile=dev ) || rc=$?
  if [ "$rc" -ne 0 ]; then
    err "Cold install at ${previous} exited non-zero (rc=${rc})."
    return 1
  fi

  local cli="${clone}/scripts/jarvis-research.sh"
  local pending="${state}/pending-update.json"
  printf '%s\n' "$clone" > "${state}/installs"

  local head_before; head_before="$(git -C "$clone" rev-parse HEAD)"

  info "Running the update with image pulls failing..."
  install_pull_failure_shim "$shim"
  local injected_log="${scratch}/update-injected.log"
  rc=0
  ( cd "$clone" && PATH="${shim}:${PATH}" JARVIS_CLI_CONFIG_DIR="$state" \
      COMPOSE_PROJECT_NAME="$project" \
      bash "$cli" --repo "$clone" update --to "$latest" --yes ) > "$injected_log" 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then
    err "The update succeeded despite the injected pull failure — the fault was not injected."
    return 1
  fi
  # A non-zero exit alone proves nothing: the update dies before it ever stages
  # images when the target carries a data-changing migration and no fresh backup
  # exists. Only the staging abort is the fault this leg injected, so match it.
  if ! grep -q "Staging images for ${latest} failed" "$injected_log"; then
    err "The update failed, but NOT at the injected staging phase (rc=${rc}) — this leg proves nothing about the transaction:"
    tail -n 40 "$injected_log" >&2
    return 1
  fi
  ok "Update aborted at the injected staging phase (rc=${rc})."

  # The contract: an interrupted update leaves a resumable record and an
  # unadvanced checkout. Both halves, or the transaction is not a transaction.
  if [ ! -f "$pending" ]; then
    err "No pending transaction at ${pending} — the interrupted update left nothing to resume."
    return 1
  fi
  if ! grep -q '"phase":"staging"' "$pending"; then
    err "Pending transaction is not at the staging phase: $(cat "$pending")"
    return 1
  fi
  if [ "$(git -C "$clone" rev-parse HEAD)" != "$head_before" ]; then
    err "The checkout advanced despite the failed staging phase."
    return 1
  fi
  ok "Pending transaction survived at phase 'staging' with the checkout unadvanced."

  info "Retrying the update with pulls restored..."
  local resume_log="${scratch}/update-resumed.log"
  rc=0
  ( cd "$clone" && JARVIS_CLI_CONFIG_DIR="$state" COMPOSE_PROJECT_NAME="$project" \
      bash "$cli" --repo "$clone" update --to "$latest" --yes ) > "$resume_log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    err "The resumed update failed (rc=${rc}) — an interrupted update is not recoverable:"
    tail -n 40 "$resume_log" >&2
    return 1
  fi
  if [ -f "$pending" ]; then
    err "The completed update left its pending transaction behind: $(cat "$pending")"
    return 1
  fi
  # Release tags here are ANNOTATED, so a bare `rev-parse <tag>` yields the tag
  # OBJECT sha and never equals a commit sha. ^{commit} is what makes this compare
  # the checkout against the release it claims to have landed on.
  if [ "$(git -C "$clone" rev-parse HEAD)" != "$(git -C "$clone" rev-parse "${latest}^{commit}")" ]; then
    err "The resumed update did not advance the checkout to ${latest}."
    return 1
  fi
  ok "Resumed update completed, advanced to ${latest}, and cleared the transaction."
  return 0
}

# -----------------------------------------------------------------------------
# Leg: uninstall
# -----------------------------------------------------------------------------
# project_state PROJECT — the containers and volumes a project owns, in a stable
# order. Byte-comparable across two calls; any mutation shows up as a diff.
project_state() {
  "$REAL_DOCKER" compose -p "$1" ps -a --format '{{.Name}} {{.Image}} {{.State}}' 2>/dev/null | sort
  "$REAL_DOCKER" volume ls -q --filter "label=com.docker.compose.project=$1" 2>/dev/null | sort
}

# assert_tier3_plan PLAN_LOG PROJECT — the tier-3 plan must describe the run an
# operator is about to authorise: the volume-removing teardown, every application
# image the deployment is actually running, and nothing from the purge tier.
assert_tier3_plan() {
  local plan="$1" project="$2" planned deployed missing
  if ! grep -qx 'PLAN compose-down-volumes' "$plan"; then
    err "The tier-3 plan omits the volume-removing teardown ('compose-down-volumes'):"
    cat "$plan" >&2
    return 1
  fi
  if grep -qx 'PLAN compose-down' "$plan"; then
    err "The tier-3 plan carries the volume-SPARING teardown ('compose-down'); tier 3 deletes data volumes:"
    cat "$plan" >&2
    return 1
  fi
  if grep -Eq '^PLAN (file|registry-line|clone|key-export) ' "$plan"; then
    err "The tier-3 plan reaches steps that belong to the purge tier:"
    grep -E '^PLAN (file|registry-line|clone|key-export) ' "$plan" >&2
    return 1
  fi

  planned="$(sed -n 's/^PLAN image //p' "$plan" | sort -u)"
  if [ -z "$planned" ]; then
    err "The tier-3 plan names no application images to remove:"
    cat "$plan" >&2
    return 1
  fi
  # Independent expectation: what the deployment is really running, read from
  # docker rather than from uninstall.sh's own view, so the plan is checked
  # against reality instead of against itself.
  deployed="$("$REAL_DOCKER" compose -p "$project" ps -a --format '{{.Image}}' 2>/dev/null \
    | grep '^ghcr\.io/limitcycle-oss/jarvis-' | sort -u || true)"
  if [ -n "$deployed" ]; then
    missing="$(comm -23 <(printf '%s\n' "$deployed") <(printf '%s\n' "$planned"))"
    if [ -n "$missing" ]; then
      err "The tier-3 plan omits application images the deployment is running (a real run would strand them):"
      printf '%s\n' "$missing" >&2
      return 1
    fi
  fi
  ok "Tier-3 plan removes volumes, names all $(printf '%s\n' "$planned" | wc -l) deployed application images, and stops short of purge."
  return 0
}

run_leg_uninstall() {
  local project scratch clone before after rc=0
  new_project uninstall; project="$PROJECT"
  new_scratch; scratch="$SCRATCH"
  clone="${scratch}/install"
  before="${scratch}/state.before"
  after="${scratch}/state.after"

  info "Installing a stack for the dry run to enumerate (project: ${project})..."
  git clone --quiet --no-hardlinks "$REPO_ROOT" "$clone" \
    || { err "Could not clone the working tree."; return 1; }
  ( cd "$clone" && COMPOSE_PROJECT_NAME="$project" \
      timeout "$TIMEOUT_SECONDS" ./setup.sh --non-interactive --profile=dev ) || rc=$?
  if [ "$rc" -ne 0 ]; then
    err "Install for the uninstall leg exited non-zero (rc=${rc})."
    return 1
  fi

  project_state "$project" > "$before"
  if [ ! -s "$before" ]; then
    err "Snapshot before the dry run is empty — there is nothing for it to leave alone."
    return 1
  fi

  info "Running the tier-3 uninstall dry run..."
  local plan="${scratch}/plan.log"
  rc=0
  ( cd "$clone" && COMPOSE_PROJECT_NAME="$project" \
      bash scripts/uninstall.sh --repo "$clone" --tier 3 --dry-run ) > "$plan" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    err "The dry run exited non-zero (rc=${rc}):"
    cat "$plan" >&2
    return 1
  fi
  assert_tier3_plan "$plan" "$project" || return 1

  # Secondary, and deliberately weak. A dry run cannot reach a docker mutation by
  # construction: uninstall.sh's _step prints `PLAN <label>` and returns without
  # invoking its arguments, and the dry-run branch of its main body skips the
  # preview and the confirmation gates outright. So this diff proves only that
  # enumerating the plan is itself side-effect-free — it does NOT prove the
  # destructive path is contained, and it cannot. The plan-content assertions
  # above are what give this leg its bite.
  project_state "$project" > "$after"
  if ! diff -u "$before" "$after" > "${scratch}/state.diff"; then
    err "Enumerating the tier-3 plan changed the deployment. Containers/volumes before vs after:"
    cat "${scratch}/state.diff" >&2
    return 1
  fi
  return 0
}

# -----------------------------------------------------------------------------
# Drive the requested legs. Every leg runs even if an earlier one fails, so one
# dispatch reports the whole lifecycle picture instead of only its first break.
# -----------------------------------------------------------------------------
for leg in "${LEGS[@]}"; do
  printf '\n%s===== lifecycle leg: %s =====%s\n' "$C_BLUE" "$leg" "$C_RESET"
  leg_rc=0
  "run_leg_${leg}" || leg_rc=$?
  if [ "$leg_rc" -eq 0 ]; then
    ok "Leg '${leg}' passed."
  else
    err "Leg '${leg}' FAILED (rc=${leg_rc})."
    FAILED_LEGS+=("$leg")
  fi
done

printf '\n'
if [ "${#FAILED_LEGS[@]}" -ne 0 ]; then
  err "Failed legs: ${FAILED_LEGS[*]}"
  exit 1
fi
ok "All lifecycle legs passed: ${LEGS[*]}"
