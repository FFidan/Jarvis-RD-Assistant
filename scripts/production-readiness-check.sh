#!/usr/bin/env bash
# scripts/production-readiness-check.sh — Production Readiness Check
#
# Prints a summary table of key configuration checks and exits with a
# three-state contract so callers can tell "clean" from "warnings present":
#   0  all checks passed (no WARN, no HIGH)
#   2  warnings present, but no HIGH issues (advisory — safe to proceed)
#   1  at least one HIGH-severity issue (must be fixed before production)
# setup.sh's wrapper treats 2 as non-fatal and 1 as fatal on the production/
# letsencrypt path (see readiness_verdict in scripts/setup_lib.sh).
#
# Usage:
#   bash scripts/production-readiness-check.sh
#
# Simulating a HIGH failure for testing:
#   ENVIRONMENT=production DEV_AUTH_BYPASS=true bash scripts/production-readiness-check.sh
#
# Source order: .env (if present in CWD) is loaded first, then environment
# variables override any .env values (standard dotenv convention).
set -euo pipefail

# ---------------------------------------------------------------------------
# Output helpers — match setup.sh style.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Load .env if present (do not override existing environment variables).
# We source only KEY=VALUE lines; skip comments and blank lines.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../.env"

if [ -f "$ENV_FILE" ]; then
  # Export each KEY=VALUE from .env, but only when the variable is not already
  # set in the calling environment (let env vars win over .env).
  while IFS= read -r _line || [ -n "$_line" ]; do
    # Skip comments and blank lines.
    case "$_line" in
      \#*|"") continue ;;
    esac
    if [[ "$_line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      _key="${BASH_REMATCH[1]}"
      _val="${BASH_REMATCH[2]}"
      # Only set if not already in the environment.
      if [ -z "${!_key+x}" ]; then
        export "${_key}=${_val}"
      fi
    fi
  done < "$ENV_FILE"
fi

# ---------------------------------------------------------------------------
# Read configuration values (with safe defaults).
# ---------------------------------------------------------------------------
ENVIRONMENT="${ENVIRONMENT:-development}"
DEV_AUTH_BYPASS="${DEV_AUTH_BYPASS:-false}"
DEV_ERROR_DETAIL="${DEV_ERROR_DETAIL:-false}"
DEV_CORS_OPEN="${DEV_CORS_OPEN:-false}"
DEV_SMTP_LOG_ONLY="${DEV_SMTP_LOG_ONLY:-false}"
DEV_CRYPTO_RELAXED="${DEV_CRYPTO_RELAXED:-false}"
JARVIS_API_KEY="${JARVIS_API_KEY:-}"
SMTP_HOST="${SMTP_HOST:-}"
SMTP_FROM="${SMTP_FROM:-}"
LETSENCRYPT_DOMAIN="${LETSENCRYPT_DOMAIN:-}"
APP_BASE_URL="${APP_BASE_URL:-}"

# Read secrets from files when the _FILE env var convention is used, otherwise
# fall back to the plain env var (same pattern as the litellm entrypoint shim).
_read_secret() {
  local var_name="$1"
  local file_var="${var_name}_FILE"
  local file_path="${!file_var:-}"
  if [ -n "$file_path" ] && [ -f "$file_path" ]; then
    cat "$file_path"
  else
    printf '%s' "${!var_name:-}"
  fi
}

# Resolve secrets from env → Docker Compose /run/secrets/<name> → local
# secrets/<basename>.txt (works inside and outside of containers).
resolve_secret() {
  local name="$1"
  local basename="$2"
  local val="${!name:-}"
  if [ -z "$val" ] && [ -f "/run/secrets/${basename}" ]; then
    val="$(cat "/run/secrets/${basename}")"
  fi
  if [ -z "$val" ] && [ -f "${SCRIPT_DIR}/../secrets/${basename}.txt" ]; then
    val="$(cat "${SCRIPT_DIR}/../secrets/${basename}.txt")"
  fi
  printf '%s' "$val"
}

LITELLM_MASTER_KEY="$(resolve_secret LITELLM_MASTER_KEY litellm_master_key)"
POSTGRES_PASSWORD="$(resolve_secret POSTGRES_PASSWORD postgres_password)"
QDRANT_API_KEY="$(resolve_secret QDRANT_API_KEY qdrant_api_key)"
JARVIS_CONFIG_KEY="$(resolve_secret JARVIS_CONFIG_KEY jarvis_config_key)"

# ---------------------------------------------------------------------------
# Check helpers.
# ---------------------------------------------------------------------------
# is_truthy VAL — returns 0 (success) if VAL looks like a true value.
is_truthy() {
  local val
  val="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$val" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

# is_production — returns 0 when ENVIRONMENT=production.
is_production() {
  [ "$ENVIRONMENT" = "production" ]
}

# ---------------------------------------------------------------------------
# Collect check results.
# Results array: "SEVERITY|CHECK_NAME|STATUS|DETAIL"
# ---------------------------------------------------------------------------
RESULTS=()

# Check: ENVIRONMENT value.
_env_status="OK"
_env_detail="ENVIRONMENT=${ENVIRONMENT}"
case "$ENVIRONMENT" in
  production|development|staging|test) ;;
  *)
    _env_status="WARN"
    _env_detail="Unrecognised value '${ENVIRONMENT}'; expected production, development, staging, or test."
    ;;
esac
RESULTS+=("INFO|ENVIRONMENT|${_env_status}|${_env_detail}")

# Check: granular dev flags in production.
_check_dev_flag() {
  local flag_name="$1"
  local flag_val="$2"
  if is_production && is_truthy "$flag_val"; then
    RESULTS+=("HIGH|${flag_name}|FAIL|${flag_name}=true is not permitted when ENVIRONMENT=production")
  elif is_truthy "$flag_val"; then
    RESULTS+=("INFO|${flag_name}|WARN|${flag_name}=true (acceptable in non-production)")
  else
    RESULTS+=("INFO|${flag_name}|OK|${flag_name}=false")
  fi
}

_check_dev_flag "DEV_AUTH_BYPASS"   "$DEV_AUTH_BYPASS"
_check_dev_flag "DEV_ERROR_DETAIL"  "$DEV_ERROR_DETAIL"
_check_dev_flag "DEV_CORS_OPEN"     "$DEV_CORS_OPEN"
_check_dev_flag "DEV_SMTP_LOG_ONLY" "$DEV_SMTP_LOG_ONLY"
_check_dev_flag "DEV_CRYPTO_RELAXED" "$DEV_CRYPTO_RELAXED"

# Check: JARVIS_API_KEY set and >= 32 chars.
_key_len="${#JARVIS_API_KEY}"
if [ -z "$JARVIS_API_KEY" ]; then
  if is_production; then
    RESULTS+=("HIGH|JARVIS_API_KEY|FAIL|Not set — API key is required in production (generate: openssl rand -hex 32)")
  else
    RESULTS+=("INFO|JARVIS_API_KEY|WARN|Not set — auth bypass may apply in dev mode")
  fi
elif [ "$_key_len" -lt 32 ]; then
  if is_production; then
    RESULTS+=("HIGH|JARVIS_API_KEY|FAIL|Key is ${_key_len} chars; minimum 32 required (generate: openssl rand -hex 32)")
  else
    RESULTS+=("INFO|JARVIS_API_KEY|WARN|Key is ${_key_len} chars; recommend >= 32")
  fi
else
  RESULTS+=("INFO|JARVIS_API_KEY|OK|Set (${_key_len} chars)")
fi

# ---------------------------------------------------------------------------
# Weak/placeholder secret detection helpers.
# ---------------------------------------------------------------------------
# _WEAK_SECRET_PATTERNS — known placeholder values to reject in production.
_is_weak_secret() {
  local val="$1"
  case "$val" in
    ""|changeme|password|secret|test|dev|jarvis_dev|"sk-jarvis-dev-test"|"sk-1234"|"1234"|"admin"|"postgres")
      return 0 ;;
    *) ;;
  esac
  # Exact partial matches for common skeleton strings (case-insensitive).
  local lower
  lower="$(printf '%s' "$val" | tr '[:upper:]' '[:lower:]')"
  case "$lower" in
    *changeme*|*placeholder*|*example*|*default*|*replace_me*|*your_*|*"<"*|*fixme*)
      return 0 ;;
  esac
  return 1
}

