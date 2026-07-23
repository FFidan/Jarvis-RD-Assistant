#!/usr/bin/env bash
# update.sh — pull newer pinned images from versions.env and restart.
#
#   --build-local   Rebuild the application images from source instead of pulling
#                   the prebuilt ones published to GHCR. Slower and needs far more
#                   disk; base images and build inputs must be cached or reachable.
#   --yes           Assume "yes" for every confirmation prompt (unattended runs).
#
# Never rolls back automatically. A direct-run failure prints bounded recovery
# commands for the affected image set. macOS-safe: no `sed -i`, no GNU-only flags.
set -euo pipefail

# -----------------------------------------------------------------------------
# Colors
# -----------------------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'
  C_BOLD=$'\033[1m'
  C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

info() { printf '%s[INFO]%s  %s\n'  "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s    %s\n'  "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[WARN]%s  %s\n'  "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[ERROR]%s %s\n'  "$C_RED"    "$C_RESET" "$*" >&2; }

# Terse death, reserved for precondition failures that predate any image work
# (a missing versions.env, no Docker, an unknown flag). Image pull/build/up
# failures use fail_with_recovery below so they always print the rollback guidance.
die() {
  err "$1"
  printf '        %s%s%s\n' "$C_YELLOW" "$2" "$C_RESET" >&2
  exit 1
}

# print_split_recovery SERVICE... — the post-failure recovery guidance, split so
# each command names only the services it can actually roll back. Third-party
# services are pinned in versions.env; application services are tagged by
# JARVIS_VERSION. THIRD_PARTY_SET (built once the topology is known) decides which
# half each service belongs to, so newly reconciled services classify correctly.
print_split_recovery() {
  local svc
  local -a failed_app=() failed_third=()
  local -a recovery_profiles=()
  for svc in "$@"; do
    if _env_key_in_list "$svc" "${THIRD_PARTY_SET:-}"; then
      failed_third+=("$svc")
    else
      failed_app+=("$svc")
    fi
  done
  if declare -p APP_PROFILE_ARGS >/dev/null 2>&1; then
    recovery_profiles=("${APP_PROFILE_ARGS[@]}")
  fi

  printf '\nRecovery is limited to the failed image services.\n'
  printf '  Repository: %s\n' "${SCRIPT_DIR:-.}"
  printf '    cd %q\n' "${SCRIPT_DIR:-.}"

  if [ "${#failed_third[@]}" -gt 0 ]; then
cat <<EOF

  ${C_BOLD}Third-party services${C_RESET} are pinned in versions.env. Roll their pins
  back one commit and re-run:
    ${C_BOLD}git checkout HEAD~1 -- versions.env && ./update.sh${C_RESET}
EOF
  fi

  if [ "${#failed_app[@]}" -gt 0 ]; then
    printf '\n  %sApplication-image recovery (not a full release rollback):%s\n' "$C_BOLD" "$C_RESET"
    printf '  Application services use JARVIS_VERSION; these commands do not move the Git checkout or restore stored data.\n'
    if ! printf '%s' "${PREVIOUS_APP_VERSION:-}" \
        | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$'; then
      printf '  The previous application version could not be read safely from .env, so no image command was printed.\n'
    elif [ "${BUILD_LOCAL:-0}" -eq 1 ]; then
      printf '  Use this only if the previous application image is still cached locally:\n'
      printf '    JARVIS_VERSION=%q docker compose' "$PREVIOUS_APP_VERSION"
      if [ "${#recovery_profiles[@]}" -gt 0 ]; then
        printf ' %q' "${recovery_profiles[@]}"
      fi
      printf ' up -d --no-build'
      printf ' %q' "${failed_app[@]}"
      printf '\n'
    else
      printf '    JARVIS_VERSION=%q docker compose' "$PREVIOUS_APP_VERSION"
      if [ "${#recovery_profiles[@]}" -gt 0 ]; then
        printf ' %q' "${recovery_profiles[@]}"
      fi
      printf ' pull'
      printf ' %q' "${failed_app[@]}"
      printf '\n'
      printf '    JARVIS_VERSION=%q docker compose' "$PREVIOUS_APP_VERSION"
      if [ "${#recovery_profiles[@]}" -gt 0 ]; then
        printf ' %q' "${recovery_profiles[@]}"
      fi
      printf ' up -d --no-build'
      printf ' %q' "${failed_app[@]}"
      printf '\n'
    fi
  fi

cat <<EOF

To inspect logs for the affected service(s):
  docker compose logs --tail=200 $*
EOF
}

