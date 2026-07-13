#!/usr/bin/env bash
# scripts/jarvis-setup.sh — non-interactive bootstrap (Linux / macOS).
#
# Goal: a clean install path for self-hosters. Get the Docker stack up
# and running with sane defaults, then point the user at the web wizard
# (frontend SetupPage / FirstRunSetupPage) for SMTP / admin email / cloud
# LLM keys.
#
# Idempotent on every step:
#   * .env is created from .env.example with strong random secrets ONLY when
#     it does not already exist — this script NEVER clobbers an existing
#     .env. (Contrast: ./setup.sh --non-interactive OVERWRITES .env keys
#     from its flags on every run, since it optimizes for scripted/CI
#     reprovisioning. This script optimizes for "run it again, nothing
#     changes" — pick whichever entry point matches your workflow.)
#   * mkcert is invoked only when present on PATH.
#   * docker compose up -d is a no-op when the stack is already running.
#
# Anything user-facing this script needs to interact with lives in the web
# wizard — this script only handles the layers Docker+TLS+secrets setup
# the wizard cannot reach.
#
# Compare with ./setup.sh: setup.sh is interactive and asks questions
# (access mode, Telegram token). This script is non-interactive and defers
# all those choices to the wizard. Use whichever matches your taste.
#
# Options:
#   --skip-disk-check   Skip the pre-install free-disk check on the Docker
#                       data root (see preflight_disk() below).

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck source=setup_lib.sh
source "${SCRIPT_DIR}/setup_lib.sh"

# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

info() { printf '%s[INFO]%s  %s\n'  "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s    %s\n'  "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[WARN]%s  %s\n'  "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[ERROR]%s %s\n'  "$C_RED"    "$C_RESET" "$*" >&2; }
die() {
  # $1 = message, $2 = next-step hint (matches setup.sh's die()).
  err "$1"
  printf '        %s%s%s\n' "$C_YELLOW" "$2" "$C_RESET" >&2
  exit 1
}

printf '%s================================================================%s\n' \
  "$C_BOLD" "$C_RESET"
printf '%s   JARVIS RD Assistant — non-interactive bootstrap                  %s\n' \
  "$C_BOLD" "$C_RESET"
printf '%s================================================================%s\n\n' \
  "$C_BOLD" "$C_RESET"

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
SKIP_DISK_CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-disk-check) SKIP_DISK_CHECK=1; shift ;;
    *) err "Unknown option: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Prerequisite: Docker
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  err "Docker is not installed."
  printf '\n        Install Docker Desktop (macOS / Windows): %s\n' \
    "https://www.docker.com/products/docker-desktop/"
  printf '        Install Docker Engine (Linux):              %s\n\n' \
    "https://docs.docker.com/engine/install/"
  exit 1
fi
DOCKER_VERSION=$(docker --version 2>/dev/null || echo unknown)
ok "Docker found: ${DOCKER_VERSION}"

if ! docker compose version >/dev/null 2>&1; then
  err "'docker compose' (V2 plugin) is missing. Update Docker Desktop or install"
  err "the docker-compose-plugin package."
  exit 1
fi
ok "docker compose plugin OK"

# ---------------------------------------------------------------------------
# .env generation (idempotent — never clobber; see the header comment above
# for how this diverges from setup.sh --non-interactive's overwrite semantics)
# ---------------------------------------------------------------------------
if [ -f .env ]; then
  ok ".env already exists — leaving it alone"
else
  if [ ! -f .env.example ]; then
    err ".env.example missing; cannot bootstrap. Are you in the repo root?"
    exit 1
  fi
  info "Generating .env from .env.example"
  cp .env.example .env
  chmod 600 .env
  ok "Created .env from .env.example (chmod 600) — secrets will be generated next"
fi

# A pre-1.1 .env carries no TORCH_VARIANT, so the paper-ingestion tag would
# resolve to the CPU flavour even on a kept GPU install — pulling a CPU image
# under a still-active GPU overlay. Backfill it (nvidia overlay -> cuda, else
# cpu) before anything resolves an image, exactly as setup.sh and update.sh do.
if _bf_variant="$(backfill_torch_variant_from_env)" && [ -n "$_bf_variant" ]; then
  info "Recorded this host's torch image variant in .env: ${_bf_variant}"
fi

# ---------------------------------------------------------------------------
# init-dirs (volume mount preconditions)
# ---------------------------------------------------------------------------
if [ -x scripts/init-dirs.sh ]; then
  info "Creating shared volume directories"
  bash scripts/init-dirs.sh
fi

# ---------------------------------------------------------------------------
# init-secrets (Docker secret files — idempotent, no-ops on existing files)
# ---------------------------------------------------------------------------
if [ -x scripts/init-secrets.sh ]; then
  info "Generating Docker secret files"
  bash scripts/init-secrets.sh
fi

