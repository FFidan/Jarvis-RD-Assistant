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
#   --tunnel-ack              Acknowledge that the Cloudflare tunnel access mode
#                             exposes this instance to the internet. Zero-Trust
#                             access policies must be configured first. Required to
#                             select the tunnel access mode non-interactively.
#   --mode <single|multi>     Install mode written to JARVIS_SETUP_MODE in .env.
#                             single (default) — personal instance, API-key login.
#                             multi            — team instance, email/magic-link login.
#   --check                   Doctor / preflight check (read-only). Exits 0 if all
#                             requirements are met, 1 if any are missing. Does NOT
#                             generate .env, install packages, or start services.
#   --install-prereqs        Explicitly run the guided prerequisite installer when
#                             Docker, Docker Compose, or openssl are missing.
#                             In --non-interactive mode this flag is required for
#                             host package installation.
#   --skip-disk-check         Skip the pre-install free-disk check on the Docker
#                             data root (a first install needs ~35-55 GB there,
#                             depending on GPU variant and model choice).
#   --build-local             Build the application images from source instead of
#                             pulling the prebuilt ones published to GHCR. Much
#                             slower and needs considerably more disk. NOT an
#                             offline path: it still needs network for base images,
#                             Python/OS wheels, third-party images (postgres, ollama,
#                             caddy, ...), and the Ollama model downloads. Use it for
#                             development or when a GHCR pull is unavailable.
#   --backend ollama|vllm|auto
#                             Override AI backend selection. Default: auto (inferred
#                             from GPU VRAM tier). Use vllm only on 24 GB+ cards.
#   --smart-model <id>        Override the model id for the active backend
#                             (Ollama tag or HuggingFace AWQ repo id).
#   --gpu cuda|rocm|vulkan|cpu
#                             Override GPU compose-overlay selection (default:
#                             detected from GPU vendor + container runtime).
#   --address <ipv4>          Override the auto-detected LAN IPv4 for LAN mode.
#   --public-origin <url>     A named private HTTPS origin (e.g. a Tailscale Serve
#                             or trusted-TLS hostname) family devices use. Writes
#                             APP_BASE_URL/DASHBOARD_SERVER_NAME/CORS_ORIGINS and,
#                             once the edge is reachable, hosts the setup link.
#                             Must be https:// with a DNS hostname (not an IP).
#   --smtp-host <host>        SMTP relay hostname.
#   --smtp-port <port>        SMTP relay port (465 = implicit TLS, 587 = STARTTLS).
#   --smtp-user <user>        SMTP relay username.
#   --smtp-from <addr>        Sender address for magic-link email (host is not
#                             deliverable without it).
#   --smtp-pass-file <path>   Path to a file whose first line is the SMTP password.
#                             Written to the smtp_pass Docker secret, never .env.
#                             An unreadable path is a fatal error, not an empty
#                             password. (Avoids passing credentials on the CLI.)
#
# In non-interactive mode every prompt is driven by flags or safe defaults;
# no stdin reads are attempted.
#
# Exit codes:
#   0  success; 1  failure.
#   3  Docker was just installed and this shell session is not in the 'docker'
#      group yet — log out and back in (or run 'newgrp docker'), then re-run
#      ./setup.sh.
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

die_enospc_aware() {
  # $1 = captured command output, $2/$3 = die() message/hint.
  # Builds and pulls land on the Docker data root — often a different
  # filesystem from the repo — so disk-exhaustion recovery reports free
  # space THERE (resolve_docker_data_root, scripts/setup_lib.sh).
  local log_file="$1" data_root
  if grep -qi 'no space left on device' "$log_file" 2>/dev/null; then
    data_root="$(resolve_docker_data_root)"
    err "Docker ran out of disk space on its data root ($data_root):"
    df -h "$data_root" >&2 || true
    cat >&2 <<EOF

Free up space on $data_root, then re-run ./setup.sh:
  1. Reclaim Docker build cache:  docker builder prune -af
  2. Still short? Grow the filesystem holding the data root — on LVM hosts:
     sudo lvextend --resizefs -L +30G <logical-volume-path>
EOF
  fi
  rm -f "$log_file"  # content already streamed to the terminal via tee
  die "$2" "$3"
}

compose_or_die() {
  # $1 = failure message, $2 = failure hint, $3.. = docker compose args.
  # Output streams to the terminal AND is captured (tee) so failures can
  # be diagnosed for disk exhaustion by die_enospc_aware.
  local message="$1" hint="$2" log_file
  shift 2
  log_file="$(mktemp "${TMPDIR:-/tmp}/jarvis-compose.XXXXXX")"
  if ! docker compose "$@" 2>&1 | tee "$log_file"; then
    die_enospc_aware "$log_file" "$message" "$hint"
  fi
  rm -f "$log_file"
}

compose_up_or_recover() {
  # Like compose_or_die, but when a GPU overlay is active (COMPOSE_OVERLAY set)
  # a failed bring-up is most often the overlay itself. Offer a one-keypress CPU
  # retry interactively before dying; otherwise extend the guidance. The CPU
  # re-exec drops any --gpu the user passed and appends --gpu cpu, so it cannot
  # re-enter this branch (COMPOSE_OVERLAY is then empty).
  # $1 = failure message, $2 = failure hint, $3.. = docker compose args.
  local message="$1" hint="$2" log_file reply
  shift 2
  log_file="$(mktemp "${TMPDIR:-/tmp}/jarvis-compose.XXXXXX")"
  if docker compose "$@" 2>&1 | tee "$log_file"; then
    rm -f "$log_file"
    return 0
  fi
  if [ "${#COMPOSE_OVERLAY[@]}" -gt 0 ]; then
    if [ "$NON_INTERACTIVE" -eq 0 ] && [ -t 0 ]; then
      read -rp "GPU overlay failed to start. Retry on CPU now? [Y/n] " reply
      case "${reply:-Y}" in
        [nN]|[nN][oO]) : ;;
        *) rm -f "$log_file"; exec "$0" --gpu cpu ${_RECOVERY_ARGS[@]+"${_RECOVERY_ARGS[@]}"} ;;
      esac
    fi
    hint="${hint}
GPU overlay failed to start — re-run ./setup.sh --gpu cpu for a CPU-only install, then please file a hardware report (GitHub issue template)."
  fi
  die_enospc_aware "$log_file" "$message" "$hint"
}

os_install_hint() {  # $1 = tool name (informational)
  case "$(uname -s 2>/dev/null)" in
    Darwin) printf 'macOS: install Docker Desktop — https://docs.docker.com/desktop/install/mac-install/' ;;
    Linux)  printf 'Linux: https://docs.docker.com/engine/install/ (then: sudo usermod -aG docker $USER && newgrp docker)' ;;
    *)      printf 'See https://docs.docker.com/engine/install/' ;;
  esac
}

detect_hw_tier() {  # echoes: cpu | lt-8 | 8-16 | 16-24 | 24-48 | ge-48
  # Vendor-neutral: the tier cuts only need a VRAM figure, whichever vendor
  # tool produced it (detect_gpu_vendor/resolve_gpu_vram_mb in setup_lib.sh).
  local mb
  mb="$(resolve_gpu_vram_mb "$(detect_gpu_vendor)")" || { echo cpu; return; }
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

  # A GPU-bearing host can still land on the CPU tier (Intel iGPUs report no
  # VRAM; an AMD card without /dev/kfd is opt-in) — name the GPU so "Detected:
  # cpu" does not read as a contradiction of the "GPU detected" line above.
  local note=""
  if [ "$tier" = "cpu" ]; then
    case "${NI_GPU_VENDOR:-none}" in
      intel) note=" (Intel GPU present — Vulkan is opt-in, see above)" ;;
      amd)   note=" (AMD GPU present — ROCm/Vulkan is opt-in, see above)" ;;
    esac
  fi

  case "$tier" in
    cpu|lt-8|8-16)
      printf '%sDetected: %s%s. Configuring Ollama.%s\n' "$C_BOLD" "$tier" "$note" "$C_RESET"
      NI_LLM_BACKEND="ollama"
      NI_SMART_MODEL=$(_default_model_for_tier "$tier" ollama)
      return
      ;;
    16-24)
      printf '%sDetected: %s%s. Configuring Ollama (advanced users can switch to vLLM in Settings).%s\n' \
        "$C_BOLD" "$tier" "$note" "$C_RESET"
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
  # python3 is a hard requirement (model selection + disk sizing shell out to it
  # under `set -euo pipefail`), so its absence FAILS --check rather than the
  # advisory-only probe below it.
  if command -v python3 >/dev/null 2>&1; then ok "python3 present"; else err "python3 missing — required for model selection and disk sizing (install python3, then re-run ./setup.sh --check)"; fail=1; fi
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
  # When the operator overrides JARVIS_NET_SUBNET to a non-default value, two sets
  # of hard-coded literals must also be updated. The gateway is auto-assigned so
  # the default (no-caddy) stack still starts, but a caddy TLS profile pins static
  # ipv4_address entries that are OUT of a custom subnet — compose WILL hard-fail
  # to attach caddy/caddy_local. nginx also trusts exactly those pinned IPs, so
  # rate limiting regresses if they drift.
  if [ -n "${JARVIS_NET_SUBNET:-}" ] && [ "$JARVIS_NET_SUBNET" != "10.137.241.0/24" ]; then
    warn "JARVIS_NET_SUBNET overridden to ${JARVIS_NET_SUBNET}: before enabling a caddy TLS profile, update BOTH docker-compose.yml caddy/caddy_local 'ipv4_address' AND frontend/nginx.conf 'set_real_ip_from' to IPs inside ${JARVIS_NET_SUBNET}, or compose will fail to attach caddy and per-client rate limiting will regress."
  fi

  if [ "$fail" -eq 0 ]; then ok "PREFLIGHT: PASS"; else err "PREFLIGHT: FAIL — fix the items above and re-run ./setup.sh --check"; fi
  return "$fail"
}