# _check_secret NAME VALUE MIN_LEN — mirrors JARVIS_API_KEY idiom.
_check_secret() {
  local name="$1"
  local val="$2"
  local min_len="$3"
  local val_len="${#val}"

  if [ -z "$val" ]; then
    if is_production; then
      RESULTS+=("HIGH|${name}|FAIL|Not set — a strong secret is required in production")
    else
      RESULTS+=("INFO|${name}|WARN|Not set — acceptable in non-production but must be changed before prod deploy")
    fi
  elif _is_weak_secret "$val"; then
    if is_production; then
      RESULTS+=("HIGH|${name}|FAIL|Placeholder/known-weak value detected — replace before deploying to production")
    else
      RESULTS+=("INFO|${name}|WARN|Placeholder value in use (acceptable in dev; must be replaced for production)")
    fi
  elif [ "$val_len" -lt "$min_len" ]; then
    if is_production; then
      RESULTS+=("HIGH|${name}|FAIL|Secret is ${val_len} chars; minimum ${min_len} required in production")
    else
      RESULTS+=("INFO|${name}|WARN|Secret is ${val_len} chars; recommend >= ${min_len}")
    fi
  else
    RESULTS+=("INFO|${name}|OK|Set (${val_len} chars)")
  fi
}

_check_secret "LITELLM_MASTER_KEY" "$LITELLM_MASTER_KEY" 16
_check_secret "POSTGRES_PASSWORD"  "$POSTGRES_PASSWORD"  12
_check_secret "QDRANT_API_KEY"     "$QDRANT_API_KEY"     16
_check_secret "JARVIS_CONFIG_KEY"  "$JARVIS_CONFIG_KEY"  16

