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

# wait_healthy <svc> [budget_seconds]
# Poll Docker healthcheck for <svc> until healthy or timeout.
# Returns 0 on healthy, 1 on unhealthy or timeout.
wait_healthy() {
  local svc="$1"
  local budget="${2:-60}"
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

# Port pre-check — warn only. Probes all ports JARVIS exposes on the host.
JARVIS_PORTS=(3001 4000 5432 5678 6333 8010 8011 11434)
PORTS_IN_USE=()
for port in "${JARVIS_PORTS[@]}"; do
  if command -v ss >/dev/null 2>&1; then
    # Match the exact port at end of address field (e.g. *:3001) to avoid
    # false positives where ":301" matches ":3010" or ":13010".
    if ss -tlnp 2>/dev/null | awk '{print $4}' | grep -qE ":${port}$"; then
      PORTS_IN_USE+=("$port")
    fi
  elif command -v lsof >/dev/null 2>&1; then
    if lsof -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
      PORTS_IN_USE+=("$port")
    fi
  fi
done
if [ "${#PORTS_IN_USE[@]}" -gt 0 ]; then
  warn "Ports already in use: ${PORTS_IN_USE[*]}. Services on these ports may conflict on startup."
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
# Fernet requires a urlsafe-base64-encoded 32-byte key. openssl rand -base64 32
# produces exactly that (44 chars with a trailing = pad — Fernet accepts it).
JARVIS_CONFIG_KEY="$(openssl rand -base64 32)"
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
CORS_ORIGINS_OVERRIDE=""
LAN_IP=""
TUNNEL_HOSTNAME=""
DASHBOARD_BIND_HOST="127.0.0.1"
JARVIS_CERT_SAN="DNS:localhost,IP:127.0.0.1"

detect_lan_ip() {
  local ip=""
  if command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [ -z "$ip" ] && command -v ipconfig >/dev/null 2>&1; then
    ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
  fi
  printf '%s' "$ip"
}

case "$access_mode" in
  1)
    ACCESS_MODE_LABEL="localhost"
    DASHBOARD_BIND_HOST="127.0.0.1"
    JARVIS_CERT_SAN="DNS:localhost,IP:127.0.0.1"
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
    DASHBOARD_BIND_HOST="0.0.0.0"
    # Remove any stale docker-compose.override.yml — port binding is now
    # controlled by DASHBOARD_BIND_HOST in .env, so the override is not needed
    # and a leftover one would cause Docker to create duplicate port bindings.
    if [ -f docker-compose.override.yml ]; then
      mv docker-compose.override.yml "docker-compose.override.yml.bak.$(date +%s)"
      warn "Existing docker-compose.override.yml backed up (LAN mode uses DASHBOARD_BIND_HOST instead)."
    fi
    LAN_IP="$(detect_lan_ip)"
    if [ -n "$LAN_IP" ]; then
      CORS_ORIGINS_OVERRIDE="https://localhost:3001,https://${LAN_IP}:3001"
      JARVIS_CERT_SAN="DNS:localhost,IP:127.0.0.1,IP:${LAN_IP}"
      ok "Detected LAN IP: ${LAN_IP} (will be added to CORS_ORIGINS and cert SAN)."
    else
      warn "Could not auto-detect LAN IP — you may need to edit CORS_ORIGINS and JARVIS_CERT_SAN in .env to add your machine's IP."
    fi
    ;;
  3)
    ACCESS_MODE_LABEL="tunnel"
    DASHBOARD_BIND_HOST="127.0.0.1"
    # Zero-Trust gate — must be acknowledged before proceeding.
    if [ -z "${JARVIS_TUNNEL_ACK_ZT_CONFIGURED:-}" ] || [ "$JARVIS_TUNNEL_ACK_ZT_CONFIGURED" != "1" ]; then
      printf '\n'
      printf '\033[0;31m[WARNING] Cloudflare tunnel exposes your services to the internet!\033[0m\n'
      printf 'You MUST configure Zero-Trust access policies at https://one.dash.cloudflare.com/\n'
      printf 'Once configured, set JARVIS_TUNNEL_ACK_ZT_CONFIGURED=1 in your .env to proceed.\n'
      exit 1
    fi
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
    # Prompt for the public hostname so CORS_ORIGINS and cert SAN are correct.
    printf '\n'
    info "What public hostname did you configure for this tunnel in Cloudflare Zero Trust?"
    while true; do
      read -r -p "Cloudflare Tunnel public hostname (e.g. jarvis.mydomain.com): " TUNNEL_HOSTNAME
      if printf '%s' "$TUNNEL_HOSTNAME" | grep -qE '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$'; then
        break
      fi
      echo "Invalid hostname. Use lowercase letters, digits, hyphens, and dots only."
    done
    CORS_ORIGINS_OVERRIDE="https://${TUNNEL_HOSTNAME},https://localhost:3001"
    JARVIS_CERT_SAN="DNS:localhost,IP:127.0.0.1,DNS:${TUNNEL_HOSTNAME}"
    CF_TRUST_OVERRIDE=true
    ok "Tunnel hostname: ${TUNNEL_HOSTNAME} (added to CORS_ORIGINS and cert SAN)."
    ok "JARVIS_TRUST_CF_CONNECTING_IP=true — rate limiting will key off the real CF-Connecting-IP header rather than the tunnel origin."
    ;;
  *)
    die "Invalid choice '$access_mode'. Expected 1, 2, or 3." \
        "Re-run ./setup.sh and pick a listed option."
    ;;
