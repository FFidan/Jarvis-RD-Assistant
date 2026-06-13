#!/usr/bin/env bash
# setup.sh — JARVIS RD Assistant first-time installer.
#
# Idempotent: second run with an existing .env prompts before clobbering.
# macOS-safe: no `sed -i`, no GNU-only flags. Uses tempfile + mv.
#
# Non-interactive mode (CI / unattended installs):
#   ./setup.sh --non-interactive [OPTIONS]
#
#   --domain <host>           Public hostname (e.g. jarvis.example.com).
#                             Determines access mode: localhost (when omitted),
#                             local-https (*.local / bare name), or sets
#                             TUNNEL_HOSTNAME when --profile=letsencrypt/tunnel.
#   --admin-email <email>     Used for Let's Encrypt ACME account.
#   --profile <dev|local-https|letsencrypt>
#                             dev          — ENVIRONMENT=development, localhost binding
#                             local-https  — self-signed cert, access mode 1
#                             letsencrypt  — Caddy + ACME; requires --domain + --admin-email
#   --mode <single|multi>     Install mode written to JARVIS_SETUP_MODE in .env.
#                             single (default) — personal instance, API-key login.
#                             multi            — team instance, email/magic-link login.
#   --check                   Doctor / preflight check (read-only). Exits 0 if all
#                             requirements are met, 1 if any are missing. Does NOT
#                             generate .env or start services.
#   --backend ollama|vllm|auto
#                             Override AI backend selection. Default: auto (inferred
#                             from GPU VRAM tier). Use vllm only on 24 GB+ cards.
#   --smart-model <id>        Override the model id for the active backend
#                             (Ollama tag or HuggingFace AWQ repo id).
#   --smtp-host <host>        SMTP relay hostname.
#   --smtp-user <user>        SMTP relay username.
#   --smtp-pass-file <path>   Path to a file whose first line is the SMTP password.
#                             (Avoids passing credentials on the command line.)
#
# In non-interactive mode every prompt is driven by flags or safe defaults;
# no stdin reads are attempted.
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

os_install_hint() {  # $1 = tool name (informational)
  case "$(uname -s 2>/dev/null)" in
    Darwin) printf 'macOS: install Docker Desktop — https://docs.docker.com/desktop/install/mac-install/' ;;
    Linux)  printf 'Linux: https://docs.docker.com/engine/install/ (then: sudo usermod -aG docker $USER && newgrp docker)' ;;
    *)      printf 'See https://docs.docker.com/engine/install/' ;;
  esac
}

detect_hw_tier() {  # echoes: cpu | lt-8 | 8-16 | 16-24 | 24-48 | ge-48
  local smi; smi=$(resolve_nvidia_smi) || { echo cpu; return; }
  "$smi" -L 2>/dev/null | grep -q . || { echo cpu; return; }
  local mb
  mb=$("$smi" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  [ -z "$mb" ] && { echo cpu; return; }
  case "$mb" in *[!0-9]*) echo cpu; return ;; esac
  if   [ "$mb" -lt 8000  ]; then echo lt-8
  elif [ "$mb" -lt 16000 ]; then echo 8-16
  elif [ "$mb" -lt 24000 ]; then echo 16-24
  elif [ "$mb" -lt 48000 ]; then echo 24-48
  else                            echo ge-48
  fi
}

# _default_model_for_tier lives in scripts/setup_lib.sh (sourced below) so the
# PyYAML-optional fallback path is unit-testable.

prompt_ai_backend() {
  local tier; tier=$(detect_hw_tier)
  NI_HW_TIER="${tier}"

  case "$tier" in
    cpu|lt-8|8-16)
      printf '%sDetected: %s. Configuring Ollama.%s\n' "$C_BOLD" "$tier" "$C_RESET"
      NI_LLM_BACKEND="ollama"
      NI_SMART_MODEL=$(_default_model_for_tier "$tier" ollama)
      return
      ;;
    16-24)
      printf '%sDetected: %s. Configuring Ollama (advanced users can switch to vLLM in Settings).%s\n' \
        "$C_BOLD" "$tier" "$C_RESET"
      NI_LLM_BACKEND="ollama"
      NI_SMART_MODEL=$(_default_model_for_tier "$tier" ollama)
      return
      ;;
  esac

  # 24-48 or ge-48: GPU box. Default to Ollama-on-GPU (wired + works out of the box).
  # vLLM is an advanced, MANUAL overlay (not auto-started) — offer it as info only.
  local ollama_model; ollama_model=$(_default_model_for_tier "$tier" ollama)
  cat <<EOF

=== AI Backend ============================================
Detected hardware tier: ${tier}

  [1] Ollama on your GPU   (recommended, default) — model: ${ollama_model}
  [2] vLLM                 (advanced; manual overlay, not auto-started)
  [3] Cancel — I'll configure later in Settings
EOF
  read -rp "Choice [1]: " _choice
  case "${_choice:-1}" in
    3) die "AI backend setup cancelled" "Re-run setup.sh; current config remains untouched" ;;
    2)
      NI_LLM_BACKEND="ollama"; NI_SMART_MODEL="${ollama_model}"
      printf '%svLLM is a manual, advanced overlay and is not started automatically.%s\n' "$C_BOLD" "$C_RESET"
      printf 'Your stack is configured for Ollama-on-GPU now. To run vLLM later:\n'
      printf '  docker compose -f docker-compose.yml -f docker-compose.vllm.yml --profile vllm up -d\n'
      printf 'then set the LiteLLM `smart` alias to vLLM in Settings.\n'
      ;;
    *) NI_LLM_BACKEND="ollama"; NI_SMART_MODEL="${ollama_model}" ;;
  esac
}

