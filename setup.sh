#!/usr/bin/env bash
# setup.sh — JARVIS RD Assistant first-time installer.
#
# Idempotent: second run with an existing .env prompts before clobbering.
# macOS-safe: no `sed -i`, no GNU-only flags. Uses tempfile + mv.
#
# Non-interactive mode (CI / unattended installs):
#   ./setup.sh --non-interactive [OPTIONS]
#
#   --domain <host>           Public hostname for --profile=letsencrypt
#                             (e.g. jarvis.example.com).
#   --admin-email <email>     Used for Let's Encrypt ACME account.
#   --profile <dev|local-https|letsencrypt|tunnel>
#                             dev          — ENVIRONMENT=development, localhost binding
#                             local-https  — locally trusted certificate, access mode 1
#                             letsencrypt  — Caddy + ACME; requires --domain + --admin-email
#                             tunnel       — Cloudflare; requires the three tunnel flags below
#   --tunnel-ack              Acknowledge that the Cloudflare tunnel access mode
#                             exposes this instance to the internet. Zero-Trust
#                             access policies must be configured first. Required to
#                             select the tunnel access mode non-interactively.
#   --tunnel-hostname <host>  Public hostname configured for the Cloudflare tunnel.
#   --tunnel-token-file <path>
#                             Read the tunnel token from the file's first line.
#                             The token is never accepted as a command-line value.
#   --tailscale               Set up private HTTPS with Tailscale. On supported
#                             Linux hosts, preview and offer to install a missing
#                             client before configuring and verifying Serve.
#   --mode <single|multi>     Sign-in preference written to JARVIS_SETUP_MODE.
#                             single (default) — show API-key login first.
#                             multi            — show email/magic-link login first.
#                             Accounts and private libraries are isolated in either.
#   --check                   Doctor / preflight check (read-only). Exits 0 if all
#                             requirements are met, 1 if any are missing. Does NOT
#                             generate .env, install packages, or start services.
#   --install-prereqs        Explicitly run the guided prerequisite installer when
#                             Docker, Docker Compose 2.24.4+, openssl, curl,
#                             Python 3, or a selected HTTPS route needs a host
#                             package.
#                             In --non-interactive mode this flag is required for
#                             host package installation.
#   --compose-project-name <name>
#                             Persist a side-by-side or automation identity for
#                             this install. Lowercase letters, digits, "_", and
#                             "-" are accepted; the first character is alphanumeric.
#   --image-tag <tag>         Select published application images by stable,
#                             prerelease, or lowercase 40-hex commit tag. The
#                             application version still comes from the checkout.
#   --skip-disk-check         Skip the pre-install free-disk check on the Docker
#                             data root (default installs need ~27-54 GB there,
#                             depending on GPU variant and model choice).
#   --build-local             Build the application images from source instead of
#                             pulling the prebuilt ones published to GHCR. Much
#                             slower and needs considerably more disk. NOT an
#                             offline path: it still needs network for base images,
#                             Python/OS wheels, third-party images (postgres, ollama,
#                             caddy, ...), and the Ollama model downloads. Use it for
#                             development or when a GHCR pull is unavailable.
#   --backend ollama|auto     Override AI backend selection. Default: auto (the
#                             installer configures Ollama for the detected hardware).
#                             vLLM remains a manual benchmark overlay; setup.sh
#                             does not install or configure it as an app backend.
#   --smart-model <id>        Override the Ollama model tag (for example qwen3:14b).
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

rollback_unverified_access_config() {
  # A reconfiguration may already have recreated the dashboard and its selected
  # edge. Roll back that JARVIS-owned runtime before reporting the route failure;
  # restoring files alone would leave the previous route pointed at new settings.
  local _rollback_snapshot=.env.pre-setup.bak _rollback_secret_snapshot=""
  if [ -f "${_SETUP_TRANSACTION_DIR:-}/active" ]; then
    _rollback_snapshot="${_SETUP_TRANSACTION_DIR}/old.env"
    _rollback_secret_snapshot="${_SETUP_TRANSACTION_DIR}/secrets"
  fi
  if [ "${_ENV_SNAPSHOT_TAKEN:-0}" -eq 1 ] && [ -f "$_rollback_snapshot" ]; then
    _ACCESS_ROLLBACK_ATTEMPTED=1
    if rollback_access_runtime \
        "${_PREVIOUS_ACCESS_MODE:-}" "${_PREVIOUS_COMPOSE_PROFILES:-}" \
        "${_PREVIOUS_TAILSCALE_PORT:-3003}" "${_PREVIOUS_APP_BASE_URL:-}" \
        "${_PREVIOUS_DASHBOARD_HOST_PORT:-3001}" "${ACCESS_MODE_LABEL:-localhost}" \
        "${COMPOSE_PROFILES_VALUE:-}" "${DASHBOARD_TRUSTED_HOST_PORT_RESOLVED:-3003}" \
        "${_REPLACEMENT_TAILSCALE_ATTEMPTED:-0}" "${NON_INTERACTIVE:-1}" \
        "$_rollback_snapshot" .env secrets/cloudflare_tunnel_token.txt "$SCRIPT_DIR" \
        "$_rollback_secret_snapshot"; then
      _STACK_STARTED=1
      if [ -n "${_SETUP_TRANSACTION_DIR:-}" ] \
          && ! discard_setup_transaction "$_SETUP_TRANSACTION_DIR"; then
        warn "The previous route was restored, but its private transaction snapshot could not be removed: $_SETUP_TRANSACTION_DIR"
        return 1
      fi
      _ACCESS_TRANSACTION_COMMITTED=1
      warn "The previous configuration, credentials, and live access route were restored and verified."
    else
      local _recovery_edge _recovery_service _recovery_url_q
      warn "Automatic runtime rollback was incomplete; recovery snapshots were retained."
      warn "Run these commands from $SCRIPT_DIR:"
      warn "  unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE"
      warn "First inspect and remove any attempted replacement edge that may still be live:"
      while IFS='|' read -r _recovery_edge _recovery_service; do
        [ -n "$_recovery_edge" ] || continue
        case "$_recovery_edge" in
          tailscale)
            warn "  bash -c '. ./scripts/setup_lib.sh; tailscale_serve_https_off ${DASHBOARD_TRUSTED_HOST_PORT_RESOLVED:-3003} 0'"
            ;;
          tunnel|caddy-local|letsencrypt)
            warn "  docker compose --profile ${_recovery_edge} rm -sf ${_recovery_service}"
            ;;
        esac
      done < <(access_edge_retirements "${ACCESS_MODE_LABEL:-localhost}" \
        "${COMPOSE_PROFILES_VALUE:-}" '' '')
      warn "Then restore and verify the previous configuration:"
      if [ -n "$_rollback_secret_snapshot" ]; then
        warn "  cp .jarvis-setup-transaction/old.env .env"
        warn "  bash -c '. ./scripts/setup_lib.sh; restore_setup_secret_snapshot .jarvis-setup-transaction/secrets ./secrets'"
      else
        warn "  cp .env.pre-setup.bak .env"
      fi
      warn "  bash scripts/init-secrets.sh"
      warn "  docker compose up -d --no-build --force-recreate --no-deps dashboard"
      while IFS='|' read -r _recovery_edge _recovery_service; do
        [ -n "$_recovery_edge" ] || continue
        case "$_recovery_edge" in
          tailscale)
            warn "  sudo tailscale serve --bg --yes --https=443 http://127.0.0.1:${_PREVIOUS_TAILSCALE_PORT:-3003}"
            ;;
          tunnel|caddy-local|letsencrypt)
            warn "  docker compose --profile ${_recovery_edge} up -d --no-build --force-recreate --no-deps ${_recovery_service}"
            ;;
        esac
      done < <(access_edge_retirements "${_PREVIOUS_ACCESS_MODE:-}" \
        "${_PREVIOUS_COMPOSE_PROFILES:-}" '' '')
      warn "  curl -fsS http://127.0.0.1:${_PREVIOUS_DASHBOARD_HOST_PORT:-3001}/health/jarvis"
      if [[ "${_PREVIOUS_APP_BASE_URL:-}" == https://* ]]; then
        printf -v _recovery_url_q '%q' "${_PREVIOUS_APP_BASE_URL%/}/health/jarvis"
        warn "  curl -fsS ${_recovery_url_q}"
      elif [ "$(selected_https_route "${_PREVIOUS_ACCESS_MODE:-}" \
          "${_PREVIOUS_COMPOSE_PROFILES:-}" "${_PREVIOUS_APP_BASE_URL:-}")" = "local-https" ]; then
        warn "  curl --cacert \"\$(mkcert -CAROOT)/rootCA.pem\" -fsS https://localhost:3443/health/jarvis"
        warn "  curl -fsS https://localhost:3443/health/jarvis"
      fi
      return 1
    fi
  fi
  return 0
}

unverified_https_exit() {
  # A selected HTTPS route is part of setup's success contract. Keep localhost
  # usable, restore prior persisted access state on a failed reconfigure, and
  # return nonzero until the exact external marker proves the route reaches this
  # JARVIS instance.
  local route_label="${1:-HTTPS}" public_origin="${2:-}" dashboard_port="${3:-3001}"
  local retry_command="${4:-./setup.sh}"
  local local_verified=0 rollback_attempted=0 edge_cleanup_failed=0
  local attempted_edge attempted_service

  if [ "${_ENV_SNAPSHOT_TAKEN:-0}" -eq 1 ]; then
    rollback_attempted=1
    if rollback_unverified_access_config; then
      local_verified=1
    fi
  else
    if [ "${_ENV_EXISTED_AT_START:-0}" -eq 0 ]; then
      _ACCESS_ROLLBACK_ATTEMPTED=1
      remove_attempted_access_runtime "${ACCESS_MODE_LABEL:-localhost}" \
        "${COMPOSE_PROFILES_VALUE:-}" \
        "${DASHBOARD_TRUSTED_HOST_PORT_RESOLVED:-3003}" \
        "${_REPLACEMENT_TAILSCALE_ATTEMPTED:-0}" "${NON_INTERACTIVE:-1}" \
        "$SCRIPT_DIR" "$SCRIPT_DIR/.env" || edge_cleanup_failed=1
    fi
    if wait_for_jarvis_marker \
        "http://127.0.0.1:${dashboard_port}/health/jarvis" 3 2; then
      local_verified=1
    fi
  fi
  printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
  printf '%s   Setup needs attention.%s\n' "$C_YELLOW" "$C_RESET"
  printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
  if [ "$local_verified" -eq 1 ]; then
    printf '  Local services are healthy and usable on this computer.\n'
    printf '  Local dashboard: http://localhost:%s\n' "$dashboard_port"
  else
    if [ "$rollback_attempted" -eq 1 ]; then
      printf '  Local access state is uncertain because recovery was not verified.\n'
      printf '  Use the recovery commands above before signing in.\n'
    else
      printf '  The local dashboard marker could not be verified.\n'
      printf '  Check: docker compose logs --tail=200 dashboard\n'
    fi
  fi
  if [ "$edge_cleanup_failed" -eq 1 ]; then
    printf '  The failed JARVIS access edge could not be removed automatically.\n'
    while IFS='|' read -r attempted_edge attempted_service; do
      [ -n "$attempted_edge" ] || continue
      case "$attempted_edge" in
        tailscale)
          printf '  Inspect without changing it: sudo tailscale serve status --json\n'
          ;;
        tunnel|caddy-local|letsencrypt)
          printf '  Remove it after review: docker compose --profile %s rm -sf %s\n' \
            "$attempted_edge" "$attempted_service"
          ;;
      esac
    done < <(access_edge_retirements "${ACCESS_MODE_LABEL:-localhost}" \
      "${COMPOSE_PROFILES_VALUE:-}" '' '')
  fi
  printf '  Selected HTTPS route: %s (not verified)\n' "$route_label"
  if [ -n "$public_origin" ]; then
    printf '  Check in a browser: %s/health/jarvis\n' "${public_origin%/}"
    printf '  That page must show only:\n'
    printf '    jarvis-rd-assistant\n'
  fi
  printf '  Fix that HTTPS route, then run this one command:\n'
  printf '    %s\n' "$retry_command"
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
  local message="$1" hint="$2" log_file reply rollback_attempted=0
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
        *)
          if [ "${_ACCESS_RECONFIGURATION_APPLY_STARTED:-0}" -eq 1 ]; then
            rollback_attempted=1
            if ! rollback_unverified_access_config; then
              hint="${hint}
The CPU retry was not started because automatic access rollback was incomplete; use the recovery commands printed above."
              die_enospc_aware "$log_file" "$message" "$hint"
              return 1
            fi
          fi
          rm -f "$log_file"
          exec "$0" --gpu cpu ${_RECOVERY_ARGS[@]+"${_RECOVERY_ARGS[@]}"}
          ;;
      esac
    fi
    hint="${hint}
GPU overlay failed to start — re-run ./setup.sh --gpu cpu for a CPU-only install, then please file a hardware report (GitHub issue template)."
  fi
  if [ "${_ACCESS_RECONFIGURATION_APPLY_STARTED:-0}" -eq 1 ] \
     && [ "$rollback_attempted" -eq 0 ]; then
    rollback_unverified_access_config || true
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

  printf '%sDetected: %s%s. Configuring Ollama.%s\n' \
    "$C_BOLD" "$tier" "$note" "$C_RESET"
  NI_LLM_BACKEND="ollama"
  [ -n "${NI_SMART_MODEL:-}" ] \
    || NI_SMART_MODEL=$(_default_model_for_tier "$tier" ollama)
}

# setup_disk_variant — print the image path this invocation will install.
setup_disk_variant() {
  local accel=cpu
  if [ -n "${NI_GPU_OVERRIDE:-}" ]; then
    [ "$NI_GPU_OVERRIDE" = cuda ] && accel=cuda
  elif docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    accel=cuda
  fi
  if [ "${BUILD_LOCAL:-0}" -eq 1 ]; then
    printf '%s-build' "$accel"
  else
    printf '%s-pull' "$accel"
  fi
}

