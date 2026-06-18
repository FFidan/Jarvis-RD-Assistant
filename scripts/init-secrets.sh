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
[ -f .env ] || touch .env

FAILED=0

# ---------------------------------------------------------------------------
# upsert_env_var KEY VALUE — write KEY=VALUE into .env idempotently:
# replace the first existing KEY= line IN PLACE (so an empty ``KEY=`` placeholder
# from .env.example is filled, not duplicated), drop any further KEY= lines
# (self-heals a .env already corrupted by an append-only writer), and append
# KEY=VALUE if the key is absent.  awk index()==1 is a literal prefix match, so
# there are no regex/escaping hazards from base64 values.
# ---------------------------------------------------------------------------
upsert_env_var() {
  local k="$1" v="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$k" -v v="$v" '
    index($0, k "=") == 1 { if (!seen) { print k "=" v; seen = 1 } ; next }
    { print }
    END { if (!seen) print k "=" v }
  ' .env > "$tmp"
  mv "$tmp" .env
}

# ---------------------------------------------------------------------------
# sync_secret KEY FILENAME [GENERATOR]
#
#   1. Resolve a non-empty value for KEY:
#        - KEY already set in .env → reuse it (preserve; never rotate)
#        - empty placeholder / absent + GENERATOR → generate
#        - empty placeholder / absent + no generator → warn, skip (manual input)
#      The value is written into .env via upsert_env_var (idempotent; no dupes).
#   2. Write secrets/FILENAME from the IN-MEMORY value — never a re-read of .env,
#      which is what silently skipped core secrets when an empty placeholder
#      shadowed the generated line.  An empty value is a loud failure (FAILED=1).
# ---------------------------------------------------------------------------
sync_secret() {
  local key="$1" file="secrets/$2" generator="${3:-}"
  local value=""

  # Step 1 — resolve a non-empty value.  ``^KEY=.+`` (ERE) matches only a
  # non-empty assignment, so an empty ``KEY=`` placeholder (straight from
  # .env.example) is treated as "needs a value", not "already set".
  if grep -qE "^${key}=.+" .env 2>/dev/null; then
    value=$(grep -E "^${key}=.+" .env | head -n 1 | cut -d'=' -f2- | tr -d '\r\n')
    upsert_env_var "${key}" "${value}"   # collapse any duplicate / empty lines
    info "${key} already in .env — preserving."
  elif [ -n "$generator" ]; then
    # Dispatch on key name to avoid eval (SH-1); each branch is byte-identical
    # to the openssl command documented in the call site's $generator argument.
    case "$key" in
      JARVIS_API_KEY)              value=$(openssl rand -hex 32) ;;
      LITELLM_MASTER_KEY)          value=$(openssl rand -hex 32) ;;
      LITELLM_SALT_KEY)            value=$(openssl rand -hex 32) ;;
      POSTGRES_PASSWORD)           value=$(openssl rand -hex 24) ;;
      QDRANT_API_KEY)              value=$(openssl rand -hex 24) ;;
      INFRA_INGEST_KEY)            value=$(openssl rand -hex 32) ;;
      JARVIS_CONFIG_KEY)           value=$(openssl rand -base64 32 | tr -d '\n') ;;
      JARVIS_MODEL_HMAC_KEY)       value=$(openssl rand -hex 32) ;;
      LANGFUSE_NEXTAUTH_SECRET)    value=$(openssl rand -hex 32) ;;
      LANGFUSE_SALT)               value=$(openssl rand -hex 16) ;;
      LANGFUSE_PG_PASSWORD)        value=$(openssl rand -hex 24) ;;
      LANGFUSE_INIT_USER_PASSWORD) value=$(openssl rand -hex 32) ;;
      BACKUP_ENCRYPT_KEY)          value=$(openssl rand -base64 32 | tr -d '\n') ;;
      *)
        warn "${key} has a generator but is not in the dispatch table — skipping."
        FAILED=1; return ;;
    esac
    upsert_env_var "${key}" "${value}"   # fill the placeholder in place (no dupe)
    ok "${key} generated and written to .env."
  else
    warn "${key} not in .env and cannot be auto-generated — set it manually then re-run."
    return
  fi

  # Step 2 — write the Docker-secret file from the in-memory value.
  if [ -z "$value" ]; then
    warn "${key} resolved to an empty value — NOT writing ${file}. Fix .env and re-run."
    FAILED=1; return
  fi
  if [ ! -f "$file" ]; then
    printf '%s' "$value" > "$file"
    chmod 600 "$file"
    ok "${file} created."
  elif [ "$(tr -d '\r\n' < "$file")" != "$value" ]; then
    printf '%s' "$value" > "$file"
    chmod 600 "$file"
    ok "${file} synced to match ${key}."
  else
    info "${file} already in sync."
  fi
}

