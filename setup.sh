#!/usr/bin/env bash
# setup.sh — JARVIS RD Assistant first-time installer.
#
# Idempotent: second run with an existing .env prompts before clobbering.
# macOS-safe: no `sed -i`, no GNU-only flags. Uses tempfile + mv.
# See docs/superpowers/specs/2026-04-12-setup-simplification-design.md
set -euo pipefail

# -----------------------------------------------------------------------------
# Pretty output helpers (no external deps — POSIX-ish)
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

info()  { printf '%s[INFO]%s  %s\n'  "$C_BLUE"   "$C_RESET" "$*"; }
ok()    { printf '%s[OK]%s    %s\n'  "$C_GREEN"  "$C_RESET" "$*"; }
warn()  { printf '%s[WARN]%s  %s\n'  "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()   { printf '%s[ERROR]%s %s\n'  "$C_RED"    "$C_RESET" "$*" >&2; }

die() {
  # $1 = message, $2 = next-step hint
  err "$1"
  printf '        %s%s%s\n' "$C_YELLOW" "$2" "$C_RESET" >&2
  exit 1
}

# -----------------------------------------------------------------------------
# 1. Banner
# -----------------------------------------------------------------------------
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   JARVIS RD Assistant — First-time setup                       %s\n' "$C_BOLD" "$C_RESET"
printf '%s================================================================%s\n\n' "$C_BOLD" "$C_RESET"

# Resolve repo root (the directory this script lives in).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# -----------------------------------------------------------------------------
# 2. Prerequisites
# -----------------------------------------------------------------------------
info "Checking prerequisites..."

command -v docker >/dev/null 2>&1 \
  || die "Docker not found in PATH." \
         "Install Docker Engine: https://docs.docker.com/engine/install/"

# docker compose v2 (space form). `docker-compose` (hyphen) is v1 and unsupported.
if ! docker compose version >/dev/null 2>&1; then
  die "Docker Compose v2 is required (the 'docker compose' plugin)." \
      "Install it: https://docs.docker.com/compose/install/"
fi
COMPOSE_VER="$(docker compose version --short 2>/dev/null || echo 'unknown')"
case "$COMPOSE_VER" in
  2.*|v2.*) ok "Docker Compose v${COMPOSE_VER#v}" ;;
  *)        warn "Unexpected Compose version '$COMPOSE_VER' — expected v2.x. Proceeding." ;;
esac

command -v openssl >/dev/null 2>&1 \
  || die "openssl required for secret generation." \
         "Install openssl (usually pre-installed). On Debian/Ubuntu: sudo apt install openssl"

# GPU is informational — not fatal.
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_LINE="$(nvidia-smi -L 2>/dev/null | head -n 1 || true)"
  if [ -n "$GPU_LINE" ]; then
    ok "GPU detected: $GPU_LINE"
  else
    warn "nvidia-smi present but no GPU enumerated — Ollama will run on CPU (slower)."
  fi
else
  info "No NVIDIA GPU detected — Ollama will run on CPU (slower)."
fi

# Port 3001 pre-check — warn only.
PORT_IN_USE=0
if command -v ss >/dev/null 2>&1; then
  # shellcheck disable=SC2143  # grep -q is sufficient; we only care about exit code
  if ss -tlnp 2>/dev/null | grep -q ':3001 '; then
    PORT_IN_USE=1
  fi
elif command -v lsof >/dev/null 2>&1; then
  if lsof -iTCP:3001 -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    PORT_IN_USE=1
  fi
fi
if [ "$PORT_IN_USE" -eq 1 ]; then
  warn "Port 3001 is already in use. Dashboard may conflict on startup."
fi

# -----------------------------------------------------------------------------
# 3. Idempotency gate
# -----------------------------------------------------------------------------
if [ -f .env ]; then
  printf '\n%sConfiguration already exists (.env).%s\n' "$C_YELLOW" "$C_RESET"
  read -rp "Overwrite? (y/N): " reply
  case "$reply" in
    [yY]|[yY][eE][sS]) info "Proceeding — existing .env will be replaced." ;;
    *) ok "Keeping existing .env. Exiting."; exit 0 ;;
  esac
fi

if [ ! -f .env.example ]; then
  die ".env.example not found in $SCRIPT_DIR." \
      "Run this script from the repo root, or: git pull"
fi

# -----------------------------------------------------------------------------
# 4. Secret generation
# -----------------------------------------------------------------------------
info "Generating secrets..."
POSTGRES_PASSWORD="$(openssl rand -hex 24)"
LITELLM_MASTER_KEY="$(openssl rand -hex 32)"
JARVIS_API_KEY="$(openssl rand -hex 32)"
N8N_ENCRYPTION_KEY="$(openssl rand -hex 32)"
N8N_JWT_SECRET="$(openssl rand -hex 32)"
ok "Secrets generated."