esac

# ---------------------------------------------------------------------------
# Detect SAN change — cert volume must be wiped when the access mode changes
# so the new SAN is included in the regenerated certificate.
# ---------------------------------------------------------------------------
OLD_SAN=$(grep '^JARVIS_CERT_SAN=' .env 2>/dev/null | cut -d= -f2-)
if [ -n "$OLD_SAN" ] && [ "$OLD_SAN" != "$JARVIS_CERT_SAN" ]; then
  warn "Access mode changed — SSL certificate SAN has changed."
  warn "  Old: ${OLD_SAN}"
  warn "  New: ${JARVIS_CERT_SAN}"
  read -r -p "Regenerate certificate? This will restart the dashboard container. [y/N] " confirm
  if [[ "$confirm" =~ ^[Yy]$ ]]; then
    docker compose down dashboard 2>/dev/null || true
    # Use 'down -v' scoped to dashboard: Compose resolves the volume name
    # with the correct project prefix (not a hardcoded jarvis_ prefix).
    docker compose down -v dashboard 2>/dev/null || true
    ok "Certificate volume removed — a new cert will be generated on next start."
  else
    warn "Skipping cert regeneration. Certificate SAN may be stale — browser may show a security warning."
  fi
fi

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
    JARVIS_CONFIG_KEY)        printf '%s' "$JARVIS_CONFIG_KEY" ;;
    N8N_ENCRYPTION_KEY)       printf '%s' "$N8N_ENCRYPTION_KEY" ;;
    N8N_JWT_SECRET)           printf '%s' "$N8N_JWT_SECRET" ;;
    CLOUDFLARE_TUNNEL_TOKEN)  printf '%s' "$CLOUDFLARE_TUNNEL_TOKEN" ;;
    TELEGRAM_BOT_TOKEN)       printf '%s' "$TELEGRAM_BOT_TOKEN" ;;
    TUNNEL_HOSTNAME)          printf '%s' "$TUNNEL_HOSTNAME" ;;
    DASHBOARD_BIND_HOST)      printf '%s' "$DASHBOARD_BIND_HOST" ;;
    JARVIS_CERT_SAN)          printf '%s' "$JARVIS_CERT_SAN" ;;
    JARVIS_TRUST_CF_CONNECTING_IP) [ -n "$CF_TRUST_OVERRIDE" ] && printf '%s' "$CF_TRUST_OVERRIDE" || return 1 ;;
    CORS_ORIGINS)
      if [ -n "$CORS_ORIGINS_OVERRIDE" ]; then
        printf '%s' "$CORS_ORIGINS_OVERRIDE"
      else
        return 1
      fi
      ;;
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