run_doctor() {
  local fail=0
  local _gpu_detected=0
  printf '%s--- setup.sh --check (read-only) -------------------------------%s\n' "$C_BOLD" "$C_RESET"
  if command -v docker >/dev/null 2>&1; then ok "docker present"; else err "docker missing — $(os_install_hint docker)"; fail=1; fi
  if docker compose version >/dev/null 2>&1; then ok "docker compose v2 present"; else err "docker compose v2 missing"; fail=1; fi
  # `docker info` (not a socket stat) so DOCKER_HOST/rootless setups are honoured.
  if docker info >/dev/null 2>&1; then ok "docker daemon reachable"; else err "docker daemon unreachable — start Docker (Docker Desktop on macOS; 'sudo systemctl start docker' on Linux), or check DOCKER_HOST/permissions"; fail=1; fi
  if command -v openssl >/dev/null 2>&1; then ok "openssl present"; else err "openssl missing"; fail=1; fi
  # Detect GPU presence and, if found, surface an advisory model recommendation.
  # VRAM detection uses nvidia-smi --query-gpu=memory.total (MiB); the advisory
  # recommendation is generated by hardware_fit.recommend_models (single source
  # of truth — no bash threshold duplication).  All paths are non-fatal: if
  # python3/the package are not importable in the host preflight environment, a
  # static pointer to Settings → System/Models is printed instead.
  local tier; tier=$(detect_hw_tier)
  info "HW tier: ${tier}"
  local _vram_mb="" _smi=""
  _smi="$(resolve_nvidia_smi || true)"
  if [ -n "$_smi" ] && "$_smi" -L >/dev/null 2>&1; then
    ok "GPU detected"
    _gpu_detected=1
    _vram_mb="$("$_smi" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n 1 | tr -d ' ' || true)"
    if [ -n "$_vram_mb" ] && [ "$_vram_mb" -eq "$_vram_mb" ] 2>/dev/null; then
      # Try to import hardware_fit and print the summary — DRY: threshold logic
      # lives only in hardware_fit.py, never reimplemented here.
      local _hw_summary=""
      if command -v python3 >/dev/null 2>&1; then
        _hw_summary="$(python3 -c "
from jarvis_common.hardware_fit import recommend_models
print(recommend_models(${_vram_mb}).summary)
" 2>/dev/null || true)"
      fi
      if [ -n "$_hw_summary" ]; then
        info "GPU VRAM ${_vram_mb} MiB — model recommendation: ${_hw_summary}"
      else
        info "GPU VRAM ${_vram_mb} MiB — see model recommendation in Settings → System/Models"
      fi
    fi
  else
    info "no GPU — Ollama on CPU (slower, OK)"
  fi
  # Advisory only (never touches the fail counter): whether the Docker daemon
  # exposes the nvidia runtime decides if the GPU overlay can engage at start.
  if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    info "Docker nvidia runtime present — GPU overlay will engage on start"
  elif [ "$_gpu_detected" -eq 1 ]; then
    warn "GPU detected but the Docker NVIDIA runtime is not configured — the stack will run on CPU. Install nvidia-container-toolkit, then: nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
  else
    info "Docker nvidia runtime not found — services run CPU-only (slower, OK)"
  fi
  # Advisory only: low free disk in the install dir invites a mid-pull failure
  # (model + image layers run to several GB). 20 GB is a soft floor.
  local _free_kb=""
  _free_kb="$(df -Pk . 2>/dev/null | awk 'NR==2{print $4}' || true)"
  if [ -n "$_free_kb" ] && [ "$_free_kb" -eq "$_free_kb" ] 2>/dev/null && [ "$_free_kb" -lt 20971520 ]; then
    warn "Low free disk: $((_free_kb / 1048576)) GB free here — recommend ≥20 GB for model + image layers."
  fi
  if [ -f .env ]; then info ".env exists (re-run setup.sh to regenerate)"; else info ".env not yet generated"; fi

  # Non-fatal heads-up: the docker network is pinned to JARVIS_NET_SUBNET; a
  # pre-existing host route in that /24 would collide. Detection only — never
  # die or touch the fail counter, so --check exit semantics are unchanged.
  local pinned_subnet="${JARVIS_NET_SUBNET:-10.137.241.0/24}"
  local subnet_prefix="${pinned_subnet%.*/*}."   # e.g. "10.137.241."
  local host_routes=""
  if command -v ip >/dev/null 2>&1; then
    host_routes="$(ip -o route 2>/dev/null || true)"
  elif command -v netstat >/dev/null 2>&1; then
    host_routes="$(netstat -rn 2>/dev/null || true)"
  fi
  if [ -n "$host_routes" ] && printf '%s' "$host_routes" | grep -qF "$subnet_prefix"; then
    warn "Host already has a route in ${pinned_subnet} — set JARVIS_NET_SUBNET in .env to a free range before starting."
  fi
  # When the operator overrides JARVIS_NET_SUBNET to a non-default value, the
  # hard-coded literals in frontend/nginx.conf must also be updated manually —
  # otherwise the rate-limit self-DoS protection silently regresses (Caddy
  # IP no longer matches set_real_ip_from → all clients collapse to one bucket).
  # Note: docker-compose.yml no longer contains a hardcoded gateway; Docker
  # auto-assigns the first host in JARVIS_NET_SUBNET, so compose won't hard-fail.
  # Network gateway is auto-assigned; set JARVIS_NET_SUBNET to change the subnet.
  if [ -n "${JARVIS_NET_SUBNET:-}" ] && [ "$JARVIS_NET_SUBNET" != "10.137.241.0/24" ]; then
    warn "JARVIS_NET_SUBNET overridden to ${JARVIS_NET_SUBNET}: also update frontend/nginx.conf set_real_ip_from literals to the new range, or per-client rate limiting will regress."
  fi

  if [ "$fail" -eq 0 ]; then ok "PREFLIGHT: PASS"; else err "PREFLIGHT: FAIL — fix the items above and re-run ./setup.sh --check"; fi
  return "$fail"
}