# preflight_disk — install-path disk check (run_doctor's --check advisory above
# stays warn-only). Sizes the whole cold install for the chosen smart model
# (app-image budget + infra pulls + Ollama model set) and measures free space
# on the Docker data root via preflight_disk_lib — images, volumes and models
# all land there, and `df .` lies on split-mount hosts. The shortfall is fatal
# only on a FIRST install (no app image present yet): a re-run with cached
# images only warns, as does a catalog-fallback estimate with the 20 GB hard
# floor still free. --skip-disk-check bypasses the check entirely.
preflight_disk() {
  if [ "$SKIP_DISK_CHECK" -eq 1 ]; then
    info "Skipping disk preflight (--skip-disk-check)."
    return 0
  fi
  # Budget the path this run will actually take: the default install PULLS the
  # published images (cpu-pull/cuda-pull), only --build-local builds them
  # (cpu-build/cuda-build). Charging the build ceiling for a pull would falsely
  # block hosts that have ample room for the smaller pull.
  # Budget the accelerator the install will ACTUALLY pull, not just whatever the
  # Docker runtime reports: an explicit --gpu wins over the runtime probe. Only
  # --gpu cuda (or, absent an override, an auto-detected NVIDIA runtime) pulls the
  # larger CUDA image; rocm, vulkan and cpu keep the CPU torch image (mirrors the
  # _gpu_choice resolution below). Without this, `--gpu cuda` on a host whose
  # nvidia runtime is not yet configured budgets cpu-pull but pulls cuda -> ENOSPC,
  # and `--gpu cpu` on an nvidia-runtime host over-budgets and can falsely block.
  local _accel="cpu"
  if [ -n "${NI_GPU_OVERRIDE:-}" ]; then
    [ "$NI_GPU_OVERRIDE" = "cuda" ] && _accel="cuda"
  elif docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    _accel="cuda"
  fi
  local _variant
  if [ "${BUILD_LOCAL:-0}" -eq 1 ]; then
    _variant="${_accel}-build"
  else
    _variant="${_accel}-pull"
  fi
  local _req_gb _req_exact=1
  _req_gb="$(compute_required_disk_gb "${NI_SMART_MODEL:-qwen3:8b}" "$_variant")" || _req_exact=0
  local _out _rc=0
  _out="$(preflight_disk_lib "$_req_gb")" || _rc=$?
  local _free_gb="${_out%% *}" _data_root="${_out#* }"
  case "$_rc" in
    0)
      ok "Disk preflight: ${_free_gb} GB free on ${_data_root} (~${_req_gb} GB needed)."
      return 0
      ;;
    2)
      warn "Disk preflight: the Docker data root (${_data_root}) is not measurable from the host — this is expected on Docker Desktop, where images and volumes live inside the Docker VM rather than on the host filesystem. Ensure the Docker Desktop VM disk has room for the images + models (Settings > Resources > Virtual disk limit), then proceed."
      return 0
      ;;
  esac
  # Shortfall. Cached app images mean this is a re-run, not a cold install — the
  # big app layers are already on disk, so the full-install figure over-charges.
  # But the Ollama model set is (re-)pulled on EVERY run, so still hold the line
  # on the space THAT needs. Keyed off the published repositories: the pre-1.1
  # `jarvis/*` names no longer exist once the install pulls from GHCR, and
  # grepping them would leave this escape hatch dead.
  local _img _cached=0
  for _img in "${PUBLISHED_IMAGE_REPOS[@]}"; do
    if [ -n "$(docker images -q "$_img" 2>/dev/null)" ]; then _cached=1; break; fi
  done
  if [ "$_cached" -eq 1 ]; then
    local _model_gb
    _model_gb="$(compute_model_disk_gb "${NI_SMART_MODEL:-qwen3:8b}")" || true
    if [ -n "$_model_gb" ] && [ "$_free_gb" -ge "$_model_gb" ]; then
      warn "Low disk: ${_free_gb} GB free on ${_data_root} (a full reinstall needs ~${_req_gb} GB) — continuing, app images are already present and the ~${_model_gb} GB model pull fits."
      return 0
    fi
    die "Not enough free disk for the model pull: ${_free_gb} GB free on ${_data_root} (df -Pk), ~${_model_gb} GB needed for the Ollama model set (app images are cached, but models are pulled every run)." \
        "Free up space on ${_data_root} (e.g. docker system prune), move the Docker data root to a larger disk, or re-run with --skip-disk-check to proceed anyway."
  fi
  if [ "$_req_exact" -eq 0 ] && [ "$_free_gb" -ge 20 ]; then
    warn "Low disk: ${_free_gb} GB free on ${_data_root}; the ~${_req_gb} GB figure is a worst-case estimate (model catalog unreadable). Proceeding — watch free space during the install."
    return 0
  fi
  die "Not enough free disk for a first install: ${_free_gb} GB free on ${_data_root} (df -Pk), ~${_req_gb} GB needed for images + models." \
      "Free up space on ${_data_root} (e.g. docker system prune), move the Docker data root to a larger disk, or re-run with --skip-disk-check to proceed anyway."
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
# Access-mode / ingress helpers (pure — no docker, no network). Defined before
# the flag parser so --public-origin can validate its value at parse time.
# -----------------------------------------------------------------------------
# _is_lan_ipv4 IP -> 0 when IP is a usable private LAN IPv4 (RFC1918). Rejects the
# jarvis docker bridge subnet, the docker default bridge (172.17.x), CGNAT/Tailscale
# (100.64.0.0/10), and link-local (169.254.x) — none of which is the address other
# devices on the home/lab network reach this host by.
_is_lan_ipv4() {
  case "$1" in
    10.137.241.*) return 1 ;;                                         # jarvis docker bridge
    172.17.*) return 1 ;;                                             # docker default bridge
    169.254.*) return 1 ;;                                            # link-local
    100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 1 ;;  # 100.64/10 CGNAT
    10.*|192.168.*) return 0 ;;
    172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;                # 172.16/12 (minus .17)
    *) return 1 ;;
  esac
}

# _append_server_name LIST NAME -> LIST with NAME appended (space-separated),
# skipping an empty NAME and de-duplicating. nginx's `server_name` accepts a
# space-separated list, so the LAN, public-origin and tunnel arms compose their
# accepted Host headers into one allowlist instead of clobbering each other.
_append_server_name() {
  local list="$1" name="$2"
  [ -n "$name" ] || { printf '%s' "$list"; return 0; }
  case " $list " in *" $name "*) printf '%s' "$list"; return 0 ;; esac
  [ -n "$list" ] && printf '%s %s' "$list" "$name" || printf '%s' "$name"
}

# _append_csv LIST ITEM -> LIST with ITEM appended (comma-separated), skipping an
# empty ITEM and de-duplicating. Used to compose CORS_ORIGINS across access arms.
_append_csv() {
  local list="$1" item="$2"
  [ -n "$item" ] || { printf '%s' "$list"; return 0; }
  case ",$list," in *",$item,"*) printf '%s' "$list"; return 0 ;; esac
  [ -n "$list" ] && printf '%s,%s' "$list" "$item" || printf '%s' "$item"
}

# _public_origin_host URL -> the hostname of an https:// URL whose host is a real
# DNS name, or non-zero when URL is not https or the host is an IP literal. A raw
# IP is never a valid WebAuthn RP-ID and cannot obtain a public certificate, so a
# named private-HTTPS origin must resolve to a hostname. Port/path are stripped;
# bracketed and bare IPv6 literals are rejected (bracket support is out of scope).
_public_origin_host() {
  local url="$1" rest host
  case "$url" in https://*) rest="${url#https://}" ;; *) return 1 ;; esac
  rest="${rest%%/*}"                       # strip /path or /?query
  case "$rest" in \[*) return 1 ;; esac    # [IPv6] literal — unsupported
  host="${rest%%:*}"                       # strip :port (also empties bare IPv6)
  case "$host" in
    *[a-zA-Z]*) printf '%s' "$host" ;;     # a DNS name carries letters; IPv4 does not
    *) return 1 ;;
  esac
}

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
NI_SMTP_PORT=""
NI_SMTP_USER=""
NI_SMTP_PASS=""
NI_SMTP_FROM=""
NI_LLM_BACKEND=""     # ollama | vllm | auto (resolved at .env-write time)
NI_SMART_MODEL=""     # model id; resolved by prompt_ai_backend or auto-resolve
NI_HW_TIER=""         # populated by prompt_ai_backend or auto-resolve
NI_GPU_VENDOR="none"  # nvidia | amd | intel | none; probed before the prompts
NI_GPU_OVERRIDE=""    # --gpu cuda|rocm|vulkan|cpu — overrides overlay detection
NI_ADDRESS=""         # --address <ipv4> — overrides LAN IP auto-detection
NI_PUBLIC_ORIGIN=""   # --public-origin <https-url> — a named private HTTPS origin
INSTALL_PREREQS=0
SKIP_DISK_CHECK=0
BUILD_LOCAL=0         # --build-local: build app images from source instead of pulling GHCR
TUNNEL_ACK=0          # --tunnel-ack: acknowledges tunnel internet exposure (NI consent)
OVERWRITE_ENV=0       # --overwrite-env: rebuild an existing .env (merge, non-destructive)
DOCKER_JUST_INSTALLED=0  # set by handle_missing_prereqs; gates the exit-3 path
_ENV_SNAPSHOT_TAKEN=0 # set once .env has been copied to .env.pre-setup.bak
_STACK_STARTED=0      # set once the stack is up; gates the pre-start restore hint

# Snapshot the original invocation before the parse loop consumes it via `shift`,
# so a GPU-overlay failure can re-exec on CPU with the user's other flags intact.
ORIG_ARGS=("$@")

while [ $# -gt 0 ]; do
  # A value-taking flag passed as the final argument would read an unset $2 under
  # `set -u` and abort with a raw "unbound variable". Guard them centrally so the
  # message is actionable. (The --flag=value forms carry their value inline.)
  case "$1" in
    --domain|--admin-email|--profile|--smtp-host|--smtp-port|--smtp-user|--smtp-from|--smtp-pass-file|--mode|--backend|--smart-model|--gpu|--address|--public-origin)
      if [ "$#" -lt 2 ] || [[ "$2" == -* ]]; then
        die "$1 requires a value." "Run: $0 --help"
      fi ;;
  esac
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
    --smtp-port)
      NI_SMTP_PORT="$2"
      shift 2
      ;;
    --smtp-port=*)
      NI_SMTP_PORT="${1#*=}"
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
    --smtp-from)
      NI_SMTP_FROM="$2"
      shift 2
      ;;
    --smtp-from=*)
      NI_SMTP_FROM="${1#*=}"
      shift
      ;;
    # An unreadable pass file is fatal — a silently-empty password would install a
    # non-deliverable relay that fails only when the first magic link is sent.
    --smtp-pass-file)
      [ -r "$2" ] || die "--smtp-pass-file: cannot read '$2'." "Point it at a readable file whose first line is the SMTP password."
      NI_SMTP_PASS="$(head -n 1 "$2")"
      shift 2
      ;;
    --smtp-pass-file=*)
      _spf="${1#*=}"
      [ -r "$_spf" ] || die "--smtp-pass-file: cannot read '$_spf'." "Point it at a readable file whose first line is the SMTP password."
      NI_SMTP_PASS="$(head -n 1 "$_spf")"
      shift
      ;;
    --mode)
      NI_MODE="$2"; NI_MODE_EXPLICIT=1; shift 2 ;;
    --mode=*)
      NI_MODE="${1#*=}"; NI_MODE_EXPLICIT=1; shift ;;
    --check)
      RUN_DOCTOR=1; shift ;;
    --install-prereqs)
      INSTALL_PREREQS=1; shift ;;
    --skip-disk-check)
      SKIP_DISK_CHECK=1; shift ;;
    --build-local)
      BUILD_LOCAL=1; shift ;;
    --tunnel-ack)
      TUNNEL_ACK=1; shift ;;
    --overwrite-env)
      OVERWRITE_ENV=1; shift ;;
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
    --gpu)
      case "$2" in
        cuda|rocm|vulkan|cpu) NI_GPU_OVERRIDE="$2"; shift 2 ;;
        *) die "Invalid --gpu '$2'. Expected: cuda|rocm|vulkan|cpu" "Run: $0 --help" ;;
      esac ;;
    --gpu=*)
      _v="${1#*=}"
      case "$_v" in
        cuda|rocm|vulkan|cpu) NI_GPU_OVERRIDE="$_v"; shift ;;
        *) die "Invalid --gpu '$_v'. Expected: cuda|rocm|vulkan|cpu" "Run: $0 --help" ;;
      esac ;;
    --address)
      NI_ADDRESS="$2"
      case "$NI_ADDRESS" in *[!0-9.]*|'') die "--address must be an IPv4 address (e.g. 192.168.1.10)." "IPv6 is not supported for LAN mode." ;; esac
      shift 2 ;;
    --address=*)
      NI_ADDRESS="${1#*=}"
      case "$NI_ADDRESS" in *[!0-9.]*|'') die "--address must be an IPv4 address (e.g. 192.168.1.10)." "IPv6 is not supported for LAN mode." ;; esac
      shift ;;
    --public-origin)
      NI_PUBLIC_ORIGIN="$2"
      _public_origin_host "$NI_PUBLIC_ORIGIN" >/dev/null \
        || die "--public-origin must be an https:// URL with a DNS hostname (an IP is not a valid origin)." "Example: --public-origin https://jarvis.example.ts.net"
      shift 2 ;;
    --public-origin=*)
      NI_PUBLIC_ORIGIN="${1#*=}"
      _public_origin_host "$NI_PUBLIC_ORIGIN" >/dev/null \
        || die "--public-origin must be an https:// URL with a DNS hostname (an IP is not a valid origin)." "Example: --public-origin https://jarvis.example.ts.net"
      shift ;;
    -h|--help)
      sed -n '/^# setup.sh/,/^set -euo/{ /^#/!d; s/^# \{0,1\}//p; }' "$0" | head -80
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


