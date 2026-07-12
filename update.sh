#!/usr/bin/env bash
# update.sh — pull newer pinned images from versions.env and restart.
#
#   --build-local   Rebuild the application images from source instead of pulling
#                   the prebuilt ones published to GHCR. Slower and needs far more
#                   disk; for development or an air-gapped host.
#
# Never auto-rollbacks. On failure, prints the exact command the operator
# should run. macOS-safe: no `sed -i`, no GNU-only flags.
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

die() {
  err "$1"
  printf '        %s%s%s\n' "$C_YELLOW" "$2" "$C_RESET" >&2
  exit 1
}

# Run from repo root.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD_LOCAL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --build-local) BUILD_LOCAL=1; shift ;;
    -h|--help)
      sed -n '/^# update.sh/,/^set -euo/{ /^#/!d; s/^# \{0,1\}//p; }' "$0"
      exit 0
      ;;
    *) die "Unknown flag: $1" "Run: $0 --help" ;;
  esac
done

# The .env writer has exactly one implementation; reuse it rather than growing a
# second one here.
# shellcheck source=scripts/setup_lib.sh
# shellcheck disable=SC1091  # resolved at runtime relative to SCRIPT_DIR
. scripts/setup_lib.sh

# -----------------------------------------------------------------------------
# 1-2. Load pinned versions
# -----------------------------------------------------------------------------
if [ ! -f versions.env ]; then
  die "versions.env not found in $SCRIPT_DIR." \
      "Run: git pull   (then re-run ./update.sh)"
fi
# shellcheck disable=SC1091  # versions.env is runtime-provided KEY=VALUE data, not a script
set -a && . ./versions.env && set +a

command -v docker >/dev/null 2>&1 \
  || die "Docker not found in PATH." \
         "Install Docker Engine: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 required ('docker compose' plugin)." \
         "Install it: https://docs.docker.com/compose/install/"

# Every published service pairs `pull_policy: missing` with a `build:` block, so a
# missing image turns any `up` into a silent multi-GB rebuild. Guard every bring-up
# below unless the user explicitly asked to build from source.
UP_NO_BUILD=()
[ "$BUILD_LOCAL" -eq 1 ] || UP_NO_BUILD=(--no-build)

# A pre-1.1 .env carries no TORCH_VARIANT, so the image tag would resolve to the
# CPU flavour even on a CUDA host. Backfill BEFORE anything resolves an image —
# section 4 below already starts services, and cloudflared depends on dashboard.
if _bf_variant="$(backfill_torch_variant_from_env)" && [ -n "$_bf_variant" ]; then
  info "Recorded this host's torch image variant in .env: ${_bf_variant}"
fi

# -----------------------------------------------------------------------------
# 3. Service → version-var mapping
# -----------------------------------------------------------------------------
# Parallel arrays keep ordering deterministic and avoid assoc-array iter gotchas.
SERVICES=(postgres  ollama       qdrant       litellm       cloudflared)
VAR_NAMES=(POSTGRES_IMAGE OLLAMA_IMAGE QDRANT_IMAGE LITELLM_IMAGE CLOUDFLARED_IMAGE)

# Columns for diff table.
printf '\n%s%-14s %-40s %-40s %s%s\n' "$C_BOLD" "SERVICE" "RUNNING" "PINNED" "STATUS" "$C_RESET"
printf '%s\n' "----------------------------------------------------------------------------------------------------------"

TO_UPDATE=()

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

for idx in "${!SERVICES[@]}"; do
  svc="${SERVICES[$idx]}"
  var="${VAR_NAMES[$idx]}"
  pinned="${!var:-}"
  running="$(get_running_image "$svc")"

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
    TO_UPDATE+=("$svc")
  fi

  printf '%-14s %-40s %-40s %s%s%s\n' \
    "$svc" \
    "${running:-<none>}" \
    "${pinned:-<unpinned>}" \
    "$status_color" "$status_text" "$C_RESET"
done
printf '\n'