# require_langfuse_secrets — precondition guard for --profile observability.
# The Langfuse image does NOT honour the Docker Secrets _FILE convention, so
# the three secrets are sourced from .env (mirrored to ./secrets/ for parity
# with the rest of the stack).  Compose has no ``:-default`` on these vars
# and will fail-fast if any is unset; we surface a friendlier error here when
# the caller asks for the observability profile without first running
# ``scripts/init-secrets.sh``.
require_langfuse_secrets() {
  local missing=()
  for v in LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT LANGFUSE_PG_PASSWORD LANGFUSE_INIT_USER_PASSWORD; do
    if ! grep -qE "^${v}=.+" .env 2>/dev/null; then
      missing+=("$v")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    die "Cannot start --profile observability: missing Langfuse secrets in .env: ${missing[*]}" \
        "Run: bash scripts/init-secrets.sh"
  fi
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
# shellcheck source=scripts/setup_lib.sh
source "${SCRIPT_DIR}/scripts/setup_lib.sh"

# -----------------------------------------------------------------------------
# Flag parsing  (must happen after cd "$SCRIPT_DIR" so relative paths resolve)
# -----------------------------------------------------------------------------
NON_INTERACTIVE=0
NI_DOMAIN=""
NI_ADMIN_EMAIL=""
NI_PROFILE="dev"      # dev | local-https | letsencrypt
NI_MODE="single"      # single | multi
NI_MODE_EXPLICIT=0
RUN_DOCTOR=0
NI_SMTP_HOST=""
NI_SMTP_USER=""
NI_SMTP_PASS=""
NI_LLM_BACKEND=""     # ollama | vllm | auto (resolved at .env-write time)
NI_SMART_MODEL=""     # model id; resolved by prompt_ai_backend or auto-resolve
NI_HW_TIER=""         # populated by prompt_ai_backend or auto-resolve

while [ $# -gt 0 ]; do
  case "$1" in
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    --domain)
      NI_DOMAIN="$2"
      shift 2
      ;;
    --domain=*)
      NI_DOMAIN="${1#*=}"
      shift
      ;;
    --admin-email)
      NI_ADMIN_EMAIL="$2"
      shift 2
      ;;
    --admin-email=*)
      NI_ADMIN_EMAIL="${1#*=}"
      shift
      ;;
    --profile)
      NI_PROFILE="$2"
      shift 2
      ;;
    --profile=*)
      NI_PROFILE="${1#*=}"
      shift
      ;;
    --smtp-host)
      NI_SMTP_HOST="$2"
      shift 2
      ;;
    --smtp-host=*)
      NI_SMTP_HOST="${1#*=}"
      shift
      ;;
    --smtp-user)
      NI_SMTP_USER="$2"
      shift 2
      ;;
    --smtp-user=*)
      NI_SMTP_USER="${1#*=}"
      shift
      ;;
    --smtp-pass-file)
      NI_SMTP_PASS="$(head -n 1 "$2" 2>/dev/null || true)"
      shift 2
      ;;
    --smtp-pass-file=*)
      NI_SMTP_PASS="$(head -n 1 "${1#*=}" 2>/dev/null || true)"
      shift
      ;;
    --mode)
      NI_MODE="$2"; NI_MODE_EXPLICIT=1; shift 2 ;;
    --mode=*)
      NI_MODE="${1#*=}"; NI_MODE_EXPLICIT=1; shift ;;
    --check)
      RUN_DOCTOR=1; shift ;;
    --backend)
      case "$2" in
        ollama|vllm|auto) NI_LLM_BACKEND="$2"; shift 2 ;;
        *) die "Invalid --backend '$2'. Expected: ollama|vllm|auto" "Run: $0 --help" ;;
      esac ;;
    --backend=*)
      _v="${1#*=}"
      case "$_v" in
        ollama|vllm|auto) NI_LLM_BACKEND="$_v"; shift ;;
        *) die "Invalid --backend '$_v'. Expected: ollama|vllm|auto" "Run: $0 --help" ;;
      esac ;;
    --smart-model)
      NI_SMART_MODEL="$2"; shift 2 ;;
    --smart-model=*)
      NI_SMART_MODEL="${1#*=}"; shift ;;
    -h|--help)
      sed -n '/^# setup.sh/,/^set -euo/{ /^#/!d; s/^# \{0,1\}//p; }' "$0" | head -40
      exit 0
      ;;
    *)
      die "Unknown flag: $1" "Run: $0 --help"
      ;;
  esac
done

# Validate --profile value.
case "$NI_PROFILE" in
  dev|local-https|letsencrypt) ;;
  *) die "Invalid --profile '$NI_PROFILE'. Expected: dev, local-https, or letsencrypt." \
         "Run: $0 --help" ;;
esac

case "$NI_MODE" in
  single|multi) ;;
  *) die "Invalid --mode '$NI_MODE'. Expected: single or multi." "Run: $0 --help" ;;
esac

# letsencrypt requires both --domain and --admin-email.
if [ "$NON_INTERACTIVE" -eq 1 ] && [ "$NI_PROFILE" = "letsencrypt" ]; then
  [ -n "$NI_DOMAIN" ]      || die "--profile=letsencrypt requires --domain."      "Provide: --domain=jarvis.example.com"
  [ -n "$NI_ADMIN_EMAIL" ] || die "--profile=letsencrypt requires --admin-email." "Provide: --admin-email=you@example.com"