missing_prereqs() {
  local missing=()
  command -v docker >/dev/null 2>&1 || missing+=(docker)
  if command -v docker >/dev/null 2>&1; then
    docker compose version >/dev/null 2>&1 || missing+=(docker-compose)
  else
    missing+=(docker-compose)
  fi
  command -v openssl >/dev/null 2>&1 || missing+=(openssl)
  # python3 is a hard install-path prerequisite, not just a dev tool: the model
  # selection and disk-sizing helpers in setup_lib.sh shell out to it, and under
  # `set -euo pipefail` its absence aborts mid-install at the first such call.
  command -v python3 >/dev/null 2>&1 || missing+=(python3)
  printf '%s\n' "${missing[@]}"
}

_host_os_id() {
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091  # /etc/os-release is standard KEY=VALUE data.
    . /etc/os-release
    printf '%s' "${ID:-unknown}"
  else
    printf 'unknown'
  fi
}

_prereq_install_plan_for_host() {
  local os os_id has_apt=0 has_brew=0 has_dnf=0
  os="$(uname -s 2>/dev/null || printf unknown)"
  os_id="$(_host_os_id)"
  command -v apt-get >/dev/null 2>&1 && has_apt=1
  command -v brew >/dev/null 2>&1 && has_brew=1
  command -v dnf >/dev/null 2>&1 && has_dnf=1
  prereq_install_plan "$os" "$os_id" "$has_apt" "$has_brew" "$has_dnf" "$@"
}

_run_prereq_plan() {
  local plan="$1" noninteractive="${2:-0}" cmd run_cmd
  while IFS= read -r cmd; do
    [ -n "$cmd" ] || continue
    run_cmd="$cmd"
    if [ "$noninteractive" -eq 1 ]; then
      case "$run_cmd" in
        sudo\ *) run_cmd="sudo -n ${run_cmd#sudo }" ;;
      esac
    fi
    info "Running: $run_cmd"
    bash -c "$run_cmd"
  done <<< "$plan"
}

handle_missing_prereqs() {
  local missing=("$@")
  local plan="" docker_in_plan=0
  case " ${missing[*]} " in
    *" docker "*) docker_in_plan=1 ;;
  esac
  printf '%sMissing prerequisites:%s %s\n' "$C_YELLOW" "$C_RESET" "${missing[*]}" >&2

  if ! plan="$(_prereq_install_plan_for_host "${missing[@]}")" || [ -z "$plan" ]; then
    die "Automatic prerequisite installation is not available on this host." \
        "$(prereq_manual_guidance "${missing[@]}")"
  fi

  printf '%sGuided prerequisite installer would run:%s\n%s\n' "$C_BOLD" "$C_RESET" "$plan" >&2

  if [ "$INSTALL_PREREQS" -eq 1 ]; then
    _run_prereq_plan "$plan" "$NON_INTERACTIVE"
    if [ "$docker_in_plan" -eq 1 ]; then DOCKER_JUST_INSTALLED=1; fi
    return
  fi

  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    die "Prerequisites are missing in non-interactive mode." \
        "Re-run with --install-prereqs after reviewing the commands above, or install them manually."
  fi

  if [ ! -t 0 ]; then
    die "Prerequisites are missing and setup cannot prompt for consent." \
        "Run interactively, pass --install-prereqs, or install them manually."
  fi

  local reply=""
  read -rp "Run these prerequisite installation commands now? [y/N]: " reply
  case "$reply" in
    [yY]|[yY][eE][sS])
      _run_prereq_plan "$plan" 0
      if [ "$docker_in_plan" -eq 1 ]; then DOCKER_JUST_INSTALLED=1; fi
      ;;
    *) die "Prerequisites are missing." "$(prereq_manual_guidance "${missing[@]}")" ;;
  esac
}

ensure_prerequisites() {
  # Snap-packaged Docker is strictly confined: bind mounts outside $HOME and
  # compose secrets break in ways that only surface mid-install. Refuse early.
  if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then
    die "Snap-packaged Docker detected ('snap list docker') — it is not supported." \
        "Remove it with 'sudo snap remove docker', then re-run ./setup.sh to install Docker Engine from Docker's official repository."
  fi

  local missing=()
  while IFS= read -r item; do
    [ -n "$item" ] && missing+=("$item")
  done < <(missing_prereqs)

  if [ "${#missing[@]}" -gt 0 ]; then
    handle_missing_prereqs "${missing[@]}"
  fi

  command -v docker >/dev/null 2>&1 \
    || die "Docker not found in PATH." "$(os_install_hint docker)"

  if ! docker compose version >/dev/null 2>&1; then
    die "Docker Compose v2 is required (the 'docker compose' plugin)." \
        "$(os_install_hint docker)"
  fi

  command -v openssl >/dev/null 2>&1 \
    || die "openssl required for secret generation." \
           "$(prereq_manual_guidance openssl)"

  command -v python3 >/dev/null 2>&1 \
    || die "python3 required for model selection and disk sizing." \
           "$(prereq_manual_guidance python3)"
}

# preflight_nvidia_toolkit — first-class GPU-runtime preflight. Runs on EVERY
# install (not only when docker/compose/openssl are missing), so a host that
# already has Docker but lacks the NVIDIA container runtime still gets the
# toolkit install plan. Non-fatal by contract: a missing GPU runtime degrades to
# CPU, so this never dies — it installs under --install-prereqs or interactive
# consent, otherwise advises and proceeds on CPU.
preflight_nvidia_toolkit() {
  _gpu_present_for_prereqs || return 0
  local plan=""
  if ! plan="$(_prereq_install_plan_for_host nvidia-toolkit)" || [ -z "$plan" ]; then
    warn "NVIDIA GPU detected but the Docker NVIDIA runtime is missing — the stack will run on CPU. $(prereq_manual_guidance nvidia-toolkit)"
    return 0
  fi
  printf '%sNVIDIA GPU detected without the Docker NVIDIA runtime.%s The stack runs on CPU until the NVIDIA Container Toolkit is installed:\n%s\n' \
    "$C_YELLOW" "$C_RESET" "$plan" >&2
  if [ "$INSTALL_PREREQS" -eq 1 ]; then
    _run_prereq_plan "$plan" "$NON_INTERACTIVE"
    return 0
  fi
  if [ "$NON_INTERACTIVE" -eq 1 ] || [ ! -t 0 ]; then
    warn "Proceeding on CPU. Re-run with --install-prereqs to install the NVIDIA Container Toolkit for GPU acceleration."
    return 0
  fi
  local reply=""
  read -rp "Install the NVIDIA Container Toolkit now for GPU acceleration? [y/N]: " reply
  case "$reply" in
    [yY]|[yY][eE][sS]) _run_prereq_plan "$plan" 0 ;;
    *) warn "Proceeding on CPU. Install the toolkit later to enable GPU acceleration." ;;
  esac
}

# -----------------------------------------------------------------------------
# 2. Prerequisites
# -----------------------------------------------------------------------------
info "Checking prerequisites..."

ensure_prerequisites

# docker compose v2+ (space form). `docker-compose` (hyphen) is v1 and
# unsupported. ensure_prerequisites proved the plugin runs; pin a REAL floor
# rather than accepting any v2: the accelerator overlays merge a dev override's
# `deploy: !reset null`, and the `!reset`/`!override` merge tags require Docker
# Compose 2.24.4+ (Docker's compose-file merge reference). An older plugin
# silently ignores the tags and mis-merges the overlay.
COMPOSE_MIN=2.24.4
COMPOSE_VER="$(docker compose version --short 2>/dev/null || echo 'unknown')"
compose_meets_floor "$COMPOSE_VER" "$COMPOSE_MIN" && _cmf=0 || _cmf=$?
case "$_cmf" in
  0) ok "Docker Compose v${COMPOSE_VER#v}" ;;
  2) warn "Could not read the Docker Compose version. Proceeding." ;;
  *) warn "Docker Compose v${COMPOSE_VER#v} is older than the required v${COMPOSE_MIN}; the accelerator overlays rely on the '!reset' merge tag added in Compose 2.24.4. Proceeding, but please upgrade the 'docker compose' plugin." ;;
esac

# Fatal daemon probe — must run before the idempotency gate and every prompt,
# so a dead daemon can never strand a half-answered wizard or persist a stale
# COMPOSE_FILE. `docker info` (not a socket stat) honours DOCKER_HOST/rootless.
# Right after a guided docker install the daemon runs but this shell predates
# the docker-group grant: exit 3 (see the usage header) tells callers
# "re-login and re-run", distinctly from failure and never as false success.
if ! _docker_info_err="$(docker info 2>&1 >/dev/null)"; then
  if [ "$DOCKER_JUST_INSTALLED" -eq 1 ] \
     && printf '%s' "$_docker_info_err" | grep -qi 'permission denied'; then
    err "docker installed — log out and back in (or run 'newgrp docker'), then re-run ./setup.sh"
    exit 3
  fi
  die "Docker daemon is not reachable ('docker info' failed)." \
      "Start Docker (Docker Desktop on macOS; 'sudo systemctl start docker' on Linux), check DOCKER_HOST/permissions, then re-run ./setup.sh"
fi

# GPU is informational — not fatal. detect_gpu_vendor is WSL2-aware for NVIDIA
# (nvidia-smi off PATH at /usr/lib/wsl/lib/nvidia-smi) and probes amd-smi /
# /dev/dri for AMD/Intel. The vendor and VRAM captured here feed the .env
# handoff (JARVIS_GPU_VENDOR + JARVIS_HOST_VRAM_MB) and overlay selection.
NI_HOST_VRAM_MB=""
NI_GPU_VENDOR="$(detect_gpu_vendor)"
case "$NI_GPU_VENDOR" in
  nvidia)
    GPU_LINE="$("$(resolve_nvidia_smi)" -L 2>/dev/null | head -n 1 || true)"
    ok "GPU detected: ${GPU_LINE:-NVIDIA}"
    ;;
  amd)
    ok "GPU detected: AMD (via amd-smi)"
    ;;
  intel)
    info "Intel GPU detected — no dedicated VRAM; installing on CPU. Vulkan acceleration is experimental and opt-in: re-run ./setup.sh --gpu vulkan."
    ;;
  *)
    info "No GPU detected — Ollama will run on CPU (slower)."
    ;;
