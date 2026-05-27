#!/usr/bin/env bash
# update.sh — pull newer pinned images from versions.env and restart.
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

# -----------------------------------------------------------------------------
# 3. Service → version-var mapping
# -----------------------------------------------------------------------------
# Parallel arrays keep ordering deterministic and avoid assoc-array iter gotchas.
SERVICES=(postgres  ollama       qdrant       litellm       n8n       cloudflared)
VAR_NAMES=(POSTGRES_IMAGE OLLAMA_IMAGE QDRANT_IMAGE LITELLM_IMAGE N8N_IMAGE CLOUDFLARED_IMAGE)

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
      info "Recreating services..."
      if ! docker compose up -d "${TO_UPDATE[@]}"; then
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
# 4b. Locally-built services — rebuild if the user wants to pick up code changes.
# -----------------------------------------------------------------------------
# These core services build from this repo. `git pull` may have changed their
# source without changing versions.env, so we always offer to rebuild them.
# Telegram is optional and intentionally excluded from the default path because
# testing installs often do not set TELEGRAM_BOT_TOKEN.
LOCAL_SERVICES=(paper_ingestion learning_engine dashboard)
LOCAL_LABEL="paper_ingestion, learning_engine, dashboard"
if [ -f .env ] && grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' .env; then
  LOCAL_SERVICES+=(telegram_bot)
  LOCAL_LABEL="${LOCAL_LABEL}, telegram_bot"
fi
printf '\n'
read -rp "Rebuild locally-built services (${LOCAL_LABEL}) to pick up code changes? (y/N): " reply
case "$reply" in
  [yY]|[yY][eE][sS])
    info "Building local images..."
    if ! docker compose build "${LOCAL_SERVICES[@]}"; then
      die "docker compose build failed." \
          "Inspect output above; re-run ./update.sh after fixing."
    fi
    info "Recreating local services..."
    if ! docker compose up -d "${LOCAL_SERVICES[@]}"; then
      die "docker compose up failed." \
          "Inspect logs: docker compose logs --tail=200 ${LOCAL_SERVICES[*]}"
    fi
    TO_UPDATE+=("${LOCAL_SERVICES[@]}")
    ;;
  *)
    info "Skipped local rebuild — running containers keep their current code."
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