fi

if [ "$RUN_DOCTOR" -eq 1 ]; then run_doctor; exit $?; fi

# -----------------------------------------------------------------------------
# 2. Prerequisites
# -----------------------------------------------------------------------------
info "Checking prerequisites..."

command -v docker >/dev/null 2>&1 \
  || die "Docker not found in PATH." \
         "$(os_install_hint docker)"

# docker compose v2 (space form). `docker-compose` (hyphen) is v1 and unsupported.
if ! docker compose version >/dev/null 2>&1; then
  die "Docker Compose v2 is required (the 'docker compose' plugin)." \
      "$(os_install_hint docker)"
fi
COMPOSE_VER="$(docker compose version --short 2>/dev/null || echo 'unknown')"
case "$COMPOSE_VER" in
  2.*|v2.*) ok "Docker Compose v${COMPOSE_VER#v}" ;;
  *)        warn "Unexpected Compose version '$COMPOSE_VER' — expected v2.x. Proceeding." ;;
esac

# Fatal daemon probe — must run before the idempotency gate and every prompt,
# so a dead daemon can never strand a half-answered wizard or persist a stale
# COMPOSE_FILE. `docker info` (not a socket stat) honours DOCKER_HOST/rootless.
docker info >/dev/null 2>&1 \
  || die "Docker daemon is not reachable ('docker info' failed)." \
         "Start Docker (Docker Desktop on macOS; 'sudo systemctl start docker' on Linux), check DOCKER_HOST/permissions, then re-run ./setup.sh"

command -v openssl >/dev/null 2>&1 \
  || die "openssl required for secret generation." \
         "$(os_install_hint docker)"

# GPU is informational — not fatal. Resolve nvidia-smi via the WSL2-aware helper
# (same as detect_hw_tier) so WSL2 hosts — where nvidia-smi is off PATH at
# /usr/lib/wsl/lib/nvidia-smi — still capture JARVIS_HOST_VRAM_MB for the GPU
# overlay handoff. A bare `command -v nvidia-smi` would miss them.
NI_HOST_VRAM_MB=""
_ni_smi="$(resolve_nvidia_smi 2>/dev/null || true)"
if [ -n "$_ni_smi" ]; then
  GPU_LINE="$("$_ni_smi" -L 2>/dev/null | head -n 1 || true)"
  if [ -n "$GPU_LINE" ]; then
    ok "GPU detected: $GPU_LINE"
    _vram_mb="$("$_ni_smi" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')"
    case "$_vram_mb" in
      ''|*[!0-9]*) ;;
      *) NI_HOST_VRAM_MB="$_vram_mb" ;;
    esac
  else
    warn "nvidia-smi present but no GPU enumerated — Ollama will run on CPU (slower)."
  fi
else
  info "No NVIDIA GPU detected — Ollama will run on CPU (slower)."
fi

# Port pre-check — warn only. Probes all ports JARVIS exposes on the host.
JARVIS_PORTS=(3001 4000 5432 6333 8010 8011 11434)
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

# existing_env_value KEY — print the current value of KEY from .env, or nothing
# if .env is absent or KEY is absent/empty. A present-but-empty `KEY=` counts
# as absent (the `=.\+` requires at least one char after `=`). The value is
# emitted verbatim after the first `=`, so `=`, `/`, `+`, and base64 padding
# survive intact.
existing_env_value() {
  [ -f .env ] || return 1
  grep -qE "^$1=.+" .env 2>/dev/null || return 1
  grep "^$1=" .env | head -n 1 | cut -d'=' -f2-
}

# -----------------------------------------------------------------------------
# 3. Idempotency gate
# Declining the overwrite keeps .env AND starts the stack with it — a re-run
# must never dead-end with services down. COMPOSE_FILE and COMPOSE_PROFILES
# persisted in .env are honoured natively by docker compose.
# -----------------------------------------------------------------------------
if [ -f .env ]; then
  printf '\n%sConfiguration already exists (.env).%s\n' "$C_YELLOW" "$C_RESET"
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    info "Non-interactive mode — overwriting existing .env."
  else
    read -rp "Overwrite? (y/N): " reply
    case "$reply" in
      [yY]|[yY][eE][sS]) info "Proceeding — existing .env will be replaced." ;;
      *)
        ok "Keeping existing .env — starting the stack with it."
        KEEP_PROFILE_ARGS=()
        if ! existing_env_value COMPOSE_PROFILES >/dev/null; then
          # Pre-v0.8 .env files never persisted the profile selection.
          if existing_env_value TELEGRAM_BOT_TOKEN >/dev/null; then
            KEEP_PROFILE_ARGS+=(--profile telegram)
            info "No COMPOSE_PROFILES in .env — enabling the telegram profile (TELEGRAM_BOT_TOKEN is set)."
          fi
        fi
        # Source versions.env so the postgres SHA-digest pin and image tags
        # are available on the keep-path the same as on the normal install path.
        if [ -f versions.env ]; then
          # shellcheck disable=SC1091  # versions.env is runtime-provided KEY=VALUE data, not a script
          set -a && . ./versions.env && set +a
        fi
        info "Starting services with: docker compose ${KEEP_PROFILE_ARGS[*]:-} up -d"
        if ! docker compose ${KEEP_PROFILE_ARGS[@]+"${KEEP_PROFILE_ARGS[@]}"} up -d; then
          die "docker compose up failed." \
              "Inspect logs: docker compose logs --tail=200"
        fi
        ok "Stack started with the existing configuration. To regenerate .env, re-run ./setup.sh and answer 'y'."
        exit 0
        ;;
    esac
  fi