esac
if [ "$NI_GPU_VENDOR" = "nvidia" ] || [ "$NI_GPU_VENDOR" = "amd" ]; then
  if _vram_mb="$(resolve_gpu_vram_mb "$NI_GPU_VENDOR")"; then
    NI_HOST_VRAM_MB="$_vram_mb"
  else
    warn "${NI_GPU_VENDOR} GPU detected but VRAM could not be measured — model defaults stay conservative (CPU tier)."
  fi
fi

# NVIDIA GPU host without the Docker runtime -> offer the toolkit as a
# first-class preflight (non-fatal; the stack runs on CPU otherwise).
preflight_nvidia_toolkit

# existing_env_value KEY — print the current value of KEY from .env, or nothing
# if .env is absent or KEY is absent/empty. A present-but-empty `KEY=` counts
# as absent (the `=.\+` requires at least one char after `=`). The value is
# emitted verbatim after the first `=`, so `=`, `/`, `+`, and base64 padding
# survive intact; a trailing CR from a Windows-edited (CRLF) .env is stripped so
# the value round-trips byte-clean.
existing_env_value() {
  [ -f .env ] || return 1
  grep -qE "^$1=.+" .env 2>/dev/null || return 1
  grep "^$1=" .env | head -n 1 | cut -d'=' -f2- | tr -d '\r'
}

# _port_or_default KEY DEFAULT — the host port for KEY, preferring a value the
# existing .env already persists over the shell-env default, so a re-run probes
# the ports this deployment ACTUALLY binds (a custom-port .env would otherwise be
# checked at defaults it does not use).
_port_or_default() {
  # Exported environment wins over .env, mirroring compose interpolation.
  local v="${!1-}"
  [ -n "$v" ] && { printf '%s' "$v"; return 0; }
  v="$(existing_env_value "$1")" && [ -n "$v" ] && { printf '%s' "$v"; return 0; }
  printf '%s' "$2"
}

# Port pre-check — warn only. Probes every host port this run will bind: the
# always-on services (from .env when present) PLUS the ports the run's active TLS
# edge / optional profile publishes (registry_profile_host_ports). NI_PROFILE is
# known here (parsed from --profile); the wizard's later opt-in profiles
# (telegram/tunnel/observability) are not yet chosen, so only the profile the
# invocation already selected contributes its extra ports.
JARVIS_PORTS=(
  "$(_port_or_default DASHBOARD_HOST_PORT 3001)"
  "$(_port_or_default LITELLM_HOST_PORT 4000)"
  "$(_port_or_default POSTGRES_HOST_PORT 5432)"
  "$(_port_or_default QDRANT_HOST_PORT 6333)"
  "$(_port_or_default PAPER_INGESTION_HOST_PORT 8010)"
  "$(_port_or_default LEARNING_ENGINE_HOST_PORT 8011)"
  "$(_port_or_default OLLAMA_HOST_PORT 11434)"
)
_PRECHECK_PROFILES=()
case "$NI_PROFILE" in
  local-https) _PRECHECK_PROFILES+=(caddy-local) ;;
  letsencrypt) _PRECHECK_PROFILES+=(letsencrypt) ;;
esac
for _pp in $(registry_profile_host_ports ${_PRECHECK_PROFILES[@]+"${_PRECHECK_PROFILES[@]}"}); do
  JARVIS_PORTS+=("$_pp")
done
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
# Declining the overwrite keeps .env AND starts the stack with it — a re-run
# must never dead-end with services down. COMPOSE_FILE and COMPOSE_PROFILES
# persisted in .env are honoured natively by docker compose.
# -----------------------------------------------------------------------------
if [ -f .env ]; then
  printf '\n%sConfiguration already exists (.env).%s\n' "$C_YELLOW" "$C_RESET"
  # An overwrite now MERGES: existing values are carried forward, so a re-run to
  # change one setting never rotates secrets or drops operator-added keys. The
  # default is still to keep .env untouched and start the stack; rebuilding is
  # opt-in (interactive "y" or --overwrite-env).
  _do_overwrite=0
  if [ "$OVERWRITE_ENV" -eq 1 ]; then
    _do_overwrite=1
    info "Rebuilding .env (--overwrite-env) — existing values are carried forward."
  elif [ "$NON_INTERACTIVE" -eq 1 ]; then
    info "Existing .env kept — pass --overwrite-env to rebuild."
  else
    read -rp "Overwrite? (y/N): " reply
    case "$reply" in
      [yY]|[yY][eE][sS]) _do_overwrite=1; info "Rebuilding .env — existing values are carried forward." ;;
    esac
  fi
  if [ "$_do_overwrite" -eq 0 ]; then
    ok "Keeping existing .env — starting the stack with it."
    KEEP_PROFILE_ARGS=()
    _keep_telegram=0
    if ! existing_env_value COMPOSE_PROFILES >/dev/null; then
      # Pre-v0.8 .env files never persisted the profile selection.
      if existing_env_value TELEGRAM_BOT_TOKEN >/dev/null; then
        KEEP_PROFILE_ARGS+=(--profile telegram)
        _keep_telegram=1
        info "No COMPOSE_PROFILES in .env — enabling the telegram profile (TELEGRAM_BOT_TOKEN is set)."
      fi
    else
      # COMPOSE_PROFILES is honoured natively by compose; we only need to know
      # whether telegram is among them so its image is materialised too.
      case ",$(existing_env_value COMPOSE_PROFILES)," in
        *,telegram,*) _keep_telegram=1 ;;
      esac
    fi
    # Source versions.env so the postgres SHA-digest pin and image tags
    # are available on the keep-path the same as on the normal install path.
    if [ -f versions.env ]; then
      # shellcheck disable=SC1091  # versions.env is runtime-provided KEY=VALUE data, not a script
      set -a && . ./versions.env && set +a
    fi
    # An .env written before 1.1 carries no TORCH_VARIANT, so the image tag
    # would resolve to the CPU flavour even on a CUDA host whose GPU overlay is
    # still recorded in COMPOSE_FILE. Backfill before anything resolves an image.
    if _keep_variant="$(backfill_torch_variant_from_env)" && [ -n "$_keep_variant" ]; then
      info "Recorded this host's torch image variant in .env: ${_keep_variant}"
    fi
    # This path bypasses the install flow below, so it needs the same
    # materialise-then-guard sequence. Without it a `up -d` here would find the
    # published images missing and SILENTLY BUILD them — `pull_policy: missing`
    # plus a `build:` block — which is the multi-GB torch build (and the ENOSPC)
    # that installing from prebuilt images exists to eliminate.
    # Only tunnel/telegram are ever persisted to COMPOSE_PROFILES, so the
    # observability profile (langfuse, the one unpublished image) cannot be
    # active here and needs no local build.
    # Materialise the file-backed Docker secrets before `up`: init-secrets.sh
    # does not create the Langfuse init keypair, so a keep-path re-run whose
    # secrets/ is missing it would otherwise dead-end at `docker compose up`
    # with "secret ... not found". Both generators are idempotent (no churn
    # for a healthy deployment).
    [ -x scripts/init-secrets.sh ] && bash scripts/init-secrets.sh
    [ -x scripts/gen-langfuse-keys.sh ] && bash scripts/gen-langfuse-keys.sh >/dev/null
    KEEP_SERVICES=("${PUBLISHED_SERVICES_BASE[@]}")
    [ "$_keep_telegram" -eq 1 ] && KEEP_SERVICES+=("$PUBLISHED_SERVICE_TELEGRAM")
    KEEP_UP_ARGS=(up -d)
    if [ "$BUILD_LOCAL" -eq 1 ]; then
      info "Building application images from source (--build-local): ${KEEP_SERVICES[*]}"
      compose_or_die "docker compose build failed." \
          "Inspect the build output above, then re-run ./setup.sh" \
          ${KEEP_PROFILE_ARGS[@]+"${KEEP_PROFILE_ARGS[@]}"} build "${KEEP_SERVICES[@]}"
    else
      info "Pulling prebuilt images: ${KEEP_SERVICES[*]}"
      compose_or_die "Image pull failed." \
          "Check network access to ghcr.io, then re-run ./setup.sh — or build from source instead: ./setup.sh --build-local" \
          ${KEEP_PROFILE_ARGS[@]+"${KEEP_PROFILE_ARGS[@]}"} pull "${KEEP_SERVICES[@]}"
      KEEP_UP_ARGS+=(--no-build)
    fi
    info "Starting services with: docker compose ${KEEP_PROFILE_ARGS[*]:-} ${KEEP_UP_ARGS[*]}"
    compose_or_die "docker compose up failed." \
        "Inspect logs: docker compose logs --tail=200" \
        ${KEEP_PROFILE_ARGS[@]+"${KEEP_PROFILE_ARGS[@]}"} "${KEEP_UP_ARGS[@]}"
    ok "Stack started with the existing configuration. To rebuild .env, re-run and choose overwrite (or pass --overwrite-env)."
    exit 0
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
# read .env BEFORE section 7 rebuilds it via tempfile + mv — on a reconfigure
# section 7 merges these values back in place (every other existing key is
# carried forward untouched).
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
  printf '\n%sHow will you access JARVIS?%s\n' "$C_BOLD" "$C_RESET"
  cat <<'EOF'
  1) On this computer only (recommended to start)
     Everything works here: sign-in links, passkeys (fingerprint/face/PIN).
  2) From devices on your home or lab network
     Sign-in links can be received on any device; a durable sign-in needs
     a named HTTPS origin (add option 3 or 4, or --public-origin). Passkeys
     work on this computer (add option 3 or 4 later for passkeys
     everywhere). Your browser will
     show a one-time certificate warning per device — expected for a
     private setup.
  3) From anywhere — Cloudflare Tunnel (free, no router changes)
     Full features everywhere incl. passkeys. Needs a free Cloudflare
     account and a tunnel token (guided).
  4) From anywhere — your own domain with Let's Encrypt
     Full features everywhere incl. passkeys. Needs a domain pointing at
     this machine and port 443 reachable.
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

# vLLM honesty: setup never auto-starts vLLM — the start path below always brings
# up Ollama. JARVIS_LLM_BACKEND=vllm is kept so the app's served-by badge stays
# truthful, but the non-interactive `--backend vllm` path must warn as loudly as
# the interactive wizard does that vLLM is a manual overlay.
if [ "${NI_LLM_BACKEND:-}" = "vllm" ] && [ "$NON_INTERACTIVE" -eq 1 ]; then
  warn "vLLM is a manual overlay — setup does NOT start it. Ollama is serving your models now."
  printf '  Run vLLM later: docker compose -f docker-compose.yml -f docker-compose.vllm.yml --profile vllm up -d\n'
  printf '  then point the LiteLLM `smart` alias at vLLM in Settings.\n'
fi