# fail_with_recovery MESSAGE HINT SERVICE... — an image pull/build/up failure.
# Emit the error and its hint, then the split rollback guidance for the affected
# services, then exit 1.
fail_with_recovery() {
  local msg="$1" hint="$2"; shift 2
  err "$msg"
  printf '        %s%s%s\n' "$C_YELLOW" "$hint" "$C_RESET" >&2
  if [ "${UPDATE_MANAGED_TRANSACTION:-0}" -eq 0 ]; then
    print_split_recovery "$@"
  fi
  exit 1
}

# Run from repo root.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD_LOCAL=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --build-local) BUILD_LOCAL=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help)
      sed -n '/^# update.sh/,/^set -euo/{ /^#/!d; s/^# \{0,1\}//p; }' "$0"
      exit 0
      ;;
    *) die "Unknown flag: $1" "Run: $0 --help" ;;
  esac
done

# confirm PROMPT — yes/no gate honouring --yes. Returns 0 for yes, 1 otherwise.
# With --yes it echoes the auto-answer instead of blocking on a closed stdin.
confirm() {
  local reply
  if [ "$ASSUME_YES" -eq 1 ]; then
    printf '%s%s\n' "$1" "yes (--yes)"
    return 0
  fi
  read -rp "$1" reply
  case "$reply" in
    [yY]|[yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

# The .env writer has exactly one implementation; reuse it rather than growing a
# second one here.
# shellcheck source=scripts/setup_lib.sh
# shellcheck disable=SC1091  # resolved at runtime relative to SCRIPT_DIR
. scripts/setup_lib.sh

# Caller-exported Compose selectors outrank this checkout's .env. Clear them
# before any Docker command so a direct update cannot target another project;
# Compose will still load this install's persisted selectors from .env.
sanitize_compose_environment

UPDATE_LIFECYCLE_OWNED=0
UPDATE_MUTATION_STARTED=0
UPDATE_MANAGED_TRANSACTION=0
_cleanup_direct_update_lifecycle() {
  local rc=$? action=clear
  trap - EXIT
  if [ "$UPDATE_LIFECYCLE_OWNED" -eq 1 ]; then
    [ "$UPDATE_MUTATION_STARTED" -ne 1 ] || action=retain
    [ "$rc" -ne 0 ] || action=clear
    finish_lifecycle_operation "$SCRIPT_DIR" direct-update "$action" 2>/dev/null || true
  fi
  exit "$rc"
}

_update_lock_rc=0
claim_host_lifecycle_lock "$SCRIPT_DIR" || _update_lock_rc=$?
case "$_update_lock_rc" in
  0) ;;
  3) die "Another lifecycle operation is already running for this JARVIS install." \
       "No services were changed. Wait for it to finish, then retry." ;;
  *) die "The per-install lifecycle lock is unavailable or unsafe." \
       "No services were changed. Run: jarvis-research doctor" ;;
esac

# -----------------------------------------------------------------------------
# 1. Resolve this checkout's application image tag.
# -----------------------------------------------------------------------------
# An exact release tag is authoritative for release-candidate checkouts, whose
# pyproject version intentionally remains the eventual stable version. Otherwise
# read the project version from this checkout. The result is exported before the
# first Compose invocation so it overrides any stale JARVIS_VERSION in .env.
resolve_checkout_app_version() {
  local exact_tag="" version=""
  if command -v git >/dev/null 2>&1; then
    exact_tag="$(git describe --tags --exact-match HEAD 2>/dev/null || true)"
  fi

  case "$exact_tag" in
    v[0-9]*) version="${exact_tag#v}" ;;
    *)
      [ -r pyproject.toml ] || return 1
      version="$(awk '
        /^\[project\][[:space:]]*$/ { in_project = 1; next }
        in_project && /^\[/ { exit }
        in_project && /^[[:space:]]*version[[:space:]]*=/ {
          line = $0
          if (line !~ /^[[:space:]]*version[[:space:]]*=[[:space:]]*"[^"]+"[[:space:]]*$/) exit
          sub(/^[[:space:]]*version[[:space:]]*=[[:space:]]*"/, "", line)
          sub(/"[[:space:]]*$/, "", line)
          print line
          exit
        }
      ' pyproject.toml 2>/dev/null)"
      ;;
  esac

  [ "${#version}" -le 128 ] || return 1
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$ ]] || return 1
  printf '%s' "$version"
}