run_doctor() {
  local fail=0
  local _gpu_detected=0
  local _docker_ready=0
  local _python_ready=0
  local _compose_ver=""
  printf '%s--- setup.sh --check (read-only) -------------------------------%s\n' "$C_BOLD" "$C_RESET"
  if command -v docker >/dev/null 2>&1; then ok "docker present"; else err "docker missing — $(os_install_hint docker)"; fail=1; fi
  if docker compose version >/dev/null 2>&1; then
    _compose_ver="$(docker compose version --short 2>/dev/null || printf unknown)"
    if compose_meets_floor "$_compose_ver" "$COMPOSE_MIN"; then
      ok "Docker Compose v${_compose_ver#v}"
    else
      err "Docker Compose v${_compose_ver#v} is too old or unreadable — v${COMPOSE_MIN}+ is required (update Docker Desktop or the docker-compose-plugin)"
      fail=1
    fi
  else
    err "docker compose v2 missing"
    fail=1
  fi
  # `docker info` (not a socket stat) so DOCKER_HOST/rootless setups are honoured.
  if docker info >/dev/null 2>&1; then
    ok "docker daemon reachable"
    _docker_ready=1
  else
    err "docker daemon unreachable — start Docker (Docker Desktop on macOS; 'sudo systemctl start docker' on Linux), or check DOCKER_HOST/permissions"
    fail=1
  fi
  if command -v openssl >/dev/null 2>&1; then ok "openssl present"; else err "openssl missing"; fail=1; fi
  if command -v curl >/dev/null 2>&1; then ok "curl present"; else err "curl missing — required for downloads and health checks"; fail=1; fi
  # python3 is a hard requirement (model selection + disk sizing shell out to it
  # under `set -euo pipefail`), so its absence FAILS --check rather than the
  # advisory-only probe below it.
  if command -v python3 >/dev/null 2>&1; then
    ok "python3 present"
    _python_ready=1
  else
    err "python3 missing — required for model selection and disk sizing (install python3, then re-run ./setup.sh --check)"
    fail=1
  fi
  if [ "$NI_PROFILE" = "local-https" ]; then
    if mkcert_toolchain_available; then
      ok "mkcert and browser trust tooling present"
    else
      err "local HTTPS needs mkcert and browser trust tooling — run ./setup.sh --profile=local-https --install-prereqs"
      fail=1
    fi
  fi
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
  # Report the same model, image path, and Docker data-root requirement that a
  # real install would enforce. The check remains advisory and read-only.
  if [ "$_docker_ready" -eq 1 ] && [ "$_python_ready" -eq 1 ]; then
    local _disk_variant _disk_model _req_gb _req_exact=1 _disk_out _disk_rc=0
    local _free_gb _data_root
    _disk_variant="$(setup_disk_variant)"
    _disk_model="${NI_SMART_MODEL:-$(_default_model_for_tier "$tier" ollama)}"
    _req_gb="$(compute_required_disk_gb "$_disk_model" "$_disk_variant")" \
      || _req_exact=0
    if [ "$_req_exact" -eq 1 ]; then
      info "Install disk requirement: ~${_req_gb} GB (${_disk_variant}, model ${_disk_model})."
    else
      warn "Install disk requirement: ~${_req_gb} GB conservative estimate (${_disk_variant}, model ${_disk_model}); the model catalog could not be read."
    fi
    _disk_out="$(preflight_disk_lib "$_req_gb")" || _disk_rc=$?
    _free_gb="${_disk_out%% *}"
    _data_root="${_disk_out#* }"
    case "$_disk_rc" in
      0) ok "Docker data root: ${_free_gb} GB free on ${_data_root}." ;;
      1) warn "Docker data root: ${_free_gb} GB free on ${_data_root}; this install needs ~${_req_gb} GB." ;;
      2) warn "Docker data root ${_data_root} is not measurable from the host; verify the Docker Desktop VM disk has ~${_req_gb} GB free." ;;
    esac
  else
    warn "Install disk requirement unavailable until Docker and Python 3 are ready."
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
  # setup.sh derives and persists all pinned ingress addresses from this subnet;
  # a custom value therefore needs no Compose/nginx hand edit.
  if [ -n "${JARVIS_NET_SUBNET:-}" ] && [ "$JARVIS_NET_SUBNET" != "10.137.241.0/24" ]; then
    info "Custom JARVIS_NET_SUBNET=${JARVIS_NET_SUBNET}; setup derives its gateway and edge addresses automatically."
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
  # Use the same selected pull/build and accelerator path reported by --check.
  local _variant
  _variant="$(setup_disk_variant)"
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

# _setup_service_container_id SERVICE — print the first Compose container ID.
_setup_service_container_id() {
  docker compose ps -q "$1" 2>/dev/null | head -n 1 || true
}

# _wait_for_setup_service SERVICE BUDGET [INTERVAL] — wait and report status.
_wait_for_setup_service() {
  local svc="$1" budget="$2" interval="${3:-3}"
  if wait_for_compose_service_health \
      "$svc" "$budget" _setup_service_container_id "$interval"; then
    case "$COMPOSE_HEALTH_RESULT" in
      healthy) ok "$svc: healthy" ;;
      running-unverified)
        warn "$svc: running (no healthcheck) — readiness not verified" ;;
    esac
    return 0
  fi
  case "$COMPOSE_HEALTH_RESULT" in
    terminal) err "$svc: not running (state: ${COMPOSE_HEALTH_LAST_STATE})" ;;
    *) err "$svc: did not become healthy within ${budget}s (last state: ${COMPOSE_HEALTH_LAST_STATE})." ;;
  esac
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
COMPOSE_MIN=2.24.4
_SETUP_TRANSACTION_DIR="${SCRIPT_DIR}/.jarvis-setup-transaction"
# An exported Compose selector outranks this checkout's .env and can otherwise
# redirect every setup mutation to another project. Persisted selectors continue
# to load from the repo .env after this caller-environment reset.
sanitize_compose_environment

# resolve_setup_browser_route BASE DASHBOARD_HTTP_PORT
#
# Resolve the address the operator's real browser can use. SSH always wins when
# the shell is genuinely remote. A non-SSH WSL launch uses Windows localhost only
# after Windows curl reaches this instance's exact marker; a failed check prints
# Windows forwarding repair steps and never invents an SSH session. Native
# desktops keep their direct address, while native headless hosts use SSH.
resolve_setup_browser_route() {
  local requested_base="${1%/}" dashboard_http_port="$2"
  local link_is_loopback=0 shell_is_remote=0 has_desktop=0
  local route tunnel_port browser_base windows_body="" requested_base_lower

  requested_base_lower="$(printf '%s' "$requested_base" | tr '[:upper:]' '[:lower:]')"

  SETUP_BROWSER_BASE="$requested_base"
  SETUP_LINK_USES_SSH_TUNNEL=0
  SETUP_LINK_USES_WINDOWS_FORWARDING=0
  SETUP_BROWSER_IS_SHARED=0
  SETUP_BROWSER_HAS_DESKTOP=0
  SETUP_BROWSER_TUNNEL_PORT=""
  SETUP_BROWSER_ROUTE_REQUESTED_BASE="$requested_base"
  SETUP_BROWSER_ROUTE_PORT="$dashboard_http_port"
  case "$requested_base_lower" in
    http://localhost|https://localhost|http://127.0.0.1|https://127.0.0.1|\
    http://localhost:*|https://localhost:*|http://127.0.0.1:*|https://127.0.0.1:*|\
    http://*.localhost|https://*.localhost|http://*.localhost:*|https://*.localhost:*)
      link_is_loopback=1
      ;;
    https://*) SETUP_BROWSER_IS_SHARED=1 ;;
  esac
  if [ -n "${SSH_CONNECTION:-}" ] || [ -n "${SSH_TTY:-}" ]; then
    shell_is_remote=1
  fi
  if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ] \
      || [ "$(uname -s 2>/dev/null || true)" = "Darwin" ]; then
    has_desktop=1
    SETUP_BROWSER_HAS_DESKTOP=1
  fi

  [ "$link_is_loopback" -eq 1 ] || return 0

  if [ "$shell_is_remote" -eq 1 ]; then
    route="$(headless_setup_route "$1" "$2")" || return 1
    IFS='|' read -r tunnel_port browser_base <<< "$route"
    [ -n "$tunnel_port" ] && [ -n "$browser_base" ] || return 1
    SETUP_BROWSER_BASE="$browser_base"
    SETUP_LINK_USES_SSH_TUNNEL=1
    SETUP_BROWSER_TUNNEL_PORT="$tunnel_port"
    return 0
  fi

  if is_wsl_host; then
    route="$(headless_setup_route "$1" "$2")" || return 1
    IFS='|' read -r tunnel_port browser_base <<< "$route"
    [ -n "$browser_base" ] || return 1
    if command -v curl.exe >/dev/null 2>&1; then
      if windows_body="$(curl.exe -fsS --max-time 10 \
          "http://localhost:${dashboard_http_port}/health/jarvis" 2>/dev/null)"; then
        windows_body="${windows_body%$'\r'}"
        if [ "$windows_body" = "jarvis-rd-assistant" ]; then
          SETUP_BROWSER_BASE="$browser_base"
          SETUP_LINK_USES_WINDOWS_FORWARDING=1
          return 0
        fi
      fi
    fi
    printf '\n  Windows localhost forwarding did not reach this JARVIS service.\n'
    printf '  No finish link was printed. In Windows PowerShell, check:\n'
    printf '    curl.exe http://localhost:%s/health/jarvis\n' "$dashboard_http_port"
    printf '  It must print exactly: jarvis-rd-assistant\n'
    printf '  If it does not, run "wsl --shutdown", restart Docker Desktop and WSL,\n'
    printf '  then retry. If needed, enable localhostForwarding=true in %%UserProfile%%\\.wslconfig.\n'
    return 1
  fi

  if [ "$has_desktop" -eq 0 ]; then
    route="$(headless_setup_route "$1" "$2")" || return 1
    IFS='|' read -r tunnel_port browser_base <<< "$route"
    [ -n "$tunnel_port" ] && [ -n "$browser_base" ] || return 1
    SETUP_BROWSER_BASE="$browser_base"
    SETUP_LINK_USES_SSH_TUNNEL=1
    SETUP_BROWSER_TUNNEL_PORT="$tunnel_port"
  fi
}

# present_setup_link BASE DASHBOARD_HTTP_PORT
#
# Print the first-admin link only after resolve_setup_browser_route has proven
# how the operator's browser reaches it.
present_setup_link() {
  if [ "${SETUP_BROWSER_ROUTE_PORT:-}" != "$2" ] \
      || { [ "${SETUP_BROWSER_ROUTE_REQUESTED_BASE:-}" != "${1%/}" ] \
        && [ "${SETUP_BROWSER_BASE:-}" != "${1%/}" ]; }; then
    resolve_setup_browser_route "$1" "$2" || return 1
  fi

  print_setup_link "$SETUP_BROWSER_BASE"
  [ -n "$SETUP_LINK" ] || return 0

  if [ "$SETUP_LINK_USES_SSH_TUNNEL" -eq 1 ]; then
    printf '\n'
    printf '  %sFinish setup from another computer:%s\n' "$C_BOLD" "$C_RESET"
    printf '  1. On that computer, run this command and keep it open:\n'
    printf '     ssh -L %s:127.0.0.1:%s <your-ssh-user>@<server-address>\n' \
      "$SETUP_BROWSER_TUNNEL_PORT" "$SETUP_BROWSER_TUNNEL_PORT"
    printf "  2. In that computer's browser, open this exact address:\n"
    printf '     %s\n' "$SETUP_LINK"
    printf '  This uses HTTP only inside the encrypted SSH connection. Keep the address\n'
    printf '  exactly as printed; the outside browser does not use the VM certificate.\n'
  elif [ "$SETUP_LINK_USES_WINDOWS_FORWARDING" -eq 1 ]; then
    printf '\n'
    printf '  %sFinish setup in your Windows browser:%s\n' "$C_BOLD" "$C_RESET"
    printf '  Open this exact address: %s\n' "$SETUP_LINK"
  elif [ "$SETUP_BROWSER_HAS_DESKTOP" -eq 1 ]; then
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$SETUP_LINK" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1; then
      open "$SETUP_LINK" >/dev/null 2>&1 &
    fi
  fi
}