# -----------------------------------------------------------------------------
# 5. Question 1 — Access mode
# -----------------------------------------------------------------------------
printf '\n%sHow do you want to access the dashboard?%s\n' "$C_BOLD" "$C_RESET"
cat <<'EOF'
  1) Localhost only (default, safest)
  2) LAN — reachable from other devices on your network
  3) Global — access from anywhere via Cloudflare Tunnel (free, no ports opened)
EOF
read -rp "Choice [1]: " access_mode
access_mode="${access_mode:-1}"

CLOUDFLARE_TUNNEL_TOKEN=""
USE_TUNNEL_PROFILE=0
ACCESS_MODE_LABEL="localhost"

case "$access_mode" in
  1)
    ACCESS_MODE_LABEL="localhost"
    # Default compose binding is 127.0.0.1 — nothing to override.
    # Remove any stale LAN override so re-runs don't silently open the port.
    if [ -f docker-compose.override.yml ]; then
      # Back up rather than delete to avoid surprising users.
      mv docker-compose.override.yml "docker-compose.override.yml.bak.$(date +%s)"
      warn "Existing docker-compose.override.yml backed up (localhost mode does not need it)."
    fi
    ;;
  2)
    ACCESS_MODE_LABEL="lan"
    info "Writing docker-compose.override.yml for LAN access..."
    cat > docker-compose.override.yml <<'YAML'
# Generated by setup.sh — LAN access override.
# Binds the dashboard to all interfaces so other devices on your network
# can reach it at http://<this-machine-ip>:3001
# Safe to delete: re-run ./setup.sh and choose option 1 to revert.
services:
  dashboard:
    ports:
      - "0.0.0.0:3001:3000"
YAML
    ok "LAN override written."
    ;;
  3)
    ACCESS_MODE_LABEL="tunnel"
    printf '\n'
    cat <<'EOF'
Create a free tunnel at:
  https://dash.cloudflare.com → Zero Trust → Networks → Tunnels
EOF
    read -rp "Paste your tunnel token: " CLOUDFLARE_TUNNEL_TOKEN
    if [ -z "${CLOUDFLARE_TUNNEL_TOKEN// }" ]; then
      warn "Token was empty. Re-prompting once..."
      read -rp "Paste your tunnel token: " CLOUDFLARE_TUNNEL_TOKEN
    fi
    if [ -z "${CLOUDFLARE_TUNNEL_TOKEN// }" ]; then
      die "Cloudflare Tunnel token is required for global mode." \
          "Get one at https://dash.cloudflare.com → Zero Trust → Networks → Tunnels, then re-run ./setup.sh"
    fi
    USE_TUNNEL_PROFILE=1
    ok "Tunnel token captured."
    ;;
  *)
    die "Invalid choice '$access_mode'. Expected 1, 2, or 3." \
        "Re-run ./setup.sh and pick a listed option."
    ;;
esac

# -----------------------------------------------------------------------------
# 6. Question 2 — Telegram
# -----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN=""
USE_TELEGRAM_PROFILE=0

prompt_telegram() {
  local token
  read -rp "(Optional) Telegram bot token — press Enter to skip: " token
  printf '%s' "$token"
}

tg_try="$(prompt_telegram)"
if [ -n "${tg_try// }" ]; then
  if [[ "$tg_try" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
    TELEGRAM_BOT_TOKEN="$tg_try"
    USE_TELEGRAM_PROFILE=1
    ok "Telegram token accepted."
  else
    warn "That didn't look like a valid Telegram token (format: <digits>:<20+ chars>). Try again or press Enter to skip."
    tg_try2="$(prompt_telegram)"
    if [ -n "${tg_try2// }" ] && [[ "$tg_try2" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
      TELEGRAM_BOT_TOKEN="$tg_try2"
      USE_TELEGRAM_PROFILE=1
      ok "Telegram token accepted."
    else
      warn "Skipping Telegram — bot will not start. Add TELEGRAM_BOT_TOKEN to .env later to enable."
    fi
  fi
else
  info "Skipping Telegram bot."
fi

# -----------------------------------------------------------------------------
# 7. Write .env (tempfile + mv, macOS-safe)
# -----------------------------------------------------------------------------
info "Writing .env from .env.example..."

TMP_ENV="$(mktemp "${TMPDIR:-/tmp}/jarvis-env.XXXXXX")"
# Make sure the tempfile is cleaned up on any exit path.
cleanup_tmp() { [ -f "$TMP_ENV" ] && rm -f "$TMP_ENV" || true; }
trap cleanup_tmp EXIT

# Look up substitution value for a KEY. Prints the value, or nothing if the
# key is not in our substitution set. We use a case statement instead of a
# bash-4 associative array so the script runs on stock macOS bash 3.2.
sub_value() {
  case "$1" in
    POSTGRES_PASSWORD)        printf '%s' "$POSTGRES_PASSWORD" ;;
    LITELLM_MASTER_KEY)       printf '%s' "$LITELLM_MASTER_KEY" ;;
    JARVIS_API_KEY)           printf '%s' "$JARVIS_API_KEY" ;;
    N8N_ENCRYPTION_KEY)       printf '%s' "$N8N_ENCRYPTION_KEY" ;;
    N8N_JWT_SECRET)           printf '%s' "$N8N_JWT_SECRET" ;;
    CLOUDFLARE_TUNNEL_TOKEN)  printf '%s' "$CLOUDFLARE_TUNNEL_TOKEN" ;;
    TELEGRAM_BOT_TOKEN)       printf '%s' "$TELEGRAM_BOT_TOKEN" ;;
    *) return 1 ;;
  esac
  return 0
}