fi

if [ ! -f .env.example ]; then
  die ".env.example not found in $SCRIPT_DIR." \
      "Run this script from the repo root, or: git pull"
fi

# -----------------------------------------------------------------------------
# 4. Secret generation
# -----------------------------------------------------------------------------
# Re-running setup.sh must NOT rotate secrets: the Postgres volume still holds
# the old POSTGRES_PASSWORD and every user_config row is Fernet-encrypted with
# the old JARVIS_CONFIG_KEY, so regenerating either makes the stack unbootable.
# When .env already exists, reuse each secret it already contains; only
# openssl-generate a fresh value when the key is absent or empty (mirrors the
# never-clobber contract of scripts/init-secrets.sh::sync_secret).  This must
# read .env BEFORE section 7 overwrites it via tempfile + mv.
# (existing_env_value is defined above section 3 — the idempotency gate's
# keep-and-start path needs it first.)

info "Generating secrets..."
POSTGRES_PASSWORD="$(existing_env_value POSTGRES_PASSWORD || openssl rand -hex 24)"
JARVIS_API_KEY="$(existing_env_value JARVIS_API_KEY || openssl rand -hex 32)"
# Fernet requires a urlsafe-base64-encoded 32-byte key. openssl rand -base64 32
# produces exactly that (44 chars with a trailing = pad — Fernet accepts it).
JARVIS_CONFIG_KEY="$(existing_env_value JARVIS_CONFIG_KEY || openssl rand -base64 32)"
# LiteLLM master_key gates all admin endpoints (/config/update etc.)
LITELLM_MASTER_KEY="$(existing_env_value LITELLM_MASTER_KEY || openssl rand -hex 32)"
ok "Secrets generated."

# Docker secret files are written by scripts/init-secrets.sh (single source of
# truth) after .env is fully populated.  See section 7a below.
QDRANT_API_KEY="$(existing_env_value QDRANT_API_KEY || openssl rand -hex 24)"

# -----------------------------------------------------------------------------
# 5. Question 1 — Access mode
# Non-interactive mode: derive access_mode from --profile and --domain.
#   dev / local-https with no domain → localhost (mode 1)
#   letsencrypt with a domain         → tunnel-equivalent (mode 3 path skipped;
#                                        handled via LETSENCRYPT_* vars below)
# -----------------------------------------------------------------------------
if [ "$NON_INTERACTIVE" -eq 1 ]; then
  case "$NI_PROFILE" in
    letsencrypt)
      access_mode="1"    # LETSENCRYPT_DOMAIN/EMAIL are written below; no Cloudflare
      ;;
    *)
      access_mode="1"    # localhost/local-https always bind locally
      ;;
  esac
else
  printf '\n%sHow do you want to access the dashboard?%s\n' "$C_BOLD" "$C_RESET"
  cat <<'EOF'
  1) Localhost only (default, safest)
  2) LAN — reachable from other devices on your network
  3) Global — access from anywhere via Cloudflare Tunnel (free, no ports opened)
EOF
  read -rp "Choice [1]: " access_mode
  access_mode="${access_mode:-1}"
fi

if [ "$NON_INTERACTIVE" -eq 0 ] && [ "$NI_MODE_EXPLICIT" -eq 0 ]; then
  printf '\n%sWho will use this instance?%s\n' "$C_BOLD" "$C_RESET"
  cat <<'EOF'
1) Just me (single-user — log in with your API key, no email setup needed)
2) A team (multi-user — email/magic-link login, SMTP required)
EOF
  read -rp "Choice [1]: " _m; case "${_m:-1}" in 2) NI_MODE="multi" ;; *) NI_MODE="single" ;; esac
fi

# Auto-resolve backend when non-interactive and --backend not specified.
if [ "${NI_LLM_BACKEND:-auto}" = "auto" ] && [ "$NON_INTERACTIVE" -eq 1 ]; then
  NI_HW_TIER=$(detect_hw_tier)
  NI_LLM_BACKEND="ollama"
  [ -z "${NI_SMART_MODEL:-}" ] && NI_SMART_MODEL=$(_default_model_for_tier "$NI_HW_TIER" "$NI_LLM_BACKEND")
fi

# Interactive: AI backend wizard (skipped when non-interactive or backend already set).
if [ "$NON_INTERACTIVE" -eq 0 ] && [ -z "${NI_LLM_BACKEND:-}" ]; then
  prompt_ai_backend
fi

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
    warn "LAN mode binds the dashboard to 0.0.0.0 — reachable by every host on your network. Only use this on a trusted LAN; for untrusted networks prefer the Tailscale/tunnel option."
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
# `|| true` so a pre-existing .env that lacks JARVIS_CERT_SAN (e.g. a partial
# run being resumed) does not abort the script under `set -e`/`pipefail`
# (matches the guarded ENVIRONMENT lookup in section 13).
OLD_SAN=$(grep '^JARVIS_CERT_SAN=' .env 2>/dev/null | cut -d= -f2- || true)
if [ -n "$OLD_SAN" ] && [ "$OLD_SAN" != "$JARVIS_CERT_SAN" ]; then
  warn "Access mode changed — SSL certificate SAN has changed."
  warn "  Old: ${OLD_SAN}"
  warn "  New: ${JARVIS_CERT_SAN}"
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    info "Non-interactive mode — regenerating certificate automatically."
    _do_regen=1
  else
    read -r -p "Regenerate certificate? This will restart the dashboard container. [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
      _do_regen=1
    else
      _do_regen=0
    fi
  fi
  if [ "${_do_regen:-0}" -eq 1 ]; then
    docker compose down dashboard 2>/dev/null || true
    # Use 'down -v' scoped to dashboard: Compose resolves the volume name
    # with the correct project prefix (not a hardcoded jarvis_ prefix).
    docker compose down -v dashboard 2>/dev/null || true
    ok "Certificate volume removed — a new cert will be generated on next start."
  else
    warn "Skipping cert regeneration. Certificate SAN may be stale — browser may show a security warning."
  fi