recover_interrupted_setup_transaction() {
  local transaction_dir="${_SETUP_TRANSACTION_DIR}"
  local old_mode old_profiles old_port old_origin old_dashboard_port
  local new_mode new_profiles new_port tailscale_attempted
  local _edge _service metadata_failed=0 owner_state=0
  local pending_q transaction_q recovery_tmp_base=/tmp recovery_base_physical
  local script_dir_physical
  printf -v pending_q '%q' "${transaction_dir}.pending"
  printf -v transaction_q '%q' "$transaction_dir"
  script_dir_physical="$(cd "$SCRIPT_DIR" && pwd -P)"
  recovery_base_physical="$(cd /tmp && pwd -P)"
  case "${recovery_base_physical}/jarvis-recovery.placeholder" in
    "${script_dir_physical}"/*)
      recovery_tmp_base=/var/tmp
      ;;
  esac
  if [ -e "${transaction_dir}.pending" ] || [ -L "${transaction_dir}.pending" ]; then
    setup_transaction_owner_state "$transaction_dir" pending || owner_state=$?
    if [ "$owner_state" -eq 0 ]; then
      warn "Another setup process is still running and owns ${transaction_dir}.pending."
      warn "Wait for it to finish, then re-run ./setup.sh."
    else
      warn "An abandoned or invalid setup staging path was retained: ${transaction_dir}.pending"
      warn "It can contain private credential copies. Do not rename it inside this checkout."
      warn "No installation mutation starts before staging is promoted. After confirming no setup process is active, delete exactly this abandoned staging path:"
      warn "  rm -rf -- ${pending_q}"
      warn "Then re-run ./setup.sh."
    fi
    return 1
  fi
  if [ ! -e "$transaction_dir" ] && [ ! -L "$transaction_dir" ]; then
    return 0
  fi
  if [ ! -d "$transaction_dir" ] || [ -L "$transaction_dir" ] \
      || [ ! -f "$transaction_dir/active" ] \
      || [ -L "$transaction_dir/active" ]; then
    warn "The retained setup journal path is invalid and was not read or deleted: $transaction_dir"
    warn "It may contain private credentials. Do not rename it inside this checkout."
    warn "After confirming no setup process is active, preserve it outside the checkout for inspection:"
    warn "  JARVIS_RECOVERY_HOLD=\"\$(mktemp -d ${recovery_tmp_base}/jarvis-recovery.XXXXXX)\""
    warn '  chmod 700 "$JARVIS_RECOVERY_HOLD"'
    warn "  mv -- ${transaction_q} \"\$JARVIS_RECOVERY_HOLD/setup-transaction\""
    warn "Do not re-run setup until the held snapshot and previous route have been inspected."
    return 1
  fi
  owner_state=0
  setup_transaction_owner_state "$transaction_dir" active || owner_state=$?
  case "$owner_state" in
    0)
      warn "Another setup process is still running and owns the active transaction journal."
      warn "Wait for it to finish, then re-run ./setup.sh."
      return 1
      ;;
    1) ;; # owner is gone: recover the interrupted mutation below
    *)
      warn "The retained setup journal has an invalid owner record and was not read or deleted."
      warn "It may contain private credentials. Do not rename it inside this checkout."
      warn "After confirming no setup process is active, preserve it outside the checkout for inspection:"
      warn "  JARVIS_RECOVERY_HOLD=\"\$(mktemp -d ${recovery_tmp_base}/jarvis-recovery.XXXXXX)\""
      warn '  chmod 700 "$JARVIS_RECOVERY_HOLD"'
      warn "  mv -- ${transaction_q} \"\$JARVIS_RECOVERY_HOLD/setup-transaction\""
      warn "Do not re-run setup until the held snapshot and previous route have been inspected."
      return 1
      ;;
  esac

  # Serialize recovery of a dead owner's active journal with the same atomic lock
  # journal creation uses. Revalidate after acquiring it: another recovery may
  # have completed between the initial owner check and this mkdir.
  if ! acquire_setup_transaction_lock "$transaction_dir"; then
    warn "Another setup or recovery process acquired the transaction lock first."
    warn "Wait for it to finish, then re-run ./setup.sh."
    return 1
  fi
  if [ ! -e "$transaction_dir" ] && [ ! -L "$transaction_dir" ]; then
    release_setup_transaction_lock "$transaction_dir" || return 1
    info "Another process already completed the interrupted setup recovery."
    return 0
  fi
  owner_state=0
  setup_transaction_owner_state "$transaction_dir" active || owner_state=$?
  if [ "$owner_state" -ne 1 ]; then
    release_setup_transaction_lock "$transaction_dir" || true
    warn "The active setup journal changed while recovery was waiting for its lock; it was not touched."
    warn "Wait for any other setup process to finish, then re-run ./setup.sh."
    return 1
  fi

  warn "An interrupted setup transaction was found. Restoring its previous configuration before this run continues..."
  old_mode="$(setup_transaction_value "$transaction_dir" old_mode)" || metadata_failed=1
  old_profiles="$(setup_transaction_value "$transaction_dir" old_profiles)" || metadata_failed=1
  old_port="$(setup_transaction_value "$transaction_dir" old_tailscale_port)" || metadata_failed=1
  old_origin="$(setup_transaction_value "$transaction_dir" old_origin)" || metadata_failed=1
  old_dashboard_port="$(setup_transaction_value "$transaction_dir" old_dashboard_port)" || metadata_failed=1
  new_mode="$(setup_transaction_value "$transaction_dir" new_mode)" || metadata_failed=1
  new_profiles="$(setup_transaction_value "$transaction_dir" new_profiles)" || metadata_failed=1
  new_port="$(setup_transaction_value "$transaction_dir" new_tailscale_port)" || metadata_failed=1
  tailscale_attempted="$(setup_transaction_value "$transaction_dir" tailscale_attempted)" || metadata_failed=1
  if [ "$metadata_failed" -ne 0 ]; then
    warn "The retained setup journal is incomplete or corrupt; it was not overwritten or deleted."
    warn "Restore its intact file snapshots first, then inspect the access route manually:"
    warn "  cp .jarvis-setup-transaction/old.env .env"
    warn "  bash -c '. ./scripts/setup_lib.sh; restore_setup_secret_snapshot .jarvis-setup-transaction/secrets ./secrets'"
    warn "  bash scripts/init-secrets.sh"
    warn "  docker compose up -d --no-build --force-recreate --no-deps dashboard"
    release_setup_transaction_lock "$transaction_dir" || true
    return 1
  fi

  if rollback_access_runtime "$old_mode" "$old_profiles" "$old_port" \
      "$old_origin" "$old_dashboard_port" "$new_mode" "$new_profiles" \
      "$new_port" "$tailscale_attempted" "$NON_INTERACTIVE" \
      "$transaction_dir/old.env" .env secrets/cloudflare_tunnel_token.txt \
      "$SCRIPT_DIR" "$transaction_dir/secrets"; then
    cp "$transaction_dir/old.env" .env.pre-setup.bak \
      && chmod 600 .env.pre-setup.bak || {
        release_setup_transaction_lock "$transaction_dir" || true
        return 1
      }
    discard_setup_transaction "$transaction_dir" || {
      release_setup_transaction_lock "$transaction_dir" || true
      return 1
    }
    release_setup_transaction_lock "$transaction_dir" || return 1
    ok "The interrupted setup was rolled back and the previous route was verified."
    return 0
  fi

  warn "Automatic recovery was incomplete. Nothing in the retained transaction snapshot was overwritten."
  warn "Inspect/remove the attempted route, then restore the private snapshot:"
  case "$new_mode,$new_profiles" in
    tailscale,* )
      warn "  bash -c '. ./scripts/setup_lib.sh; tailscale_serve_https_off ${new_port} 0'" ;;
  esac
  while IFS='|' read -r _edge _service; do
    [ -n "$_edge" ] || continue
    case "$_edge" in
      tunnel|caddy-local|letsencrypt)
        warn "  docker compose --profile ${_edge} rm -sf ${_service}" ;;
    esac
  done < <(access_edge_retirements "$new_mode" "$new_profiles" '' '')
  warn "  cp .jarvis-setup-transaction/old.env .env"
  warn "  bash -c '. ./scripts/setup_lib.sh; restore_setup_secret_snapshot .jarvis-setup-transaction/secrets ./secrets'"
  warn "  bash scripts/init-secrets.sh"
  warn "Then re-run ./setup.sh. It will keep the snapshot until recovery verifies."
  release_setup_transaction_lock "$transaction_dir" \
    || warn "The recovery lock could not be released safely; do not delete it while another setup may be running."
  return 1
}

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

# _public_origin_host URL -> the hostname of an origin-only https:// URL whose
# host is a valid DNS name, or non-zero otherwise. A raw IP is never a valid
# WebAuthn RP-ID and cannot obtain a public certificate, so a named private-HTTPS
# origin must resolve to a hostname. A numeric TCP port is optional; paths,
# queries, fragments, userinfo, IP literals, malformed labels, and invalid ports
# are rejected rather than silently discarded.
_public_origin_host() {
  local url="$1" rest host port label remainder last_label numeric_tail
  case "$url" in https://*) rest="${url#https://}" ;; *) return 1 ;; esac
  case "$rest" in
    ''|*/*|*\?*|*\#*|*@*) return 1 ;;
  esac
  case "$rest" in
    *:*)
      host="${rest%%:*}"
      port="${rest#*:}"
      case "$port" in ''|*[!0-9]*) return 1 ;; esac
      # Strip leading zeroes before the bounded integer comparison so Bash 3.2
      # never interprets a port such as 080 as octal.
      while [ "${port#0}" != "$port" ]; do port="${port#0}"; done
      [ -n "$port" ] && [ "${#port}" -le 5 ] && [ "$port" -le 65535 ] || return 1
      ;;
    *) host="$rest" ;;
  esac
  host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  [ -n "$host" ] && [ "${#host}" -le 253 ] || return 1
  case "$host" in localhost|*.localhost) return 1 ;; esac
  case "$host" in
    .*|*.|*[!a-zA-Z0-9.-]*) return 1 ;;
    *[a-zA-Z]*) ;;
    *) return 1 ;;                           # all-numeric host / IPv4 literal
  esac
  # WHATWG URL parsers treat a numeric final label as IPv4 syntax, including
  # abbreviated and hexadecimal forms (for example 127.1 or 0x7f000001).
  # Reject that whole class so the hostname browsers use cannot differ from the
  # DNS name validated here. A numeric TLD is not a usable browser origin either.
  last_label="${host##*.}"
  case "$last_label" in *[!0-9]*) ;; *) return 1 ;; esac
  case "$last_label" in
    0[xX]*)
      numeric_tail="${last_label#??}"
      [ -n "$numeric_tail" ] || return 1
      case "$numeric_tail" in *[!0-9a-fA-F]*) ;; *) return 1 ;; esac
      ;;
  esac
  remainder="$host"
  while [ -n "$remainder" ]; do
    case "$remainder" in
      *.*) label="${remainder%%.*}"; remainder="${remainder#*.}" ;;
      *)   label="$remainder"; remainder="" ;;
    esac
    [ -n "$label" ] && [ "${#label}" -le 63 ] || return 1
    case "$label" in -*|*-|*[!a-zA-Z0-9-]*) return 1 ;; esac
  done
  printf '%s' "$host"
}

# -----------------------------------------------------------------------------
# Flag parsing  (must happen after cd "$SCRIPT_DIR" so relative paths resolve)
# -----------------------------------------------------------------------------
NON_INTERACTIVE=0
NI_DOMAIN=""
NI_ADMIN_EMAIL=""
NI_PROFILE="dev"      # dev | local-https | letsencrypt | tunnel
NI_MODE="single"      # single | multi
NI_MODE_EXPLICIT=0
RUN_DOCTOR=0
NI_SMTP_HOST=""
NI_SMTP_PORT=""
NI_SMTP_USER=""
NI_SMTP_PASS=""
NI_SMTP_FROM=""
NI_LLM_BACKEND=""     # ollama | auto (resolved before .env is written)
NI_SMART_MODEL=""     # model id; resolved by prompt_ai_backend or auto-resolve
NI_HW_TIER=""         # populated by prompt_ai_backend or auto-resolve
NI_GPU_VENDOR="none"  # nvidia | amd | intel | none; probed before the prompts
NI_GPU_OVERRIDE=""    # --gpu cuda|rocm|vulkan|cpu — overrides overlay detection
NI_ADDRESS=""         # --address <ipv4> — overrides LAN IP auto-detection
NI_PUBLIC_ORIGIN=""   # --public-origin <https-url> — a named private HTTPS origin
NI_COMPOSE_PROJECT_NAME="" # --compose-project-name — explicit install identity
NI_IMAGE_TAG=""       # --image-tag — explicit published application image identity
NI_IMAGE_TAG_EXPLICIT=0
INSTALL_PREREQS=0
SKIP_DISK_CHECK=0
BUILD_LOCAL=0         # --build-local: build app images from source instead of pulling GHCR
TUNNEL_ACK=0          # --tunnel-ack: acknowledges tunnel internet exposure (NI consent)
NI_TUNNEL_HOSTNAME="" # --tunnel-hostname: Cloudflare public hostname (NI only)
NI_TUNNEL_TOKEN_FILE="" # --tunnel-token-file: credential source (never an argv token)
NI_TAILSCALE=0        # --tailscale: select guided private-network HTTPS
OVERWRITE_ENV=0       # --overwrite-env: rebuild an existing .env (merge, non-destructive)
DOCKER_JUST_INSTALLED=0  # set by handle_missing_prereqs; gates the exit-3 path
_ENV_SNAPSHOT_TAKEN=0 # set once .env has been copied to .env.pre-setup.bak
_STACK_STARTED=0      # set once the stack is up; gates the pre-start restore hint
_REPLACEMENT_TAILSCALE_ATTEMPTED=0 # a failed client call may still mutate daemon state
_ACCESS_RECONFIGURATION_APPLY_STARTED=0 # main Compose may partially recreate an edge
_ACCESS_ROLLBACK_ATTEMPTED=0 # prevents EXIT from repeating rollback/edge cleanup
_ACCESS_TRANSACTION_COMMITTED=0 # set only after route, readiness, and retirement gates pass
_PREVIOUS_ACCESS_EDGES_QUIESCED=0 # old edges cannot answer replacement marker probes
_ENV_EXISTED_AT_START=0
[ -f .env ] && _ENV_EXISTED_AT_START=1

# Snapshot the original invocation before the parse loop consumes it via `shift`,
# so a GPU-overlay failure can re-exec on CPU with the user's other flags intact.
ORIG_ARGS=("$@")

while [ $# -gt 0 ]; do
  # A value-taking flag passed as the final argument would read an unset $2 under
  # `set -u` and abort with a raw "unbound variable". Guard them centrally so the
  # message is actionable. (The --flag=value forms carry their value inline.)
  case "$1" in
    --domain|--admin-email|--profile|--smtp-host|--smtp-port|--smtp-user|--smtp-from|--smtp-pass-file|--mode|--backend|--smart-model|--gpu|--address|--public-origin|--tunnel-hostname|--tunnel-token-file|--compose-project-name|--image-tag)
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
    --compose-project-name)
      NI_COMPOSE_PROJECT_NAME="$2"; shift 2 ;;
    --compose-project-name=*)
      NI_COMPOSE_PROJECT_NAME="${1#*=}"; shift ;;
    --image-tag)
      NI_IMAGE_TAG="$2"; NI_IMAGE_TAG_EXPLICIT=1; shift 2 ;;
    --image-tag=*)
      NI_IMAGE_TAG="${1#*=}"; NI_IMAGE_TAG_EXPLICIT=1; shift ;;
    --build-local)
      BUILD_LOCAL=1; shift ;;
    --tunnel-ack)
      TUNNEL_ACK=1; shift ;;
    --tunnel-hostname)
      NI_TUNNEL_HOSTNAME="$2"; shift 2 ;;
    --tunnel-hostname=*)
      NI_TUNNEL_HOSTNAME="${1#*=}"; shift ;;
    --tunnel-token-file)
      [ -f "$2" ] && [ -r "$2" ] || die "--tunnel-token-file: cannot read regular file '$2'." "Point it at a readable file whose first line is the Cloudflare tunnel token."
      NI_TUNNEL_TOKEN_FILE="$2"; shift 2 ;;
    --tunnel-token-file=*)
      _ttf="${1#*=}"
      [ -f "$_ttf" ] && [ -r "$_ttf" ] || die "--tunnel-token-file: cannot read regular file '$_ttf'." "Point it at a readable file whose first line is the Cloudflare tunnel token."
      NI_TUNNEL_TOKEN_FILE="$_ttf"; shift ;;
    --tailscale)
      NI_TAILSCALE=1; shift ;;
    --overwrite-env)
      OVERWRITE_ENV=1; shift ;;
    --backend)
      case "$2" in
        ollama|auto) NI_LLM_BACKEND="$2"; shift 2 ;;
        vllm) die "--backend vllm is not an installer backend." \
          "Setup configures Ollama. vLLM remains a manual benchmark overlay; configure it separately after installation." ;;
        *) die "Invalid --backend '$2'. Expected: ollama|auto" "Run: $0 --help" ;;
      esac ;;
    --backend=*)
      _v="${1#*=}"
      case "$_v" in
        ollama|auto) NI_LLM_BACKEND="$_v"; shift ;;
        vllm) die "--backend vllm is not an installer backend." \
          "Setup configures Ollama. vLLM remains a manual benchmark overlay; configure it separately after installation." ;;
        *) die "Invalid --backend '$_v'. Expected: ollama|auto" "Run: $0 --help" ;;
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
        || die "--public-origin must be exactly https://DNS-host[:port] (no path, query, fragment, userinfo, or IP literal)." "Example: --public-origin https://jarvis.example.ts.net"
      shift 2 ;;
    --public-origin=*)
      NI_PUBLIC_ORIGIN="${1#*=}"
      _public_origin_host "$NI_PUBLIC_ORIGIN" >/dev/null \
        || die "--public-origin must be exactly https://DNS-host[:port] (no path, query, fragment, userinfo, or IP literal)." "Example: --public-origin https://jarvis.example.ts.net"
      shift ;;
    -h|--help)
      sed -n '/^# setup.sh/,/^set -euo/{ /^#/!d; s/^# \{0,1\}//p; }' "$0"
      exit 0
      ;;
    *)
      die "Unknown flag: $1" "Run: $0 --help"
      ;;
  esac
done

