#!/usr/bin/env bash
# scripts/init-secrets.sh — Idempotent secret bootstrapper.
#
# Generates missing secrets, appends them to .env, and creates the
# secrets/ files that docker-compose.yml bind-mounts into containers.
# Safe to run multiple times on any machine — existing values and files
# are never clobbered, only created or synced when stale.
#
# Usage: bash scripts/init-secrets.sh
#
# Must be run from the repo root (the directory that contains .env).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

if [ -t 1 ]; then
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RESET=$'\033[0m'
else
  C_GREEN=""; C_YELLOW=""; C_RESET=""
fi

ok()   { printf '%s[OK]%s    %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
info() { printf '[INFO]  %s\n' "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }

command -v openssl >/dev/null 2>&1 \
  || { warn "openssl not found — cannot generate secrets."; exit 1; }

mkdir -p secrets

# ---------------------------------------------------------------------------
# sync_secret KEY FILENAME [GENERATOR]
#
#   1. If KEY is absent from .env:
#        - GENERATOR given  → generate value, append to .env
#        - No generator     → warn and skip (requires manual input)
#   2. If secrets/FILENAME does not exist → create from .env value
#   3. If secrets/FILENAME exists but content differs from .env → sync
# ---------------------------------------------------------------------------
sync_secret() {
  local key="$1" file="secrets/$2" generator="${3:-}"

  # Step 1 — ensure value exists in .env
  if ! grep -q "^${key}=.\+" .env 2>/dev/null; then
    if [ -n "$generator" ]; then
      echo "${key}=$(eval "$generator")" >> .env
      ok "${key} generated and appended to .env."
    else
      warn "${key} not in .env and cannot be auto-generated — set it manually then re-run."
      return
    fi
  else
    info "${key} already in .env — skipping generation."
  fi

  # Step 2+3 — create or sync the secrets file
  local val
  val=$(grep "^${key}=" .env | cut -d'=' -f2- | tr -d '\n')
  if [ -z "$val" ]; then
    warn "Could not read ${key} from .env — skipping ${file}."
    return
  fi

  if [ ! -f "$file" ]; then
    printf '%s' "$val" > "$file"
    chmod 600 "$file"
    ok "${file} created."
  elif [ "$(tr -d '\n' < "$file")" != "$val" ]; then
    printf '%s' "$val" > "$file"
    ok "${file} synced to match ${key} in .env."
  else
    info "${file} already in sync."
  fi
}

# ---------------------------------------------------------------------------
# Auto-generated secrets
# ---------------------------------------------------------------------------
sync_secret JARVIS_API_KEY     jarvis_api_key.txt     "openssl rand -hex 32"
sync_secret LITELLM_MASTER_KEY litellm_master_key.txt "openssl rand -hex 32"
sync_secret POSTGRES_PASSWORD  postgres_password.txt  "openssl rand -hex 24"
sync_secret QDRANT_API_KEY     qdrant_api_key.txt     "openssl rand -hex 24"

# JARVIS_CONFIG_KEY is a Fernet key passed as a plain env var — no secrets file needed.
if ! grep -q '^JARVIS_CONFIG_KEY=.\+' .env 2>/dev/null; then
  echo "JARVIS_CONFIG_KEY=$(openssl rand -base64 32)" >> .env
  ok "JARVIS_CONFIG_KEY generated and appended to .env."
else
  info "JARVIS_CONFIG_KEY already in .env — skipping."
fi

# ---------------------------------------------------------------------------
# Manual secrets (cannot be auto-generated)
# ---------------------------------------------------------------------------
sync_secret TELEGRAM_BOT_TOKEN telegram_bot_token.txt
