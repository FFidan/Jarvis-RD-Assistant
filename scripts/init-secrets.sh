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

# Secret files are world-readable (644) so the non-root service containers can
# read the compose bind-mounted files; the mode 700 secrets/ directory below
# carries host confidentiality via owner-only traversal.
SECRET_FILE_MODE=644

mkdir -p secrets
chmod 700 secrets
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
  # Colocate the temp with .env so the final mv is an atomic same-filesystem
  # rename (a $TMPDIR temp can land on a different filesystem, degrading mv to a
  # non-atomic copy that a concurrent reader could observe half-written).
  tmp="$(mktemp .env.XXXXXX)" || { printf 'upsert_env_var: mktemp failed\n' >&2; return 1; }
  awk -v k="$k" -v v="$v" '
    index($0, k "=") == 1 { if (!seen) { print k "=" v; seen = 1 } ; next }
    { print }
    END { if (!seen) print k "=" v }
  ' .env > "$tmp" || { rm -f "$tmp"; printf 'upsert_env_var: awk rewrite of .env failed\n' >&2; return 1; }
  mv "$tmp" .env || { rm -f "$tmp"; printf 'upsert_env_var: mv to .env failed\n' >&2; return 1; }
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
      JARVIS_SETUP_TOKEN)          value=$(openssl rand -hex 32) ;;
      JARVIS_TELEGRAM_SERVICE_TOKEN) value=$(openssl rand -hex 32) ;;
      JARVIS_RESEARCH_SERVICE_TOKEN) value=$(openssl rand -hex 32) ;;
      JARVIS_LEARNING_SERVICE_TOKEN) value=$(openssl rand -hex 32) ;;
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
    chmod "$SECRET_FILE_MODE" "$file"
    ok "${file} created."
  elif [ "$(tr -d '\r\n' < "$file")" != "$value" ]; then
    printf '%s' "$value" > "$file"
    chmod "$SECRET_FILE_MODE" "$file"
    ok "${file} synced to match ${key}."
  else
    chmod "$SECRET_FILE_MODE" "$file"
    info "${file} already in sync."
  fi
}

# Data-encryption keys travel with a restored database.  When a restore has
# already installed one of these files, the file is authoritative and .env is
# reconciled to it.  Host credentials continue to use sync_secret above, where
# the target host's .env remains authoritative.
sync_data_key() {
  local key="$1" filename="$2" generator="$3"
  local file="secrets/${filename}" value="" file_size=""

  if [ -e "$file" ] || [ -L "$file" ]; then
    if [ -L "$file" ]; then
      warn "${file} is a symbolic link — refusing to use it as a restored data key."
      FAILED=1
      return
    fi
    if [ ! -f "$file" ] || [ ! -r "$file" ]; then
      warn "${file} is not a small regular data-key file — refusing to use it."
      FAILED=1
      return
    fi
    # `stat -c` is GNU-only. POSIX wc works on Linux and stock macOS; strip its
    # padded whitespace, then validate before any file content is imported.
    if ! file_size="$(LC_ALL=C wc -c < "$file" 2>/dev/null)"; then
      warn "${file} could not be measured safely — refusing to use it."
      FAILED=1
      return
    fi
    file_size="${file_size//[[:space:]]/}"
    case "$file_size" in
      ''|*[!0-9]*)
        warn "${file} has an unreadable size — refusing to use it."
        FAILED=1
        return
        ;;
    esac
    if [ "$file_size" -eq 0 ]; then
      warn "${file} contains no usable data key — refusing to use it."
      FAILED=1
      return
    fi
    if [ "$file_size" -gt 4096 ]; then
      warn "${file} is not a small regular data-key file — refusing to use it."
      FAILED=1
      return
    fi
    value="$(tr -d '\r\n' < "$file")"
    if [ -z "$value" ]; then
      warn "${file} contains no usable data key — refusing to use it."
      FAILED=1
      return
    fi
    upsert_env_var "$key" "$value"
    chmod "$SECRET_FILE_MODE" "$file"
    info "${file} is authoritative; ${key} in .env is in sync."
    return
  fi

  sync_secret "$key" "$filename" "$generator"
}

# Database login passwords are Docker-secret files only.  Unlike application
# data keys, they are deliberately not copied into .env: operators rotate or
# recover these authorities through their isolated credential procedures.
sync_database_password() {
  local filename="secrets/$1" value=""

  if [ -e "$filename" ]; then
    if [ ! -f "$filename" ] || [ ! -r "$filename" ] || [ -L "$filename" ] || [ ! -s "$filename" ]; then
      warn "$filename is not a readable non-empty regular password file."
      FAILED=1
      return
    fi
    chmod "$SECRET_FILE_MODE" "$filename"
    info "$filename already exists — preserving."
    return
  fi

  value="$(openssl rand -hex 24)"
  printf '%s' "$value" > "$filename"
  chmod "$SECRET_FILE_MODE" "$filename"
  ok "$filename created."
}