fi

# Ensure secrets/ exists before the Telegram section writes into it.
# scripts/init-secrets.sh also `mkdir -p secrets`, but it runs much later
# (section 7a); on a fresh checkout the directory is absent here and the
# `printf > secrets/telegram_bot_token.txt` below would silently fail.
mkdir -p secrets

# -----------------------------------------------------------------------------
# 6. Question 2 — Telegram
# In non-interactive mode the token is sourced from the environment variable
# TELEGRAM_BOT_TOKEN (if set) or skipped silently.
# -----------------------------------------------------------------------------
# Snapshot any env-provided token BEFORE the unconditional reset below clobbers
# it — otherwise the documented non-interactive env-sourced path reads an
# always-empty value.
_ENV_TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_BOT_TOKEN=""
USE_TELEGRAM_PROFILE=0

if [ "$NON_INTERACTIVE" -eq 1 ]; then
  # Honour a token pre-set in the environment (e.g. from CI secrets).
  _ni_tg="${_ENV_TELEGRAM_BOT_TOKEN:-}"
  if [ -n "${_ni_tg// }" ] && [[ "$_ni_tg" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
    TELEGRAM_BOT_TOKEN="$_ni_tg"
    USE_TELEGRAM_PROFILE=1
    ok "Telegram token accepted (from environment)."
    printf '%s' "$TELEGRAM_BOT_TOKEN" > secrets/telegram_bot_token.txt && chmod 600 secrets/telegram_bot_token.txt
  else
    info "No valid TELEGRAM_BOT_TOKEN in environment — skipping Telegram bot."
    TELEGRAM_BOT_TOKEN=""
  fi
else
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
      printf '%s' "$TELEGRAM_BOT_TOKEN" > secrets/telegram_bot_token.txt && chmod 600 secrets/telegram_bot_token.txt
    else
      warn "That didn't look like a valid Telegram token (format: <digits>:<20+ chars>). Try again or press Enter to skip."
      tg_try2="$(prompt_telegram)"
      if [ -n "${tg_try2// }" ] && [[ "$tg_try2" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
        TELEGRAM_BOT_TOKEN="$tg_try2"
        USE_TELEGRAM_PROFILE=1
        ok "Telegram token accepted."
        printf '%s' "$TELEGRAM_BOT_TOKEN" > secrets/telegram_bot_token.txt && chmod 600 secrets/telegram_bot_token.txt
      else
        warn "Skipping Telegram — bot will not start. Add TELEGRAM_BOT_TOKEN to .env later to enable."
      fi
    fi
  else
    info "Skipping Telegram bot."
  fi
fi

# Comma-joined profile selection, persisted as COMPOSE_PROFILES in .env so a
# bare `docker compose up -d` (and the keep-and-start re-run path) starts the
# same profile set the wizard selected.
COMPOSE_PROFILES_VALUE=""
if [ "$USE_TUNNEL_PROFILE" -eq 1 ]; then
  COMPOSE_PROFILES_VALUE="tunnel"
fi
if [ "$USE_TELEGRAM_PROFILE" -eq 1 ]; then
  COMPOSE_PROFILES_VALUE="${COMPOSE_PROFILES_VALUE:+${COMPOSE_PROFILES_VALUE},}telegram"
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
    JARVIS_API_KEY)           printf '%s' "$JARVIS_API_KEY" ;;
    JARVIS_CONFIG_KEY)        printf '%s' "$JARVIS_CONFIG_KEY" ;;
    LITELLM_MASTER_KEY)        printf '%s' "$LITELLM_MASTER_KEY" ;;
    QDRANT_API_KEY)           printf '%s' "$QDRANT_API_KEY" ;;
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
    # Non-interactive: SMTP relay flags
    SMTP_HOST)
      [ -n "$NI_SMTP_HOST" ] && printf '%s' "$NI_SMTP_HOST" || return 1 ;;
    SMTP_USER)
      [ -n "$NI_SMTP_USER" ] && printf '%s' "$NI_SMTP_USER" || return 1 ;;
    SMTP_PASS)
      [ -n "$NI_SMTP_PASS" ] && printf '%s' "$NI_SMTP_PASS" || return 1 ;;
    # Non-interactive: Let's Encrypt / Caddy profile
    LETSENCRYPT_DOMAIN)
      [ -n "$NI_DOMAIN" ] && [ "$NI_PROFILE" = "letsencrypt" ] && printf '%s' "$NI_DOMAIN" || return 1 ;;
    LETSENCRYPT_EMAIL)
      [ -n "$NI_ADMIN_EMAIL" ] && [ "$NI_PROFILE" = "letsencrypt" ] && printf '%s' "$NI_ADMIN_EMAIL" || return 1 ;;
    # Non-interactive: ENVIRONMENT based on --profile
    ENVIRONMENT)
      if [ "$NON_INTERACTIVE" -eq 1 ]; then
        case "$NI_PROFILE" in
          dev)           printf 'development' ;;
          local-https)   printf 'development' ;;
          letsencrypt)   printf 'production'  ;;
        esac
      else
        return 1
      fi
      ;;
    JARVIS_SETUP_MODE) printf '%s' "$NI_MODE" ;;
    API_KEY_LOGIN_ENABLED) [ "$NI_MODE" = "single" ] && printf 'true' || printf 'false' ;;
    JARVIS_HW_TIER)
      printf '%s' "${NI_HW_TIER:-$(detect_hw_tier)}" ;;
    JARVIS_HOST_VRAM_MB)
      [ -n "$NI_HOST_VRAM_MB" ] && printf '%s' "$NI_HOST_VRAM_MB" || return 1 ;;
    JARVIS_LLM_BACKEND)
      printf '%s' "${NI_LLM_BACKEND:-auto}" ;;
    JARVIS_SMART_MODEL)
      printf '%s' "${NI_SMART_MODEL:-}" ;;
    COMPOSE_PROFILES) printf '%s' "$COMPOSE_PROFILES_VALUE" ;;
    OLLAMA_MODELS) compute_ollama_models "${NI_SMART_MODEL:-qwen3:8b}" ;;
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
# 7a. Write Docker secret files — delegate to init-secrets.sh (single source of
#     truth).  .env is now fully populated, so init-secrets.sh can read all
#     values and create every secrets/*.txt the compose stack requires.
#     init-secrets.sh is idempotent: existing files whose content already matches
#     .env are left untouched.
# -----------------------------------------------------------------------------
info "Writing Docker secret files via scripts/init-secrets.sh..."
bash "${SCRIPT_DIR}/scripts/init-secrets.sh"
ok "Docker secret files ready in secrets/ (mode 600)."