# -----------------------------------------------------------------------------
# 2. Load pinned versions
# -----------------------------------------------------------------------------
if [ ! -f versions.env ]; then
  die "versions.env not found in $SCRIPT_DIR." \
      "Run: git pull   (then re-run ./update.sh)"
fi
# shellcheck disable=SC1091  # versions.env is runtime-provided KEY=VALUE data, not a script
set -a && . ./versions.env && set +a

if ! CHECKOUT_APP_VERSION="$(resolve_checkout_app_version)"; then
  die "Could not determine a valid application version from this checkout." \
      "Use an exact vMAJOR.MINOR.PATCH[-PRERELEASE] tag, or fix [project].version in pyproject.toml."
fi
PREVIOUS_APP_VERSION="$(sed -n 's/^JARVIS_VERSION=//p' .env 2>/dev/null | head -1)"
if ! printf '%s' "$PREVIOUS_APP_VERSION" \
    | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$'; then
  PREVIOUS_APP_VERSION=""
fi
export JARVIS_VERSION="$CHECKOUT_APP_VERSION"

command -v docker >/dev/null 2>&1 \
  || die "Docker not found in PATH." \
         "Install Docker Engine: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 required ('docker compose' plugin)." \
         "Install it: https://docs.docker.com/compose/install/"
docker info >/dev/null 2>&1 \
  || die "The Docker daemon is not reachable." \
         "Start Docker, then re-run: ./update.sh"

if printf '%s' "${JARVIS_UPDATE_VOLUME_GUARD_ID:-}" | grep -Eq '^[0-9a-f]{32}$' \
    && lifecycle_update_guard_is_promoted \
         "$SCRIPT_DIR" "$JARVIS_UPDATE_VOLUME_GUARD_ID"; then
  # jarvis-research owns the promoted sidecar lease and inherited host lock.
  [ "${JARVIS_TRANSACTIONAL_UPDATE:-0}" != 1 ] || UPDATE_MANAGED_TRANSACTION=1
else
  _update_lock_rc=0
  claim_lifecycle_operation "$SCRIPT_DIR" direct-update || _update_lock_rc=$?
  case "$_update_lock_rc" in
    0) UPDATE_LIFECYCLE_OWNED=1; trap _cleanup_direct_update_lifecycle EXIT ;;
    3|4) die "Another lifecycle operation is active or needs recovery." \
           "No services were changed. Finish it, then retry." ;;
    *) die "The private lifecycle volume is unavailable or unsafe." \
         "No services were changed. Run: jarvis-research doctor" ;;
  esac
fi

# Every published service pairs `pull_policy: missing` with a `build:` block, so a
# missing image turns any `up` into a silent multi-GB rebuild. Guard every bring-up
# below unless the user explicitly asked to build from source.
UP_NO_BUILD=()
[ "$BUILD_LOCAL" -eq 1 ] || UP_NO_BUILD=(--no-build)

# v1.2 narrows proxy trust to exact pinned container addresses. Older installs
# recorded only JARVIS_NET_SUBNET, so materialize those peers before any Compose
# pull or recreate can resolve the new network configuration.
_ingress_keys_missing=0
for _key in JARVIS_NET_GATEWAY_IP JARVIS_CADDY_IP JARVIS_CADDY_LOCAL_IP \
  JARVIS_DASHBOARD_IP JARVIS_CLOUDFLARED_IP; do
  grep -q "^${_key}=" .env 2>/dev/null || _ingress_keys_missing=1
done
if ! sync_ingress_ips_from_env; then
  die "Could not derive trusted ingress addresses from JARVIS_NET_SUBNET." \
      "JARVIS_NET_SUBNET must be a valid IPv4 /27 or larger network, such as 10.137.241.0/24."
fi
if [ "$_ingress_keys_missing" -eq 1 ]; then
  info "Recorded this install's trusted ingress addresses in .env."
fi

# A pre-1.1 .env carries no TORCH_VARIANT, so the image tag would resolve to the
# CPU flavour even on a CUDA host. Backfill BEFORE anything resolves an image —
# section 4 below already starts services, and cloudflared depends on dashboard.
if _bf_variant="$(backfill_torch_variant_from_env)" && [ -n "$_bf_variant" ]; then
  info "Recorded this host's torch image variant in .env: ${_bf_variant}"
fi