if ! CHECKOUT_APP_VERSION="$(resolve_checkout_app_version)"; then
  die "Could not determine a valid application version from this checkout." \
      "Use an exact vMAJOR.MINOR.PATCH[-PRERELEASE] tag, or fix [project].version in pyproject.toml."
fi

SELECTED_IMAGE_TAG="$CHECKOUT_APP_VERSION"
_installed_app_version="$(existing_env_value JARVIS_VERSION || true)"
_installed_image_tag="$(existing_env_value JARVIS_IMAGE_TAG || true)"
if [ -n "$_installed_app_version" ] && ! app_version_is_valid "$_installed_app_version"; then
  die "The existing JARVIS_VERSION in .env is invalid." \
      "Set it to a version such as ${CHECKOUT_APP_VERSION}, then re-run setup."
fi
if [ -n "$_installed_image_tag" ] && ! image_tag_is_valid "$_installed_image_tag"; then
  die "The existing JARVIS_IMAGE_TAG in .env is invalid." \
      "Set it to a stable, prerelease, or lowercase 40-hex tag, then re-run setup."
fi
_installed_image_tag="${_installed_image_tag:-$_installed_app_version}"
if [ "$NI_IMAGE_TAG_EXPLICIT" -eq 1 ]; then
  image_tag_is_valid "$NI_IMAGE_TAG" \
    || die "Invalid --image-tag '${NI_IMAGE_TAG}'." \
      "Use X.Y.Z, X.Y.Z-prerelease, or a lowercase 40-hex commit."
  if [ -n "$_installed_image_tag" ] && [ "$NI_IMAGE_TAG" != "$_installed_image_tag" ]; then
    die "--image-tag '${NI_IMAGE_TAG}' does not match this existing install." \
      "Use jarvis-research update to change an installed application's image identity."
  fi
  SELECTED_IMAGE_TAG="$NI_IMAGE_TAG"
elif [ -n "$_installed_image_tag" ]; then
  SELECTED_IMAGE_TAG="$_installed_image_tag"
fi

# _validate_compose_project_request REPO REQUEST — return 2 for an invalid
# request, 3 for an existing-install mismatch, or 4 for invalid persisted state.
_validate_compose_project_request() {
  local repo="$1" requested="$2" current
  [ -n "$requested" ] || return 0
  compose_project_name_is_valid "$requested" || return 2
  [ -f "$repo/.env" ] || return 0
  current="$(_lifecycle_compose_project_name "$repo")" || return 4
  [ "$requested" = "$current" ] || return 3
}

# _persist_compose_project_request NAME — add or normalize an explicit identity.
_persist_compose_project_request() {
  local requested="$1" current=""
  [ -n "$requested" ] || return 0
  current="$(existing_env_value COMPOSE_PROJECT_NAME || true)"
  [ "$current" = "$requested" ] || upsert_env_var COMPOSE_PROJECT_NAME "$requested"
}

_compose_project_rc=0
_validate_compose_project_request "$SCRIPT_DIR" "$NI_COMPOSE_PROJECT_NAME" \
  || _compose_project_rc=$?
case "$_compose_project_rc" in
  0) ;;
  2) die "Invalid --compose-project-name '${NI_COMPOSE_PROJECT_NAME}'." \
       "Use lowercase letters, digits, underscores, and hyphens; start with a letter or digit." ;;
  3) die "--compose-project-name '${NI_COMPOSE_PROJECT_NAME}' does not match this existing install." \
       "Use the project identity already defined by .env or this checkout's directory name." ;;
  *) die "The existing Compose project identity is invalid." \
       "Repair COMPOSE_PROJECT_NAME in .env before re-running setup." ;;
esac

# Validate --profile value.
case "$NI_PROFILE" in
  dev|local-https|letsencrypt|tunnel) ;;
  *) die "Invalid --profile '$NI_PROFILE'. Expected: dev, local-https, letsencrypt, or tunnel." \
         "Run: $0 --help" ;;
esac

case "$NI_MODE" in
  single|multi) ;;
  *) die "Invalid --mode '$NI_MODE'. Expected: single or multi." "Run: $0 --help" ;;
esac

if [ "$NI_PROFILE" = "tunnel" ] && [ "$NON_INTERACTIVE" -ne 1 ]; then
  die "--profile=tunnel is the unattended setup path and requires --non-interactive." \
    "For guided setup, run ./setup.sh and choose Cloudflare Tunnel from the access menu."
fi
if [ "$NI_TAILSCALE" -eq 1 ] && [ "$NI_PROFILE" != "dev" ]; then
  die "--tailscale cannot be combined with --profile=${NI_PROFILE}." \
    "Choose one HTTPS access route per setup run."
fi
if { [ -n "$NI_TUNNEL_HOSTNAME" ] || [ -n "$NI_TUNNEL_TOKEN_FILE" ]; } \
   && [ "$NI_PROFILE" != "tunnel" ]; then
  die "Tunnel hostname/token flags require --profile=tunnel." \
    "Use all tunnel flags together with --non-interactive --profile=tunnel."
fi

# letsencrypt requires both --domain and --admin-email.
if [ "$NON_INTERACTIVE" -eq 1 ] && [ "$NI_PROFILE" = "letsencrypt" ]; then
  [ -n "$NI_DOMAIN" ]      || die "--profile=letsencrypt requires --domain."      "Provide: --domain=jarvis.example.com"
  [ -n "$NI_ADMIN_EMAIL" ] || die "--profile=letsencrypt requires --admin-email." "Provide: --admin-email=you@example.com"
fi