# De-seed guard: switchable model aliases (smart/fast/smart-fallback) live in
# LiteLLM's admin DB (delivered by the paper_ingestion boot reconciler), never
# in litellm/config.yaml — a YAML alias would stack with its DB replacement and
# keep routing the stale model. The script scrubs any legacy YAML aliases
# (upgrade path) and needs no env inputs; the chosen model reaches the system
# via .env (JARVIS_SMART_MODEL) + the boot reconciler.
bash scripts/render-litellm-config.sh || warn "litellm config de-seed failed (continuing)"

# init-secrets.sh does NOT generate the Langfuse init keypair, yet the default
# (no-profile) paper_ingestion/learning_engine services mount langfuse_init_pk
# /_sk as file:-backed Docker secrets — so a fresh clone would hard-fail
# `docker compose up`.  gen-langfuse-keys.sh is the single source of truth
# (also invoked by `make up`); it is idempotent and self-rotates burned keys.
info "Generating Langfuse init keypair via scripts/gen-langfuse-keys.sh..."
bash "${SCRIPT_DIR}/scripts/gen-langfuse-keys.sh"
ok "Langfuse init keypair ready in secrets/ (mode 600)."

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
# USE_OBSERVABILITY_PROFILE is opt-in (env-driven, defaults to 0).  Guard
# against the common footgun where a user runs ``USE_OBSERVABILITY_PROFILE=1
# ./setup.sh`` without first generating Langfuse secrets via init-secrets.sh.
if [ "${USE_OBSERVABILITY_PROFILE:-0}" -eq 1 ]; then
  require_langfuse_secrets
  PROFILE_ARGS+=(--profile observability)
fi

printf '\n'
# GPU overlay is opt-in based on the Docker nvidia runtime (NOT host nvidia-smi):
# only when the runtime is wired do we add docker-compose.gpu.yml, which re-adds
# the GPU reservation for ollama + paper_ingestion. CPU-only hosts stay on the
# base compose so install never hard-fails for lack of a GPU.
COMPOSE_OVERLAY=()
if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
  COMPOSE_OVERLAY=(-f docker-compose.gpu.yml)
  info "Docker nvidia runtime detected — enabling GPU overlay"
else
  info "Docker nvidia runtime not found — CPU-only (Ollama on CPU; slower, OK)"
fi
# An explicit -f list suppresses Compose's implicit docker-compose.override.yml
# auto-load, so when that file exists we must list it back explicitly. gpu BEFORE
# override so a dev override's `deploy: !reset null` on ollama still wins.
if [ -f docker-compose.override.yml ]; then
  COMPOSE_FILE_ARGS=(-f docker-compose.yml "${COMPOSE_OVERLAY[@]}" -f docker-compose.override.yml)
elif [ "${#COMPOSE_OVERLAY[@]}" -gt 0 ]; then
  COMPOSE_FILE_ARGS=(-f docker-compose.yml "${COMPOSE_OVERLAY[@]}")
else
  # No override, no overlay: keep Compose's implicit discovery untouched.
  COMPOSE_FILE_ARGS=()
fi
# Persist the resolved compose-file set so a later bare `docker compose up` uses
# the same overlays. Written UNCONDITIONALLY so a GPU→CPU re-run (or a vanished
# nvidia runtime) overwrites a stale `docker-compose.gpu.yml` entry — a leftover
# GPU overlay on a now-CPU host fails the device reservation.
_has_override=0; [ -f docker-compose.override.yml ] && _has_override=1
_has_nvidia=0; [ "${#COMPOSE_OVERLAY[@]}" -gt 0 ] && _has_nvidia=1
upsert_env_var COMPOSE_FILE "$(compute_compose_file "$_has_nvidia" "$_has_override")"
# Start Ollama alone first, then run the model pull as an attached one-off so
# its progress streams to the terminal — buried inside a bare `up -d` the
# 7-11 GB first pull looks like a hang.
info "Starting Ollama: docker compose ${COMPOSE_FILE_ARGS[*]:-} up -d ollama"
if ! docker compose ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} up -d ollama; then
  die "docker compose up failed." \
      "Inspect logs: docker compose logs --tail=200"
fi
wait_healthy ollama 180 \
  || warn "Ollama is still starting — model inventory unknown; proceeding to the model pull."