# -----------------------------------------------------------------------------
# 4. Pull and restart pinned third-party services (if any are stale).
# -----------------------------------------------------------------------------
if [ "${#TO_UPDATE[@]}" -gt 0 ]; then
  info "Updates available for: ${TO_UPDATE[*]}"
  read -rp "Pull and restart affected services? (y/N): " reply
  case "$reply" in
    [yY]|[yY][eE][sS])
      info "Pulling images..."
      if ! docker compose pull "${TO_UPDATE[@]}"; then
        die "docker compose pull failed." \
            "Check network / registry auth, then re-run ./update.sh"
      fi
      # cloudflared depends on dashboard, so this bring-up can reach an application
      # service whose image may be absent — hence the same no-build guard.
      info "Recreating services..."
      if ! docker compose up -d ${UP_NO_BUILD[@]+"${UP_NO_BUILD[@]}"} "${TO_UPDATE[@]}"; then
        die "docker compose up failed." \
            "Inspect logs: docker compose logs --tail=200 ${TO_UPDATE[*]}"
      fi
      ;;
    *)
      info "Skipped third-party image pull."
      TO_UPDATE=()
      ;;
  esac
else
  ok "All pinned third-party services up to date."
fi

# -----------------------------------------------------------------------------
# 4b. Application services — refresh them to this checkout's images.
# -----------------------------------------------------------------------------
# These are published as prebuilt images from this repo. `git pull` may have moved
# them to a newer version without touching versions.env, so always offer to
# refresh. Telegram is optional and only included when a token is configured; its
# profile must be named explicitly or Compose hides the service.
APP_SERVICES=(paper_ingestion learning_engine dashboard restore-uploader)
APP_PROFILE_ARGS=()
if [ -f .env ] && grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' .env; then
  APP_SERVICES+=(telegram_bot)
  APP_PROFILE_ARGS+=(--profile telegram)
fi

printf '\n'
if [ "$BUILD_LOCAL" -eq 1 ]; then
  read -rp "Rebuild application services from source (${APP_SERVICES[*]})? (y/N): " reply
else
  read -rp "Pull the published application images (${APP_SERVICES[*]}) and restart? (y/N): " reply
fi
case "$reply" in
  [yY]|[yY][eE][sS])
    if [ "$BUILD_LOCAL" -eq 1 ]; then
      info "Building application images from source..."
      if ! docker compose ${APP_PROFILE_ARGS[@]+"${APP_PROFILE_ARGS[@]}"} build "${APP_SERVICES[@]}"; then
        die "docker compose build failed." \
            "Inspect output above; re-run ./update.sh after fixing."
      fi
    else
      info "Pulling published application images..."
      if ! docker compose ${APP_PROFILE_ARGS[@]+"${APP_PROFILE_ARGS[@]}"} pull "${APP_SERVICES[@]}"; then
        die "docker compose pull failed." \
            "Check network access to ghcr.io, then re-run ./update.sh — or build from source: ./update.sh --build-local"
      fi
    fi
    info "Recreating application services..."
    if ! docker compose ${APP_PROFILE_ARGS[@]+"${APP_PROFILE_ARGS[@]}"} up -d ${UP_NO_BUILD[@]+"${UP_NO_BUILD[@]}"} "${APP_SERVICES[@]}"; then
      die "docker compose up failed." \
          "Inspect logs: docker compose logs --tail=200 ${APP_SERVICES[*]}"
    fi
    TO_UPDATE+=("${APP_SERVICES[@]}")
    ;;
  *)
    info "Skipped — running application containers keep their current images."
    ;;
esac

if [ "${#TO_UPDATE[@]}" -eq 0 ]; then
  ok "Nothing to do."
  exit 0
fi

# -----------------------------------------------------------------------------
# 6. Health wait loop (per service, 180s budget, 3s interval).
# -----------------------------------------------------------------------------
wait_healthy() {
  local svc="$1"
  local budget=180
  local interval=3
  local elapsed=0
  local cid status

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
      "")        info "$svc: no healthcheck defined — skipping wait."; return 0 ;;
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
# 7. Report
# -----------------------------------------------------------------------------
if [ "${#FAILED[@]}" -eq 0 ]; then
  ok "Updated ${#TO_UPDATE[@]} service(s) successfully."
  exit 0
fi

printf '\n'
warn "The following service(s) failed health checks: ${FAILED[*]}"
cat <<EOF

To rollback the version pin and re-run the update:
  ${C_BOLD}git checkout HEAD~1 -- versions.env && ./update.sh${C_RESET}

To inspect logs for the failed service(s):
  docker compose logs --tail=200 ${FAILED[*]}
EOF
exit 1