# Preserve the v1.2.5 cluster-owner password solely for the one-time upgrade
# bootstrap and existing recovery compatibility. It is never mounted at runtime.
sync_legacy_postgres_password() {
  local filename="secrets/postgres_password.txt" value="" clean_env=""

  if [ -e "$filename" ]; then
    if [ ! -f "$filename" ] || [ ! -r "$filename" ] || [ -L "$filename" ] || [ ! -s "$filename" ]; then
      warn "$filename is not a readable non-empty regular password file."
      FAILED=1
      return
    fi
    chmod "$SECRET_FILE_MODE" "$filename"
    info "$filename already exists — preserving for upgrade and recovery."
    return
  fi

  if grep -qE '^POSTGRES_PASSWORD=.+' .env 2>/dev/null; then
    value="$(grep -E '^POSTGRES_PASSWORD=.+' .env | head -n 1 | cut -d'=' -f2- | tr -d '\r\n')"
    clean_env="$(mktemp .env.without-postgres.XXXXXX)" || {
      warn "Could not create a temporary .env file."
      FAILED=1
      return
    }
    if ! awk 'index($0, "POSTGRES_PASSWORD=") != 1' .env > "$clean_env" \
      || ! mv "$clean_env" .env; then
      rm -f "$clean_env"
      warn "Could not remove the migrated PostgreSQL password from .env."
      FAILED=1
      return
    fi
    info "Legacy PostgreSQL password moved out of .env."
  else
    value="$(openssl rand -hex 24)"
  fi
  printf '%s' "$value" > "$filename"
  chmod "$SECRET_FILE_MODE" "$filename"
  ok "$filename created for isolated upgrade and recovery use."
}

# ---------------------------------------------------------------------------
# Auto-generated secrets
# ---------------------------------------------------------------------------
sync_secret JARVIS_API_KEY     jarvis_api_key.txt     "openssl rand -hex 32"
# JARVIS_SETUP_TOKEN gates the first-run setup wizard's WRITE endpoints while no
# admin exists (closes the unauthenticated first-admin-takeover window). setup.sh
# prints it in the #setup_token= URL fragment of the
# click-to-finish link (a fragment never reaches the server/logs); the wizard
# also accepts it pasted on a second device.
sync_secret JARVIS_SETUP_TOKEN jarvis_setup_token.txt "openssl rand -hex 32"
sync_secret JARVIS_TELEGRAM_SERVICE_TOKEN telegram_service_token.txt "openssl rand -hex 32"
sync_secret JARVIS_RESEARCH_SERVICE_TOKEN research_service_token.txt "openssl rand -hex 32"
sync_secret JARVIS_LEARNING_SERVICE_TOKEN learning_service_token.txt "openssl rand -hex 32"

# Platform alone receives the Ed25519 private key. Research and Learning mount
# only the derived public key. The pair is validated on every run so a stale or
# mismatched public file cannot silently break the signed-identity boundary.
sync_identity_key_pair() {
  local private_file="secrets/platform_identity_private_key.txt"
  local public_file="secrets/platform_identity_public_key.txt"
  local private_tmp="" public_tmp="" size=""

  for candidate in "$private_file" "$public_file"; do
    if [ -L "$candidate" ]; then
      warn "${candidate} is a symbolic link — refusing identity key custody."
      FAILED=1
      return
    fi
  done
  if [ -e "$public_file" ] && [ ! -e "$private_file" ]; then
    warn "${public_file} exists without the Platform private key — refusing an incoherent pair."
    FAILED=1
    return
  fi

  if [ ! -e "$private_file" ]; then
    private_tmp="$(mktemp secrets/.platform-identity-private.XXXXXX)" || {
      warn "Could not allocate an identity private-key temporary file."
      FAILED=1
      return
    }
    public_tmp="$(mktemp secrets/.platform-identity-public.XXXXXX)" || {
      rm -f "$private_tmp"
      warn "Could not allocate an identity public-key temporary file."
      FAILED=1
      return
    }
    if ! openssl genpkey -algorithm ED25519 -out "$private_tmp" >/dev/null 2>&1 \
      || ! openssl pkey -in "$private_tmp" -pubout -out "$public_tmp" >/dev/null 2>&1; then
      rm -f "$private_tmp" "$public_tmp"
      warn "Could not generate the Platform Ed25519 identity key pair."
      FAILED=1
      return
    fi
    chmod "$SECRET_FILE_MODE" "$private_tmp" "$public_tmp"
    mv "$private_tmp" "$private_file"
    mv "$public_tmp" "$public_file"
    ok "Platform Ed25519 identity key pair generated."
    return
  fi

  size="$(LC_ALL=C wc -c < "$private_file" 2>/dev/null || true)"
  size="${size//[[:space:]]/}"
  if [ -z "$size" ] || [[ "$size" == *[!0-9]* ]] || [ "$size" -eq 0 ] || [ "$size" -gt 16384 ]; then
    warn "${private_file} is not a small regular key file."
    FAILED=1
    return
  fi
  public_tmp="$(mktemp secrets/.platform-identity-public.XXXXXX)" || {
    warn "Could not allocate an identity public-key validation file."
    FAILED=1
    return
  }
  if ! openssl pkey -in "$private_file" -pubout -out "$public_tmp" >/dev/null 2>&1; then
    rm -f "$public_tmp"
    warn "${private_file} is not a valid Ed25519 private key."
    FAILED=1
    return
  fi
  if ! openssl pkey -pubin -in "$public_tmp" -text_pub -noout 2>/dev/null \
    | grep -q '^ED25519 Public-Key:'; then
    rm -f "$public_tmp"
    warn "${private_file} does not contain an Ed25519 key."
    FAILED=1
    return
  fi
  if [ -e "$public_file" ] && ! cmp -s "$public_file" "$public_tmp"; then
    rm -f "$public_tmp"
    warn "${public_file} does not match the Platform private key."
    FAILED=1
    return
  fi
  if [ ! -e "$public_file" ]; then
    chmod "$SECRET_FILE_MODE" "$public_tmp"
    mv "$public_tmp" "$public_file"
    ok "${public_file} derived from the existing private key."
  else
    rm -f "$public_tmp"
    chmod "$SECRET_FILE_MODE" "$public_file"
    info "Platform Ed25519 identity key pair is valid and in sync."
  fi
  chmod "$SECRET_FILE_MODE" "$private_file"
}