# Check: environment SMTP presence. This reports only whether the env carries
# a host and sender — NOT whether email can be delivered. The app layers
# wizard-saved config over env per-field without a restart, and presence is not
# reachability, so the effective delivery state lives in the app. This script
# never calls the app (it runs pre-auth/pre-liveness).
if [ -n "$SMTP_HOST" ] && [ -n "$SMTP_FROM" ]; then
  RESULTS+=("INFO|SMTP|OK|Environment: host and sender set; effective delivery state: GET /api/setup/status")
else
  RESULTS+=("WARN|SMTP|WARN|Environment: host/sender missing; admins can use manual links. Effective state: GET /api/setup/status")
fi

# Check: HTTPS. A successful TLS handshake or arbitrary 2xx page is not enough:
# require this installation's fixed marker at the same endpoint every other
# access-edge gate uses.
if [ -n "$LETSENCRYPT_DOMAIN" ]; then
  _https_url="https://${LETSENCRYPT_DOMAIN}/health/jarvis"
  if _https_result="$(curl -fsS --max-time 10 "$_https_url" 2>&1)"; then
    if [ "$_https_result" = "jarvis-rd-assistant" ]; then
      RESULTS+=("INFO|HTTPS|OK|HTTPS marker probe succeeded: ${_https_url}")
    else
      # Do not echo a foreign service's response body into setup/readiness logs.
      RESULTS+=("WARN|HTTPS|WARN|HTTPS address answered, but it is a different app (not this JARVIS instance)")
    fi
  else
    RESULTS+=("WARN|HTTPS|WARN|HTTPS marker probe failed for ${_https_url}: ${_https_result:-curl exited non-zero}")
  fi
elif [[ "$APP_BASE_URL" == https://* ]]; then
  if _https_body="$(curl -fsS --max-time 10 "${APP_BASE_URL%/}/health/jarvis" 2>/dev/null)" \
     && [ "$_https_body" = "jarvis-rd-assistant" ]; then
    RESULTS+=("INFO|HTTPS|OK|Configured HTTPS origin reaches this JARVIS instance")
  else
    RESULTS+=("WARN|HTTPS|WARN|Configured HTTPS origin is not verified; localhost remains available")
  fi
else
  if is_production; then
    RESULTS+=("WARN|HTTPS|WARN|No LETSENCRYPT_DOMAIN set — HTTPS may not be active")
  else
    RESULTS+=("INFO|HTTPS|OK|Not required for localhost-only use; remote sign-in needs named HTTPS")
  fi
fi

# Note: MULTITENANT_ENABLED is retired — not checked here.

# ---------------------------------------------------------------------------
# Render summary table.
# ---------------------------------------------------------------------------
printf '\n%s%s%-20s  %-8s  %s%s\n' "$C_BOLD" "$C_BLUE" "CHECK" "STATUS" "DETAIL" "$C_RESET"
printf '%s%-20s  %-8s  %s%s\n' "$C_BLUE" "--------------------" "--------" "------" "$C_RESET"

HAS_HIGH=0
HAS_WARN=0

for entry in "${RESULTS[@]}"; do
  IFS='|' read -r _sev _name _status _detail <<< "$entry"
  case "$_status" in
    OK)
      _color="$C_GREEN"
      ;;
    WARN)
      _color="$C_YELLOW"
      HAS_WARN=1
      ;;
    FAIL)
      _color="$C_RED"
      if [ "$_sev" = "HIGH" ]; then
        HAS_HIGH=1
      fi
      ;;
    *)
      _color="$C_RESET"
      ;;
  esac
  printf '%-20s  %s%-8s%s  %s\n' "$_name" "$_color" "$_status" "$C_RESET" "$_detail"
done

printf '\n'

# ---------------------------------------------------------------------------
# Final verdict.
# ---------------------------------------------------------------------------
if [ "$HAS_HIGH" -eq 1 ]; then
  printf '%s[FAIL]%s  Production readiness check: HIGH issues found — see table above.\n' \
    "$C_RED" "$C_RESET" >&2
  exit 1
elif [ "$HAS_WARN" -eq 1 ]; then
  printf '%s[WARN]%s  Production readiness check: warnings present (no HIGH issues).\n' \
    "$C_YELLOW" "$C_RESET"
  exit 2
else
  printf '%s[OK]%s    Production readiness check: all checks passed.\n' \
    "$C_GREEN" "$C_RESET"
  exit 0
fi