# Header banner marking machine-edited file.
{
  printf '# ==========================================================\n'
  printf '# .env — generated by setup.sh on %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  printf '# Secrets below (POSTGRES_PASSWORD, *_KEY, *_SECRET) were\n'
  # shellcheck disable=SC2016  # literal backticks in human-readable comment, not a command substitution
  printf '# produced by `openssl rand -hex`. Do not commit this file.\n'
  printf '# ==========================================================\n\n'
} > "$TMP_ENV"

# Walk .env.example line by line. For every `KEY=...` line whose KEY has a
# substitution, emit `KEY=<value>`; otherwise emit the line verbatim.
# Using read with IFS= to preserve leading whitespace and exact formatting.
while IFS= read -r line || [ -n "$line" ]; do
  # Match lines that look like assignments: KEY=rest (no leading whitespace
  # in .env.example, but be forgiving). BASH_REMATCH is available in bash 3.2+.
  if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
    key="${BASH_REMATCH[1]}"
    if value="$(sub_value "$key")"; then
      printf '%s=%s\n' "$key" "$value" >> "$TMP_ENV"
      continue
    fi
  fi
  printf '%s\n' "$line" >> "$TMP_ENV"
done < .env.example

# Atomically replace .env.
mv "$TMP_ENV" .env
chmod 600 .env
ok ".env written (mode 600)."

# -----------------------------------------------------------------------------
# 8. Create shared directories for volume mounts
# -----------------------------------------------------------------------------
info "Creating shared/ directories..."
mkdir -p shared/pdf_storage shared/snapshots shared/local_pdfs
ok "shared/ directories ready."

# -----------------------------------------------------------------------------
# 9. Source versions.env (optional — compose has fallbacks)
# -----------------------------------------------------------------------------
if [ -f versions.env ]; then
  # shellcheck disable=SC1091  # versions.env is runtime-provided KEY=VALUE data, not a script
  set -a && . ./versions.env && set +a
  ok "versions.env loaded."
else
  warn "versions.env missing — docker-compose fallback image tags will be used."
fi

# -----------------------------------------------------------------------------
# 10. Start services
# -----------------------------------------------------------------------------
PROFILE_ARGS=()
if [ "$USE_TUNNEL_PROFILE" -eq 1 ]; then
  PROFILE_ARGS+=(--profile tunnel)
fi
if [ "$USE_TELEGRAM_PROFILE" -eq 1 ]; then
  PROFILE_ARGS+=(--profile telegram)
fi

printf '\n'
info "Starting services with: docker compose ${PROFILE_ARGS[*]:-} up -d"
if ! docker compose "${PROFILE_ARGS[@]}" up -d; then
  die "docker compose up failed." \
      "Inspect logs: docker compose logs --tail=200"
fi

# -----------------------------------------------------------------------------
# 11. Summary
# -----------------------------------------------------------------------------
DASHBOARD_URL="http://localhost:3001"
case "$ACCESS_MODE_LABEL" in
  lan)
    lan_ip=""
    if command -v hostname >/dev/null 2>&1; then
      # hostname -I is Linux-specific; fall back to ipconfig getifaddr on macOS.
      lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    fi
    if [ -z "$lan_ip" ] && command -v ipconfig >/dev/null 2>&1; then
      lan_ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
    fi
    if [ -n "$lan_ip" ]; then
      DASHBOARD_URL="http://${lan_ip}:3001"
    else
      DASHBOARD_URL="http://<this-machine-ip>:3001"
    fi
    ;;
  tunnel)
    DASHBOARD_URL="via your Cloudflare tunnel hostname"
    ;;
esac

printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   Setup complete.%s\n' "$C_GREEN" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '  Dashboard:    %s\n' "$DASHBOARD_URL"
printf '  API key:      %s%s%s\n' "$C_BOLD" "$JARVIS_API_KEY" "$C_RESET"
printf '  %s%s Save this key — you will need it to log in.%s\n' \
  "$C_YELLOW" "WARNING:" "$C_RESET"
printf '\n'
printf '  Services are starting. Check status:  docker compose ps\n'
printf '  First-run downloads can take 5–10 minutes (Ollama models).\n'
printf '  Tail logs:                            docker compose logs -f\n'
printf '\n'