# Lookup current image for a compose service. Prints the image string or empty.
# Uses `docker compose ps -q` so we work regardless of the compose project name.
get_running_image() {
  local svc="$1"
  local cid
  cid="$(docker compose ps -q "$svc" 2>/dev/null | head -n 1 || true)"
  if [ -z "$cid" ]; then
    printf ''
    return
  fi
  docker inspect --format '{{.Config.Image}}' "$cid" 2>/dev/null || printf ''
}

# -----------------------------------------------------------------------------
# 3. Service → version-var mapping (topology-aware).
# -----------------------------------------------------------------------------
# Parallel arrays keep ordering deterministic and avoid assoc-array iter gotchas.
# The always-on third-party set plus the disaster-recovery backup sidecar, which
# shares the Postgres pin and drifts otherwise.
SERVICES=(postgres        ollama       qdrant       litellm       postgres-backup)
VAR_NAMES=(POSTGRES_IMAGE OLLAMA_IMAGE QDRANT_IMAGE LITELLM_IMAGE POSTGRES_IMAGE)

# Optional third-party services (an active TLS edge, the observability stack) are
# reconciled only when actually deployed — a running container exists — so an
# install that never enabled them stays quiet instead of reporting them "not
# running". Their distinctive image is pinned in versions.env like the rest.
OPTIONAL_TP_SVCS=(cloudflared       caddy       caddy_local vector       langfuse-postgres)
OPTIONAL_TP_VARS=(CLOUDFLARED_IMAGE CADDY_IMAGE CADDY_IMAGE VECTOR_IMAGE LANGFUSE_POSTGRES_IMAGE)
for _i in "${!OPTIONAL_TP_SVCS[@]}"; do
  if [ -n "$(get_running_image "${OPTIONAL_TP_SVCS[$_i]}")" ]; then
    SERVICES+=("${OPTIONAL_TP_SVCS[$_i]}")
    VAR_NAMES+=("${OPTIONAL_TP_VARS[$_i]}")
  fi
done

# The full third-party set for this run, used to classify recovery guidance
# (versions.env pins vs. JARVIS_VERSION-tagged application images).
THIRD_PARTY_SET="${SERVICES[*]}"

# In v1.2 the trusted ingress containers and dashboard use pinned source IPs.
# Recreate every active edge in the same Compose transaction as dashboard so an
# upgrade cannot leave an old container attached at a now-untrusted address.
ACTIVE_INGRESS_SERVICES=()
for svc in caddy caddy_local cloudflared; do
  if [ -n "$(get_running_image "$svc")" ]; then
    ACTIVE_INGRESS_SERVICES+=("$svc")
  fi
done

# Columns for diff table.
printf '\n%s%-18s %-40s %-40s %s%s\n' "$C_BOLD" "SERVICE" "RUNNING" "PINNED" "STATUS" "$C_RESET"
printf '%s\n' "----------------------------------------------------------------------------------------------------------"

TP_TO_UPDATE=()

for idx in "${!SERVICES[@]}"; do
  svc="${SERVICES[$idx]}"
  var="${VAR_NAMES[$idx]}"
  pinned="${!var:-}"
  running="$(get_running_image "$svc")"

  # ollama has a ROCm variant (docker-compose.rocm.yml) pinned separately as
  # OLLAMA_ROCM_IMAGE. When the running image is that flavour, diff and update it
  # against the ROCm pin so an AMD host is not perpetually "update available" and
  # actually moves to the tracked ROCm version rather than the CPU one.
  if [ "$svc" = "ollama" ] && [[ "$running" == *-rocm ]]; then
    pinned="${OLLAMA_ROCM_IMAGE:-$pinned}"
  fi

  if [ -z "$pinned" ]; then
    status_color="$C_YELLOW"
    status_text="no pin — skipped"
  elif [ -z "$running" ]; then
    status_color="$C_RED"
    status_text="not running"
  elif [ "$running" = "$pinned" ]; then
    status_color="$C_GREEN"
    status_text="up-to-date"
  else
    status_color="$C_YELLOW"
    status_text="update available"
    TP_TO_UPDATE+=("$svc")
  fi

  printf '%-18s %-40s %-40s %s%s%s\n' \
    "$svc" \
    "${running:-<none>}" \
    "${pinned:-<unpinned>}" \
    "$status_color" "$status_text" "$C_RESET"
done
printf '\n'