# The bootstrap that actually pulls models is Ollama on every setup.sh path, so a
# HuggingFace repo id (contains '/') is not a valid tag and would hard-fail
# ollama-bootstrap. Reject it early with the fix.
case "${NI_SMART_MODEL:-}" in
  */*) die "--smart-model '${NI_SMART_MODEL}' looks like a HuggingFace id, but setup starts Ollama." \
           "HuggingFace ids need the vLLM overlay; Ollama models look like qwen3:14b." ;;
esac

# Disk preflight — sized to the smart model chosen above, so it must run after
# the backend/model resolution and before anything pulls or builds.
preflight_disk

CLOUDFLARE_TUNNEL_TOKEN=""
USE_TUNNEL_PROFILE=0
ACCESS_MODE_LABEL="localhost"
APP_BASE_URL_VALUE=""   # canonical public origin; derived per mode below, written to .env
CORS_ORIGINS_OVERRIDE=""
CF_TRUST_OVERRIDE=""
LAN_IP=""
TUNNEL_HOSTNAME=""
DASHBOARD_BIND_HOST="127.0.0.1"
DASHBOARD_SERVER_NAME_VALUE=""   # accumulating nginx Host allowlist (lan + public-origin + tunnel)

# detect_lan_ip -> the private LAN IPv4 other devices reach this host by, or empty.
# Prefers the source address of the default route (the primary egress NIC) over
# hostname -I token order, which can lead with a docker bridge or VPN address;
# both paths skip docker/CGNAT/link-local ranges via _is_lan_ipv4.
detect_lan_ip() {
  local ip="" tok
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)"
    if [ -n "$ip" ] && _is_lan_ipv4 "$ip"; then printf '%s' "$ip"; return 0; fi
  fi
  if command -v hostname >/dev/null 2>&1; then
    for tok in $(hostname -I 2>/dev/null || true); do
      if _is_lan_ipv4 "$tok"; then printf '%s' "$tok"; return 0; fi
    done
  fi
  if command -v ipconfig >/dev/null 2>&1; then
    ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
    [ -n "$ip" ] && { printf '%s' "$ip"; return 0; }
  fi
  printf ''
}

case "$access_mode" in
  1)
    ACCESS_MODE_LABEL="localhost"
    DASHBOARD_BIND_HOST="127.0.0.1"
    # Binding is governed by DASHBOARD_BIND_HOST in .env; the user-owned
    # docker-compose.override.yml is never moved or overwritten by setup.
    ;;
  2)
    ACCESS_MODE_LABEL="lan"
    DASHBOARD_BIND_HOST="0.0.0.0"
    warn "LAN mode binds the dashboard to 0.0.0.0 over plain HTTP — reachable by every host on your network. Only use this on a trusted LAN; for untrusted networks prefer the Tailscale/tunnel option, or add --public-origin for a named HTTPS route."
    # Port binding is governed by DASHBOARD_BIND_HOST in .env; the user-owned
    # docker-compose.override.yml is never moved or overwritten by setup.
    LAN_IP="${NI_ADDRESS:-$(detect_lan_ip)}"
    if [ -n "$LAN_IP" ]; then
      _lan_port="${DASHBOARD_HOST_PORT:-3001}"
      CORS_ORIGINS_OVERRIDE="http://localhost:${_lan_port},http://${LAN_IP}:${_lan_port}"
      DASHBOARD_SERVER_NAME_VALUE="$(_append_server_name "$DASHBOARD_SERVER_NAME_VALUE" "$LAN_IP")"
      ok "Detected LAN IP: ${LAN_IP} (added to CORS_ORIGINS and the dashboard Host allowlist)."
    elif hostname -I 2>/dev/null | grep -q ':'; then
      die "This host advertises only IPv6 addresses; LAN mode needs an IPv4 address." \
          "Assign an IPv4 LAN address, pass --address <ipv4>, or use the Tailscale/tunnel option."
    else
      warn "Could not auto-detect a private LAN IPv4 — pass --address <ipv4>, or set DASHBOARD_SERVER_NAME and CORS_ORIGINS in .env for your machine's IP."
    fi
    ;;
  3)
    ACCESS_MODE_LABEL="tunnel"
    DASHBOARD_BIND_HOST="127.0.0.1"
    # Zero-Trust consent — a tunnel exposes this instance to the internet, so
    # require an explicit acknowledgement in-flow (no hand-edited .env values):
    # a typed confirmation interactively, or --tunnel-ack non-interactively.
    printf '\n'
    warn "A Cloudflare tunnel exposes your services to the internet. You MUST configure Zero-Trust access policies at https://one.dash.cloudflare.com/ first."
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
      [ "$TUNNEL_ACK" -eq 1 ] || die "Tunnel access requires acknowledging the internet exposure." \
        "Configure Zero-Trust access policies, then re-run with --tunnel-ack."
    else
      read -rp 'Type "I understand" to continue (anything else aborts): ' _tunnel_ack
      [ "$_tunnel_ack" = "I understand" ] || die "Tunnel setup aborted — acknowledgement not given." \
        "Configure Zero-Trust access policies, then re-run ./setup.sh and choose the tunnel option."
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
    DASHBOARD_SERVER_NAME_VALUE="$(_append_server_name "$DASHBOARD_SERVER_NAME_VALUE" "$TUNNEL_HOSTNAME")"
    CF_TRUST_OVERRIDE=true
    ok "Tunnel hostname: ${TUNNEL_HOSTNAME} (added to CORS_ORIGINS and the dashboard Host allowlist)."
    ok "JARVIS_TRUST_CF_CONNECTING_IP=true — rate limiting will key off the real CF-Connecting-IP header rather than the tunnel origin."
    ;;
  4)
    # Interactive Let's Encrypt: drive the SAME machinery as --profile=letsencrypt
    # (no parallel cert/Caddy path). Setting NI_PROFILE/NI_DOMAIN/NI_ADMIN_EMAIL makes
    # the existing LETSENCRYPT_*, ENVIRONMENT, and summary logic fire from these prompts.
    ACCESS_MODE_LABEL="letsencrypt"
    DASHBOARD_BIND_HOST="127.0.0.1"   # dashboard stays local; Caddy terminates public TLS
    NI_PROFILE="letsencrypt"
    printf '\n'
    info "Let's Encrypt issues a real TLS certificate for a public domain that resolves to this machine (port 443 must be reachable)."
    while true; do
      read -r -p "Public domain (e.g. jarvis.example.com): " NI_DOMAIN
      if printf '%s' "$NI_DOMAIN" | grep -qE '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$'; then
        break
      fi
      echo "Invalid domain. Use lowercase letters, digits, hyphens, and dots only."
    done
    while true; do
      read -r -p "Admin email (Let's Encrypt expiry notices): " NI_ADMIN_EMAIL
      if printf '%s' "$NI_ADMIN_EMAIL" | grep -qE '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'; then
        break
      fi
      echo "Invalid email address. Enter a valid address, e.g. you@example.com."
    done
    ok "Let's Encrypt configured for ${NI_DOMAIN}. Setup will start the public TLS edge and wait for the certificate before finishing."
    ;;
  *)
    die "Invalid choice '$access_mode'. Expected 1, 2, 3, or 4." \
        "Re-run ./setup.sh and pick a listed option."
    ;;
esac

# Named private HTTPS origin — a supported family route layered on top of the
# localhost/LAN admin bootstrap (SSH-forward). Offer it interactively only where
# the chosen mode has not already established a public HTTPS origin.
if [ "$NON_INTERACTIVE" -eq 0 ] && [ -z "$NI_PUBLIC_ORIGIN" ] \
   && { [ "$ACCESS_MODE_LABEL" = "localhost" ] || [ "$ACCESS_MODE_LABEL" = "lan" ]; }; then
  printf '\n'
  read -rp "Do you already have a private HTTPS hostname for this host (Tailscale Serve / trusted-TLS)? [y/N] " _po_yn
  case "${_po_yn:-N}" in
    y|Y)
      while true; do
        read -rp "Private HTTPS URL (e.g. https://jarvis.example.ts.net), or blank to skip: " _po_url
        [ -z "$_po_url" ] && break
        if _public_origin_host "$_po_url" >/dev/null; then NI_PUBLIC_ORIGIN="$_po_url"; break; fi
        echo "Enter an https:// URL with a DNS hostname (an IP is not a valid origin)."
      done
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# Canonical access-mode identity, written to .env below. JARVIS_ACCESS_MODE
# names the mode; APP_BASE_URL is the single server-known public origin the
# passkey ceremony validates against (empty for localhost/LAN, where the origin
# is implicit — a raw LAN IP is not a valid WebAuthn origin). Both are derived
# here and never hand-edited. NI --profile=letsencrypt lands on access_mode 1
# above, so re-label it here from the profile.
# ---------------------------------------------------------------------------
if [ "$NI_PROFILE" = "letsencrypt" ]; then
  ACCESS_MODE_LABEL="letsencrypt"
fi
case "$ACCESS_MODE_LABEL" in
  tunnel)      APP_BASE_URL_VALUE="https://${TUNNEL_HOSTNAME}" ;;
  letsencrypt) APP_BASE_URL_VALUE="https://${NI_DOMAIN}" ;;
  *)           APP_BASE_URL_VALUE="" ;;
esac

# A named private HTTPS origin layers onto any mode: it fills APP_BASE_URL when
# the mode left it empty (localhost/LAN), and always joins the CORS list and the
# nginx Host allowlist. The allowlist accumulates, so LAN + origin keep BOTH
# hostnames. PUBLIC_ORIGIN_HOST is the validated hostname the edge probe targets.
PUBLIC_ORIGIN_HOST=""
if [ -n "$NI_PUBLIC_ORIGIN" ]; then
  PUBLIC_ORIGIN_HOST="$(_public_origin_host "$NI_PUBLIC_ORIGIN")" \
    || die "--public-origin must be an https:// URL with a DNS hostname (an IP is not a valid origin)." "Example: https://jarvis.example.ts.net"
  [ -z "$APP_BASE_URL_VALUE" ] && APP_BASE_URL_VALUE="$NI_PUBLIC_ORIGIN"
  DASHBOARD_SERVER_NAME_VALUE="$(_append_server_name "$DASHBOARD_SERVER_NAME_VALUE" "$PUBLIC_ORIGIN_HOST")"
  [ -n "$CORS_ORIGINS_OVERRIDE" ] || CORS_ORIGINS_OVERRIDE="http://localhost:${DASHBOARD_HOST_PORT:-3001}"
  CORS_ORIGINS_OVERRIDE="$(_append_csv "$CORS_ORIGINS_OVERRIDE" "$NI_PUBLIC_ORIGIN")"
  ok "Private HTTPS origin: ${NI_PUBLIC_ORIGIN} (APP_BASE_URL, CORS_ORIGINS, and the dashboard Host allowlist updated)."
fi

# local-https serves through caddy_local at https://localhost:3443 (Host rewritten
# to localhost for nginx), so the browser origin is that HTTPS terminator — add it
# to CORS alongside the direct loopback port.
if [ "$NI_PROFILE" = "local-https" ]; then
  [ -n "$CORS_ORIGINS_OVERRIDE" ] || CORS_ORIGINS_OVERRIDE="http://localhost:${DASHBOARD_HOST_PORT:-3001}"
  CORS_ORIGINS_OVERRIDE="$(_append_csv "$CORS_ORIGINS_OVERRIDE" "https://localhost:3443")"
fi
if [ "$NON_INTERACTIVE" -eq 0 ]; then
  case "$ACCESS_MODE_LABEL" in
    localhost)   info "Access mode: on this computer only — sign-in links and passkeys all work here." ;;
    lan)         info "Access mode: home/lab network — view from any device; a durable sign-in needs a named HTTPS origin (--public-origin); passkeys on this computer." ;;
    tunnel)      info "Access mode: anywhere via Cloudflare Tunnel — full features including passkeys." ;;
    letsencrypt) info "Access mode: anywhere via your own domain — full features including passkeys." ;;
  esac
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
    printf '%s' "$TELEGRAM_BOT_TOKEN" > secrets/telegram_bot_token.txt && chmod 644 secrets/telegram_bot_token.txt
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
      printf '%s' "$TELEGRAM_BOT_TOKEN" > secrets/telegram_bot_token.txt && chmod 644 secrets/telegram_bot_token.txt
    else
      warn "That didn't look like a valid Telegram token (format: <digits>:<20+ chars>). Try again or press Enter to skip."
      tg_try2="$(prompt_telegram)"
      if [ -n "${tg_try2// }" ] && [[ "$tg_try2" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
        TELEGRAM_BOT_TOKEN="$tg_try2"
        USE_TELEGRAM_PROFILE=1
        ok "Telegram token accepted."
        printf '%s' "$TELEGRAM_BOT_TOKEN" > secrets/telegram_bot_token.txt && chmod 644 secrets/telegram_bot_token.txt
      else
        warn "Skipping Telegram — bot will not start. Add TELEGRAM_BOT_TOKEN to .env later to enable."
      fi
    fi
  else
    info "Skipping Telegram bot."
  fi
fi

# The optional service groups this run engages, resolved from the wizard/flags.
# PROFILE_REGISTRY (setup_lib.sh) is the single source of truth for each group's
# compose profile flag, whether it persists to COMPOSE_PROFILES, and which extra
# services join the health gate — no hand-maintained profile list lives here.
# The TLS edges run as compose profiles so a bare `up` (and the readiness gates
# below) start the same edge the wizard selected: local-https -> caddy_local on
# https://localhost:3443, letsencrypt -> the ACME caddy on :443.
ACTIVE_PROFILES=()
[ "$USE_TUNNEL_PROFILE" -eq 1 ]                && ACTIVE_PROFILES+=(tunnel)
[ "$USE_TELEGRAM_PROFILE" -eq 1 ]              && ACTIVE_PROFILES+=(telegram)
[ "$NI_PROFILE" = "local-https" ]              && ACTIVE_PROFILES+=(caddy-local)
[ "$NI_PROFILE" = "letsencrypt" ]              && ACTIVE_PROFILES+=(letsencrypt)
[ "${USE_OBSERVABILITY_PROFILE:-0}" -eq 1 ]    && ACTIVE_PROFILES+=(observability)

# Persist only the groups the registry marks persist=yes, so a bare
# `docker compose up -d` (and the keep-and-start re-run path) re-engages the same
# set. observability is opt-in per run and intentionally not persisted.
COMPOSE_PROFILES_VALUE=""
_PERSIST_PROFILES="$(registry_profiles_to_persist)"
for _ap in ${ACTIVE_PROFILES[@]+"${ACTIVE_PROFILES[@]}"}; do
  if _env_key_in_list "$_ap" "$_PERSIST_PROFILES"; then
    COMPOSE_PROFILES_VALUE="${COMPOSE_PROFILES_VALUE:+${COMPOSE_PROFILES_VALUE},}${_ap}"
  fi
done

# A caddy profile pins static container IPs inside JARVIS_NET_SUBNET (and nginx
# trusts exactly those IPs), so a non-default subnet would attach-fail or silently
# regress rate limiting. Refuse with the exact two literals to change rather than
# start a broken edge.
case ",$COMPOSE_PROFILES_VALUE," in
  *,caddy-local,*|*,letsencrypt,*)
    if [ -n "${JARVIS_NET_SUBNET:-}" ] && [ "$JARVIS_NET_SUBNET" != "10.137.241.0/24" ]; then
      die "A caddy TLS profile cannot start under a custom JARVIS_NET_SUBNET (${JARVIS_NET_SUBNET}) without updating two hard-coded literals." \
          "Edit docker-compose.yml (caddy/caddy_local 'ipv4_address') and frontend/nginx.conf ('set_real_ip_from') to IPs inside ${JARVIS_NET_SUBNET}, or keep the default 10.137.241.0/24."
    fi
    ;;
esac
# -----------------------------------------------------------------------------
# 7. Write .env (tempfile + mv, macOS-safe)
# -----------------------------------------------------------------------------
info "Writing .env from .env.example..."

TMP_ENV="$(mktemp "${TMPDIR:-/tmp}/jarvis-env.XXXXXX")"
# Clean up the tempfile on any exit path; and if we rebuilt .env but never
# reached a running stack, point the operator at the one-command restore of the
# configuration snapshot taken just below.
cleanup_tmp() {
  [ -f "$TMP_ENV" ] && rm -f "$TMP_ENV" || true
  if [ "$_ENV_SNAPSHOT_TAKEN" -eq 1 ] && [ "$_STACK_STARTED" -eq 0 ] && [ -f .env.pre-setup.bak ]; then
    warn "Setup did not reach a running stack. Restore your previous configuration with:"
    warn "  cp .env.pre-setup.bak .env"
  fi
}
trap cleanup_tmp EXIT

# Snapshot the existing .env before rebuilding it (single rolling backup) so a
# failure before the stack is up is recoverable with the one-liner above. Fresh
# installs have nothing to snapshot.
if [ -f .env ]; then
  cp .env .env.pre-setup.bak && chmod 600 .env.pre-setup.bak
  _ENV_SNAPSHOT_TAKEN=1
fi

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
    DASHBOARD_SERVER_NAME)
      [ -n "$DASHBOARD_SERVER_NAME_VALUE" ] && printf '%s' "$DASHBOARD_SERVER_NAME_VALUE" || return 1 ;;
    APP_BASE_URL)             printf '%s' "$APP_BASE_URL_VALUE" ;;
    JARVIS_ACCESS_MODE)       printf '%s' "$ACCESS_MODE_LABEL" ;;
    JARVIS_TRUST_CF_CONNECTING_IP) [ -n "$CF_TRUST_OVERRIDE" ] && printf '%s' "$CF_TRUST_OVERRIDE" || return 1 ;;
    CORS_ORIGINS)
      if [ -n "$CORS_ORIGINS_OVERRIDE" ]; then
        printf '%s' "$CORS_ORIGINS_OVERRIDE"
      else
        return 1
      fi
      ;;
    # Non-interactive: SMTP relay flags. SMTP_PASS is deliberately NOT here — the
    # password rides the smtp_pass Docker secret (written from --smtp-pass-file
    # below), never a plaintext .env line visible in `docker inspect`.
    SMTP_HOST)
      [ -n "$NI_SMTP_HOST" ] && printf '%s' "$NI_SMTP_HOST" || return 1 ;;
    SMTP_PORT)
      [ -n "$NI_SMTP_PORT" ] && printf '%s' "$NI_SMTP_PORT" || return 1 ;;
    SMTP_USER)
      [ -n "$NI_SMTP_USER" ] && printf '%s' "$NI_SMTP_USER" || return 1 ;;
    SMTP_FROM)
      [ -n "$NI_SMTP_FROM" ] && printf '%s' "$NI_SMTP_FROM" || return 1 ;;
    # Non-interactive: Let's Encrypt / Caddy profile
    LETSENCRYPT_DOMAIN)
      [ -n "$NI_DOMAIN" ] && [ "$NI_PROFILE" = "letsencrypt" ] && printf '%s' "$NI_DOMAIN" || return 1 ;;
    LETSENCRYPT_EMAIL)
      [ -n "$NI_ADMIN_EMAIL" ] && [ "$NI_PROFILE" = "letsencrypt" ] && printf '%s' "$NI_ADMIN_EMAIL" || return 1 ;;
    # ENVIRONMENT from --profile (also the interactive Let's Encrypt mode, which
    # sets NI_PROFILE=letsencrypt): a public deployment must run in production.
    ENVIRONMENT)
      if [ "$NON_INTERACTIVE" -eq 1 ] || [ "$NI_PROFILE" = "letsencrypt" ]; then
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
    JARVIS_GPU_VENDOR)
      printf '%s' "${NI_GPU_VENDOR:-none}" ;;
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

# Keys retired in this release are dropped from a carried-forward .env unless
# this run still owns them (see merge_env_file). JARVIS_CERT_SAN's writer has been
# removed (the dashboard nginx serves no TLS — cert-SAN was inert theater), so a
# carried-forward .env drops it on the next reconfigure.
RETIRED_ENV_KEYS="JARVIS_CERT_SAN"

if [ -f .env ]; then
  # Reconfigure (--overwrite-env / interactive "y"): rebuild WITHOUT discarding
  # operator state. The keys this run "owns" are the sub_value arms that a
  # genuinely-supplied flag/prompt filled with a non-empty value; every other
  # existing key — secrets, operator-added keys, SMTP settings — is carried
  # forward byte-for-byte by merge_env_file, so no secret rotates and no custom
  # key is lost. Empty owned values are skipped so an unset prompt (e.g. no
  # Telegram token this run) never clobbers a previously-saved value.
  UPSERTS="$(mktemp "${TMPDIR:-/tmp}/jarvis-upserts.XXXXXX")"
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      key="${BASH_REMATCH[1]}"
      if _val="$(sub_value "$key")" && [ -n "$_val" ]; then
        printf '%s=%s\n' "$key" "$_val" >> "$UPSERTS"
      fi
    fi
  done < .env.example
  merge_env_file .env .env.example "$UPSERTS" "$RETIRED_ENV_KEYS" > "$TMP_ENV"
  rm -f "$UPSERTS"
else
  # Fresh install: write straight from the template.
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
fi

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
ok "Docker secret files ready in secrets/ (files 644, directory 700)."

# The SMTP password is an operator credential (never auto-generated). init-secrets
# left an empty placeholder; overwrite it with the supplied password so the app
# reads it via SMTP_PASS_FILE. It stays out of .env entirely.
if [ -n "${NI_SMTP_PASS:-}" ]; then
  printf '%s' "$NI_SMTP_PASS" > secrets/smtp_pass.txt && chmod 644 secrets/smtp_pass.txt
  ok "SMTP password written to secrets/smtp_pass.txt (Docker secret)."
fi

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
ok "Langfuse init keypair ready in secrets/ (files 644, directory 700)."

# Enforce the secrets contract (rationale at SECRET_FILE_MODE in scripts/init-secrets.sh)
if [ -d secrets ]; then
  chmod 700 secrets
  find secrets -maxdepth 1 -type f -name "*.txt" -exec chmod 644 {} \;
  ok "secrets/ hardened: directory 700 (owner-only), files 644 (readable by service containers via bind mount)."
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
# Every active group becomes a `--profile <flag>` for the install `up`, driven by
# ACTIVE_PROFILES (built above from the registry) — not a second hand-kept list.
# A couple of groups carry preconditions that must fail loudly before start.
PROFILE_ARGS=()
for _ap in ${ACTIVE_PROFILES[@]+"${ACTIVE_PROFILES[@]}"}; do
  case "$_ap" in
    caddy-local)
      # caddy_local hard-requires mkcert certs; fail loudly rather than start an
      # edge that cannot serve TLS.
      if [ ! -f certs/cert.pem ] || [ ! -f certs/key.pem ]; then
        die "local-https needs locally-trusted certs, but certs/cert.pem and certs/key.pem are missing." \
            "Generate them first: make certs   (installs the mkcert root CA), then re-run ./setup.sh --profile=local-https"
      fi
      ;;
    observability)
      # Guard the footgun of USE_OBSERVABILITY_PROFILE=1 without first generating
      # the Langfuse secrets via init-secrets.sh.
      require_langfuse_secrets
      ;;
  esac
  PROFILE_ARGS+=(--profile "$_ap")
done

printf '\n'
# GPU overlay selection is vendor-driven: NVIDIA engages docker-compose.gpu.yml
# (re-adds the GPU reservation for ollama + paper_ingestion) only when the
# Docker nvidia runtime is wired — a present GPU without the runtime is called
# out loudly, never silently degraded. AMD engages the ROCm overlay when
# /dev/kfd exists, else the Vulkan overlay; Intel uses Vulkan; no GPU stays on
# the CPU base so install never hard-fails. --gpu cuda|rocm|vulkan|cpu
# overrides detection.
_gpu_choice=""
if [ -n "$NI_GPU_OVERRIDE" ]; then
  _gpu_choice="$NI_GPU_OVERRIDE"
  info "GPU overlay forced by --gpu: ${_gpu_choice}"
  if [ "$_gpu_choice" = "cuda" ] \
     && ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    warn "--gpu cuda but the Docker NVIDIA runtime is not configured — startup will fail the GPU reservation. Install nvidia-container-toolkit, then: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
  fi
else
  case "$NI_GPU_VENDOR" in
    nvidia)
      if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
        _gpu_choice="cuda"
        info "NVIDIA GPU + Docker nvidia runtime detected — enabling the GPU overlay"
      else
        _gpu_choice="cpu"
        warn "NVIDIA GPU detected but the Docker NVIDIA runtime is not configured — the stack will run on CPU. Install nvidia-container-toolkit, then: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
      fi
      ;;
    amd)
      if [ -e "${JARVIS_KFD_DEV:-/dev/kfd}" ]; then
        _gpu_choice="rocm"
        info "AMD GPU with /dev/kfd detected — enabling the ROCm overlay (experimental)"
      else
        _gpu_choice="cpu"
        info "AMD GPU without /dev/kfd (no ROCm kernel driver) — CPU install (Vulkan acceleration is experimental; opt in with ./setup.sh --gpu vulkan)"
      fi
      ;;
    intel)
      _gpu_choice="cpu"
      info "Intel GPU detected — CPU install (Vulkan acceleration is experimental; opt in with ./setup.sh --gpu vulkan)"
      ;;
    *)
      _gpu_choice="cpu"
      info "No GPU detected — CPU-only (Ollama on CPU; slower, OK)"
      ;;
  esac
fi
# The Vulkan/ROCm overlays pass /dev/dri into the container and must join its
# video + render groups by NUMERIC GID (a group NAME is resolved against the
# container image's /etc/group at start and stock ollama images have no `render`
# group). Resolve the host GIDs now and persist them for the overlay's
# ${JARVIS_*_GID} interpolation; with no render node the overlay cannot work, so
# fall back to CPU BEFORE the overlay is selected below.
if [ "$_gpu_choice" = "vulkan" ] || [ "$_gpu_choice" = "rocm" ]; then
  if _dri_gids="$(resolve_dri_gids)"; then
    upsert_env_var JARVIS_VIDEO_GID "${_dri_gids%% *}"
    upsert_env_var JARVIS_RENDER_GID "${_dri_gids##* }"
  else
    warn "No /dev/dri render node — GPU overlay disabled, running on CPU."
    _gpu_choice="cpu"
  fi
fi
# Replay args for the interactive CPU-retry re-exec: the original invocation
# minus any --gpu selection, so compose_up_or_recover's appended `--gpu cpu` is
# the only GPU flag and the retry cannot loop back into the overlay path.
_RECOVERY_ARGS=()
while IFS= read -r _a; do
  [ -n "$_a" ] && _RECOVERY_ARGS+=("$_a")
done < <(strip_gpu_args ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"})
case "$_gpu_choice" in
  cuda)   _overlay_name="gpu";    COMPOSE_OVERLAY=(-f docker-compose.gpu.yml) ;;
  rocm)   _overlay_name="rocm";   COMPOSE_OVERLAY=(-f docker-compose.rocm.yml) ;;
  vulkan) _overlay_name="vulkan"; COMPOSE_OVERLAY=(-f docker-compose.vulkan.yml) ;;
  *)      _overlay_name="";       COMPOSE_OVERLAY=() ;;
esac

# paper_ingestion is published in two torch flavours: a CUDA build (image tag
# suffix `-cuda`, several GB larger) and a CPU build (no suffix). Key the choice
# off the SAME effective-GPU decision the overlay just made, so the torch wheel
# and the Ollama overlay can never disagree. In particular an NVIDIA card WITHOUT
# the Docker nvidia runtime already resolved to the CPU overlay above, and must
# likewise take the CPU image — a CUDA image it cannot reach the GPU from would
# be multiple wasted GB. AMD/Intel accelerate Ollama through their own overlay
# while paper_ingestion's torch stays CPU either way.
if [ "$_gpu_choice" = "cuda" ]; then
  TORCH_VARIANT="cuda"; TORCH_VARIANT_SUFFIX="-cuda"
else
  TORCH_VARIANT="cpu";  TORCH_VARIANT_SUFFIX=""
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
upsert_env_var COMPOSE_FILE "$(compute_compose_file "$_overlay_name" "$_has_override")"
# Persist the torch flavour for the same reason: TORCH_VARIANT_SUFFIX picks the
# image tag that a later bare `docker compose pull`/`up` resolves, and
# TORCH_VARIANT is the build arg the --build-local path hands the Dockerfile.
upsert_env_var TORCH_VARIANT "$TORCH_VARIANT"
upsert_env_var TORCH_VARIANT_SUFFIX "$TORCH_VARIANT_SUFFIX"
# The published CUDA image ships the optional reranker dependency (it costs only
# sentence-transformers there — the CUDA runtime it needs is already in that image),
# while the CPU image deliberately omits it. Persist the matching build arg so a
# --build-local build of the SAME tag produces the SAME contents; without this a
# locally built :X.Y.Z-cuda would silently lack the reranker the pulled one has.
upsert_env_var INSTALL_OPTIONAL "$([ "$TORCH_VARIANT" = "cuda" ] && echo true || echo false)"
# Materialise the application images BEFORE anything heavy lands on disk: this is
# a cold install's peak disk consumer, so an ENOSPC surfaces here — with recovery
# guidance and nothing else half-downloaded — instead of mid-model-pull.
#
# The published services are pulled BY NAME (see PUBLISHED_SERVICES_BASE above for
# why `--ignore-buildable` cannot be used). telegram_bot is profile-gated, hence
# only named when its profile is active; langfuse is never published (local-build
# only) and is handled below.
PUBLISHED_SERVICES=("${PUBLISHED_SERVICES_BASE[@]}")
if [ "$USE_TELEGRAM_PROFILE" -eq 1 ]; then
  PUBLISHED_SERVICES+=("$PUBLISHED_SERVICE_TELEGRAM")
fi

if [ "$BUILD_LOCAL" -eq 1 ]; then
  info "Building application images from source (--build-local)."
  info "This takes minutes and needs considerably more disk than a pull; later runs reuse the cache."
  compose_or_die "docker compose build failed." \
      "Inspect the build output above, then re-run ./setup.sh" \
      ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} build
else
  info "Pulling prebuilt images (${TORCH_VARIANT} build): ${PUBLISHED_SERVICES[*]}"
  compose_or_die "Image pull failed." \
      "Check network access to ghcr.io, then re-run ./setup.sh — or build from source instead: ./setup.sh --build-local" \
      ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} pull "${PUBLISHED_SERVICES[@]}"
  # langfuse is not published, so the observability profile still needs a local
  # build — and the bring-up below runs --no-build, which would otherwise fail on
  # its missing image. Small hardened image, not a torch build.
  if [ "${USE_OBSERVABILITY_PROFILE:-0}" -eq 1 ]; then
    info "Building the observability image (langfuse) locally — it is not published."
    compose_or_die "docker compose build langfuse failed." \
        "Inspect the build output above, then re-run ./setup.sh" \
        ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} --profile observability build langfuse
  fi
fi
# Start Ollama alone first, then run the model pull as an attached one-off so
# its progress streams to the terminal — buried inside a bare `up -d` the
# 7-11 GB first pull looks like a hang.
info "Starting Ollama: docker compose ${COMPOSE_FILE_ARGS[*]:-} up -d ollama"
compose_up_or_recover "docker compose up failed." \
    "Inspect logs: docker compose logs --tail=200" \
    ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} up -d ollama
wait_healthy ollama 180 \
  || warn "Ollama is still starting — model inventory unknown; proceeding to the model pull."

_FIRST_RUN_PULL=0
if docker compose exec -T ollama ollama list 2>/dev/null | tail -n +2 | grep -q .; then
  : # models already present — bootstrap below is a fast verify
else
  _FIRST_RUN_PULL=1
  printf '\n'
  printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
  printf '%s  Images built — downloading models next (7-11 GB, 20-60 min).%s\n' "$C_YELLOW" "$C_RESET"
  printf '%s  Pull progress streams below. This is not an error.           %s\n' "$C_YELLOW" "$C_RESET"
  printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
  printf '\n'
fi

info "Pulling models via ollama-bootstrap..."
if ! docker compose ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} run --rm ollama-bootstrap; then
  die "Model download failed (ollama-bootstrap)." \
      "Check network/disk space and re-run ./setup.sh — or pull manually: docker compose exec ollama ollama pull <model>"
fi

# --no-build is a guard, not a nicety: every published service pairs
# `pull_policy: missing` with a `build:` block, so if an image were somehow
# missing here Compose would silently BUILD it — resurrecting the very multi-GB
# torch build (and its ENOSPC) that installing from prebuilt images exists to
# avoid. Failing loudly is the correct outcome. On --build-local the images were
# just built from source, so no guard applies.
UP_ARGS=(up -d)
[ "$BUILD_LOCAL" -eq 1 ] || UP_ARGS+=(--no-build)
info "Starting services with: docker compose ${COMPOSE_FILE_ARGS[*]:-} ${PROFILE_ARGS[*]:-} ${UP_ARGS[*]}"
compose_up_or_recover "docker compose up failed." \
    "Inspect logs: docker compose logs --tail=200" \
    ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} "${UP_ARGS[@]}"
# The stack is up: a failure past this point must not offer to roll back .env
# (the new configuration is already live), so suppress the pre-start restore hint.
_STACK_STARTED=1

# -----------------------------------------------------------------------------
# 11. Wait for mandatory services to become healthy
# -----------------------------------------------------------------------------
# The health gate is the shared always-on base plus each active group's own
# service (registry extra_health_svcs): a group deliberately started is a group
# whose health is verified. The base is shared with scripts/jarvis-setup.sh via
# mandatory_health_services (setup_lib.sh), so the two entry points cannot drift.
read -ra MANDATORY_SVCS <<< "$(mandatory_health_services "$MANDATORY_HEALTH_BASE" ${ACTIVE_PROFILES[@]+"${ACTIVE_PROFILES[@]}"})"

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
    langfuse)                        _budget=240 ;;  # heavy Node app + its own postgres
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

# -----------------------------------------------------------------------------
# 12. Readiness gates + summary (only reached when all mandatory services healthy)
# -----------------------------------------------------------------------------
DASHBOARD_HOST_PORT_RESOLVED="${DASHBOARD_HOST_PORT:-3001}"

# The tokenized setup link mints the first admin via an X-Setup-Token header, so
# it must never ride raw-IP plaintext (the header is sniffable on a shared LAN).
# Its base defaults to loopback and only moves to a VERIFIED HTTPS origin below.
# The DISPLAYED dashboard URL is separate — it may name the LAN IP the operator
# browses to.
DASHBOARD_URL="http://localhost:${DASHBOARD_HOST_PORT_RESOLVED}"
SETUP_LINK_BASE="http://localhost:${DASHBOARD_HOST_PORT_RESOLVED}"
case "$ACCESS_MODE_LABEL" in
  lan)
    if [ -n "$LAN_IP" ]; then
      DASHBOARD_URL="http://${LAN_IP}:${DASHBOARD_HOST_PORT_RESOLVED}"
    else
      DASHBOARD_URL="http://<this-machine-ip>:${DASHBOARD_HOST_PORT_RESOLVED}"
    fi
    # SETUP_LINK_BASE stays loopback — the token never rides raw-IP HTTP.
    ;;
  tunnel)
    if [ -n "$TUNNEL_HOSTNAME" ]; then
      DASHBOARD_URL="https://${TUNNEL_HOSTNAME}"
      SETUP_LINK_BASE="https://${TUNNEL_HOSTNAME}"
    else
      DASHBOARD_URL="via your Cloudflare tunnel hostname"
    fi
    ;;
esac
if [ "$NI_PROFILE" = "local-https" ]; then
  DASHBOARD_URL="https://localhost:3443"
  SETUP_LINK_BASE="https://localhost:3443"
fi

# LAN reachability probe (non-fatal, informational): a success only proves the
# service answers on THIS host — a host firewall can still block LAN peers, so
# verify from a second device.
if [ "$ACCESS_MODE_LABEL" = "lan" ] && [ -n "$LAN_IP" ]; then
  _lan_probe_url="http://${LAN_IP}:${DASHBOARD_HOST_PORT_RESOLVED}/health"
  info "Probing LAN reachability at ${_lan_probe_url} ..."
  if curl -fso /dev/null "$_lan_probe_url" 2>/dev/null; then
    ok "LAN reachable from this machine — verify from a second device; a host firewall can still block LAN clients."
  else
    warn "LAN probe did not answer yet — services may still be starting, or a host firewall may block port ${DASHBOARD_HOST_PORT_RESOLVED}."
    warn "  Verify from another LAN device, or on this host: curl -so /dev/null ${_lan_probe_url}"
  fi
fi

# Named private HTTPS origin edge probe (non-fatal): only host the setup link at
# the origin once the edge actually answers; otherwise keep it on loopback and
# print a pending-edge status so the operator finishes the edge and re-checks.
PUBLIC_ORIGIN_VERIFIED=0
if [ -n "$NI_PUBLIC_ORIGIN" ]; then
  info "Probing the private HTTPS origin at ${NI_PUBLIC_ORIGIN}/health (best-effort) ..."
  _po_attempt=0
  while [ "$_po_attempt" -lt 3 ]; do
    if curl -fsS --max-time 10 "${NI_PUBLIC_ORIGIN}/health" >/dev/null 2>&1; then
      PUBLIC_ORIGIN_VERIFIED=1; break
    fi
    _po_attempt=$((_po_attempt + 1))
    [ "$_po_attempt" -lt 3 ] && sleep 5
  done
  if [ "$PUBLIC_ORIGIN_VERIFIED" -eq 1 ]; then
    ok "Private HTTPS origin reachable — the setup link will be hosted there."
    SETUP_LINK_BASE="$NI_PUBLIC_ORIGIN"
  else
    warn "Private HTTPS origin ${NI_PUBLIC_ORIGIN} not reachable yet — keeping the setup link on loopback."
  fi
fi

# Let's Encrypt certificate gate: the caddy edge is running under the letsencrypt
# profile started above; wait for ACME issuance before advertising the https://
# URL or hosting the setup link. In production a timeout is FATAL — never claim a
# route that has not served.
if [ "$NI_PROFILE" = "letsencrypt" ] && [ -n "$NI_DOMAIN" ]; then
  info "Waiting for the public certificate at https://${NI_DOMAIN} (up to 120s)..."
  _le_ok=0
  _le_attempt=0
  while [ "$_le_attempt" -lt 12 ]; do
    if curl -fsS --max-time 10 "https://${NI_DOMAIN}/health" >/dev/null 2>&1; then
      _le_ok=1; break
    fi
    _le_attempt=$((_le_attempt + 1))
    [ "$_le_attempt" -lt 12 ] && sleep 10
  done
  if [ "$_le_ok" -eq 1 ]; then
    ok "Public TLS is live at https://${NI_DOMAIN}."
    DASHBOARD_URL="https://${NI_DOMAIN}"
    SETUP_LINK_BASE="https://${NI_DOMAIN}"
  else
    err "Public TLS did not come up at https://${NI_DOMAIN} within the timeout."
    err "Check that DNS resolves to this host and ports 80/443 are reachable, then inspect: docker compose logs caddy"
    exit 1
  fi
fi

# Neutral heading — the green "Setup complete." banner is emitted only AFTER the
# readiness gate below passes, so a production HIGH abort never follows a success
# claim.
printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   Services are healthy — running final checks...%s\n' "$C_BOLD" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '  Dashboard:    %s\n' "$DASHBOARD_URL"
if [ -n "$NI_PUBLIC_ORIGIN" ] && [ "$PUBLIC_ORIGIN_VERIFIED" -eq 1 ]; then
  printf '  Family URL:   %s\n' "$NI_PUBLIC_ORIGIN"
fi

# Click-to-finish setup link: carries the setup token so the wizard can complete
# first-run setup (the token gates the bootstrap WRITE endpoints). init-secrets.sh
# generated secrets/jarvis_setup_token.txt above. print_setup_link (setup_lib.sh)
# is shared with scripts/jarvis-setup.sh so both entry points surface it. The base
# is loopback (or a verified HTTPS origin) — never a raw LAN IP.
print_setup_link "$SETUP_LINK_BASE"
if [ -n "$SETUP_LINK" ]; then
  # Best-effort: open the click-to-finish link in the operator's browser.
  # Non-fatal — a headless/server box simply skips this.
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$SETUP_LINK" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$SETUP_LINK" >/dev/null 2>&1 &
  fi
fi

if [ "$ACCESS_MODE_LABEL" = "lan" ]; then
  # Raw-IP LAN is HTTP-only and NOT an authenticated family route: Secure cookies
  # will not persist over http://IP, and the setup token must never be entered
  # from another device over plaintext. Point at the two real auth routes.
  printf '\n'
  printf '  %sFinishing first-admin setup on a plain-HTTP LAN:%s\n' "$C_BOLD" "$C_RESET"
  printf '    - The setup link above is scoped to localhost on purpose. Finish first-admin\n'
  printf '      setup from the server itself, or via an SSH local-forward, then open the link:\n'
  printf '        ssh -L %s:127.0.0.1:%s user@host\n' "$DASHBOARD_HOST_PORT_RESOLVED" "$DASHBOARD_HOST_PORT_RESOLVED"
  printf '    - Sign-ins over the raw http://%s address will not stick, and never enter the\n' "${LAN_IP:-<ip>}"
  printf '      setup token from another device over http://IP.\n'
  printf '    - For a durable family route add a named HTTPS origin (Tailscale Serve / trusted TLS):\n'
  printf '        ./setup.sh --public-origin https://<host>.<tailnet>.ts.net\n'
fi

if [ -n "$NI_PUBLIC_ORIGIN" ] && [ "$PUBLIC_ORIGIN_VERIFIED" -ne 1 ]; then
  printf '\n'
  printf '  %sPrivate HTTPS origin CONFIGURED but NOT YET VERIFIED:%s %s\n' "$C_BOLD" "$C_RESET" "$NI_PUBLIC_ORIGIN"
  printf '    - Finish the edge, e.g.: tailscale serve --bg --https=443 http://127.0.0.1:%s\n' "$DASHBOARD_HOST_PORT_RESOLVED"
  printf '    - See the deployment guide, then re-check: ./setup.sh --check\n'
  printf '    - Until then the setup link above stays on loopback (bootstrap via SSH-forward).\n'
fi

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
  printf '  Sign in:      request a magic link by email (needs SMTP), or hand out a manual sign-in link from %s/admin/users.\n' "$DASHBOARD_URL"
  printf '                (API key login is disabled in multi-user mode)\n'
fi

# Register this checkout with the jarvis-research lifecycle CLI. This installs the
# `jarvis-research` launcher on PATH (status / logs / doctor / update) and records
# this repo as the managed install. Non-fatal: an install that cannot write the
# launcher still completes.
install_cli_shim "$SCRIPT_DIR" || warn "Could not install the jarvis-research launcher (non-fatal)."

printf '\n'
printf '  All mandatory services healthy. You can now open the dashboard.\n'
printf '  Tail logs:  docker compose logs -f\n'
printf '\n'

# -----------------------------------------------------------------------------
# 13. Production-readiness check
# Run the readiness script (non-fatal for dev; aborts for letsencrypt/production).
# -----------------------------------------------------------------------------
_READINESS_SCRIPT="$SCRIPT_DIR/scripts/production-readiness-check.sh"
if [ -f "$_READINESS_SCRIPT" ]; then
  printf '%s--- Production Readiness Check -----------------------------------%s\n' "$C_BOLD" "$C_RESET"
  # Determine the effective environment from .env (letsencrypt => production).
  _ENV_VALUE="$(grep '^ENVIRONMENT=' .env 2>/dev/null | cut -d= -f2- || true)"
  _ENV_VALUE="${_ENV_VALUE:-development}"

  # Readiness exit contract (production-readiness-check.sh header): 0 = clean,
  # 2 = warnings present, 1 = HIGH issues. readiness_verdict maps that exit code
  # to the wrapper action, so a routine warning (e.g. missing SMTP, now exit 2)
  # never aborts a production install — only a HIGH issue (exit 1) does.
  _rc=0
  bash "$_READINESS_SCRIPT" || _rc=$?
  case "$(readiness_verdict "$_rc" "$_ENV_VALUE")" in
    ok)
      ok "Production readiness: all checks passed."
      ;;
    warn)
      if [ "$_rc" -eq 2 ]; then
        warn "Production readiness: passed with warnings — review the table above."
      else
        warn "Production readiness check found issues (non-fatal in dev profile)."
        warn "Run 'bash scripts/production-readiness-check.sh' to see details."
      fi
      ;;
    abort)
      err "Production readiness check found HIGH issues. Aborting."
      err "Fix the issues listed above and re-run: ./setup.sh"
      exit "$_rc"
      ;;
  esac
else
  warn "scripts/production-readiness-check.sh not found — skipping readiness check."
fi

# Green success banner — reached only past the readiness gate (a production HIGH
# abort exits above), so "Setup complete." is never printed ahead of the checks.
printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   Setup complete.%s\n' "$C_GREEN" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"

# -----------------------------------------------------------------------------
# 14. Next steps
# -----------------------------------------------------------------------------
printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   Next steps%s\n' "$C_BOLD" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '  Open the dashboard: %s\n' "$DASHBOARD_URL"
if [ "$NI_MODE" = "single" ]; then
  printf '  1. Log in with your API key (stored in ~/.config/jarvis/api-key).\n'
else
  # Dependency order: the first admin is bootstrapped by the token-bearing setup
  # link (no SMTP, no existing account) BEFORE anything that presupposes an admin
  # or working email.
  printf '  1. Bootstrap the first admin: open the "Finish setup" link above — it carries the setup token, so no SMTP or existing account is needed.\n'
  printf '  2. Verify your final origin and APP_BASE_URL match how users will reach JARVIS (an exact-origin match is required for passkeys).\n'
  printf '  3. Configure SMTP in Settings for magic-link email, or hand out manual sign-in links from %s/admin/users.\n' "$DASHBOARD_URL"
  printf '  4. Each user enrols a passkey at the final origin for password-free sign-in.\n'
fi
printf '\n'
printf '  Admin user management: %s/admin/users\n' "$DASHBOARD_URL"
printf '  Tail logs:             docker compose logs -f\n'
printf '\n'