# ---------------------------------------------------------------------------
# Auto-generated secrets
# ---------------------------------------------------------------------------
sync_secret JARVIS_API_KEY     jarvis_api_key.txt     "openssl rand -hex 32"
sync_secret LITELLM_MASTER_KEY litellm_master_key.txt "openssl rand -hex 32"
# LITELLM_SALT_KEY encrypts model credentials LiteLLM stores in its database.
# Without it litellm falls back to the master key as salt, so a master-key
# rotation would brick every encrypted DB row — pin a dedicated salt instead;
# never rotate this key manually.
sync_secret LITELLM_SALT_KEY   litellm_salt_key.txt   "openssl rand -hex 32"
sync_secret POSTGRES_PASSWORD  postgres_password.txt  "openssl rand -hex 24"
sync_secret QDRANT_API_KEY     qdrant_api_key.txt     "openssl rand -hex 24"
# INFRA_INGEST_KEY authenticates the Vector log-shipper sidecar to POST /infra-events.
# Mounted by paper_ingestion and vector services via Docker Secret.
sync_secret INFRA_INGEST_KEY   infra_ingest_key.txt   "openssl rand -hex 32"
# JARVIS_CONFIG_KEY is the Fernet write-key for the user_config table.
# sync_secret preserves any existing .env value verbatim; rotating this key
# would render every encrypted user_config row unreadable, so we never
# regenerate when an existing value is present.  ``openssl rand -base64 32``
# emits 32 random bytes as standard base64; Fernet's urlsafe decoder accepts it
# (it only maps -_ to +/, leaving +/ intact), so it is a valid JARVIS_CONFIG_KEY.
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
sync_secret LANGFUSE_NEXTAUTH_SECRET      langfuse_nextauth_secret.txt      "openssl rand -hex 32"
sync_secret LANGFUSE_SALT                 langfuse_salt.txt                 "openssl rand -hex 16"
sync_secret LANGFUSE_PG_PASSWORD          langfuse_pg_password.txt          "openssl rand -hex 24"
sync_secret LANGFUSE_INIT_USER_PASSWORD   langfuse_init_user_password.txt   "openssl rand -hex 32"

# ---------------------------------------------------------------------------
# Cloudflare Tunnel (--profile tunnel)
# ---------------------------------------------------------------------------
# CLOUDFLARE_TUNNEL_TOKEN must be obtained from the Cloudflare Zero Trust
# dashboard (Networks → Tunnels → Create tunnel → token).  It cannot be
# auto-generated locally.
sync_secret CLOUDFLARE_TUNNEL_TOKEN cloudflare_tunnel_token.txt

# ---------------------------------------------------------------------------
# Backup encryption (postgres-backup service)
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

# ---------------------------------------------------------------------------
# Fail loudly if any auto-generated secret could not be written — a missing
# secrets/*.txt for a _FILE-mounted secret breaks `docker compose up`.
# ---------------------------------------------------------------------------
if [ "${FAILED}" -ne 0 ]; then
  warn "One or more secrets could not be written. Fix .env (remove empty/duplicate KEY= lines) and re-run."
  exit 1
fi
