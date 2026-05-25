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
  # Use POSIX ERE (-E) so "+" is a quantifier on both GNU and BSD/macOS grep.
  # GNU BRE "\+" is a literal "+" on macOS and would misfire, causing duplicate
  # KEY= entries on re-run (INST-1).
  if ! grep -qE "^${key}=.+" .env 2>/dev/null; then
    if [ -n "$generator" ]; then
      # Dispatch on key name to avoid eval (SH-1).  Each branch runs the same
      # literal openssl command that was previously held in $generator, so
      # output is byte-identical.  The $generator parameter is retained for the
      # "is a generator provided?" guard above and for self-documentation; it is
      # no longer executed.
      local value
      case "$key" in
        JARVIS_API_KEY)
          value=$(openssl rand -hex 32) ;;
        LITELLM_MASTER_KEY)
          value=$(openssl rand -hex 32) ;;
        POSTGRES_PASSWORD)
          value=$(openssl rand -hex 24) ;;
        QDRANT_API_KEY)
          value=$(openssl rand -hex 24) ;;
        INFRA_INGEST_KEY)
          value=$(openssl rand -hex 32) ;;
        VECTOR_WRITER_PASSWORD)
          value=$(openssl rand -hex 32) ;;
        JARVIS_CONFIG_KEY)
          value=$(openssl rand -base64 32 | tr -d '\n') ;;
        JARVIS_MODEL_HMAC_KEY)
          value=$(openssl rand -hex 32) ;;
        LANGFUSE_NEXTAUTH_SECRET)
          value=$(openssl rand -hex 32) ;;
        LANGFUSE_SALT)
          value=$(openssl rand -hex 16) ;;
        LANGFUSE_PG_PASSWORD)
          value=$(openssl rand -hex 24) ;;
        N8N_ENCRYPTION_KEY)
          value=$(openssl rand -base64 32 | tr -d '\n') ;;
        N8N_JWT_SECRET)
          value=$(openssl rand -hex 32) ;;
        BACKUP_ENCRYPT_KEY)
          value=$(openssl rand -base64 32 | tr -d '\n') ;;
        *)
          warn "${key} has a generator but is not in the dispatch table — skipping to avoid mis-generation."
          return
          ;;
      esac
      echo "${key}=${value}" >> .env
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
  val=$(grep "^${key}=" .env | head -n 1 | cut -d'=' -f2- | tr -d '\n')
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
# INFRA_INGEST_KEY authenticates the Vector log-shipper sidecar to POST /infra-events.
# Mounted by paper_ingestion and vector services via Docker Secret.
sync_secret INFRA_INGEST_KEY   infra_ingest_key.txt   "openssl rand -hex 32"
# VECTOR_WRITER_PASSWORD is declared in the compose secrets block; generate it
# so docker compose up never fails with a missing file error.
sync_secret VECTOR_WRITER_PASSWORD vector_writer_password.txt "openssl rand -hex 32"

# JARVIS_CONFIG_KEY is the Fernet write-key for the user_config table.
# sync_secret preserves any existing .env value verbatim; rotating this key
# would render every encrypted user_config row unreadable, so we never
# regenerate when an existing value is present.  ``Fernet.generate_key()``
# emits the same shape as ``openssl rand -base64 32`` (32 bytes urlsafe-b64).
sync_secret JARVIS_CONFIG_KEY  jarvis_config_key.txt  "openssl rand -base64 32 | tr -d '\\n'"

# JARVIS_MODEL_HMAC_KEY signs the Pulse classifier pickle blobs (HMAC-SHA256).
# Mandatory in production (auth.py / pulse/training.py refuse to start without
# it); 32 bytes hex = 64 chars, comfortably above the 32-char minimum.
sync_secret JARVIS_MODEL_HMAC_KEY jarvis_model_hmac_key.txt "openssl rand -hex 32"

# ---------------------------------------------------------------------------
# Langfuse observability (--profile observability)
# ---------------------------------------------------------------------------
# Langfuse's image does not honour the _FILE convention — secrets are now
# injected via an entrypoint shim that reads them from /run/secrets/*.
# We still write values into .env (sync_secret does that on the first leg)
# and mirror copies into ./secrets/ so the Docker Secret bind-mount works.
sync_secret LANGFUSE_NEXTAUTH_SECRET langfuse_nextauth_secret.txt "openssl rand -hex 32"
sync_secret LANGFUSE_SALT            langfuse_salt.txt            "openssl rand -hex 16"
sync_secret LANGFUSE_PG_PASSWORD     langfuse_pg_password.txt     "openssl rand -hex 24"

# ---------------------------------------------------------------------------
# n8n workflow automation (--profile n8n)
# ---------------------------------------------------------------------------
sync_secret N8N_ENCRYPTION_KEY n8n_encryption_key.txt "openssl rand -base64 32 | tr -d '\\n'"
sync_secret N8N_JWT_SECRET     n8n_jwt_secret.txt     "openssl rand -hex 32"

# ---------------------------------------------------------------------------
# Cloudflare Tunnel (--profile tunnel)
# ---------------------------------------------------------------------------
# CLOUDFLARE_TUNNEL_TOKEN must be obtained from the Cloudflare Zero Trust
# dashboard (Networks → Tunnels → Create tunnel → token).  It cannot be
# auto-generated locally.
sync_secret CLOUDFLARE_TUNNEL_TOKEN cloudflare_tunnel_token.txt

# ---------------------------------------------------------------------------
# Backup encryption (--profile backup)
# ---------------------------------------------------------------------------
# Passphrase file for AES-256-CBC backup encryption (PBKDF2, 600k iterations).
# Auto-generated on first run; keep a copy offsite — losing this key means
# losing access to all encrypted backup archives.
sync_secret BACKUP_ENCRYPT_KEY backup_encrypt_key.txt "openssl rand -base64 32 | tr -d '\\n'"

# ---------------------------------------------------------------------------
# Manual secrets (cannot be auto-generated)
# ---------------------------------------------------------------------------
sync_secret TELEGRAM_BOT_TOKEN telegram_bot_token.txt

# paper_ingestion declares telegram_bot_token as a Docker Secret (not profile-
# gated), so docker compose up aborts with "secret not found" when the file is
# absent — even when the Telegram profile is not active.  If sync_secret above
# skipped creation (no token in .env and no generator), create an empty
# placeholder so compose can mount the secret without error.  An empty file
# resolves to None in SecretsSettings._resolve_file_indirection, which is the
# correct sentinel for "Telegram not configured".
if [ ! -f "secrets/telegram_bot_token.txt" ]; then
  : > secrets/telegram_bot_token.txt
  chmod 600 secrets/telegram_bot_token.txt
  info "secrets/telegram_bot_token.txt created as empty placeholder (Telegram not configured)."
fi