if [ "$NON_INTERACTIVE" -eq 1 ] && [ "$NI_PROFILE" = "tunnel" ]; then
  [ "$TUNNEL_ACK" -eq 1 ] || die "--profile=tunnel requires --tunnel-ack." \
    "Configure Cloudflare Zero Trust access policies before acknowledging internet exposure."
  [ -n "$NI_TUNNEL_HOSTNAME" ] || die "--profile=tunnel requires --tunnel-hostname." \
    "Provide the public DNS hostname configured on the tunnel."
  case "$NI_TUNNEL_HOSTNAME" in
    *:*|*/*) die "--tunnel-hostname must be a DNS hostname without a scheme, port, or path." \
      "Example: --tunnel-hostname=jarvis.example.com" ;;
  esac
  _validated_tunnel_host="$(_public_origin_host "https://${NI_TUNNEL_HOSTNAME}" || true)"
  [ -n "$_validated_tunnel_host" ] || die "--tunnel-hostname must be a valid DNS hostname." \
    "Example: --tunnel-hostname=jarvis.example.com"
  NI_TUNNEL_HOSTNAME="$_validated_tunnel_host"
  [ -n "$NI_TUNNEL_TOKEN_FILE" ] || die "--profile=tunnel requires --tunnel-token-file." \
    "Store the token in an owner-readable file, then pass its path."
fi

if [ "$RUN_DOCTOR" -eq 1 ]; then run_doctor; exit $?; fi


missing_prereqs() {
  local missing=() compose_ver=""
  command -v docker >/dev/null 2>&1 || missing+=(docker)
  if command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
      compose_ver="$(docker compose version --short 2>/dev/null || printf unknown)"
      compose_meets_floor "$compose_ver" "$COMPOSE_MIN" || missing+=(docker-compose)
    else
      missing+=(docker-compose)
    fi
  else
    missing+=(docker-compose)
  fi
  command -v openssl >/dev/null 2>&1 || missing+=(openssl)
  command -v curl >/dev/null 2>&1 || missing+=(curl)
  # python3 is a hard install-path prerequisite, not just a dev tool: the model
  # selection and disk-sizing helpers in setup_lib.sh shell out to it, and under
  # `set -euo pipefail` its absence aborts mid-install at the first such call.
  command -v python3 >/dev/null 2>&1 || missing+=(python3)
  if [ "$NI_PROFILE" = "local-https" ] && ! mkcert_toolchain_available; then
    missing+=(mkcert)
  fi
  printf '%s\n' "${missing[@]}"
}

_os_release_value() {
  local key="$1" file="${JARVIS_OS_RELEASE_FILE:-/etc/os-release}"
  local line value
  [ -r "$file" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      "$key="*)
        value="${line#*=}"
        # The three fields consumed below are identifier tokens. Strip their
        # optional os-release quotes, then let each caller validate the token;
        # never source or eval host-controlled file content.
        value="${value#\"}"; value="${value%\"}"
        value="${value#\'}"; value="${value%\'}"
        printf '%s' "$value"
        return 0
        ;;
    esac
  done < "$file"
  return 1
}

_host_os_id() {
  local value
  value="$(_os_release_value ID 2>/dev/null || true)"
  case "$value" in
    ''|*[!a-z0-9._-]*) printf 'unknown' ;;
    *) printf '%s' "$value" ;;
  esac
}

_host_os_codename() {
  local os_id value
  os_id="$(_host_os_id)"
  case "$os_id" in
    linuxmint|pop|popos)
      value="$(_os_release_value UBUNTU_CODENAME 2>/dev/null || true)"
      [ -n "$value" ] || value="$(_os_release_value VERSION_CODENAME 2>/dev/null || true)"
      ;;
    *) value="$(_os_release_value VERSION_CODENAME 2>/dev/null || true)" ;;
  esac
  case "$value" in
    ''|*[!a-z0-9._-]*) return 1 ;;
    *) printf '%s' "$value" ;;
  esac
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

_tailscale_install_plan_for_host() {
  local os os_id codename has_apt=0 has_dnf=0 has_systemd=0
  os="$(uname -s 2>/dev/null || printf unknown)"
  os_id="$(_host_os_id)"
  codename="$(_host_os_codename)"
  command -v apt-get >/dev/null 2>&1 && has_apt=1
  command -v dnf >/dev/null 2>&1 && has_dnf=1
  if command -v systemctl >/dev/null 2>&1 \
     && [ -d "${JARVIS_SYSTEMD_DIR:-/run/systemd/system}" ]; then
    has_systemd=1
  fi
  tailscale_install_plan "$os" "$os_id" "$codename" "$has_apt" "$has_dnf" "$has_systemd"
}

_run_prereq_plan() {
  local plan="$1" noninteractive="${2:-0}" cmd run_cmd effective_uid="${EUID:-}"
  [ -n "$effective_uid" ] || effective_uid="$(id -u)"
  while IFS= read -r cmd; do
    [ -n "$cmd" ] || continue
    run_cmd="$(rewrite_prereq_command "$cmd" "$noninteractive" "$effective_uid")"
    info "Running: $run_cmd"
    bash -c "$run_cmd" || return $?
  done <<< "$plan"
}

install_tailscale_for_access() {
  command -v tailscale >/dev/null 2>&1 && return 0

  local plan="" reply=""
  if ! plan="$(_tailscale_install_plan_for_host)" || [ -z "$plan" ]; then
    warn "Automatic Tailscale installation is not available on this host. Localhost setup will keep working."
    warn "Install the client from https://tailscale.com/download, then re-run ./setup.sh --tailscale --overwrite-env."
    return 1
  fi

  printf '%sGuided Tailscale installer would run:%s\n%s\n' "$C_BOLD" "$C_RESET" "$plan" >&2

  if [ "$INSTALL_PREREQS" -ne 1 ]; then
    if [ "$NON_INTERACTIVE" -eq 1 ] || [ ! -t 0 ]; then
      warn "Tailscale is missing. Re-run with --install-prereqs after reviewing the commands above; localhost setup will keep working."
      return 1
    fi
    read -rp "Install Tailscale with these commands now? [y/N]: " reply
    case "$reply" in
      [yY]|[yY][eE][sS]) ;;
      *)
        warn "Tailscale installation was skipped. Localhost setup will keep working."
        return 1
        ;;
    esac
  fi

  if ! _run_prereq_plan "$plan" "$NON_INTERACTIVE"; then
    warn "Tailscale installation failed. Localhost setup will keep working; review the command error above and retry later."
    return 1
  fi

  hash -r
  if ! command -v tailscale >/dev/null 2>&1; then
    warn "The package install completed but the tailscale command is not in PATH. Localhost setup will keep working."
    return 1
  fi
  ok "Tailscale installed."
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
    hash -r
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
      hash -r
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

  local compose_ver
  compose_ver="$(docker compose version --short 2>/dev/null || printf unknown)"
  compose_meets_floor "$compose_ver" "$COMPOSE_MIN" \
    || die "Docker Compose v${compose_ver#v} is too old or unreadable; v${COMPOSE_MIN}+ is required." \
           "Update Docker Desktop or the docker-compose-plugin, then re-run ./setup.sh --check."

  command -v openssl >/dev/null 2>&1 \
    || die "openssl required for secret generation." \
           "$(prereq_manual_guidance openssl)"

  command -v curl >/dev/null 2>&1 \
    || die "curl required for downloads and health checks." \
           "$(prereq_manual_guidance curl)"

  command -v python3 >/dev/null 2>&1 \
    || die "python3 required for model selection and disk sizing." \
           "$(prereq_manual_guidance python3)"

  if [ "$NI_PROFILE" = "local-https" ] && ! mkcert_toolchain_available; then
    die "local HTTPS requires mkcert and browser trust tooling." \
        "$(prereq_manual_guidance mkcert)"
  fi
}

prepare_local_https() {
  if ! mkcert_toolchain_available; then
    handle_missing_prereqs mkcert
  fi
  mkcert_toolchain_available \
    || die "local HTTPS requires mkcert and browser trust tooling." \
           "$(prereq_manual_guidance mkcert)"
  info "Creating and trusting the local HTTPS certificate..."
  if ! bash scripts/init-mkcert.sh; then
    die "Local HTTPS certificate setup failed." \
        "Review the error above. 'make certs' is the manual repair command; then re-run setup."
  fi
  ok "Local HTTPS certificate and trust are ready."
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

# Serialize host-side setup processes immediately. Existing installs add the
# private named-volume lease before recovery; fresh installs add it after disk
# preflight and before any config, secret, or service mutation.
_SETUP_LIFECYCLE_CLAIMED=0
_SETUP_MUTATION_STARTED=0
_setup_lock_rc=0
claim_host_lifecycle_lock "$SCRIPT_DIR" || _setup_lock_rc=$?
case "$_setup_lock_rc" in
  0) ;;
  3) die "Another JARVIS lifecycle operation is already running." \
       "Wait for it to finish, then re-run ./setup.sh" ;;
  *) die "The per-install lifecycle lock is unavailable or unsafe." \
       "Check ${JARVIS_CLI_CONFIG_DIR:-${HOME}/.config/jarvis-research}, then re-run ./setup.sh" ;;
esac

claim_setup_volume_lease() {
  [ "${_SETUP_LIFECYCLE_CLAIMED:-0}" -ne 1 ] || return 0
  local rc=0
  claim_lifecycle_operation "$SCRIPT_DIR" setup "$NI_COMPOSE_PROJECT_NAME" \
    || rc=$?
  case "$rc" in
    0) _SETUP_LIFECYCLE_CLAIMED=1 ;;
    3|4) die "Another lifecycle operation is active or needs recovery." \
           "Finish that operation, then re-run ./setup.sh" ;;
    *) die "The private lifecycle volume is unavailable or unsafe." \
         "Check Docker and this install's postgres_backups volume, then re-run ./setup.sh" ;;
  esac
}

cleanup_setup_lifecycle_exit() {
  local exit_rc=$? action=clear
  trap - EXIT
  if [ "${_SETUP_LIFECYCLE_CLAIMED:-0}" -eq 1 ]; then
    [ "${_SETUP_MUTATION_STARTED:-0}" -ne 1 ] || action=retain
    [ "$exit_rc" -ne 0 ] || action=clear
    finish_lifecycle_operation "$SCRIPT_DIR" setup "$action" 2>/dev/null || true
  fi
  exit "$exit_rc"
}
trap cleanup_setup_lifecycle_exit EXIT

# docker compose v2+ (space form). `docker-compose` (hyphen) is v1 and
# unsupported. ensure_prerequisites proved the plugin runs; pin a REAL floor
# rather than accepting any v2: the accelerator overlays merge a dev override's
# `deploy: !reset null`, and the `!reset`/`!override` merge tags require Docker
# Compose 2.24.4+ (Docker's compose-file merge reference). An older plugin
# silently ignores the tags and mis-merges the overlay.
COMPOSE_VER="$(docker compose version --short 2>/dev/null || echo 'unknown')"
compose_meets_floor "$COMPOSE_VER" "$COMPOSE_MIN" && _cmf=0 || _cmf=$?
case "$_cmf" in
  0) ok "Docker Compose v${COMPOSE_VER#v}" ;;
  *) die "Docker Compose v${COMPOSE_VER#v} is too old or unreadable; v${COMPOSE_MIN}+ is required." \
         "Update Docker Desktop or the docker-compose-plugin, then re-run ./setup.sh --check." ;;
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

if [ "$_ENV_EXISTED_AT_START" -eq 1 ]; then
  claim_setup_volume_lease
fi

# SIGKILL cannot run EXIT traps. Reconcile the durable private journal before
# the current .env can be treated as a new baseline or its backup overwritten.
recover_interrupted_setup_transaction \
  || die "The interrupted setup transaction could not be recovered automatically." \
      "Follow the recovery commands above; do not delete .jarvis-setup-transaction until the old route works."

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

# Reconfiguration is a replacement of the selected access edge, not an
# additive merge. Keep the prior identity long enough to retire only the edge
# this JARVIS setup previously owned after the replacement is verified.
_PREVIOUS_ACCESS_MODE="$(existing_env_value JARVIS_ACCESS_MODE || true)"
_PREVIOUS_COMPOSE_PROFILES="$(existing_env_value COMPOSE_PROFILES || true)"
_PREVIOUS_TAILSCALE_PORT="$(existing_env_value DASHBOARD_TRUSTED_HOST_PORT || printf '3003')"
_PREVIOUS_APP_BASE_URL="$(existing_env_value APP_BASE_URL || true)"
_PREVIOUS_DASHBOARD_HOST_PORT="$(existing_env_value DASHBOARD_HOST_PORT || printf '3001')"

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

DASHBOARD_TRUSTED_HOST_PORT_RESOLVED="$(_port_or_default DASHBOARD_TRUSTED_HOST_PORT 3003)"

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
  tunnel) _PRECHECK_PROFILES+=(tunnel) ;;
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
  if [ -f .env ] && docker compose ps -q 2>/dev/null | grep -q .; then
    info "Ports already in use: ${PORTS_IN_USE[*]}. Existing JARVIS services are expected on a re-run; Compose will report any real conflict."
  else
    warn "Ports already in use: ${PORTS_IN_USE[*]}. Services on these ports may conflict on startup."
  fi
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
    case ",$(existing_env_value COMPOSE_PROFILES || true)," in
      *,caddy-local,*) prepare_local_https ;;
    esac
    # An .env written before 1.1 carries no TORCH_VARIANT, so the image tag
    # would resolve to the CPU flavour even on a CUDA host whose GPU overlay is
    # still recorded in COMPOSE_FILE. Backfill before anything resolves an image.
    _keep_app_version="$(existing_env_value JARVIS_VERSION || true)"
    _SETUP_MUTATION_STARTED=1
    if [ -z "$_keep_app_version" ]; then
      _keep_app_version="$CHECKOUT_APP_VERSION"
      upsert_env_var JARVIS_VERSION "$_keep_app_version"
      info "Recorded this install's application version in .env: ${_keep_app_version}"
    fi
    _keep_image_tag="$(existing_env_value JARVIS_IMAGE_TAG || true)"
    if [ -z "$_keep_image_tag" ]; then
      _keep_image_tag="$SELECTED_IMAGE_TAG"
      upsert_env_var JARVIS_IMAGE_TAG "$_keep_image_tag"
      info "Recorded this install's application image tag in .env: ${_keep_image_tag}"
    fi
    export JARVIS_VERSION="$_keep_app_version"
    export JARVIS_IMAGE_TAG="$_keep_image_tag"
    _persist_compose_project_request "$NI_COMPOSE_PROJECT_NAME"
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

    # This branch is also the interrupted-install resume path: setup may have
    # written .env successfully and then lost the network or Docker transport
    # during the image/model pull. Do not report success immediately after
    # `up`; finish the same health, first-admin, and readiness guidance that a
    # clean first run provides.
    _keep_profiles_csv="$(existing_env_value COMPOSE_PROFILES || true)"
    KEEP_ACTIVE_PROFILES=()
    if [ -n "$_keep_profiles_csv" ]; then
      IFS=',' read -ra KEEP_ACTIVE_PROFILES <<< "$_keep_profiles_csv"
    elif [ "$_keep_telegram" -eq 1 ]; then
      KEEP_ACTIVE_PROFILES=(telegram)
    fi
    read -ra KEEP_MANDATORY_SVCS <<< "$(mandatory_health_services "$MANDATORY_HEALTH_BASE" ${KEEP_ACTIVE_PROFILES[@]+"${KEEP_ACTIVE_PROFILES[@]}"})"
    info "Stack started; waiting for mandatory services to become healthy..."
    KEEP_FAILED=()
    for svc in "${KEEP_MANDATORY_SVCS[@]}"; do
      case "$svc" in
        ollama)                          _keep_budget=180 ;;
        paper_ingestion|learning_engine) _keep_budget=3600 ;;
        langfuse)                        _keep_budget=240 ;;
        *)                               _keep_budget=60 ;;
      esac
      if ! _wait_for_setup_service "$svc" "$_keep_budget"; then
        KEEP_FAILED+=("$svc")
        docker compose logs --tail 50 "$svc" >&2 || true
      fi
    done
    if [ "${#KEEP_FAILED[@]}" -gt 0 ]; then
      die "The resumed setup did not become healthy: ${KEEP_FAILED[*]}" \
          "Inspect: docker compose logs --tail=200 ${KEEP_FAILED[*]}; then re-run ./setup.sh"
    fi

    _keep_dashboard_port="$(_port_or_default DASHBOARD_HOST_PORT 3001)"
    _keep_dashboard_url="http://localhost:${_keep_dashboard_port}"
    _keep_access_mode="$(existing_env_value JARVIS_ACCESS_MODE || true)"
    _keep_public_origin="$(existing_env_value APP_BASE_URL || true)"
    _keep_route_kind="$(selected_https_route "$_keep_access_mode" "$_keep_profiles_csv" "$_keep_public_origin")"
    _keep_edge_state="unavailable"
    if [ "$_keep_route_kind" = "local-https" ]; then
      _keep_edge_state="$(probe_local_https_app \
        "https://localhost:3443/health/jarvis")"
      if [ "$_keep_edge_state" = "verified" ]; then
        _keep_dashboard_url="https://localhost:3443"
      else
        warn "The configured local HTTPS address is not trusted and verified yet. Re-run setup with --profile=local-https; use 'make certs' only for manual repair."
      fi
    elif [ "$_keep_route_kind" != "none" ] && [[ "$_keep_public_origin" == https://* ]]; then
      _keep_edge_state="$(probe_external_app "${_keep_public_origin}/health/jarvis")"
      if [ "$_keep_edge_state" = "verified" ]; then
        _keep_dashboard_url="$_keep_public_origin"
      else
        warn "The configured HTTPS address is not verified yet. Finish setup on localhost; existing data is safe."
      fi
    elif [ "$_keep_route_kind" != "none" ]; then
      warn "The selected HTTPS mode has no valid APP_BASE_URL to verify. Localhost remains usable."
    fi
    resolve_setup_browser_route "$_keep_dashboard_url" "$_keep_dashboard_port" \
      || die "Windows could not reach the local JARVIS dashboard." \
             "Follow the Windows forwarding steps above, then re-run this launcher"
    _keep_dashboard_url="$SETUP_BROWSER_BASE"

    _keep_mode="$(existing_env_value JARVIS_SETUP_MODE || printf 'single')"
    _keep_configured="unknown"
    _keep_setup_completed="unknown"
    _keep_status_json="$(curl -fsS --max-time 10 "http://127.0.0.1:${_keep_dashboard_port}/api/setup/status" 2>/dev/null || true)"
    if _keep_status_fields="$(printf '%s' "$_keep_status_json" | parse_setup_status_json 2>/dev/null)"; then
      read -r _keep_configured _keep_setup_completed _keep_mode <<< "$_keep_status_fields"
    else
      warn "The stack is healthy, but its setup state could not be read. The dashboard will show the correct next step."
    fi

    _keep_key_file=""
    if [ "$_keep_mode" = "single" ]; then
      _keep_api_key="$(existing_env_value JARVIS_API_KEY || true)"
      [ -n "$_keep_api_key" ] \
        || die "JARVIS_API_KEY is missing from the existing .env." \
               "Restore the original .env or backup before re-running setup"
      _keep_key_file="$(materialize_api_key_file "$_keep_api_key")" \
        || die "Could not write the local API-key file." \
               "Check permissions on ${HOME}/.config/jarvis, then re-run ./setup.sh"
    fi

    case "$_keep_configured:$_keep_setup_completed" in
      false:*) printf '\n%sInstallation is healthy and ready for first-time setup.%s\n' "$C_BOLD" "$C_RESET" ;;
      true:false) printf '\n%sJARVIS is healthy; signed-in onboarding is not finished yet.%s\n' "$C_BOLD" "$C_RESET" ;;
      true:true) printf '\n%sJARVIS is healthy and its in-app onboarding is complete.%s\n' "$C_BOLD" "$C_RESET" ;;
      *) printf '\n%sJARVIS is healthy.%s\n' "$C_BOLD" "$C_RESET" ;;
    esac
    printf '  Dashboard:    %s\n' "$_keep_dashboard_url"
    if [ "$_keep_configured" = "false" ]; then
      printf '  First-admin link: deferred until the route and readiness checks pass.\n'
    elif [ "$_keep_configured" = "true" ]; then
      if [ "$_keep_mode" = "multi" ]; then
        if [ "$SETUP_BROWSER_IS_SHARED" -eq 1 ]; then
          printf '  Next: sign in using the method you already configured.'
          [ "$_keep_setup_completed" = "false" ] && printf ' Then continue the onboarding wizard.'
          printf '\n'
        else
          printf '  Next: sign in on this computer. Before sharing JARVIS, configure one named HTTPS address.\n'
          printf '        Do not invite family members or enrol passkeys at this temporary address.\n'
        fi
      else
        printf '  Next: sign in with the API key stored in %s.' "$_keep_key_file"
        [ "$_keep_setup_completed" = "false" ] && printf ' Then continue the onboarding wizard.'
        printf '\n'
      fi
    else
      printf '  Next: open the dashboard; it will show whether to finish setup or sign in.\n'
    fi

    _keep_readiness="$SCRIPT_DIR/scripts/production-readiness-check.sh"
    if [ -f "$_keep_readiness" ]; then
      printf '\n%s--- Production Readiness Check -----------------------------------%s\n' "$C_BOLD" "$C_RESET"
      _keep_environment="$(existing_env_value ENVIRONMENT || printf 'development')"
      _keep_rc=0
      bash "$_keep_readiness" || _keep_rc=$?
      case "$(readiness_verdict "$_keep_rc" "$_keep_environment")" in
        ok) ok "Production readiness: all checks passed." ;;
        warn) warn "Production readiness passed with warnings; review the table above." ;;
        abort) die "Production readiness found a blocking issue." "Fix the issue above, then re-run ./setup.sh" ;;
      esac
    else
      warn "scripts/production-readiness-check.sh not found — skipping readiness check."
    fi

    install_cli_shim "$SCRIPT_DIR" || warn "Could not install the jarvis-research launcher (non-fatal)."
    if ! selected_https_is_verified "$_keep_route_kind" "$_keep_edge_state"; then
      _keep_route_label="HTTPS"
      _keep_retry="./setup.sh"
      case "$_keep_route_kind" in
        tailscale)
          _keep_route_label="Tailscale Serve"
          _keep_retry="./setup.sh --tailscale --overwrite-env"
          ;;
        tunnel)      _keep_route_label="Cloudflare Tunnel" ;;
        letsencrypt) _keep_route_label="Let's Encrypt" ;;
        local-https)
          _keep_route_label="local HTTPS"
          _keep_retry="./setup.sh --profile=local-https --overwrite-env"
          ;;
        private)     _keep_route_label="named private HTTPS" ;;
      esac
      unverified_https_exit "$_keep_route_label" "$_keep_public_origin" "$_keep_dashboard_port" "$_keep_retry"
    fi

    if [ "$_keep_configured" = "false" ]; then
      present_setup_link "$_keep_dashboard_url" "$_keep_dashboard_port" \
        || die "Could not prepare a safe first-admin link." \
               "Re-run ./setup.sh; do not copy the setup token to a LAN address"
      if [ "$_keep_mode" = "multi" ]; then
        printf '  Next: open the "Finish setup" link above to create the first admin; no SMTP is required.\n'
        if [ "$SETUP_BROWSER_IS_SHARED" -eq 1 ]; then
          printf '        That admin can manage users at %s/admin/users.\n' "$SETUP_BROWSER_BASE"
        else
          printf '        Do not invite family members or enrol passkeys at this temporary address.\n'
          printf '        First configure shared HTTPS: ./setup.sh --tailscale --overwrite-env\n'
        fi
      else
        printf '  Next: open the "Finish setup" link above. Your API key is ready at %s.\n' "$_keep_key_file"
      fi
    fi

    ok "All mandatory services are healthy. To rebuild .env, re-run with --overwrite-env."
    finish_lifecycle_operation "$SCRIPT_DIR" setup \
      || die "Setup finished, but its lifecycle state could not be cleared." \
        "Check this install's postgres_backups volume before running another lifecycle command."
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
#   letsencrypt with a domain         → localhost bootstrap plus the Caddy/ACME
#                                        profile handled below
# -----------------------------------------------------------------------------
if [ "$NON_INTERACTIVE" -eq 1 ]; then
  if [ "$NI_TAILSCALE" -eq 1 ]; then
    access_mode="3"
  else case "$NI_PROFILE" in
    tunnel)
      access_mode="4"
      ;;
    letsencrypt)
      access_mode="1"    # LETSENCRYPT_DOMAIN/EMAIL are written below; no Cloudflare
      ;;
    *)
      access_mode="1"    # localhost/local-https always bind locally
      ;;
  esac
  fi
else
  printf '\n%sHow will you access JARVIS?%s\n' "$C_BOLD" "$C_RESET"
  cat <<'EOF'
  1) On this computer only (recommended to start)
     Everything works here: sign-in links, passkeys (fingerprint/face/PIN).
  2) Check JARVIS status from your home or lab network
     Exposes a plain-HTTP health check on your trusted LAN. Setup, sign-in,
     and passkeys stay on localhost or a verified HTTPS address.
  3) From your private Tailscale network (recommended for family)
     Full sign-in and passkey support without opening router ports. Setup
     uses an existing Tailscale login and configures the HTTPS address.
  4) From anywhere — Cloudflare Tunnel (free, no router changes)
     Can support sign-in and passkeys after setup verifies the public address.
     Needs a free Cloudflare account and a tunnel token (guided).
  5) From anywhere — your own domain with Let's Encrypt
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
2) A family or team (multi-user — passkeys or one-time links; SMTP is optional)
EOF
  read -rp "Choice [1]: " _m; case "${_m:-1}" in 2) NI_MODE="multi" ;; *) NI_MODE="single" ;; esac
fi

# Auto-resolve backend when non-interactive and --backend not specified.
if [ "${NI_LLM_BACKEND:-auto}" = "auto" ] && [ "$NON_INTERACTIVE" -eq 1 ]; then
  NI_HW_TIER=$(detect_hw_tier)
  NI_LLM_BACKEND="ollama"
  [ -z "${NI_SMART_MODEL:-}" ] && NI_SMART_MODEL=$(_default_model_for_tier "$NI_HW_TIER" "$NI_LLM_BACKEND")
fi

# Interactive: report the detected choice unless the caller explicitly selected
# Ollama. There is no second installer backend to choose here.
if [ "$NON_INTERACTIVE" -eq 0 ] \
    && { [ -z "${NI_LLM_BACKEND:-}" ] || [ "$NI_LLM_BACKEND" = "auto" ]; }; then
  prompt_ai_backend
fi

# An explicit --backend ollama still needs the same tier/model defaults. Keep a
# caller's --smart-model, but never persist an empty installer backend/model.
[ -n "$NI_HW_TIER" ] || NI_HW_TIER=$(detect_hw_tier)
[ -n "$NI_LLM_BACKEND" ] || NI_LLM_BACKEND="ollama"
[ -n "$NI_SMART_MODEL" ] \
  || NI_SMART_MODEL=$(_default_model_for_tier "$NI_HW_TIER" ollama)

# The bootstrap that actually pulls models is Ollama on every setup.sh path, so a
# HuggingFace repo id (contains '/') is not a valid tag and would hard-fail
# ollama-bootstrap. Reject it early with the fix.
case "${NI_SMART_MODEL:-}" in
  */*) die "--smart-model '${NI_SMART_MODEL}' looks like a HuggingFace id, but setup starts Ollama." \
           "Use an Ollama tag such as qwen3:14b. vLLM is a separate manual benchmark overlay." ;;
