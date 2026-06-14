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
#     it does not already exist.
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

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

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

printf '%s================================================================%s\n' \
  "$C_BOLD" "$C_RESET"
printf '%s   JARVIS RD Assistant — non-interactive bootstrap                  %s\n' \
  "$C_BOLD" "$C_RESET"
printf '%s================================================================%s\n\n' \
  "$C_BOLD" "$C_RESET"

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
# .env generation (idempotent — never clobber)
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
    mkcert -install >/dev/null 2>&1 || warn "mkcert -install failed (non-fatal)"
    mkdir -p caddy/data
    (cd caddy/data && mkcert localhost 127.0.0.1 >/dev/null 2>&1) \
      || warn "mkcert localhost mint failed (non-fatal)"
  fi
  ok "Local TLS via mkcert ready"
else
  warn "mkcert not found — HTTPS will use the auto-generated self-signed cert."
  warn "Browsers will warn on first visit. Install mkcert for trusted local TLS:"
  warn "  https://github.com/FiloSottile/mkcert#installation"
fi

# ---------------------------------------------------------------------------
# Bring stack up
# ---------------------------------------------------------------------------
COMPOSE="docker compose"
COMPOSE="${COMPOSE} --env-file .env"
if [ -f versions.env ]; then
  COMPOSE="${COMPOSE} --env-file versions.env"
fi

# Detect already-running containers so we don't churn them.
RUNNING_CONTAINERS="$(${COMPOSE} ps --status running -q 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
if [ "${RUNNING_CONTAINERS}" -gt 0 ]; then
  ok "Stack already has ${RUNNING_CONTAINERS} running containers — re-running 'up -d' (idempotent)"
fi

info "Starting Docker Compose stack (${COMPOSE} up -d)"
${COMPOSE} up -d

# ---------------------------------------------------------------------------
# Wait for the dashboard to come up
# ---------------------------------------------------------------------------
DASHBOARD_HOST_PORT="$(
  awk -F= '/^DASHBOARD_HOST_PORT=/{gsub(/["'\'']/, "", $2); print $2; exit}' .env 2>/dev/null || true
)"
DASHBOARD_HOST_PORT="${DASHBOARD_HOST_PORT:-3001}"
DASHBOARD_URL="http://localhost:${DASHBOARD_HOST_PORT}/"
TIMEOUT_SECONDS=60
INTERVAL=3

info "Waiting up to ${TIMEOUT_SECONDS}s for the dashboard to respond at ${DASHBOARD_URL}"
elapsed=0
ready=false
while [ "${elapsed}" -lt "${TIMEOUT_SECONDS}" ]; do
  if curl -fs --max-time 3 "${DASHBOARD_URL}" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep "${INTERVAL}"
  elapsed=$((elapsed + INTERVAL))
done

if [ "${ready}" = "true" ]; then
  ok "Dashboard responded — JARVIS is up"
else
  warn "Dashboard did not respond within ${TIMEOUT_SECONDS}s."
  warn "Check 'docker compose logs -f dashboard' for boot diagnostics."
fi

# ---------------------------------------------------------------------------
# Final pointer
# ---------------------------------------------------------------------------
printf '\n'
printf '%s================================================================%s\n' \
  "$C_GREEN" "$C_RESET"
printf '%sJARVIS is starting.%s Open %s%s%s to finish setup.\n' \
  "${C_BOLD}" "${C_RESET}" "${C_BOLD}" "${DASHBOARD_URL}" "${C_RESET}"
printf 'The first-run web wizard will walk you through SMTP, the admin email,\n'
printf 'and (optionally) cloud LLM provider keys.\n'
printf '%s================================================================%s\n' \
  "$C_GREEN" "$C_RESET"