_FIRST_RUN_PULL=0
if docker compose exec -T ollama ollama list 2>/dev/null | tail -n +2 | grep -q .; then
  : # models already present — bootstrap below is a fast verify
else
  _FIRST_RUN_PULL=1
  printf '\n'
  printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
  printf '%s  Downloading models (7-11 GB) — first run can take 20-60 min. %s\n' "$C_YELLOW" "$C_RESET"
  printf '%s  Pull progress streams below. This is not an error.           %s\n' "$C_YELLOW" "$C_RESET"
  printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
  printf '\n'
fi

info "Pulling models via ollama-bootstrap..."
if ! docker compose ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} run --rm ollama-bootstrap; then
  die "Model download failed (ollama-bootstrap)." \
      "Check network/disk space and re-run ./setup.sh — or pull manually: docker compose exec ollama ollama pull <model>"
fi

info "Starting services with: docker compose ${COMPOSE_FILE_ARGS[*]:-} ${PROFILE_ARGS[*]:-} up -d"
if ! docker compose ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} up -d; then
  die "docker compose up failed." \
      "Inspect logs: docker compose logs --tail=200"
fi

# -----------------------------------------------------------------------------
# 11. Wait for mandatory services to become healthy
# -----------------------------------------------------------------------------
# Optional services (telegram_bot) are profile-gated and intentionally
# excluded from this list.
MANDATORY_SVCS=(postgres ollama litellm paper_ingestion learning_engine dashboard)

printf '\n'
info "Waiting for services to become healthy..."

# _FIRST_RUN_PULL was detected in section 10 (before the streamed bootstrap
# pull). The pull already finished by now, but the compose-managed bootstrap
# re-runs as a dependency of paper_ingestion/learning_engine, so first runs
# keep the generous 3600s budgets as a safety margin.
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
DASHBOARD_URL="http://localhost:3001"
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
printf '%s   Setup complete.%s\n' "$C_GREEN" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '  Dashboard:    %s\n' "$DASHBOARD_URL"
if [ "$NI_MODE" = "single" ]; then
  # Single-user mode: API key auth is enabled.
  _KEY_FILE="${HOME}/.config/jarvis/api-key"
  mkdir -p "${HOME}/.config/jarvis"
  chmod 700 "${HOME}/.config/jarvis"
  printf '%s' "$JARVIS_API_KEY" > "$_KEY_FILE"
  chmod 600 "$_KEY_FILE"
  printf '  API key:      written to %s (starts: %s...)\n' "$_KEY_FILE" "${JARVIS_API_KEY:0:8}"
  printf '  %sTo retrieve:%s grep JARVIS_API_KEY .env\n' "$C_BOLD" "$C_RESET"
  printf '  Sign in:      open the dashboard and enter your API key.\n'
else
  # Multi/team mode: API key login is disabled — sign in via magic link.
  printf '  Sign in:      open the dashboard and request a magic link to your email.\n'
  printf '                (API key login is disabled in multi-user mode)\n'
fi
printf '\n'
printf '  All mandatory services healthy. You can now open the dashboard.\n'
printf '  Tail logs:  docker compose logs -f\n'
printf '  Public TLS: set LETSENCRYPT_DOMAIN and LETSENCRYPT_EMAIL, then run docker compose --profile letsencrypt up -d caddy\n'
printf '\n'

# -----------------------------------------------------------------------------
# 13. Production-readiness check
# Run the readiness script (non-fatal for dev; aborts for letsencrypt/production).
# -----------------------------------------------------------------------------
_READINESS_SCRIPT="$SCRIPT_DIR/scripts/production-readiness-check.sh"
if [ -f "$_READINESS_SCRIPT" ]; then
  printf '%s--- Production Readiness Check -----------------------------------%s\n' "$C_BOLD" "$C_RESET"
  # Determine the effective environment from .env.
  _ENV_VALUE="$(grep '^ENVIRONMENT=' .env 2>/dev/null | cut -d= -f2- || true)"
  _ENV_VALUE="${_ENV_VALUE:-development}"

  if bash "$_READINESS_SCRIPT"; then
    ok "Production readiness: all checks passed."
  else
    _rc=$?
    case "$_ENV_VALUE" in
      production)
        # In letsencrypt/production profile, HIGH findings are fatal.
        err "Production readiness check found HIGH issues. Aborting."
        err "Fix the issues listed above and re-run: ./setup.sh"
        exit "$_rc"
        ;;
      *)
        warn "Production readiness check found issues (non-fatal in dev profile)."
        warn "Run 'bash scripts/production-readiness-check.sh' to see details."
        ;;
    esac
  fi
else
  warn "scripts/production-readiness-check.sh not found — skipping readiness check."
fi

# -----------------------------------------------------------------------------
# 14. Next steps
# -----------------------------------------------------------------------------
printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   Next steps%s\n' "$C_BOLD" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '  1. Open the dashboard: %s\n' "$DASHBOARD_URL"
if [ "$NI_MODE" = "single" ]; then
  printf '  2. Log in with your API key (stored in ~/.config/jarvis/api-key).\n'
else
  printf '  2. Request a magic link at the sign-in page (API key login is disabled in multi-user mode).\n'
fi
printf '  3. Invite the first admin user:\n'
printf '       %s/admin/users%s  — use the web UI to add users and set roles.\n' "$DASHBOARD_URL" ""
printf '  4. Complete the setup wizard (runs automatically on first visit).\n'
printf '\n'
printf '  Admin user management: %s/admin/users\n' "$DASHBOARD_URL"
printf '  Tail logs:             docker compose logs -f\n'
printf '\n'