esac

# Disk preflight — sized to the smart model chosen above, so it must run after
# the backend/model resolution and before anything pulls or builds.
preflight_disk

# A missing .env can also mean an interrupted or manually damaged existing
# install. Resolve the Compose-owned backup volume from the repository model
# and enter the shared lifecycle domain before writing config or secrets. The
# helper is idempotent for the existing-install path, which claimed earlier so
# transaction recovery was protected too.
claim_setup_volume_lease

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
    warn "LAN diagnostics bind port 3001 to every interface, but remote clients can reach only /health/jarvis. Setup, sign-in, and the dashboard stay on localhost or verified HTTPS."
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
    ACCESS_MODE_LABEL="tailscale"
    DASHBOARD_BIND_HOST="127.0.0.1"
    if ! command -v tailscale >/dev/null 2>&1; then
      install_tailscale_for_access || true
    fi
    if command -v tailscale >/dev/null 2>&1; then
      if ! TAILSCALE_HOSTNAME="$(tailscale_dns_name)"; then
        if [ "$NON_INTERACTIVE" -eq 1 ] || [ ! -t 0 ]; then
          warn "Tailscale is installed but not connected. Localhost setup will keep working. Run 'sudo tailscale up', then re-run with --tailscale --overwrite-env."
        else
          printf 'Tailscale needs one account sign-in. The next command opens its browser login:\n  sudo tailscale up\n'
          read -rp "Run it now? [y/N]: " _ts_up
          case "${_ts_up:-N}" in
            y|Y|yes|YES)
              sudo tailscale up || true
              TAILSCALE_HOSTNAME="$(tailscale_dns_name || true)"
              ;;
          esac
          if [ -z "${TAILSCALE_HOSTNAME:-}" ]; then
            warn "Tailscale is not connected. Continuing with localhost; after 'sudo tailscale up', re-run ./setup.sh --tailscale --overwrite-env."
          fi
        fi
      fi
      if [ -n "${TAILSCALE_HOSTNAME:-}" ]; then
        NI_PUBLIC_ORIGIN="https://${TAILSCALE_HOSTNAME}"
        ok "Tailscale name detected: ${TAILSCALE_HOSTNAME}"
      fi
    else
      warn "Tailscale was selected but is not installed. Localhost remains available while setup records the incomplete HTTPS route."
    fi
    ;;
  4)
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
Set its public-hostname service URL to:
  http://dashboard:3002
EOF
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
      # Validation above guarantees a readable file. Read only its first line;
      # the credential never appears in argv or terminal output.
      IFS= read -r CLOUDFLARE_TUNNEL_TOKEN < "$NI_TUNNEL_TOKEN_FILE" || true
      CLOUDFLARE_TUNNEL_TOKEN="${CLOUDFLARE_TUNNEL_TOKEN%$'\r'}"
      TUNNEL_HOSTNAME="$NI_TUNNEL_HOSTNAME"
    else
      read -rsp "Paste your tunnel token: " CLOUDFLARE_TUNNEL_TOKEN
      printf '\n'
      if [ -z "${CLOUDFLARE_TUNNEL_TOKEN// }" ]; then
        warn "Token was empty. Re-prompting once..."
        read -rsp "Paste your tunnel token: " CLOUDFLARE_TUNNEL_TOKEN
        printf '\n'
      fi
    fi
    if [ -z "${CLOUDFLARE_TUNNEL_TOKEN// }" ]; then
      die "Cloudflare Tunnel token is required for global mode." \
          "Get one at https://dash.cloudflare.com → Zero Trust → Networks → Tunnels, then re-run ./setup.sh"
    fi
    USE_TUNNEL_PROFILE=1
    ok "Tunnel credential loaded."
    if [ "$NON_INTERACTIVE" -eq 0 ]; then
      # Prompt for the public hostname so CORS_ORIGINS and the Host allowlist
      # match the browser origin.
      printf '\n'
      info "What public hostname did you configure for this tunnel in Cloudflare Zero Trust?"
      while true; do
        read -r -p "Cloudflare Tunnel public hostname (e.g. jarvis.mydomain.com): " TUNNEL_HOSTNAME
        if _validated_tunnel_host="$(_public_origin_host "https://${TUNNEL_HOSTNAME}" 2>/dev/null)"; then
          TUNNEL_HOSTNAME="$_validated_tunnel_host"
          break
        fi
        echo "Invalid hostname. Use a DNS hostname without a scheme, port, or path."
      done
    fi
    CORS_ORIGINS_OVERRIDE="https://${TUNNEL_HOSTNAME},https://localhost:3001"
    DASHBOARD_SERVER_NAME_VALUE="$(_append_server_name "$DASHBOARD_SERVER_NAME_VALUE" "$TUNNEL_HOSTNAME")"
    CF_TRUST_OVERRIDE=true
    ok "Tunnel hostname: ${TUNNEL_HOSTNAME} (added to CORS_ORIGINS and the dashboard Host allowlist)."
    ok "JARVIS_TRUST_CF_CONNECTING_IP=true — rate limiting will key off the real CF-Connecting-IP header rather than the tunnel origin."
    ;;
  5)
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
    die "Invalid choice '$access_mode'. Expected 1, 2, 3, 4, or 5." \
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
    || die "--public-origin must be exactly https://DNS-host[:port] (no path, query, fragment, userinfo, or IP literal)." "Example: https://jarvis.example.ts.net"
  [ -z "$APP_BASE_URL_VALUE" ] && APP_BASE_URL_VALUE="$NI_PUBLIC_ORIGIN"
  DASHBOARD_SERVER_NAME_VALUE="$(_append_server_name "$DASHBOARD_SERVER_NAME_VALUE" "$PUBLIC_ORIGIN_HOST")"
  [ -n "$CORS_ORIGINS_OVERRIDE" ] || CORS_ORIGINS_OVERRIDE="http://localhost:${DASHBOARD_HOST_PORT:-3001}"
  CORS_ORIGINS_OVERRIDE="$(_append_csv "$CORS_ORIGINS_OVERRIDE" "$NI_PUBLIC_ORIGIN")"
  ok "Private HTTPS origin: ${NI_PUBLIC_ORIGIN} (APP_BASE_URL, CORS_ORIGINS, and the dashboard Host allowlist updated)."
fi

# local-https preserves localhost:3443 through caddy_local, so generated links
# retain the browser-facing port. Add that HTTPS origin to CORS alongside the
# direct loopback port.
if [ "$NI_PROFILE" = "local-https" ]; then
  [ -n "$CORS_ORIGINS_OVERRIDE" ] || CORS_ORIGINS_OVERRIDE="http://localhost:${DASHBOARD_HOST_PORT:-3001}"
  CORS_ORIGINS_OVERRIDE="$(_append_csv "$CORS_ORIGINS_OVERRIDE" "https://localhost:3443")"
fi
if [ "$NON_INTERACTIVE" -eq 0 ]; then
  case "$ACCESS_MODE_LABEL" in
    localhost)   info "Access mode: on this computer only — sign-in links and passkeys all work here." ;;
    lan)         info "Access mode: home/lab diagnostics — other devices can check /health/jarvis; the dashboard stays on localhost or verified HTTPS." ;;
    tailscale)   info "Access mode: private Tailscale network — setup will configure and verify HTTPS after the dashboard starts." ;;
    tunnel)      info "Access mode: Cloudflare Tunnel selected — setup will verify the public address before marking this install complete." ;;
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

_ACCESS_EDGE_RETIREMENTS="$(access_edge_retirements "$_PREVIOUS_ACCESS_MODE" "$_PREVIOUS_COMPOSE_PROFILES" "$ACCESS_MODE_LABEL" "$COMPOSE_PROFILES_VALUE")"

finalize_previous_access_edge_retirement() {
  # Retired runtimes were quiesced before the replacement probe so they could
  # not answer on its behalf. Only irreversible cleanup remains after every
  # route/readiness gate: delete an old tunnel credential no live edge uses.
  local _old_edge _old_service
  while IFS='|' read -r _old_edge _old_service; do
    [ -n "$_old_edge" ] || continue
    case "$_old_edge" in
      tunnel) rm -f secrets/cloudflare_tunnel_token.txt ;;
    esac
  done <<< "$_ACCESS_EDGE_RETIREMENTS"
}

# Derive all pinned peers from one subnet. Existing .env wins on a reconfigure;
# an exported value wins over that, matching Docker Compose interpolation.
JARVIS_NET_SUBNET_VALUE="${JARVIS_NET_SUBNET:-$(existing_env_value JARVIS_NET_SUBNET || printf '10.137.241.0/24')}"
_INGRESS_IPS="$(allocate_ingress_ips "$JARVIS_NET_SUBNET_VALUE")" \
  || die "JARVIS_NET_SUBNET must be a valid IPv4 /27 or larger network." \
      "Use a network such as 10.137.241.0/24 so ingress and application services have enough addresses."
read -r JARVIS_NET_GATEWAY_IP_VALUE JARVIS_TELEGRAM_BOT_IP_VALUE JARVIS_CADDY_IP_VALUE \
  JARVIS_CADDY_LOCAL_IP_VALUE JARVIS_DASHBOARD_IP_VALUE JARVIS_CLOUDFLARED_IP_VALUE \
  <<< "$_INGRESS_IPS"
# -----------------------------------------------------------------------------
# 7. Write .env (tempfile + mv, macOS-safe)
# -----------------------------------------------------------------------------
info "Writing .env from .env.example..."

TMP_ENV="$(mktemp "${TMPDIR:-/tmp}/jarvis-env.XXXXXX")"
# Every nonzero exit after the durable snapshot is transactional: pulls, builds,
# model bootstrap, health checks, route probes, readiness, and final retirement
# all restore the previous files, credentials, dashboard, and owned edge. The
# attempted flag prevents a failed rollback from being retried recursively.
cleanup_setup_exit() {
  local exit_rc=$? action=clear
  trap - EXIT
  [ -f "${TMP_ENV:-}" ] && rm -f "$TMP_ENV" || true
  if [ "$exit_rc" -ne 0 ] \
      && [ "${_ENV_SNAPSHOT_TAKEN:-0}" -eq 1 ] \
      && [ "${_ACCESS_TRANSACTION_COMMITTED:-0}" -eq 0 ] \
      && [ "${_ACCESS_ROLLBACK_ATTEMPTED:-0}" -eq 0 ]; then
    warn "Setup failed before the reconfiguration transaction committed; restoring the previous installation..."
    rollback_unverified_access_config || true
  elif [ "$exit_rc" -ne 0 ] \
      && [ "${_ENV_EXISTED_AT_START:-0}" -eq 0 ] \
      && [ "${_ACCESS_TRANSACTION_COMMITTED:-0}" -eq 0 ] \
      && [ "${_ACCESS_ROLLBACK_ATTEMPTED:-0}" -eq 0 ]; then
    # Fresh installs retain generated files for a cheap resume, but a public or
    # host-daemon edge must not remain live after setup reports failure.
    _ACCESS_ROLLBACK_ATTEMPTED=1
    if ! remove_attempted_access_runtime "${ACCESS_MODE_LABEL:-localhost}" \
        "${COMPOSE_PROFILES_VALUE:-}" \
        "${DASHBOARD_TRUSTED_HOST_PORT_RESOLVED:-3003}" \
        "${_REPLACEMENT_TAILSCALE_ATTEMPTED:-0}" \
        "${NON_INTERACTIVE:-1}" "$SCRIPT_DIR" "$SCRIPT_DIR/.env"; then
      warn "The failed fresh install could not remove its attempted access edge automatically."
      warn "Inspect it before exposing this host: docker compose ps; sudo tailscale serve status --json"
    fi
  fi
  if [ "${_SETUP_LIFECYCLE_CLAIMED:-0}" -eq 1 ]; then
    [ "${_SETUP_MUTATION_STARTED:-0}" -ne 1 ] || action=retain
    [ "$exit_rc" -ne 0 ] || action=clear
    if ! finish_lifecycle_operation "$SCRIPT_DIR" setup "$action"; then
      if [ "$action" = retain ]; then
        warn "Setup failed, and its lifecycle recovery state could not be retained safely."
      else
        warn "Setup completed, but its lifecycle state could not be cleared."
      fi
    fi
  fi
  exit "$exit_rc"
}
trap cleanup_setup_exit EXIT