# ---------------------------------------------------------------------------
# mkcert (best-effort; skipped silently when absent)
# ---------------------------------------------------------------------------
if command -v mkcert >/dev/null 2>&1; then
  info "mkcert detected — installing local CA + minting localhost cert"
  if [ -x scripts/init-mkcert.sh ]; then
    bash scripts/init-mkcert.sh || warn "mkcert script returned non-zero; continuing with self-signed"
  else
    # Fallback path (init-mkcert.sh missing). Certs must land in ./certs as
    # cert.pem/key.pem — that is the mount Caddy's local profile and the
    # dashboard expect (docker-compose.yml:541,772; caddy/Caddyfile.local:19).
    mkcert -install >/dev/null 2>&1 || warn "mkcert -install failed (non-fatal)"
    mkdir -p certs
    mkcert -cert-file certs/cert.pem -key-file certs/key.pem \
      jarvis.localhost localhost 127.0.0.1 ::1 >/dev/null 2>&1 \
      || warn "mkcert localhost mint failed (non-fatal)"
  fi
  ok "Local TLS via mkcert ready"
else
  warn "mkcert not found — HTTPS will use the auto-generated self-signed cert."
  warn "Browsers will warn on first visit. Install mkcert for trusted local TLS:"
  warn "  https://github.com/FiloSottile/mkcert#installation"
fi

# ---------------------------------------------------------------------------
# Disk preflight (data-root free space vs. a cold install's footprint)
# ---------------------------------------------------------------------------
# Mirrors setup.sh's preflight_disk(): sizes the cold install for the default
# smart model (app-image budget + infra pulls + Ollama model set) and checks
# free space on the Docker data root — NOT `df .`, since images/volumes/models
# land on the data root, which is a different filesystem on split-mount hosts
# (resolve_docker_data_root, scripts/setup_lib.sh). Fatal only on a first
# install (no app image present yet); a re-run with cached images only warns.
# --skip-disk-check bypasses this entirely. The least-technical entry point
# must not retain the original ENOSPC failure mode.
preflight_disk() {
  if [ "$SKIP_DISK_CHECK" -eq 1 ]; then
    info "Skipping disk preflight (--skip-disk-check)."
    return 0
  fi
  # This launcher PULLS the CPU image set (no GPU overlay, no --build-local path),
  # so the cold-install footprint is the cpu-pull budget even on an NVIDIA host —
  # strictly smaller than a build, which acquires the build cache as well.
  local _variant="cpu-pull"
  local _req_gb _req_exact=1
  _req_gb="$(compute_required_disk_gb "qwen3:8b" "$_variant")" || _req_exact=0
  local _out _rc=0
  _out="$(preflight_disk_lib "$_req_gb")" || _rc=$?
  local _free_gb="${_out%% *}" _data_root="${_out#* }"
  case "$_rc" in
    0)
      ok "Disk preflight: ${_free_gb} GB free on ${_data_root} (~${_req_gb} GB needed)."
      return 0
      ;;
    2)
      warn "Disk preflight: could not measure free space on ${_data_root} — proceeding."
      return 0
      ;;
  esac
  # Shortfall. Cached app images mean this is a re-run, not a cold install —
  # the big layers are already on disk, so never block it. Key off the published
  # repositories the install actually pulls: the pre-1.1 `jarvis/*` names no
  # longer exist once images come from GHCR, so grepping them would leave this
  # escape hatch dead and make every low-disk re-run of a v1.1 install falsely fatal.
  local _img
  for _img in "${PUBLISHED_IMAGE_REPOS[@]}"; do
    if [ -n "$(docker images -q "$_img" 2>/dev/null)" ]; then
      warn "Low disk: ${_free_gb} GB free on ${_data_root} (a full reinstall needs ~${_req_gb} GB) — continuing, app images are already present."
      return 0
    fi
  done
  if [ "$_req_exact" -eq 0 ] && [ "$_free_gb" -ge 20 ]; then
    warn "Low disk: ${_free_gb} GB free on ${_data_root}; the ~${_req_gb} GB figure is a worst-case estimate (model catalog unreadable). Proceeding — watch free space during the install."
    return 0
  fi
  die "Not enough free disk for a first install: ${_free_gb} GB free on ${_data_root} (df -Pk), ~${_req_gb} GB needed for images + models." \
      "Free up space on ${_data_root} (e.g. docker system prune), move the Docker data root to a larger disk, or re-run with --skip-disk-check to proceed anyway."
}

preflight_disk

# ---------------------------------------------------------------------------
# Bring stack up
# ---------------------------------------------------------------------------
COMPOSE="docker compose"
COMPOSE="${COMPOSE} --env-file .env"
if [ -f versions.env ]; then
  COMPOSE="${COMPOSE} --env-file versions.env"
fi