sync_identity_key_pair
sync_secret LITELLM_MASTER_KEY litellm_master_key.txt "openssl rand -hex 32"
# LITELLM_SALT_KEY encrypts model credentials LiteLLM stores in its database.
# Without it litellm falls back to the master key as salt, so a master-key
# rotation would brick every encrypted DB row — pin a dedicated salt instead;
# never rotate this key manually.
sync_data_key LITELLM_SALT_KEY litellm_salt_key.txt "openssl rand -hex 32"
sync_legacy_postgres_password
sync_database_password postgres_platform_runtime_password.txt
sync_database_password postgres_research_runtime_password.txt
sync_database_password postgres_learning_runtime_password.txt
sync_database_password postgres_migrator_password.txt
sync_database_password postgres_cluster_bootstrap_password.txt
sync_database_password postgres_legacy_rollback_password.txt
sync_database_password postgres_backup_reader_password.txt
sync_database_password postgres_restore_operator_password.txt
sync_database_password postgres_erasure_executor_password.txt
sync_database_password litellm_runtime_password.txt
sync_database_password litellm_migrator_password.txt
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
sync_data_key JARVIS_CONFIG_KEY jarvis_config_key.txt "openssl rand -base64 32 | tr -d '\\n'"

# JARVIS_MODEL_HMAC_KEY signs the Pulse classifier pickle blobs (HMAC-SHA256).
# Mandatory in production (auth.py / pulse/training.py refuse to start without
# it); 32 bytes hex = 64 chars, comfortably above the 32-char minimum.
sync_data_key JARVIS_MODEL_HMAC_KEY jarvis_model_hmac_key.txt "openssl rand -hex 32"

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
  chmod "$SECRET_FILE_MODE" secrets/telegram_bot_token.txt
  info "secrets/telegram_bot_token.txt created as empty placeholder (Telegram not configured)."
fi

# paper_ingestion mounts smtp_pass as a Docker Secret (declared unconditionally),
# so docker compose up aborts with "secret not found" when the file is absent.
# The SMTP password is an operator credential, NEVER openssl-generated: create an
# empty placeholder when absent (empty resolves to None in SecretsSettings, the
# correct "SMTP password not configured" sentinel). setup.sh --smtp-pass-file
# writes the real password here.
if [ ! -f "secrets/smtp_pass.txt" ]; then
  : > secrets/smtp_pass.txt
  chmod "$SECRET_FILE_MODE" secrets/smtp_pass.txt
  info "secrets/smtp_pass.txt created as empty placeholder (SMTP password not configured)."
fi

# ---------------------------------------------------------------------------
# Deliberately NOT created here: secrets/langfuse_init_pk.txt and
# secrets/langfuse_init_sk.txt. Langfuse headless init is write-once, so those
# come from scripts/gen-langfuse-keys.sh at setup time only — creating a fresh
# keypair during an update would 401 against an already-provisioned Langfuse
# volume. Every secret docker-compose.yml declares must either be created above
# or classified setup-time-only in tests/test_docker_compose_invariants.py;
# that test fails the build when a new compose secret has no provisioning path.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fail loudly if any auto-generated secret could not be written — a missing
# secrets/*.txt for a _FILE-mounted secret breaks `docker compose up`.
# ---------------------------------------------------------------------------
if [ "${FAILED}" -ne 0 ]; then
  warn "One or more secrets could not be written. Fix .env (remove empty/duplicate KEY= lines) and re-run."
  exit 1
fi