# Enforce 600 mode on any secret files that already exist
if [ -d secrets ]; then
  find secrets -maxdepth 1 -type f -name "*.txt" -exec chmod 600 {} \;
  ok "secrets/ files enforced to mode 600."
fi

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
# 11. Wait for mandatory services to become healthy
# -----------------------------------------------------------------------------
# Optional services (n8n, telegram_bot) are profile-gated and intentionally
# excluded from this list.
MANDATORY_SVCS=(postgres ollama litellm paper_ingestion learning_engine dashboard)

printf '\n'
info "Waiting for services to become healthy..."

SETUP_FAILED=()
for svc in "${MANDATORY_SVCS[@]}"; do
  case "$svc" in
    ollama) _budget=180 ;;  # first-run model pull can be slow
    *)      _budget=60  ;;
  esac
  if ! wait_healthy "$svc" "$_budget"; then
    SETUP_FAILED+=("$svc")
    warn "Dumping last 50 log lines for $svc:"
    docker compose logs --tail 50 "$svc" >&2 || true
  fi
done

if [ "${#SETUP_FAILED[@]}" -gt 0 ]; then
  printf '\n'
  err "The following service(s) did not become healthy: ${SETUP_FAILED[*]}"
  cat >&2 <<EOF

Recovery steps:
  1. Check full logs:   docker compose logs --tail=200 ${SETUP_FAILED[*]}
  2. Verify .env has correct values and re-run: ./setup.sh
  3. For Ollama model pull issues, run manually: docker compose exec ollama ollama pull <model>
EOF
  exit 1
fi

# LAN reachability probe (non-fatal — just informational).
if [ "$ACCESS_MODE_LABEL" = "lan" ] && [ -n "$LAN_IP" ]; then
  info "Probing LAN reachability at https://${LAN_IP}:3001/health ..."
  if curl -fkso /dev/null "https://${LAN_IP}:3001/health" 2>/dev/null; then
    ok "LAN reachable at https://${LAN_IP}:3001"
  else
    warn "LAN probe failed — services may still be starting, or a firewall may be blocking port 3001."
    warn "  Once the dashboard is up, verify with: curl -kso /dev/null https://${LAN_IP}:3001/health"
  fi
fi

# -----------------------------------------------------------------------------
# 12. Summary (only reached when all mandatory services are healthy)
# -----------------------------------------------------------------------------
DASHBOARD_URL="https://localhost:3001"
case "$ACCESS_MODE_LABEL" in
  lan)
    if [ -n "$LAN_IP" ]; then
      DASHBOARD_URL="https://${LAN_IP}:3001"
    else
      DASHBOARD_URL="https://<this-machine-ip>:3001"
    fi
    ;;
  tunnel)
    if [ -n "$TUNNEL_HOSTNAME" ]; then
      DASHBOARD_URL="https://${TUNNEL_HOSTNAME}"
    else
      DASHBOARD_URL="via your Cloudflare tunnel hostname"
    fi
    ;;
esac

printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   ✅ Setup complete.%s\n' "$C_GREEN" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '  Dashboard:    %s\n' "$DASHBOARD_URL"
# Write the API key to a protected file instead of printing it to the terminal.
_KEY_FILE="${HOME}/.config/jarvis/api-key"
mkdir -p "${HOME}/.config/jarvis"
chmod 700 "${HOME}/.config/jarvis"
printf '%s' "$JARVIS_API_KEY" > "$_KEY_FILE"
chmod 600 "$_KEY_FILE"
printf '  API key:      written to %s (starts: %s...)\n' "$_KEY_FILE" "${JARVIS_API_KEY:0:8}"
printf '  %sTo retrieve:%s grep JARVIS_API_KEY .env\n' "$C_BOLD" "$C_RESET"
printf '\n'
printf '  All mandatory services healthy. You can now open the dashboard.\n'
printf '  Tail logs:  docker compose logs -f\n'
printf '  Public TLS: set LETSENCRYPT_DOMAIN and LETSENCRYPT_EMAIL, then run docker compose --profile letsencrypt up -d caddy\n'
printf '\n'