# wait_healthy <svc> [budget_seconds]
# Poll Docker healthcheck for <svc> until healthy or timeout. Duplicated from
# setup.sh (setup.sh is a plain script, not cleanly sourceable — only the
# helpers in scripts/setup_lib.sh are shared between the two entry points).
wait_healthy() {
  local svc="$1"
  local budget="${2:-60}"
  local interval=3
  local elapsed=0
  local cid status

  while [ "$elapsed" -lt "$budget" ]; do
    cid="$(${COMPOSE} ps -q "$svc" 2>/dev/null | head -n 1 || true)"
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

# Detect already-running containers so we don't churn them.
RUNNING_CONTAINERS="$(${COMPOSE} ps --status running -q 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
if [ "${RUNNING_CONTAINERS}" -gt 0 ]; then
  ok "Stack already has ${RUNNING_CONTAINERS} running containers — re-running 'up -d' (idempotent)"
fi

# Start Ollama alone first, then run the model pull as an attached one-off so
# its progress streams to the terminal — buried inside a bare `up -d` the
# 7-11 GB first pull looks like a hang (mirrors setup.sh's streamed pull).
info "Starting Ollama: ${COMPOSE} up -d ollama"
${COMPOSE} up -d ollama
wait_healthy ollama 180 \
  || warn "Ollama is still starting — model inventory unknown; proceeding to the model pull."

_FIRST_RUN_PULL=0
if ${COMPOSE} exec -T ollama ollama list 2>/dev/null | tail -n +2 | grep -q .; then
  : # models already present — bootstrap below is a fast verify
else
  _FIRST_RUN_PULL=1
  printf '\n'
  printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
  printf '%s  Downloading models next (7-11 GB, 20-60 min).                 %s\n' "$C_YELLOW" "$C_RESET"
  printf '%s  Pull progress streams below. This is not an error.           %s\n' "$C_YELLOW" "$C_RESET"
  printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
  printf '\n'
fi

info "Pulling models via ollama-bootstrap..."
${COMPOSE} run --rm ollama-bootstrap

# Materialise the published images, then forbid a build. A bare `up -d` here would
# find them missing and SILENTLY BUILD them (`pull_policy: missing` + a `build:`
# block) — the multi-GB torch build, and the ENOSPC, that installing from prebuilt
# images exists to eliminate. This launcher has no --build-local path, so a failed
# pull must fail loudly rather than fall back. No profiles are engaged here, so the
# base set (no telegram_bot, no langfuse) is exactly what starts.
info "Pulling prebuilt images: ${PUBLISHED_SERVICES_BASE[*]}"
if ! ${COMPOSE} pull "${PUBLISHED_SERVICES_BASE[@]}"; then
  die "Image pull failed." \
      "Check network access to ghcr.io, then re-run this installer. To build from source instead, use ./setup.sh --build-local"
fi

info "Starting Docker Compose stack (${COMPOSE} up -d --no-build)"
${COMPOSE} up -d --no-build

# ---------------------------------------------------------------------------
# Wait for mandatory services to become healthy
# ---------------------------------------------------------------------------
MANDATORY_SVCS=(postgres ollama litellm paper_ingestion learning_engine dashboard)

printf '\n'
info "Waiting for services to become healthy..."

SETUP_FAILED=()
for svc in "${MANDATORY_SVCS[@]}"; do
  case "$svc" in
    ollama)                          _budget=180 ;;  # model pull can be slow
    paper_ingestion|learning_engine) [ "$_FIRST_RUN_PULL" -eq 1 ] && _budget=3600 || _budget=60 ;;
    *)                               _budget=60  ;;
  esac
  if ! wait_healthy "$svc" "$_budget"; then
    SETUP_FAILED+=("$svc")
    warn "Dumping last 50 log lines for $svc:"
    ${COMPOSE} logs --tail 50 "$svc" >&2 || true
  fi
done

if [ "${#SETUP_FAILED[@]}" -gt 0 ]; then
  printf '\n'
  err "The following service(s) did not become healthy: ${SETUP_FAILED[*]}"
  cat >&2 <<EOF

Recovery steps:
  1. Check full logs:   docker compose logs --tail=200 ${SETUP_FAILED[*]}
  2. Verify .env has correct values and re-run: ./scripts/jarvis-setup.sh
  3. For Ollama model pull issues, run manually: docker compose exec ollama ollama pull <model>
EOF
  exit 1
fi

# ---------------------------------------------------------------------------
# Final pointer
# ---------------------------------------------------------------------------
DASHBOARD_HOST_PORT="$(
  awk -F= '/^DASHBOARD_HOST_PORT=/{gsub(/["'\'']/, "", $2); print $2; exit}' .env 2>/dev/null || true
)"
DASHBOARD_HOST_PORT="${DASHBOARD_HOST_PORT:-3001}"
DASHBOARD_URL="http://localhost:${DASHBOARD_HOST_PORT}/"

printf '\n'
printf '%s================================================================%s\n' \
  "$C_GREEN" "$C_RESET"
printf '%sJARVIS is up.%s Open %s%s%s to finish setup.\n' \
  "${C_BOLD}" "${C_RESET}" "${C_BOLD}" "${DASHBOARD_URL}" "${C_RESET}"
printf 'The first-run web wizard will walk you through SMTP, the admin email,\n'
printf 'and (optionally) cloud LLM provider keys.\n'
printf '%s================================================================%s\n' \
  "$C_GREEN" "$C_RESET"