# -----------------------------------------------------------------------------
# 4. Decide what to refresh (third-party pins + application images).
# -----------------------------------------------------------------------------
# Nothing is pulled or recreated yet: all decisions are taken first, then every
# image is STAGED (pulled/built), and only after that is anything recreated. A
# staging failure therefore leaves the whole running cohort untouched.
DO_TP=0
if [ "${#TP_TO_UPDATE[@]}" -gt 0 ]; then
  info "Updates available for: ${TP_TO_UPDATE[*]}"
  if confirm "Pull and restart affected services? (y/N): "; then
    DO_TP=1
  else
    info "Skipped third-party image pull."
    TP_TO_UPDATE=()
  fi
else
  ok "All pinned third-party services up to date."
fi

# Application services — refresh them to this checkout's images. These are
# published as prebuilt images from this repo; `git pull` may have moved them to a
# newer version without touching versions.env, so always offer to refresh.
# Telegram is optional and only included when a token is configured; its profile
# must be named explicitly or Compose hides the service. Langfuse is the hardened,
# JARVIS_VERSION-tagged observability image; it is local-build only, so it is
# refreshed exclusively on the --build-local path and only when deployed.
APP_SERVICES=("${PUBLISHED_SERVICES_BASE[@]}")
APP_PROFILE_ARGS=()
if [ -f .env ] && grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' .env; then
  APP_SERVICES+=("$PUBLISHED_SERVICE_TELEGRAM")
  APP_PROFILE_ARGS+=(--profile telegram)
fi
if [ "$BUILD_LOCAL" -eq 1 ] && [ -n "$(get_running_image langfuse)" ]; then
  APP_SERVICES+=(langfuse)
  APP_PROFILE_ARGS+=(--profile observability)
fi

printf '\n'
DO_APP=0
if [ "$BUILD_LOCAL" -eq 1 ]; then
  if confirm "Rebuild application services from source (${APP_SERVICES[*]})? (y/N): "; then
    DO_APP=1
  fi
else
  if confirm "Pull the published application images (${APP_SERVICES[*]}) and restart? (y/N): "; then
    DO_APP=1
  fi
fi
[ "$DO_APP" -eq 1 ] || info "Skipped — running application containers keep their current images."

if [ "$DO_TP" -eq 0 ] && [ "$DO_APP" -eq 0 ]; then
  ok "Nothing to do."
  exit 0
fi

# -----------------------------------------------------------------------------
# 5. Stage every image FIRST — all pulls/builds complete before any recreate.
# -----------------------------------------------------------------------------
if [ "$DO_TP" -eq 1 ]; then
  info "Pulling third-party images..."
  if ! docker compose pull "${TP_TO_UPDATE[@]}"; then
    fail_with_recovery "docker compose pull failed." \
        "Check network / registry auth, then re-run ./update.sh" \
        "${TP_TO_UPDATE[@]}"
  fi
fi

if [ "$DO_APP" -eq 1 ]; then
  if [ "$BUILD_LOCAL" -eq 1 ]; then
    info "Building application images from source..."
    if ! docker compose ${APP_PROFILE_ARGS[@]+"${APP_PROFILE_ARGS[@]}"} build "${APP_SERVICES[@]}"; then
      fail_with_recovery "docker compose build failed." \
          "Inspect output above; re-run ./update.sh after fixing." \
          "${APP_SERVICES[@]}"
    fi
  else
    info "Pulling published application images..."
    if ! docker compose ${APP_PROFILE_ARGS[@]+"${APP_PROFILE_ARGS[@]}"} pull "${APP_SERVICES[@]}"; then
      fail_with_recovery "docker compose pull failed." \
          "Check network access to ghcr.io, then re-run ./update.sh — or build from source: ./update.sh --build-local" \
          "${APP_SERVICES[@]}"
    fi
  fi
fi

# -----------------------------------------------------------------------------
# 6. Recreate — every image is staged, so a bring-up only swaps containers.
# -----------------------------------------------------------------------------
UPDATE_MUTATION_STARTED=1
TO_UPDATE=()
if [ "$DO_TP" -eq 1 ]; then
  TP_RECREATE_SERVICES=()
  for svc in "${TP_TO_UPDATE[@]}"; do
    if [ "$DO_APP" -eq 1 ] && _env_key_in_list "$svc" "${ACTIVE_INGRESS_SERVICES[*]}"; then
      continue
    fi
    TP_RECREATE_SERVICES+=("$svc")
  done
  if [ "${#TP_RECREATE_SERVICES[@]}" -gt 0 ]; then
    info "Recreating third-party services..."
    if ! docker compose up -d ${UP_NO_BUILD[@]+"${UP_NO_BUILD[@]}"} \
        --no-deps "${TP_RECREATE_SERVICES[@]}"; then
      fail_with_recovery "docker compose up failed." \
          "Inspect logs: docker compose logs --tail=200 ${TP_RECREATE_SERVICES[*]}" \
          "${TP_RECREATE_SERVICES[@]}"
    fi
    TO_UPDATE+=("${TP_RECREATE_SERVICES[@]}")
  fi