# Persist the previous .env plus present/absent snapshots of every credential
# setup can mutate. The private journal is gitignored, outside secrets/ (so
# backups do not collect it), and recovered before a later run may replace it.
if [ -f .env ]; then
  begin_setup_transaction "$_SETUP_TRANSACTION_DIR" .env secrets \
    "$_PREVIOUS_ACCESS_MODE" "$_PREVIOUS_COMPOSE_PROFILES" \
    "$_PREVIOUS_TAILSCALE_PORT" "$_PREVIOUS_APP_BASE_URL" \
    "$_PREVIOUS_DASHBOARD_HOST_PORT" "$ACCESS_MODE_LABEL" \
    "$COMPOSE_PROFILES_VALUE" "$DASHBOARD_TRUSTED_HOST_PORT_RESOLVED" \
    || die "Could not create the private setup transaction snapshot." \
        "Check permissions and resolve any retained .jarvis-setup-transaction before re-running."
  cp "$_SETUP_TRANSACTION_DIR/old.env" .env.pre-setup.bak \
    && chmod 600 .env.pre-setup.bak \
    || die "Could not write .env.pre-setup.bak." \
        "The private transaction snapshot is retained at .jarvis-setup-transaction."
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
    DASHBOARD_SERVER_NAME)     printf '%s' "$DASHBOARD_SERVER_NAME_VALUE" ;;
    APP_BASE_URL)             printf '%s' "$APP_BASE_URL_VALUE" ;;
    JARVIS_ACCESS_MODE)       printf '%s' "$ACCESS_MODE_LABEL" ;;
    JARVIS_TRUST_CF_CONNECTING_IP) printf '%s' "${CF_TRUST_OVERRIDE:-false}" ;;
    CORS_ORIGINS)
      if [ -n "$CORS_ORIGINS_OVERRIDE" ]; then
        printf '%s' "$CORS_ORIGINS_OVERRIDE"
      else
        printf 'http://localhost:%s' "${DASHBOARD_HOST_PORT:-3001}"
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
      [ "$NI_PROFILE" = "letsencrypt" ] && printf '%s' "$NI_DOMAIN" || printf '' ;;
    LETSENCRYPT_EMAIL)
      [ "$NI_PROFILE" = "letsencrypt" ] && printf '%s' "$NI_ADMIN_EMAIL" || printf '' ;;
    # Any route that carries authenticated browser traffic off-host runs with
    # production safeguards, including a named --public-origin layered onto the
    # localhost/LAN modes. Loopback-only HTTP/local HTTPS remain development.
    ENVIRONMENT)
      environment_for_access_route "$ACCESS_MODE_LABEL" \
        "$COMPOSE_PROFILES_VALUE" "$APP_BASE_URL_VALUE"
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
      printf '%s' "${NI_LLM_BACKEND:-ollama}" ;;
    JARVIS_SMART_MODEL)
      printf '%s' "${NI_SMART_MODEL:-}" ;;
    COMPOSE_PROFILES) printf '%s' "$COMPOSE_PROFILES_VALUE" ;;
    OLLAMA_MODELS) compute_ollama_models "${NI_SMART_MODEL:-qwen3:8b}" ;;
    *) return 1 ;;
  esac
  return 0
}

# Keys retired in this release are dropped from a carried-forward .env unless
# this run still owns them (see merge_env_file). The dashboard serves plain HTTP
# behind the selected edge, so neither a certificate SAN nor an in-container
# self-signed generator switch belongs in its runtime configuration.
RETIRED_ENV_KEYS="JARVIS_CERT_SAN JARVIS_SKIP_SELFSIGNED_GEN"

if [ -f .env ]; then
  # Reconfigure (--overwrite-env / interactive "y"): rebuild WITHOUT discarding
  # operator state. Access-mode keys are owned even when their replacement is
  # empty, so leaving an edge cannot preserve its hostname, token, trust flag,
  # or Compose profile. Every other existing key — secrets, operator-added
  # keys, SMTP settings — is carried
  # forward byte-for-byte by merge_env_file, so no secret rotates and no custom
  # key is lost. Other empty optional values are skipped so an unset prompt
  # (e.g. no Telegram token this run) never clobbers a previously-saved value.
  UPSERTS="$(mktemp "${TMPDIR:-/tmp}/jarvis-upserts.XXXXXX")"
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      key="${BASH_REMATCH[1]}"
      if _val="$(sub_value "$key")"; then
        case "$key" in
          APP_BASE_URL|CLOUDFLARE_TUNNEL_TOKEN|COMPOSE_PROFILES|CORS_ORIGINS|DASHBOARD_BIND_HOST|DASHBOARD_SERVER_NAME|JARVIS_ACCESS_MODE|JARVIS_TRUST_CF_CONNECTING_IP|LETSENCRYPT_DOMAIN|LETSENCRYPT_EMAIL|TUNNEL_HOSTNAME)
            printf '%s=%s\n' "$key" "$_val" >> "$UPSERTS" ;;
          *)
            [ -n "$_val" ] && printf '%s=%s\n' "$key" "$_val" >> "$UPSERTS" ;;
        esac
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

# Atomically replace .env. From this point a failed setup must retain its exact
# lifecycle identity so a retry can complete or recover the partial install.
_SETUP_MUTATION_STARTED=1
mv "$TMP_ENV" .env
chmod 600 .env

_persist_compose_project_request "$NI_COMPOSE_PROJECT_NAME"
upsert_env_var JARVIS_VERSION "$CHECKOUT_APP_VERSION"
export JARVIS_VERSION="$CHECKOUT_APP_VERSION"
upsert_env_var JARVIS_IMAGE_TAG "$SELECTED_IMAGE_TAG"
export JARVIS_IMAGE_TAG="$SELECTED_IMAGE_TAG"
upsert_env_var JARVIS_NET_SUBNET "$JARVIS_NET_SUBNET_VALUE"
upsert_env_var JARVIS_NET_GATEWAY_IP "$JARVIS_NET_GATEWAY_IP_VALUE"
upsert_env_var JARVIS_TELEGRAM_BOT_IP "$JARVIS_TELEGRAM_BOT_IP_VALUE"
upsert_env_var JARVIS_CADDY_IP "$JARVIS_CADDY_IP_VALUE"
upsert_env_var JARVIS_CADDY_LOCAL_IP "$JARVIS_CADDY_LOCAL_IP_VALUE"
upsert_env_var JARVIS_DASHBOARD_IP "$JARVIS_DASHBOARD_IP_VALUE"
upsert_env_var JARVIS_CLOUDFLARED_IP "$JARVIS_CLOUDFLARED_IP_VALUE"
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
if [ "$NI_PROFILE" = "local-https" ]; then
  prepare_local_https
fi
PROFILE_ARGS=()
for _ap in ${ACTIVE_PROFILES[@]+"${ACTIVE_PROFILES[@]}"}; do
  case "$_ap" in
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
_wait_for_setup_service ollama 180 \
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
[ "$_ENV_SNAPSHOT_TAKEN" -eq 1 ] && _ACCESS_RECONFIGURATION_APPLY_STARTED=1
compose_up_or_recover "docker compose up failed." \
    "Inspect logs: docker compose logs --tail=200" \
    ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} ${PROFILE_ARGS[@]+"${PROFILE_ARGS[@]}"} "${UP_ARGS[@]}"
# A same-mode tunnel reconfiguration can change the token or hostname without
# changing Compose's container spec. Force-recreate cloudflared so `/ready` and
# the marker probe exercise the replacement connector, not the old process.
if [ "$_ENV_SNAPSHOT_TAKEN" -eq 1 ] \
    && { [ "$ACCESS_MODE_LABEL" = "tunnel" ] \
      || [[ ",$COMPOSE_PROFILES_VALUE," == *,tunnel,* ]]; }; then
  compose_up_or_recover "Cloudflare tunnel restart failed." \
      "Inspect logs: docker compose logs --tail=200 cloudflared" \
      ${COMPOSE_FILE_ARGS[@]+"${COMPOSE_FILE_ARGS[@]}"} --profile tunnel up -d \
      --no-build --force-recreate --no-deps cloudflared
fi
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
  if ! _wait_for_setup_service "$svc" "$_budget"; then
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

# Stop only old JARVIS-owned edges that this mode change will retire before any
# replacement marker probe. Without this quiesce, the old edge can answer a
# reused hostname and make a dead replacement look verified. A later failure
# restores it from the durable transaction snapshot.
if [ -n "$_ACCESS_EDGE_RETIREMENTS" ]; then
  info "Temporarily stopping the previous JARVIS access edge before replacement verification..."
  if ! quiesce_previous_access_runtime \
      "$_PREVIOUS_ACCESS_MODE" "$_PREVIOUS_COMPOSE_PROFILES" \
      "$ACCESS_MODE_LABEL" "$COMPOSE_PROFILES_VALUE" \
      "$_PREVIOUS_TAILSCALE_PORT" "$NON_INTERACTIVE" \
      "$SCRIPT_DIR" "$SCRIPT_DIR/.env"; then
    die "The previous JARVIS access edge could not be stopped safely for replacement verification." \
        "Setup will restore the previous configuration; inspect the recovery result below."
  fi
  _PREVIOUS_ACCESS_EDGES_QUIESCED=1
fi

# Tailscale Serve proxies only to the host-loopback trusted listener. --bg
# persists the route across daemon restarts; --yes makes an idempotent re-run
# update the same Serve config without a second confirmation prompt.
TAILSCALE_APP_STATE="unavailable"
if [ "$ACCESS_MODE_LABEL" = "tailscale" ] && [ -n "${TAILSCALE_HOSTNAME:-}" ]; then
  info "Configuring private HTTPS at https://${TAILSCALE_HOSTNAME} ..."
  _REPLACEMENT_TAILSCALE_ATTEMPTED=1
  if [ "$_ENV_SNAPSHOT_TAKEN" -eq 1 ]; then
    mark_setup_transaction_tailscale_attempted "$_SETUP_TRANSACTION_DIR" \
      || die "Could not record the pending Tailscale mutation safely." \
          "The previous configuration is still in .jarvis-setup-transaction; follow the recovery guidance below."
  fi
  if tailscale_serve_https "$DASHBOARD_TRUSTED_HOST_PORT_RESOLVED" "$NON_INTERACTIVE"; then
    TAILSCALE_APP_STATE="$(probe_external_app "https://$TAILSCALE_HOSTNAME/health/jarvis")"
    if [ "$TAILSCALE_APP_STATE" = "verified" ]; then
      ok "Tailscale HTTPS reaches this JARVIS instance."
    else
      warn "Tailscale Serve is configured but the JARVIS marker is not reachable yet (${TAILSCALE_APP_STATE}). Localhost remains available."
    fi
  else
    warn "Tailscale Serve could not be configured. Localhost remains available; after checking 'tailscale status', re-run ./setup.sh --tailscale --overwrite-env."
  fi
fi

# -----------------------------------------------------------------------------
# 12. Readiness gates + summary (only reached when all mandatory services healthy)
# -----------------------------------------------------------------------------
DASHBOARD_HOST_PORT_RESOLVED="${DASHBOARD_HOST_PORT:-3001}"

# The tokenized setup link mints the first admin via an X-Setup-Token header, so
# it must never ride raw-IP plaintext (the header is sniffable on a shared LAN).
# Its base defaults to loopback and only moves to a VERIFIED HTTPS origin below.
# The displayed application URL stays loopback unless a named HTTPS edge has
# been verified. Raw LAN HTTP is printed separately as a health-check URL.
DASHBOARD_URL="http://localhost:${DASHBOARD_HOST_PORT_RESOLVED}"
SETUP_LINK_BASE="http://localhost:${DASHBOARD_HOST_PORT_RESOLVED}"
LAN_DIAGNOSTIC_URL=""
case "$ACCESS_MODE_LABEL" in
  lan)
    if [ -n "$LAN_IP" ]; then
      LAN_DIAGNOSTIC_URL="http://${LAN_IP}:${DASHBOARD_HOST_PORT_RESOLVED}/health/jarvis"
    else
      LAN_DIAGNOSTIC_URL="http://<this-machine-ip>:${DASHBOARD_HOST_PORT_RESOLVED}/health/jarvis"
    fi
    # Both dashboard and setup stay loopback — credentials never ride raw-IP HTTP.
    ;;
  tunnel)
    # Keep the usable loopback URL until the exact external marker verifies.
    # Cloudflare status is printed separately below; DASHBOARD_URL must remain
    # a URL because later instructions append paths to it.
    ;;
esac
LOCAL_HTTPS_APP_STATE="unavailable"
if [ "$NI_PROFILE" = "local-https" ]; then
  info "Verifying local HTTPS and the installed mkcert trust (up to 24s)..."
  if wait_for_local_https_marker \
      "https://localhost:3443/health/jarvis" 12 2; then
    LOCAL_HTTPS_APP_STATE="verified"
    DASHBOARD_URL="https://localhost:3443"
    SETUP_LINK_BASE="https://localhost:3443"
    ok "Local HTTPS reaches this JARVIS instance through the trusted mkcert CA."
  else
    LOCAL_HTTPS_APP_STATE="$(probe_local_https_app \
      "https://localhost:3443/health/jarvis")"
    warn "Local HTTPS is not trusted and verified yet (${LOCAL_HTTPS_APP_STATE}); the setup link stays on localhost."
    warn "Re-run setup with --profile=local-https; use 'make certs' only for manual repair."
  fi
fi

# LAN reachability probe (non-fatal, informational): a success only proves the
# service answers on THIS host — a host firewall can still block LAN peers, so
# verify from a second device.
if [ "$ACCESS_MODE_LABEL" = "lan" ] && [ -n "$LAN_IP" ]; then
  _lan_probe_url="http://${LAN_IP}:${DASHBOARD_HOST_PORT_RESOLVED}/health/jarvis"
  info "Probing LAN reachability at ${_lan_probe_url} ..."
  if curl -fso /dev/null "$_lan_probe_url" 2>/dev/null; then
    ok "LAN reachable from this machine — verify from a second device; a host firewall can still block LAN clients."
  else
    warn "LAN probe did not answer yet — services may still be starting, or a host firewall may block port ${DASHBOARD_HOST_PORT_RESOLVED}."
    warn "  Verify from another LAN device, or on this host: curl -so /dev/null ${_lan_probe_url}"
  fi
