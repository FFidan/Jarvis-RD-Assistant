#!/usr/bin/env bash
# scripts/init-secrets.sh — Idempotent secret bootstrapper.
#
# Generates missing secrets and appends them to .env without clobbering
# values that are already set.  Intended for CI, re-runs, and partial
# upgrades where only a subset of secrets is absent.
#
# Usage: bash scripts/init-secrets.sh
#
# This script MUST be run from the repo root (the directory that contains .env
# or .env.example).  It is safe to run multiple times — each block is guarded
# by a "not already set" check.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

# Colour helpers (same palette as setup.sh, gracefully degrades in CI).
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

# ---------------------------------------------------------------------------
# JARVIS_API_KEY — 32-byte hex master API key
# ---------------------------------------------------------------------------
if [ -z "${JARVIS_API_KEY:-}" ] && ! grep -q '^JARVIS_API_KEY=.\+' .env 2>/dev/null; then
  echo "JARVIS_API_KEY=$(openssl rand -hex 32)" >> .env
  ok "JARVIS_API_KEY generated and appended to .env."
else
  info "JARVIS_API_KEY already set — skipping."
fi

# Reconcile secrets file if it exists
if [ -f "secrets/jarvis_api_key.txt" ]; then
  env_key=$(grep '^JARVIS_API_KEY=' .env 2>/dev/null | cut -d'=' -f2)
  file_key=$(cat secrets/jarvis_api_key.txt 2>/dev/null | tr -d '\n')
  if [ -n "$env_key" ] && [ "$env_key" != "$file_key" ]; then
    echo -n "$env_key" > secrets/jarvis_api_key.txt
    ok "secrets/jarvis_api_key.txt synced to match JARVIS_API_KEY in .env"
  fi
fi

# ---------------------------------------------------------------------------
# LITELLM_MASTER_KEY — 32-byte hex key for LiteLLM admin endpoints
# ---------------------------------------------------------------------------
if [ -z "${LITELLM_MASTER_KEY:-}" ] && ! grep -q '^LITELLM_MASTER_KEY=' .env 2>/dev/null; then
  echo "LITELLM_MASTER_KEY=$(openssl rand -hex 32)" >> .env
  ok "LITELLM_MASTER_KEY generated and appended to .env."
else
  info "LITELLM_MASTER_KEY already set — skipping."
fi

# Reconcile secrets file if it exists
if [ -f "secrets/litellm_master_key.txt" ]; then
  env_key=$(grep '^LITELLM_MASTER_KEY=' .env 2>/dev/null | cut -d'=' -f2)
  file_key=$(cat secrets/litellm_master_key.txt 2>/dev/null | tr -d '\n')
  if [ -n "$env_key" ] && [ "$env_key" != "$file_key" ]; then
    echo -n "$env_key" > secrets/litellm_master_key.txt
    ok "secrets/litellm_master_key.txt synced to match LITELLM_MASTER_KEY in .env"
  fi
fi

# ---------------------------------------------------------------------------
# JARVIS_CONFIG_KEY — Fernet base64 key for at-rest secret encryption
# ---------------------------------------------------------------------------
if [ -z "${JARVIS_CONFIG_KEY:-}" ] && ! grep -q '^JARVIS_CONFIG_KEY=.\+' .env 2>/dev/null; then
  echo "JARVIS_CONFIG_KEY=$(openssl rand -base64 32)" >> .env
  ok "JARVIS_CONFIG_KEY generated and appended to .env."
else
  info "JARVIS_CONFIG_KEY already set — skipping."
fi