fi

if [ "$DO_APP" -eq 1 ]; then
  APP_RECREATE_SERVICES=("${APP_SERVICES[@]}" "${ACTIVE_INGRESS_SERVICES[@]}")
  info "Recreating application services and active ingress..."
  if ! docker compose ${APP_PROFILE_ARGS[@]+"${APP_PROFILE_ARGS[@]}"} up -d ${UP_NO_BUILD[@]+"${UP_NO_BUILD[@]}"} "${APP_RECREATE_SERVICES[@]}"; then
    fail_with_recovery "docker compose up failed." \
        "Inspect logs: docker compose logs --tail=200 ${APP_RECREATE_SERVICES[*]}" \
        "${APP_RECREATE_SERVICES[@]}"
  fi
  TO_UPDATE+=("${APP_RECREATE_SERVICES[@]}")
fi

# -----------------------------------------------------------------------------
# 7. Health wait loop (per service, 180s budget, 3s interval).
# -----------------------------------------------------------------------------
UNVERIFIED=()
wait_healthy() {
  local svc="$1"
  local budget=180
  local interval=3
  local elapsed=0
  local cid status run_state

  while [ "$elapsed" -lt "$budget" ]; do
    cid="$(docker compose ps -q "$svc" 2>/dev/null | head -n 1 || true)"
    if [ -z "$cid" ]; then
      sleep "$interval"
      elapsed=$((elapsed + interval))
      continue
    fi
    # `.State.Health.Status` is empty when the image has no HEALTHCHECK.
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)"
    case "$status" in
      "")
        # No healthcheck: verify the container is at least RUNNING, and say so
        # plainly instead of silently claiming success — it is not health-verified.
        run_state="$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null || true)"
        if [ "$run_state" = "running" ]; then
          warn "$svc: running (no healthcheck) — readiness not verified"
          UNVERIFIED+=("$svc")
          return 0
        fi
        err "$svc: not running (state: ${run_state:-unknown}, no healthcheck)"
        return 1
        ;;
      healthy)   ok "$svc: healthy"; return 0 ;;
      starting)  ;;  # still coming up
      unhealthy) err "$svc: unhealthy"; return 1 ;;
      *)         ;;  # unknown states — keep polling
    esac
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  err "$svc: did not become healthy within ${budget}s."
  return 1
}

FAILED=()
for svc in "${TO_UPDATE[@]}"; do
  if ! wait_healthy "$svc"; then
    FAILED+=("$svc")
  fi
done

# -----------------------------------------------------------------------------
# 8. Report
# -----------------------------------------------------------------------------
if [ "${#FAILED[@]}" -eq 0 ]; then
  if [ "$DO_APP" -eq 1 ]; then
    if ! upsert_env_var JARVIS_VERSION "$CHECKOUT_APP_VERSION"; then
      err "Application services are healthy, but JARVIS_VERSION could not be recorded in .env."
      printf '        %sFix .env permissions, then re-run ./update.sh so future Compose commands keep version %s.%s\n' \
        "$C_YELLOW" "$CHECKOUT_APP_VERSION" "$C_RESET" >&2
      exit 1
    fi
  fi
  if [ "${#UNVERIFIED[@]}" -gt 0 ]; then
    ok "Updated ${#TO_UPDATE[@]} service(s); ${#UNVERIFIED[@]} running without a healthcheck (not health-verified): ${UNVERIFIED[*]}"
  else
    ok "Updated ${#TO_UPDATE[@]} service(s) successfully."
  fi
  install_cli_shim "$SCRIPT_DIR" || warn "Could not install the jarvis-research launcher (non-fatal)."
  exit 0
fi

printf '\n'
warn "The following service(s) failed health checks: ${FAILED[*]}"
if [ "$UPDATE_MANAGED_TRANSACTION" -eq 0 ]; then
  print_split_recovery "${FAILED[@]}"
fi
exit 1