fi

# A running cloudflared process is not enough. First require /ready (an active
# Cloudflare connection), then prove the public hostname returns this app's
# exact marker without following redirects.
TUNNEL_APP_STATE="unavailable"
if [ "$ACCESS_MODE_LABEL" = "tunnel" ] && [ -n "$TUNNEL_HOSTNAME" ]; then
  info "Waiting for an active Cloudflare connection (up to 60s)..."
  _cf_attempt=0
  while [ "$_cf_attempt" -lt 12 ]; do
    if cloudflared_ready >/dev/null 2>&1; then break; fi
    _cf_attempt=$((_cf_attempt + 1))
    [ "$_cf_attempt" -lt 12 ] && sleep 5
  done
  if [ "$_cf_attempt" -ge 12 ]; then
    warn "Cloudflare tunnel has no active edge connection. Localhost remains available; check 'docker compose logs cloudflared'."
  else
    TUNNEL_APP_STATE="$(probe_external_app "https://${TUNNEL_HOSTNAME}/health/jarvis")"
    case "$TUNNEL_APP_STATE" in
      verified)
        ok "Cloudflare hostname reaches this JARVIS instance; sign-in and passkeys can use this HTTPS address."
        DASHBOARD_URL="https://${TUNNEL_HOSTNAME}"
        SETUP_LINK_BASE="https://${TUNNEL_HOSTNAME}"
        ;;
      access)
        warn "Cloudflare Access is protecting the hostname, but it also blocks setup's exact marker check."
        warn "Create a separate Access application for only /health/jarvis with Bypass / Everyone; keep the main application behind Allow."
        warn "That exact path returns only a fixed non-secret marker. Never bypass the whole application."
        ;;
      waf)
        warn "Cloudflare challenged /health/jarvis, so setup cannot verify the app. Exclude only that fixed-marker path from the challenge; keep the application protected."
        ;;
      wrong-app)
        warn "The Cloudflare hostname returned a different application. Set its service URL to http://dashboard:3002; the setup link stays on localhost."
        ;;
      dns-tls)
        warn "The Cloudflare hostname failed DNS or TLS verification. Check the public hostname and certificate; the setup link stays on localhost."
        ;;
      *)
        warn "The Cloudflare hostname did not return the JARVIS marker. The setup link stays on localhost."
        ;;
    esac
  fi
fi

# Named private HTTPS origin edge probe (non-fatal): only host the setup link at
# the origin once the edge actually answers; otherwise keep it on loopback and
# print a pending-edge status so the operator finishes the edge and re-checks.
PUBLIC_ORIGIN_VERIFIED=0
if [ -n "$NI_PUBLIC_ORIGIN" ]; then
  info "Probing the private HTTPS origin at ${NI_PUBLIC_ORIGIN}/health/jarvis (best-effort) ..."
  _po_attempt=0
  while [ "$_po_attempt" -lt 3 ]; do
    if [ "$(probe_external_app "${NI_PUBLIC_ORIGIN}/health/jarvis")" = "verified" ]; then
      PUBLIC_ORIGIN_VERIFIED=1; break
    fi
    _po_attempt=$((_po_attempt + 1))
    [ "$_po_attempt" -lt 3 ] && sleep 5
  done
  if [ "$PUBLIC_ORIGIN_VERIFIED" -eq 1 ]; then
    ok "Private HTTPS origin reachable — the setup link will be hosted there."
    SETUP_LINK_BASE="$NI_PUBLIC_ORIGIN"
    DASHBOARD_URL="$NI_PUBLIC_ORIGIN"
  else
    warn "Private HTTPS origin ${NI_PUBLIC_ORIGIN} not reachable yet — keeping the setup link on loopback."
  fi
fi

# Let's Encrypt certificate gate: the caddy edge is running under the letsencrypt
# profile started above; wait for ACME issuance before advertising the https://
# URL or hosting the setup link. In production a timeout is FATAL — never claim a
# route that has not served.
LETSENCRYPT_APP_STATE="unavailable"
if [ "$NI_PROFILE" = "letsencrypt" ] && [ -n "$NI_DOMAIN" ]; then
  info "Waiting for the public certificate at https://${NI_DOMAIN} (up to 120s)..."
  _le_ok=0
  _le_attempt=0
  while [ "$_le_attempt" -lt 12 ]; do
    if [ "$(probe_external_app "https://${NI_DOMAIN}/health/jarvis")" = "verified" ]; then
      _le_ok=1; break
    fi
    _le_attempt=$((_le_attempt + 1))
    [ "$_le_attempt" -lt 12 ] && sleep 10
  done
  if [ "$_le_ok" -eq 1 ]; then
    LETSENCRYPT_APP_STATE="verified"
    ok "Public TLS is live at https://${NI_DOMAIN}."
    DASHBOARD_URL="https://${NI_DOMAIN}"
    SETUP_LINK_BASE="https://${NI_DOMAIN}"
  else
    warn "Public TLS did not come up at https://${NI_DOMAIN} within the timeout. Localhost remains available."
    warn "Check that DNS resolves to this host and ports 80/443 are reachable."
    unverified_https_exit "Let's Encrypt" "https://${NI_DOMAIN}" \
      "$DASHBOARD_HOST_PORT_RESOLVED" "./setup.sh --profile=letsencrypt --overwrite-env"
  fi
fi

# Neutral heading — the green "Setup complete." banner is emitted only AFTER the
# readiness gate below passes, so a production HIGH abort never follows a success
# claim.
printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   Services are healthy — running final checks...%s\n' "$C_BOLD" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"

# The token-bearing finish link is deliberately deferred until the selected
# route, readiness, old-edge finalization, and transaction-commit gates pass.

if [ -n "$NI_PUBLIC_ORIGIN" ] && [ "$PUBLIC_ORIGIN_VERIFIED" -ne 1 ]; then
  printf '\n'
  printf '  %sPrivate HTTPS origin CONFIGURED but NOT YET VERIFIED:%s %s\n' "$C_BOLD" "$C_RESET" "$NI_PUBLIC_ORIGIN"
  printf '    - Finish the edge, e.g.: tailscale serve --bg --yes --https=443 http://127.0.0.1:%s\n' "$DASHBOARD_TRUSTED_HOST_PORT_RESOLVED"
  printf '    - See the deployment guide, then re-check: ./setup.sh --check\n'
  printf '    - Setup will not print a finish link for this unverified address. Bootstrap later through the verified route or a loopback SSH-forward.\n'
fi

# Selected HTTPS completion gate. A configured edge is not a completed install
# until the exact JARVIS marker is reachable through the selected origin. Run
# this before other final checks so a failed reconfiguration always restores
# its previous access files before exiting.
_SELECTED_HTTPS_ROUTE="$(selected_https_route "$ACCESS_MODE_LABEL" "$COMPOSE_PROFILES_VALUE" "$APP_BASE_URL_VALUE")"
_SELECTED_HTTPS_STATE="unavailable"
case "$_SELECTED_HTTPS_ROUTE" in
  tailscale)
    if [ "$PUBLIC_ORIGIN_VERIFIED" -eq 1 ]; then
      _SELECTED_HTTPS_STATE="verified"
    else
      _SELECTED_HTTPS_STATE="$TAILSCALE_APP_STATE"
    fi
    ;;
  tunnel)      _SELECTED_HTTPS_STATE="$TUNNEL_APP_STATE" ;;
  letsencrypt) _SELECTED_HTTPS_STATE="$LETSENCRYPT_APP_STATE" ;;
  local-https) _SELECTED_HTTPS_STATE="$LOCAL_HTTPS_APP_STATE" ;;
  private)
    if [ "$PUBLIC_ORIGIN_VERIFIED" -eq 1 ]; then
      _SELECTED_HTTPS_STATE="verified"
    fi
    ;;
esac
if ! selected_https_is_verified "$_SELECTED_HTTPS_ROUTE" "$_SELECTED_HTTPS_STATE"; then
  _selected_route_label="HTTPS"
  _selected_retry="./setup.sh"
  case "$_SELECTED_HTTPS_ROUTE" in
    tailscale)
      _selected_route_label="Tailscale Serve"
      _selected_retry="./setup.sh --tailscale --overwrite-env"
      ;;
    tunnel)      _selected_route_label="Cloudflare Tunnel" ;;
    letsencrypt) _selected_route_label="Let's Encrypt" ;;
    local-https)
      _selected_route_label="local HTTPS"
      _selected_retry="./setup.sh --profile=local-https --overwrite-env"
      ;;
    private)     _selected_route_label="named private HTTPS" ;;
  esac
  unverified_https_exit "$_selected_route_label" "$APP_BASE_URL_VALUE" \
    "$DASHBOARD_HOST_PORT_RESOLVED" "$_selected_retry"
fi

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

# The old runtime was quiesced before marker verification. Only after readiness
# passes may setup delete a retired tunnel credential and discard the private
# rollback journal. Any failure here still triggers the EXIT rollback.
finalize_previous_access_edge_retirement
if [ "$_ENV_SNAPSHOT_TAKEN" -eq 1 ]; then
  discard_setup_transaction "$_SETUP_TRANSACTION_DIR" \
    || die "The install passed its gates, but the private setup transaction could not be removed." \
        "Check ownership of .jarvis-setup-transaction and re-run setup; the previous route will be restored on exit."
fi
_ACCESS_TRANSACTION_COMMITTED=1

# Resolve the operator's actual browser route before claiming completion. This
# is especially important on WSL, where a healthy Linux listener is not proof
# that Windows localhost forwarding reaches the same JARVIS instance.
resolve_setup_browser_route "$SETUP_LINK_BASE" "$DASHBOARD_HOST_PORT_RESOLVED" \
  || die "Windows could not reach the local JARVIS dashboard." \
         "Follow the Windows forwarding steps above, then re-run this launcher"

# Green success banner — reached only past the readiness gate (a production HIGH
# abort exits above), so "Setup complete." is never printed ahead of the checks.
printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   Setup complete.%s\n' "$C_GREEN" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"

# Click-to-finish setup link: carries the setup token in a URL fragment so it
# never reaches server logs. Its base is loopback or a verified HTTPS origin,
# never a raw LAN IP. Do not print or open it before the transaction commits.
present_setup_link "$SETUP_LINK_BASE" "$DASHBOARD_HOST_PORT_RESOLVED" \
  || die "Could not prepare a safe first-admin link." \
         "Re-run ./setup.sh; do not copy the setup token to a LAN address"
# Every actionable URL below must match the route just resolved for the real
# browser (direct desktop, verified Windows localhost, SSH forward, or named
# HTTPS), not the server-side address setup started with.
DASHBOARD_URL="$SETUP_BROWSER_BASE"

# Register this checkout with the jarvis-research lifecycle CLI only after the
# successful finish-link gate. This is non-fatal: a launcher-install failure
# must not turn a healthy dashboard into a failed setup.
install_cli_shim "$SCRIPT_DIR" || warn "Could not install the jarvis-research launcher (non-fatal)."

printf '\n'
printf '  Dashboard:    %s\n' "$DASHBOARD_URL"
if [ -n "$LAN_DIAGNOSTIC_URL" ]; then
  printf '  LAN check:    %s\n' "$LAN_DIAGNOSTIC_URL"
fi
if [ "$ACCESS_MODE_LABEL" = "lan" ]; then
  # Raw-IP LAN is HTTP-only: it is deliberately a status route, never an
  # authenticated family route. The loopback finish link above remains safe
  # through the SSH forward when this host has no local browser.
  printf '  Plain-HTTP LAN: setup and sign-in are blocked on raw http://%s.\n' "${LAN_IP:-<ip>}"
  printf '                  For a private family dashboard, run: ./setup.sh --tailscale --overwrite-env\n'
fi

if [ "$NI_MODE" = "single" ]; then
  # Single-user mode: API key auth is enabled.
  _KEY_FILE="$(materialize_api_key_file "$JARVIS_API_KEY")" \
    || die "Could not write the local API-key file." \
           "Check permissions on ${HOME}/.config/jarvis, then re-run ./setup.sh"
  printf '  API key:      written to %s\n' "$_KEY_FILE"
  printf '  %sTo retrieve:%s grep JARVIS_API_KEY .env\n' "$C_BOLD" "$C_RESET"
  printf '  Sign in:      open the dashboard and enter your API key.\n'
else
  if [ "$SETUP_BROWSER_IS_SHARED" -eq 1 ]; then
    printf '  Sign in:      Family members sign in with a passkey or one-time link.\n'
    printf '                The configured owner can use local API-key recovery.\n'
  else
    printf '  Sign in:      Use the "Finish setup" link above to create the first admin.\n'
    printf '  Family use:   Wait until JARVIS has one named HTTPS address shared by everyone.\n'
  fi
fi
printf '  All mandatory services healthy. You can now open the dashboard.\n'
printf '  Tail logs:  docker compose logs -f\n'

# -----------------------------------------------------------------------------
# 14. Next steps
# -----------------------------------------------------------------------------
printf '\n%s================================================================%s\n' "$C_BOLD" "$C_RESET"
printf '%s   Next steps%s\n' "$C_BOLD" "$C_RESET"
printf '%s================================================================%s\n' "$C_BOLD" "$C_RESET"
if [ "$NI_MODE" = "single" ]; then
  printf '  1. Open the dashboard: %s\n' "$DASHBOARD_URL"
  printf '  2. Log in with your API key (stored in ~/.config/jarvis/api-key).\n'
else
  # Dependency order: the first admin is bootstrapped by the token-bearing setup
  # link (no SMTP, no existing account) BEFORE anything that presupposes an admin
  # or working email.
  printf '  1. Bootstrap the first admin: open the "Finish setup" link above — it carries the setup token, so no SMTP or existing account is needed.\n'
  if [ "$SETUP_BROWSER_IS_SHARED" -eq 1 ]; then
    printf '  2. Keep using this exact address for sign-in and passkeys: %s\n' "$DASHBOARD_URL"
    printf '     To change it later, re-run ./setup.sh before anyone enrols a passkey.\n'
    printf '  3. Invite family members from %s/admin/users. SMTP is optional: configure it for email, or copy one-time sign-in links.\n' "$DASHBOARD_URL"
    printf '  4. Each user enrols a passkey at that same address for password-free sign-in.\n'
  else
    printf '  2. Do not invite family members or enrol passkeys at this temporary address.\n'
    printf '  3. Configure one shared private HTTPS address, then re-run setup. The guided choice is:\n'
    printf '       ./setup.sh --tailscale --overwrite-env\n'
    printf '  4. When setup verifies and prints that named HTTPS address, use it for every family member.\n'
  fi
fi
printf '\n'
if [ "$NI_MODE" = "multi" ] && [ "$SETUP_BROWSER_IS_SHARED" -eq 1 ]; then
  printf '  Admin user management: %s/admin/users\n' "$DASHBOARD_URL"
fi
printf '  Tail logs:             docker compose logs -f\n'
printf '\n'
