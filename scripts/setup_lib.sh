#!/usr/bin/env bash
# Shared setup.sh helpers, factored out so they can be unit-tested directly
# (setup.sh itself is not cleanly sourceable). Concerns:
#   - compute_compose_file       : which compose overlays to persist into .env
#   - compute_ollama_models      : which Ollama tags the bootstrap must pull
#   - compute_model_disk_gb      : GB the Ollama model set pulls (every run)
#   - compute_required_disk_gb   : GB a cold install writes to the data root
#   - resolve_docker_data_root   : where Docker keeps images/volumes/cache
#   - preflight_disk_lib         : free-vs-required disk measurement core
#   - compose_meets_floor        : Docker Compose version-floor gate
#   - registry_profile_host_ports: extra host ports an active profile publishes
#   - readiness_verdict          : readiness exit-code -> wrapper action (0/2/1)
#   - tailscale_install_plan     : reviewed package-manager setup commands
#   - tailscale_serve_https      : privilege-aware private HTTPS configuration
#   - sync_ingress_ips_from_env  : persist exact proxy peers for an install subnet
#   - upsert_env_var             : idempotent in-place .env key write
#   - upsert_app_identity        : atomic semantic-version + image-tag write
#   - print_setup_link           : click-to-finish wizard link when token exists
#   - headless_setup_route       : safe loopback browser base + SSH tunnel port
#   - is_wsl_host                : Windows/WSL launcher and kernel detection
#   - app_version_is_valid       : semantic application-version validation
#   - image_tag_is_valid         : published image tag validation
#   - resolve_checkout_app_version : exact tag / project-version resolution
#   - latest_stable_tag          : highest vX.Y.Z release tag on a git remote
#   - install_cli_shim           : install the jarvis-research launcher + registry
#   - verify_release_manifests   : registry-backed images for a release all exist
#   - resolve_nvidia_smi         : locate nvidia-smi (PATH or the WSL2 location)
#   - resolve_amd_smi            : locate amd-smi (the stable AMD interface)
#   - detect_gpu_vendor          : nvidia | amd | intel | none probe
#   - resolve_gpu_vram_mb        : vendor-neutral total-VRAM (MB) probe
#   - strip_gpu_args             : drop --gpu selection for the CPU-retry re-exec
#   - _default_model_for_tier    : tier+backend -> default model id
#   - info / ok / warn / err     : the level-prefixed output every script shares
# Sourced by setup.sh (which cd's to the repo root first, so the relative `.env`
# in upsert_env_var resolves correctly).

# Presentation primitives for the scripts that source this library. Not every
# script shares them: setup.sh and update.sh keep same-format copies of the
# colours and of info/ok/warn/err for the output they print before their
# source line, and scripts/init-secrets.sh (uncoloured info, no err),
# scripts/update-bootstrap.sh (plain err only) and
# scripts/production-readiness-check.sh (the colour block) define their own,
# so a changed prefix or colour here must be mirrored in those copies by hand.
# Colours are emitted only when stdout is a terminal, so piped output and log
# files stay free of escape codes.
# `die`/`usage_error`/`env_die` deliberately stay with their scripts: each owns a
# different exit code and next-step hint.
# C_BOLD is read only by the scripts that source this library (setup.sh, update.sh,
# scripts/jarvis-research.sh, scripts/uninstall.sh,
# scripts/validate-hardware.sh) and never by the helpers below, so shellcheck
# cannot see the use.
# shellcheck disable=SC2034
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_BOLD=""; C_RESET=""
fi

info() { printf '%s[INFO]%s  %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s[OK]%s    %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[ERROR]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }

# sanitize_compose_environment
# Caller-exported Compose selectors outrank a checkout's .env and can redirect
# install or rollback mutations to another project. Clear only Compose's control
# plane here; Docker transport variables and ordinary application overrides keep
# their documented behavior. Compose can then load COMPOSE_FILE/PROFILES from the
# repo .env normally.
sanitize_compose_environment() {
  unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR
  unset COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE
}

# Canonicalize an existing or not-yet-created path with symlink resolution for
# every component that already exists. Python 3 is a setup prerequisite; using
# it here avoids GNU-only `realpath -m` and macOS's different realpath surface.
canonical_path_portable() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 -c '
import os, sys
print(os.path.realpath(os.path.abspath(sys.argv[1])), end="")
' "$1"
}

_lifecycle_operation_valid_kind() {
  case "$1" in
    setup|uninstall|control|direct-update) return 0 ;;
    *) return 1 ;;
  esac
}

host_lifecycle_lock_path() {
  local repo="$1" state_dir canonical_repo install_key
  state_dir="${JARVIS_CLI_CONFIG_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/jarvis-research}"
  canonical_repo="$(cd "$repo" && pwd -P)" || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  install_key="$(python3 -c \
    'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' \
    "$canonical_repo")" || return 1
  case "$install_key" in
    (*[!0-9a-f]*|'') return 1 ;;
  esac
  [ "${#install_key}" -eq 64 ] || return 1
  printf '%s/locks/%s.lock' "$state_dir" "$install_key"
}

# Compare an inherited descriptor with its intended path without relying on
# Linux-only /dev/fd paths or GNU/BSD stat flags. Python is already a required
# host prerequisite and fcntl is available on every supported host OS.
_host_fd_matches_regular_path() {
  local fd="$1" path="$2"
  command -v python3 >/dev/null 2>&1 || return 127
  python3 -c '
import os, stat, sys
path_stat = os.lstat(sys.argv[2])
fd_stat = os.fstat(int(sys.argv[1]))
same = stat.S_ISREG(path_stat.st_mode) and stat.S_ISREG(fd_stat.st_mode)
same = same and (path_stat.st_dev, path_stat.st_ino) == (fd_stat.st_dev, fd_stat.st_ino)
raise SystemExit(0 if same else 1)
' "$fd" "$path"
}

_host_flock_nonblocking() {
  local fd="$1"
  if command -v flock >/dev/null 2>&1; then
    flock -n "$fd"
    return
  fi
  command -v python3 >/dev/null 2>&1 || return 127
  python3 -c '
import errno, fcntl, sys
try:
    fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as exc:
    raise SystemExit(1 if exc.errno in (errno.EACCES, errno.EAGAIN) else 2)
' "$fd"
}

_host_flock_unlock() {
  local fd="$1"
  if command -v flock >/dev/null 2>&1; then
    flock -u "$fd"
    return
  fi
  command -v python3 >/dev/null 2>&1 || return 127
  python3 -c 'import fcntl, sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_UN)' "$fd"
}

# The external lock survives a tier-4 uninstall unlinking secrets/ and the
# clone. Every host-side mutator takes it first, then a named-volume lease.
claim_host_lifecycle_lock() {
  local repo="$1" lock lock_dir
  lock="$(host_lifecycle_lock_path "$repo")" || return 2
  lock_dir="$(dirname "$lock")"
  [ ! -L "$lock_dir" ] || return 2
  mkdir -p "$lock_dir" || return 2
  [ -d "$lock_dir" ] && [ ! -L "$lock_dir" ] || return 2
  [ ! -L "$lock" ] || return 2
  if [ "${JARVIS_HOST_LIFECYCLE_LOCK_HELD:-0}" = 1 ]; then
    if _host_fd_matches_regular_path 8 "$lock" \
        && _host_flock_nonblocking 8; then
      return 0
    fi
    return 2
  fi
  if [ ! -e "$lock" ]; then
    (set -C; umask 077; : > "$lock") 2>/dev/null || true
  fi
  [ -f "$lock" ] && [ ! -L "$lock" ] || return 2
  # Read/write opening does not wait for a racing FIFO peer. Validate the
  # opened descriptor before trying to lock it so a symlink/inode swap fails
  # closed instead of authenticating a different object.
  exec 8<>"$lock" || return 2
  if ! _host_fd_matches_regular_path 8 "$lock"; then
    exec 8>&-
    return 2
  fi
  if ! _host_flock_nonblocking 8; then
    exec 8>&-
    return 3
  fi
  JARVIS_HOST_LIFECYCLE_LOCK_HELD=1
  export JARVIS_HOST_LIFECYCLE_LOCK_HELD
}

# The cross-actor lease lives in postgres_backups, never a host bind mount.
# Host commands enter that Linux lock domain through short-lived `docker run`
# helpers, while a detached helper owns the operation flock for the command's
# lifetime. This remains one lock domain on Linux and Docker Desktop alike.
# compose_project_name_is_valid NAME — accept Docker Compose's local project
# identity subset used by JARVIS.
compose_project_name_is_valid() {
  [[ "${1:-}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]
}

_lifecycle_compose_project_name() {
  local repo="$1" explicit="${2:-}" name
  if [ -n "$explicit" ]; then
    compose_project_name_is_valid "$explicit" || return 1
    printf '%s' "$explicit"
    return 0
  fi
  name="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$repo/.env" 2>/dev/null | head -1)"
  case "$name" in
    \"*\") name="${name#\"}"; name="${name%\"}" ;;
    \'*\') name="${name#\'}"; name="${name%\'}" ;;
  esac
  [ -n "$name" ] \
    || name="$(basename "$repo" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  compose_project_name_is_valid "$name" || return 1
  printf '%s' "$name"
}

_lifecycle_postgres_image() {
  local repo="$1" image
  image="$(sed -n 's/^POSTGRES_IMAGE=//p' "$repo/versions.env" 2>/dev/null | head -1)"
  image="${image:-postgres:16.8}"
  printf '%s' "$image" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9./:@_-]{0,511}$' || return 1
  printf '%s' "$image"
}

_lifecycle_path_inside_repo() {
  case "$1/" in "$2"/*) return 0 ;; esac
  return 1
}

# Render the exact managed Compose model rather than guessing the default
# `${project}_postgres_backups` name. A repo-local override may assign an
# explicit volume name; using the guess would put host and sidecar actors in
# different lock domains.
_lifecycle_compose_config_json() {
  local repo="$1" project="$2" raw item candidate canon base env_file seen=""
  local -a requested_files=() files=() cmd=()
  repo="$(cd -- "$repo" 2>/dev/null && pwd -P)" || return 1
  base="$(canonical_path_portable "$repo/docker-compose.yml" 2>/dev/null || true)"
  [ -n "$base" ] && [ -f "$base" ] || return 1
  raw="$(sed -n 's/^COMPOSE_FILE=//p' "$repo/.env" 2>/dev/null | head -1)"
  case "$raw" in
    \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
    \'*\') raw="${raw#\'}"; raw="${raw%\'}" ;;
  esac
  if [ -z "$raw" ]; then
    raw=docker-compose.yml
    [ ! -f "$repo/docker-compose.override.yml" ] \
      || raw="${raw}:docker-compose.override.yml"
  fi
  IFS=: read -r -a requested_files <<< "$raw"
  for item in "${requested_files[@]}"; do
    [ -n "$item" ] || return 1
    case "$item" in /*) candidate="$item" ;; *) candidate="$repo/$item" ;; esac
    canon="$(canonical_path_portable "$candidate" 2>/dev/null || true)"
    [ -n "$canon" ] && [ -f "$canon" ] \
      && _lifecycle_path_inside_repo "$canon" "$repo" || return 1
    printf '%s\n' "$seen" | grep -qxF "$canon" && return 1
    files+=("$canon")
    seen="${seen}${canon}"$'\n'
  done
  [ "${files[0]:-}" = "$base" ] || return 1
  env_file="$repo/.env"
  [ -f "$env_file" ] || env_file=/dev/null
  cmd=(docker compose --project-directory "$repo" --env-file "$env_file" -p "$project")
  for item in "${files[@]}"; do cmd+=(-f "$item"); done
  env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
      -u COMPOSE_PATH_SEPARATOR -u COMPOSE_ENV_FILES -u COMPOSE_DISABLE_ENV_FILE \
      "${cmd[@]}" config --format json 8>&-
}

# Print: <resolved-name><TAB><managed><TAB><safe-to-create-directly>.
_lifecycle_volume_spec() {
  local repo="$1" project="$2" config
  config="$(_lifecycle_compose_config_json "$repo" "$project")" || return 1
  printf '%s' "$config" | python3 -c '
import json
import re
import sys

try:
    document = json.load(sys.stdin)
    volume = document["volumes"]["postgres_backups"]
    name = volume["name"]
except (KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", name):
    raise SystemExit(1)
external = volume.get("external") is True
configured_creation = any(key in volume for key in ("driver", "driver_opts", "labels"))
print(f"{name}\t{0 if external else 1}\t{0 if external or configured_creation else 1}", end="")
'
}

prepare_lifecycle_volume() {
  local repo="$1" project="${2:-}" spec volume managed creatable labels
  project="$(_lifecycle_compose_project_name "$repo" "$project")" || return 2
  spec="$(_lifecycle_volume_spec "$repo" "$project")" || return 2
  IFS=$'\t' read -r volume managed creatable <<< "$spec"
  [ "$managed" = 1 ] || return 4
  if ! docker volume inspect "$volume" >/dev/null 2>&1 8>&-; then
    [ "$creatable" = 1 ] || return 4
    docker volume create \
      --label "com.docker.compose.project=${project}" \
      --label 'com.docker.compose.volume=postgres_backups' \
      "$volume" >/dev/null 8>&- || return 2
  fi
  labels="$(docker volume inspect --format '{{ index .Labels "com.docker.compose.project" }}|{{ index .Labels "com.docker.compose.volume" }}' "$volume" 2>/dev/null 8>&- || true)"
  [ "$labels" = "${project}|postgres_backups" ] || return 4
  printf '%s' "$volume"
}

_lifecycle_docker_run() {
  local repo="$1" project="$2" mode="$3"; shift 3
  local volume image helper
  volume="$(prepare_lifecycle_volume "$repo" "$project")" || return $?
  image="$(_lifecycle_postgres_image "$repo")" || return 2
  helper="$repo/scripts/backup-lifecycle.sh"
  [ -f "$helper" ] && [ ! -L "$helper" ] || return 2
  if [ "$mode" = detached ]; then
    docker run --rm -d --network none --read-only \
      --security-opt no-new-privileges --cap-drop ALL \
      --mount "type=volume,src=${volume},dst=/backups" \
      --mount "type=bind,src=${helper},dst=/tmp/backup-lifecycle.sh,readonly" \
      "$image" bash /tmp/backup-lifecycle.sh "$@" 8>&-
  else
    docker run --rm --network none --read-only \
      --security-opt no-new-privileges --cap-drop ALL \
      --mount "type=volume,src=${volume},dst=/backups" \
      --mount "type=bind,src=${helper},dst=/tmp/backup-lifecycle.sh,readonly" \
      "$image" bash /tmp/backup-lifecycle.sh "$@" 8>&-
  fi
}

# Run one lifecycle command against an already-validated Compose project.
_lifecycle_volume_helper_for_project() {
  local repo="$1" project="$2"; shift 2
  _lifecycle_docker_run "$repo" "$project" foreground "$@"
}

lifecycle_volume_helper() {
  local repo="$1" project; shift
  project="$(_lifecycle_compose_project_name "$repo")" || return 2
  _lifecycle_volume_helper_for_project "$repo" "$project" "$@"
}

lifecycle_update_guard_is_active() {
  lifecycle_volume_helper "$1" update-status "$2" >/dev/null 2>&1
}

lifecycle_update_guard_is_promoted() {
  lifecycle_volume_helper "$1" update-promoted-status "$2" >/dev/null 2>&1
}

claim_lifecycle_operation() {
  local repo="$1" kind="$2" project="" current="" id="" action="" helper=""
  local attempts="${JARVIS_HOST_GUARD_READY_ATTEMPTS:-100}"
  local interval="${JARVIS_HOST_GUARD_READY_INTERVAL:-0.1}"
  local timeout="${JARVIS_HOST_GUARD_TIMEOUT:-21600}"
  _lifecycle_operation_valid_kind "$kind" || return 2
  project="$(_lifecycle_compose_project_name "$repo" "${3:-}")" || return 2
  if [ "${JARVIS_SHARED_LIFECYCLE_LOCK_HELD:-0}" = 1 ]; then
    [ "${JARVIS_SHARED_LIFECYCLE_KIND:-}" = "$kind" ] \
      && [ "${JARVIS_SHARED_LIFECYCLE_PROJECT:-}" = "$project" ] \
      && _lifecycle_volume_helper_for_project "$repo" "$project" host-status "$kind" \
           "${JARVIS_SHARED_LIFECYCLE_ID:-}" >/dev/null 2>&1
    return
  fi
  current="$(_lifecycle_volume_helper_for_project "$repo" "$project" current-host 2>/dev/null || true)"
  case "$current" in
    "${kind}:"*) id="${current#*:}" ;;
    "") id="$(python3 -c 'import secrets; print(secrets.token_hex(16))' 2>/dev/null || true)" ;;
    *) return 4 ;;
  esac
  printf '%s' "$id" | grep -Eq '^[0-9a-f]{32}$' || return 2
  action="$(_lifecycle_volume_helper_for_project "$repo" "$project" reserve-host "$kind" "$id")" \
    || return 4
  case "$action" in
    launch)
      helper="$(_lifecycle_docker_run "$repo" "$project" detached hold-host "$kind" "$id" "$timeout")" \
        || {
          _lifecycle_volume_helper_for_project "$repo" "$project" \
            cancel-host-reservation "$kind" "$id" >/dev/null 2>&1 || true
          return 2
        }
      ;;
    adopt) ;;
    *) return 2 ;;
  esac
  if ! _lifecycle_volume_helper_for_project "$repo" "$project" \
      wait-host "$kind" "$id" "$attempts" "$interval" >/dev/null 2>&1; then
    _lifecycle_volume_helper_for_project "$repo" "$project" \
      cancel-host-reservation "$kind" "$id" >/dev/null 2>&1 \
      && return 2
    return 4
  fi
  JARVIS_SHARED_LIFECYCLE_LOCK_HELD=1
  JARVIS_SHARED_LIFECYCLE_KIND="$kind"
  JARVIS_SHARED_LIFECYCLE_ID="$id"
  JARVIS_SHARED_LIFECYCLE_PROJECT="$project"
  export JARVIS_SHARED_LIFECYCLE_LOCK_HELD JARVIS_SHARED_LIFECYCLE_KIND
  export JARVIS_SHARED_LIFECYCLE_ID JARVIS_SHARED_LIFECYCLE_PROJECT
}

finish_lifecycle_operation() {
  local repo="$1" kind="$2" action="${3:-clear}" attempt=0 id project
  [ "${JARVIS_SHARED_LIFECYCLE_LOCK_HELD:-0}" = 1 ] || return 0
  [ "${JARVIS_SHARED_LIFECYCLE_KIND:-}" = "$kind" ] || return 1
  id="${JARVIS_SHARED_LIFECYCLE_ID:-}"
  project="${JARVIS_SHARED_LIFECYCLE_PROJECT:-}"
  compose_project_name_is_valid "$project" || return 1
  _lifecycle_volume_helper_for_project "$repo" "$project" \
    release-host "$kind" "$id" "$action" >/dev/null \
    || return 1
  while [ "$attempt" -lt 50 ]; do
    if _lifecycle_volume_helper_for_project "$repo" "$project" host-release-complete \
        "$kind" "$id" "$action" >/dev/null 2>&1; then
      JARVIS_SHARED_LIFECYCLE_LOCK_HELD=0
      export JARVIS_SHARED_LIFECYCLE_LOCK_HELD
      return 0
    fi
    sleep 0.1
    attempt=$((attempt + 1))
  done
  return 1
}

clear_retained_lifecycle_operation() {
  local repo="$1" kind="$2" id="$3"
  local project="${4:-${JARVIS_SHARED_LIFECYCLE_PROJECT:-}}"
  project="$(_lifecycle_compose_project_name "$repo" "$project")" || return 2
  _lifecycle_volume_helper_for_project "$repo" "$project" \
    clear-retained-host "$kind" "$id" >/dev/null
}

# wait_for_compose_service_health SERVICE BUDGET LOOKUP_FUNCTION [INTERVAL]
# Sets COMPOSE_HEALTH_RESULT to healthy, running-unverified, terminal, or
# timeout, and COMPOSE_HEALTH_LAST_STATE to the last observed container state.
# Both globals are read by the callers (setup.sh and update.sh)
# after this returns, never inside this library, so shellcheck cannot see the use.
# shellcheck disable=SC2034
wait_for_compose_service_health() {
  local service="${1:-}" budget="${2:-}" lookup="${3:-}" interval="${4:-3}"
  local elapsed=0 step cid inspection health_state run_state
  COMPOSE_HEALTH_RESULT=timeout
  COMPOSE_HEALTH_LAST_STATE=absent

  [ -n "$service" ] || return 2
  case "$budget" in ''|*[!0-9]*) return 2 ;; esac
  case "$interval" in ''|*[!0-9]*) return 2 ;; esac
  case "$lookup" in ''|[0-9]*|*[!A-Za-z0-9_]*) return 2 ;; esac
  declare -F "$lookup" >/dev/null || return 2
  if [ "$interval" -eq 0 ]; then step=1; else step="$interval"; fi

  while [ "$elapsed" -lt "$budget" ]; do
    cid="$("$lookup" "$service" 2>/dev/null || true)"
    if [ -z "$cid" ]; then
      COMPOSE_HEALTH_LAST_STATE=absent
    else
      inspection="$(docker inspect --format \
        '{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.State.Status}}' \
        "$cid" 2>/dev/null || true)"
      case "$inspection" in
        *'|'*)
          health_state="${inspection%%|*}"
          run_state="${inspection#*|}"
          ;;
        *)
          health_state=""
          run_state=""
          ;;
      esac
      COMPOSE_HEALTH_LAST_STATE="${health_state:-${run_state:-unknown}}"
      case "$run_state" in
        exited|dead)
          COMPOSE_HEALTH_RESULT=terminal
          COMPOSE_HEALTH_LAST_STATE="$run_state"
          return 1
          ;;
      esac
      case "$health_state" in
        healthy)
          COMPOSE_HEALTH_RESULT=healthy
          return 0
          ;;
        "")
          if [ "$run_state" = running ]; then
            COMPOSE_HEALTH_RESULT=running-unverified
            COMPOSE_HEALTH_LAST_STATE="$run_state"
            return 0
          fi
          ;;
        starting|unhealthy) ;;
        *) ;;
      esac
    fi
    [ "$interval" -eq 0 ] || sleep "$interval"
    elapsed=$((elapsed + step))
  done
  COMPOSE_HEALTH_RESULT=timeout
  return 1
}

# resolve_nvidia_smi -> echoes a usable nvidia-smi path, or returns 1 if none.
# WSL2 ships nvidia-smi at /usr/lib/wsl/lib/nvidia-smi but does NOT put it on
# PATH in non-login shells, so a bare `command -v nvidia-smi` wrongly concludes
# "no GPU" on a CUDA-capable WSL2 host. Check PATH first, then the WSL location
# (overridable via JARVIS_WSL_NVIDIA_SMI for testing).
resolve_nvidia_smi() {
  if command -v nvidia-smi >/dev/null 2>&1; then
    command -v nvidia-smi
    return 0
  fi
  local wsl="${JARVIS_WSL_NVIDIA_SMI:-/usr/lib/wsl/lib/nvidia-smi}"
  if [ -x "$wsl" ]; then
    printf '%s\n' "$wsl"
    return 0
  fi
  return 1
}

# resolve_amd_smi -> echoes a usable amd-smi path, or returns 1 if none.
# amd-smi (ROCm >= 5.7) is AMD's stable machine interface; rocm-smi's JSON
# output is explicitly unstable across releases, so it is never parsed here.
resolve_amd_smi() {
  command -v amd-smi 2>/dev/null || return 1
}

# detect_gpu_vendor -> echoes nvidia | amd | intel | none.
# Probe order nvidia -> amd -> render node: discrete vendor tools first
# (nvidia-smi enumerates a GPU; amd-smi static reports one), then a /dev/dri
# render node identified by its PCI vendor id. A bare render node is NOT proof
# of an Intel GPU — VMs expose a virtio-gpu render node (0x1af4) that has no
# GPU acceleration path — so classify only known accelerators (0x8086 Intel,
# 0x1002 AMD); virtio / unknown / non-PCI nodes (ARM SoCs have no vendor file)
# stay on CPU. JARVIS_DRI_DIR / JARVIS_DRM_SYS_DIR override the device and
# /sys/class/drm locations for tests.
detect_gpu_vendor() {
  local smi nodes vendor_file vid
  if smi="$(resolve_nvidia_smi)" && "$smi" -L 2>/dev/null | grep -q .; then
    printf 'nvidia'
    return 0
  fi
  if smi="$(resolve_amd_smi)" && "$smi" static --json 2>/dev/null | grep -qi '"gpu"'; then
    printf 'amd'
    return 0
  fi
  nodes=("${JARVIS_DRI_DIR:-/dev/dri}"/renderD*)
  if [ -e "${nodes[0]}" ]; then
    vendor_file="${JARVIS_DRM_SYS_DIR:-/sys/class/drm}/${nodes[0]##*/}/device/vendor"
    if [ -r "$vendor_file" ] && read -r vid < "$vendor_file"; then
      case "$vid" in
        0x8086) printf 'intel'; return 0 ;;
        0x1002) printf 'amd';   return 0 ;;
      esac
    fi
  fi
  printf 'none'
}

# resolve_dri_gids -> echoes "<video_gid> <render_gid>", the owning group ids of
# the first card* and first renderD* node under ${JARVIS_DRI_DIR:-/dev/dri}.
# The GPU overlays need NUMERIC GIDs in group_add: a group NAME is resolved
# against the CONTAINER image's /etc/group at container start, and stock ollama
# images ship no `render` group, so a name fails start on every host. video
# falls back to the render GID when there is no card* node; returns 1 and echoes
# nothing when there is no render node (the overlay cannot work without one).
resolve_dri_gids() {
  local dri="${JARVIS_DRI_DIR:-/dev/dri}" renders cards render_gid video_gid
  renders=("$dri"/renderD*)
  [ -e "${renders[0]}" ] || return 1
  render_gid="$(stat -c %g "${renders[0]}")" || return 1
  cards=("$dri"/card*)
  if [ -e "${cards[0]}" ]; then
    video_gid="$(stat -c %g "${cards[0]}")" || return 1
  else
    video_gid="$render_gid"
  fi
  printf '%s %s' "$video_gid" "$render_gid"
}

# resolve_gpu_vram_mb VENDOR -> echoes the GPU's total VRAM in MB, or returns 1
# when it cannot be measured (unknown vendor, missing tool, missing fields).
# nvidia reads nvidia-smi; amd parses `amd-smi static --json` tolerant of
# missing fields and of both size shapes ({"value": N, "unit": "MB"} and a
# plain number, treated as MB). Intel iGPUs share system RAM — no VRAM figure,
# so callers keep their conservative CPU-tier defaults.
resolve_gpu_vram_mb() {
  local vendor="$1" smi mb
  case "$vendor" in
    nvidia)
      smi="$(resolve_nvidia_smi)" || return 1
      mb="$("$smi" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')" || mb=""
      ;;
    amd)
      smi="$(resolve_amd_smi)" || return 1
      # -c (not a heredoc program) so the piped JSON stays on stdin.
      mb="$("$smi" static --json 2>/dev/null | python3 -c '
import json
import sys

def size_mb(vram):
    if not isinstance(vram, dict):
        return None
    size = vram.get("size", vram.get("size_mb"))
    if isinstance(size, dict):
        value = size.get("value")
        unit = str(size.get("unit", "MB")).upper()
        if isinstance(value, (int, float)):
            return float(value) * 1024 if unit == "GB" else float(value)
        return None
    if isinstance(size, (int, float)):
        return float(size)
    return None

try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
gpus = data if isinstance(data, list) else [data]
sizes = [s for g in gpus if isinstance(g, dict) for s in [size_mb(g.get("vram"))] if s]
if not sizes:
    sys.exit(1)
print(int(max(sizes)))
')" || mb=""
      ;;
    *) return 1 ;;
  esac
  case "$mb" in
    ''|*[!0-9]*) return 1 ;;
  esac
  printf '%s' "$mb"
}

# strip_gpu_args ARGS... -> echoes ARGS with any --gpu selection removed, one
# arg per line: both the `--gpu VALUE` pair and the `--gpu=VALUE` form. setup.sh
# rebuilds the interactive CPU-retry re-exec argv from this so its appended
# `--gpu cpu` is the only GPU flag and the retry cannot loop back into the
# overlay path. One arg per line (not space-joined) so a value containing spaces
# survives intact; callers read it back with a while-read loop.
strip_gpu_args() {
  local a skip_val=0
  for a in "$@"; do
    if [ "$skip_val" -eq 1 ]; then skip_val=0; continue; fi
    case "$a" in
      --gpu)   skip_val=1 ;;
      --gpu=*) ;;
      *)       printf '%s\n' "$a" ;;
    esac
  done
}



# compose_meets_floor VERSION FLOOR -> 0 when VERSION >= FLOOR (dotted numeric,
# an optional leading 'v' and build suffix tolerated), 1 when it is older, 2
# when either value is unreadable. Keep this pure Bash: stock macOS `sort` has
# no GNU `-V` flag. Used to pin a real Compose floor instead of accepting any
# v2: the accelerator overlays merge a dev override's `deploy: !reset null`, and
# the `!reset`/`!override` merge tags require Docker Compose 2.24.4+.
compose_meets_floor() {
  local ver="${1#v}" floor="${2#v}" ver_core floor_core
  local ver_major ver_minor ver_patch ver_extra
  local floor_major floor_minor floor_patch floor_extra
  [ "$ver" != unknown ] && [ "$floor" != unknown ] || return 2
  ver_core="${ver%%+*}"; ver_core="${ver_core%%-*}"
  floor_core="${floor%%+*}"; floor_core="${floor_core%%-*}"
  IFS=. read -r ver_major ver_minor ver_patch ver_extra <<< "$ver_core"
  IFS=. read -r floor_major floor_minor floor_patch floor_extra <<< "$floor_core"
  [ -z "${ver_extra:-}" ] && [ -z "${floor_extra:-}" ] || return 2
  case "${ver_major:-}:${ver_minor:-}:${ver_patch:-}:${floor_major:-}:${floor_minor:-}:${floor_patch:-}" in
    *[!0-9:]*) return 2 ;;
  esac
  [ -n "${ver_major:-}" ] && [ -n "${ver_minor:-}" ] && [ -n "${ver_patch:-}" ] \
    && [ -n "${floor_major:-}" ] && [ -n "${floor_minor:-}" ] && [ -n "${floor_patch:-}" ] \
    || return 2
  [ "$ver_major" -gt "$floor_major" ] && return 0
  [ "$ver_major" -lt "$floor_major" ] && return 1
  [ "$ver_minor" -gt "$floor_minor" ] && return 0
  [ "$ver_minor" -lt "$floor_minor" ] && return 1
  [ "$ver_patch" -gt "$floor_patch" ] && return 0
  [ "$ver_patch" -lt "$floor_patch" ] && return 1
  # A prerelease of the exact floor is older than the stable floor. A `+...`
  # suffix is build/package metadata and does not change precedence.
  case "${ver%%+*}" in
    *-*) return 1 ;;
  esac
  return 0
}

# rewrite_prereq_command COMMAND NONINTERACTIVE EFFECTIVE_UID
# Plans use one line-leading sudo for privileged commands. Root does not need
# sudo; an unattended non-root run must not block on a password prompt.
rewrite_prereq_command() {
  local command="$1" noninteractive="${2:-0}" effective_uid="$3"
  case "$command" in
    sudo\ *)
      if [ "$effective_uid" -eq 0 ]; then
        printf '%s' "${command#sudo }"
      elif [ "$noninteractive" -eq 1 ]; then
        printf 'sudo -n %s' "${command#sudo }"
      else
        printf '%s' "$command"
      fi
      ;;
    *) printf '%s' "$command" ;;
  esac
}

mkcert_toolchain_available() {
  command -v mkcert >/dev/null 2>&1 || return 1
  if [ "$(uname -s 2>/dev/null || printf unknown)" = Darwin ]; then
    command -v brew >/dev/null 2>&1 \
      && brew list --versions nss >/dev/null 2>&1
  else
    command -v certutil >/dev/null 2>&1
  fi
}

# is_wsl_host -> 0 when setup is running in WSL. The Windows launcher sets an
# explicit marker so routing stays deterministic even when a test fixture or an
# unusual kernel string hides Microsoft's usual WSL identifiers.
is_wsl_host() {
  [ "${JARVIS_WINDOWS_LAUNCHER:-0}" = 1 ] && return 0
  [ -n "${WSL_INTEROP:-}" ] && return 0
  [ -n "${WSL_DISTRO_NAME:-}" ] && return 0
  grep -qi microsoft "${JARVIS_PROC_VERSION:-/proc/version}" 2>/dev/null
}

# _wsl_without_systemd -> 0 when running under WSL (a Microsoft kernel) with no
# systemd as PID 1. On such a host the docker-ce + `systemctl` install plan
# starts a SECOND daemon that shadows Docker Desktop's and cannot be enabled
# (systemctl fails without systemd); the correct fix is to turn on Docker
# Desktop's WSL integration instead. Probes are env-overridable for testing:
# JARVIS_PROC_VERSION (the kernel version string) and JARVIS_SYSTEMD_DIR (a
# present directory means systemd is running).
_wsl_without_systemd() {
  is_wsl_host || return 1
  [ -d "${JARVIS_SYSTEMD_DIR:-/run/systemd/system}" ] && return 1
  return 0
}

# prereq_install_plan OS OS_ID HAS_APT HAS_BREW HAS_DNF MISSING...
# Prints explicit package-manager commands for supported hosts, one command per
# line. Docker comes from Docker's official repository (docker-ce +
# docker-compose-plugin): stock distro packages miss the compose plugin on
# Debian/Ubuntu and lag Engine releases. A WSL host without systemd gets NO
# docker plan (return 1) — see _wsl_without_systemd; the manual guidance points
# it at Docker Desktop's WSL integration instead. Root-escalation contract:
# setup.sh prints the plan verbatim for consent; root drops one line-leading
# sudo, while unattended non-root runs use `sudo -n`. Every line is unprivileged
# or starts with exactly one sudo. Remote content is never piped into a shell.
# `nvidia-toolkit` in MISSING...
# appends the NVIDIA Container Toolkit + docker runtime wiring. Returns
# non-zero when the host cannot be installed safely. This function only plans;
# setup.sh decides whether to prompt and execute the plan.
prereq_install_plan() {
  local os="$1" os_id="$2" has_apt="$3" has_brew="$4" has_dnf="$5"
  shift 5
  local needs_docker=0 needs_compose=0 needs_openssl=0 needs_toolkit=0
  local needs_python3=0 needs_curl=0 needs_mkcert=0 item
  for item in "$@"; do
    case "$item" in
      docker) needs_docker=1 ;;
      docker-compose) needs_compose=1 ;;
      openssl) needs_openssl=1 ;;
      nvidia-toolkit) needs_toolkit=1 ;;
      python3) needs_python3=1 ;;
      curl) needs_curl=1 ;;
      mkcert) needs_mkcert=1 ;;
    esac
  done

  # WSL without systemd: refuse to auto-plan a docker-ce install. It would stand
  # up a second, systemctl-less daemon shadowing Docker Desktop's; the caller
  # falls through to prereq_manual_guidance, which points at Docker Desktop's WSL
  # integration instead.
  if { [ "$needs_docker" = "1" ] || [ "$needs_compose" = "1" ]; } \
     && _wsl_without_systemd; then
    return 1
  fi

  case "$os" in
    Linux)
      case "$os_id" in
        debian|ubuntu|linuxmint|pop|popos)
          [ "$has_apt" = "1" ] || return 1
          _prereq_plan_apt "$os_id" "$needs_docker" "$needs_compose" "$needs_openssl" "$needs_toolkit" "$needs_python3" "$needs_curl" "$needs_mkcert"
          ;;
        fedora)
          [ "$has_dnf" = "1" ] || return 1
          _prereq_plan_dnf "$needs_docker" "$needs_compose" "$needs_openssl" "$needs_toolkit" "$needs_python3" "$needs_curl" "$needs_mkcert"
          ;;
        *) return 1 ;;
      esac
      ;;
    Darwin)
      [ "$has_brew" = "1" ] || return 1
      if [ "$needs_docker" = "1" ]; then
        printf 'brew install --cask docker\n'
      elif [ "$needs_compose" = "1" ]; then
        printf 'brew upgrade --cask docker\n'
      fi
      local brew_packages=()
      [ "$needs_openssl" = "1" ] && brew_packages+=(openssl)
      [ "$needs_python3" = "1" ] && brew_packages+=(python)
      [ "$needs_curl" = "1" ] && brew_packages+=(curl)
      if [ "$needs_mkcert" = "1" ]; then
        brew_packages+=(mkcert nss)
      fi
      if [ "${#brew_packages[@]}" -gt 0 ]; then
        printf 'brew install %s\n' "${brew_packages[*]}"
      fi
      ;;
    *) return 1 ;;
  esac
}

# tailscale_install_plan OS OS_ID CODENAME HAS_APT HAS_DNF HAS_SYSTEMD
# Prints the reviewed package-manager commands for the first-class private
# HTTPS route. The caller previews the plan and obtains consent before running
# it. macOS and WSL without systemd stay manual because their Tailscale clients
# require an app/system-extension flow that a shell installer cannot complete.
tailscale_install_plan() {
  local os="$1" os_id="$2" codename="$3" has_apt="$4" has_dnf="$5" has_systemd="$6"
  local repo_base

  [ "$os" = "Linux" ] || return 1
  [ "$has_systemd" = "1" ] || return 1
  _wsl_without_systemd && return 1

  case "$os_id" in
    ubuntu|linuxmint|pop|popos)
      [ "$has_apt" = "1" ] || return 1
      repo_base=ubuntu
      ;;
    debian)
      [ "$has_apt" = "1" ] || return 1
      repo_base=debian
      ;;
    fedora)
      [ "$has_dnf" = "1" ] || return 1
      printf 'sudo dnf install -y ca-certificates curl\n'
      printf 'sudo install -d -m 0755 /etc/yum.repos.d\n'
      printf 'sudo curl -fsSL https://pkgs.tailscale.com/stable/fedora/tailscale.repo -o /etc/yum.repos.d/tailscale.repo\n'
      printf 'sudo dnf install -y tailscale\n'
      printf 'sudo systemctl enable --now tailscaled\n'
      return 0
      ;;
    *) return 1 ;;
  esac

  # The codename becomes part of two repository URLs. Accept only the shape
  # used by /etc/os-release; do not let host metadata introduce shell syntax
  # or path traversal into a command that will later run with sudo.
  case "$codename" in
    ''|*[!a-z0-9._-]*) return 1 ;;
  esac

  printf 'sudo apt-get update\n'
  printf 'sudo apt-get install -y ca-certificates curl\n'
  printf 'sudo install -d -m 0755 /usr/share/keyrings\n'
  printf 'sudo curl -fsSL https://pkgs.tailscale.com/stable/%s/%s.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg\n' "$repo_base" "$codename"
  printf 'sudo chmod 0644 /usr/share/keyrings/tailscale-archive-keyring.gpg\n'
  printf 'sudo curl -fsSL https://pkgs.tailscale.com/stable/%s/%s.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list\n' "$repo_base" "$codename"
  printf 'sudo apt-get update\n'
  printf 'sudo apt-get install -y tailscale\n'
  printf 'sudo systemctl enable --now tailscaled\n'
}

# tailscale_serve_https PORT NON_INTERACTIVE
# Configure the daemon-owned Serve state with the privilege level Tailscale
# requires. Interactive runs show sudo's exact command and may prompt;
# unattended runs use sudo -n so they fail instead of hanging on a password
# prompt. Root callers execute tailscale directly.
tailscale_serve_https() {
  local port="$1" non_interactive="$2" uid
  local -a command=(
    tailscale serve --bg --yes --https=443 "http://127.0.0.1:${port}"
  )

  uid="$(id -u)" || return $?
  if [ "$uid" -eq 0 ]; then
    "${command[@]}"
  elif [ "$non_interactive" -eq 1 ]; then
    sudo -n "${command[@]}"
  else
    printf '  sudo'
    printf ' %s' "${command[@]}"
    printf '\n'
    sudo "${command[@]}"
  fi
}

# tailscale_serve_config_is_jarvis_only PORT
# Read `tailscale serve status --json` on stdin. Return 0 only when the whole
# node-scoped Serve configuration is the one route setup.sh owns, 3 when no
# Serve configuration exists, and 1 for malformed/shared/unexpected state.
# The reset command is global, so strict shape matching is intentional.
tailscale_serve_config_is_jarvis_only() {
  local port="$1"
  python3 -c '
import json
import sys

port = sys.argv[1]
try:
    config = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError):
    raise SystemExit(1)
if config == {}:
    raise SystemExit(3)
if not isinstance(config, dict) or set(config) != {"TCP", "Web"}:
    raise SystemExit(1)
if config.get("TCP") != {"443": {"HTTPS": True}}:
    raise SystemExit(1)
web = config.get("Web")
if not isinstance(web, dict) or len(web) != 1:
    raise SystemExit(1)
host_port, server = next(iter(web.items()))
expected = {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}
raise SystemExit(0 if host_port.endswith(":443") and server == expected else 1)
' "$port"
}

# tailscale_serve_route_classification OLD_PORT TRUSTED_PORT
# Read Tailscale's JSON status on stdin and classify only an exact singleton
# HTTPS-443 loopback proxy. Output: none, legacy, healthy, or custom.
tailscale_serve_route_classification() {
  local old_port="$1" trusted_port="$2"
  python3 -c '
import json
import sys
from urllib.parse import urlsplit

old_port, trusted_port = sys.argv[1:]
try:
    config = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError):
    print("custom")
    raise SystemExit(0)
if config == {}:
    print("none")
    raise SystemExit(0)
if not isinstance(config, dict) or set(config) != {"TCP", "Web"}:
    print("custom")
    raise SystemExit(0)
if config.get("TCP") != {"443": {"HTTPS": True}}:
    print("custom")
    raise SystemExit(0)
web = config.get("Web")
if not isinstance(web, dict) or len(web) != 1:
    print("custom")
    raise SystemExit(0)
host_port, server = next(iter(web.items()))
if not host_port.endswith(":443") or not isinstance(server, dict):
    print("custom")
    raise SystemExit(0)
handlers = server.get("Handlers")
if not isinstance(handlers, dict) or set(handlers) != {"/"}:
    print("custom")
    raise SystemExit(0)
handler = handlers.get("/")
if not isinstance(handler, dict) or set(handler) != {"Proxy"}:
    print("custom")
    raise SystemExit(0)
target = urlsplit(handler["Proxy"] if isinstance(handler["Proxy"], str) else "")
if (target.scheme != "http" or target.hostname != "127.0.0.1"
        or target.path not in ("", "/") or target.query or target.fragment):
    print("custom")
    raise SystemExit(0)
try:
    port = str(target.port) if target.port is not None else ""
except ValueError:
    print("custom")
    raise SystemExit(0)
print("legacy" if port == old_port else "healthy" if port == trusted_port else "custom")
' "$old_port" "$trusted_port"
}

# tailscale_legacy_route_notice OLD_PORT TRUSTED_PORT
# Read-only upgrade diagnosis. It never mutates node-global Serve state and
# prints a targeted command only when an operator confirms route ownership.
tailscale_legacy_route_notice() {
  local old_port="$1" trusted_port="$2" uid status classification
  case "$old_port:$trusted_port" in
    *[!0-9:]*|:*|*:) warn "Tailscale Serve diagnosis skipped: invalid dashboard ports; inspect the persisted port settings."; return 0 ;;
  esac
  if [ "$old_port" -lt 1 ] || [ "$old_port" -gt 65535 ] \
     || [ "$trusted_port" -lt 1 ] || [ "$trusted_port" -gt 65535 ] \
     || [ "$old_port" -eq "$trusted_port" ]; then
    warn "Tailscale Serve diagnosis skipped: invalid dashboard ports; inspect the persisted port settings."
    return 0
  fi
  command -v tailscale >/dev/null 2>&1 || return 0
  uid="$(id -u 2>/dev/null)" || {
    warn "Could not inspect Tailscale Serve; inspect Tailscale Serve before changing it."
    return 0
  }
  if [ "$uid" -eq 0 ]; then
    status="$(tailscale serve status --json 2>/dev/null)" || status=""
  else
    status="$(sudo -n tailscale serve status --json 2>/dev/null)" || status=""
  fi
  if [ -z "$status" ]; then
    warn "Could not inspect Tailscale Serve; inspect Tailscale Serve before changing it."
    return 0
  fi
  classification="$(printf '%s' "$status" | \
    tailscale_serve_route_classification "$old_port" "$trusted_port")" \
    || classification=custom
  case "$classification" in
    none|healthy) return 0 ;;
    legacy)
      warn "Tailscale Serve still targets the legacy dashboard port ${old_port}."
      printf '  Before changing it, confirm that the existing HTTPS 443 route belongs to this JARVIS installation.\n'
      printf '  Then run: sudo tailscale serve --bg --yes --https=443 http://127.0.0.1:%s\n' "$trusted_port"
      ;;
    *)
      warn "Tailscale Serve is shared, custom, malformed, or unreadable; inspect Tailscale Serve before changing it."
      printf '  Inspect with: sudo tailscale serve status --json\n'
      ;;
  esac
  return 0
}

# tailscale_serve_https_off PORT NON_INTERACTIVE
# Use the documented `serve reset` primitive only after inspecting live state;
# targeted `off` grammar has varied between client versions. Reset only when
# the complete configuration is exactly JARVIS's 443 -> loopback route. Refuse
# a shared or unfamiliar config rather than deleting another operator's routes.
tailscale_serve_https_off() {
  local port="$1" non_interactive="$2" uid
  local status classification_rc

  uid="$(id -u)" || return $?
  if [ "$uid" -eq 0 ]; then
    status="$(tailscale serve status --json)" || return $?
  elif [ "$non_interactive" -eq 1 ]; then
    status="$(sudo -n tailscale serve status --json)" || return $?
  else
    printf '  sudo tailscale serve status --json\n'
    status="$(sudo tailscale serve status --json)" || return $?
  fi

  if printf '%s' "$status" | tailscale_serve_config_is_jarvis_only "$port"; then
    :
  else
    classification_rc=$?
    if [ "$classification_rc" -eq 3 ]; then
      return 0
    fi
    printf 'Refusing to reset Tailscale Serve: the active configuration is not exclusively the JARVIS route to 127.0.0.1:%s.\n' "$port" >&2
    return 64
  fi

  if [ "$uid" -eq 0 ]; then
    tailscale serve reset
  elif [ "$non_interactive" -eq 1 ]; then
    sudo -n tailscale serve reset
  else
    printf '  sudo tailscale serve reset\n'
    sudo tailscale serve reset
  fi
}

# access_edge_retirements OLD_MODE OLD_PROFILES NEW_MODE NEW_PROFILES
# Print the JARVIS-owned access edges present in the old configuration but not
# the replacement, one `profile|service` row per edge. Tailscale is a host route
# rather than a Compose profile and is represented as `tailscale|tailscale`.
access_edge_retirements() {
  local old_mode="$1" old_profiles="$2" new_mode="$3" new_profiles="$4"
  local old_has new_has

  old_has=0; new_has=0
  [ "$old_mode" = "tailscale" ] && old_has=1
  [ "$new_mode" = "tailscale" ] && new_has=1
  [ "$old_has" -eq 1 ] && [ "$new_has" -eq 0 ] && printf 'tailscale|tailscale\n'

  old_has=0; new_has=0
  case ",${old_profiles}," in *,tunnel,*) old_has=1 ;; esac
  [ "$old_mode" = "tunnel" ] && old_has=1
  case ",${new_profiles}," in *,tunnel,*) new_has=1 ;; esac
  [ "$new_mode" = "tunnel" ] && new_has=1
  [ "$old_has" -eq 1 ] && [ "$new_has" -eq 0 ] && printf 'tunnel|cloudflared\n'

  old_has=0; new_has=0
  case ",${old_profiles}," in *,caddy-local,*) old_has=1 ;; esac
  case ",${new_profiles}," in *,caddy-local,*) new_has=1 ;; esac
  [ "$old_has" -eq 1 ] && [ "$new_has" -eq 0 ] && printf 'caddy-local|caddy_local\n'

  old_has=0; new_has=0
  case ",${old_profiles}," in *,letsencrypt,*) old_has=1 ;; esac
  [ "$old_mode" = "letsencrypt" ] && old_has=1
  case ",${new_profiles}," in *,letsencrypt,*) new_has=1 ;; esac
  [ "$new_mode" = "letsencrypt" ] && new_has=1
  [ "$old_has" -eq 1 ] && [ "$new_has" -eq 0 ] && printf 'letsencrypt|caddy\n'
  return 0
}

# quiesce_previous_access_runtime OLD_MODE OLD_PROFILES NEW_MODE NEW_PROFILES
#   OLD_TAILSCALE_PORT NON_INTERACTIVE PROJECT_DIR ENV_FILE
#
# Stop every old JARVIS-owned edge that the replacement will retire before the
# replacement marker is probed. Otherwise an unchanged hostname (or a DNS alias)
# can let the old edge answer the probe, after which deleting that edge would
# turn a reported success into an outage. This is reversible: the caller keeps
# the old tunnel credential and invokes rollback_access_runtime on any failure.
quiesce_previous_access_runtime() {
  local old_mode="$1" old_profiles="$2" new_mode="$3" new_profiles="$4"
  local old_tailscale_port="$5" non_interactive="$6"
  local project_dir="$7" env_file="$8" edge service failed=0

  while IFS='|' read -r edge service; do
    [ -n "$edge" ] || continue
    case "$edge" in
      tailscale)
        tailscale_serve_https_off "$old_tailscale_port" "$non_interactive" \
          || failed=1
        ;;
      tunnel|caddy-local|letsencrypt)
        access_rollback_compose "$project_dir" "$env_file" \
          --profile "$edge" rm -sf "$service" || failed=1
        ;;
    esac
  done < <(access_edge_retirements "$old_mode" "$old_profiles" \
    "$new_mode" "$new_profiles")

  [ "$failed" -eq 0 ]
}

_setup_transaction_path_is_safe() {
  local transaction_dir="$1"
  [ -n "$transaction_dir" ] || return 1
  case "${transaction_dir##*/}" in
    .jarvis-setup-transaction) return 0 ;;
    *) return 1 ;;
  esac
}

# setup_transaction_owner_state DIR PHASE
#
# Inspect, but never delete, a pending or active transaction. Return 0 when its
# recorded process is live, 1 when the owner is gone, and 2 for an invalid path
# or owner record. Retaining abandoned staging avoids a validate-then-delete race
# in which another setup could create a new lock at the same pathname.
setup_transaction_owner_state() {
  local transaction_dir="$1" phase="$2" state_dir owner_pid
  _setup_transaction_path_is_safe "$transaction_dir" || return 2
  case "$phase" in
    active) state_dir="$transaction_dir" ;;
    pending) state_dir="${transaction_dir}.pending" ;;
    *) return 2 ;;
  esac
  [ -d "$state_dir" ] && [ ! -L "$state_dir" ] || return 2
  [ -f "$state_dir/owner_pid" ] && [ ! -L "$state_dir/owner_pid" ] \
    || return 2
  owner_pid="$(cat "$state_dir/owner_pid")" || return 2
  case "$owner_pid" in ''|*[!0-9]*) return 2 ;; esac
  [ "$owner_pid" -gt 1 ] || return 2
  if kill -0 "$owner_pid" 2>/dev/null; then
    return 0
  fi
  return 1
}

# acquire_setup_transaction_lock DIR / release_setup_transaction_lock DIR
#
# The pending directory is the one atomic mutex shared by journal creation and
# interrupted-journal recovery. An abandoned lock is never reaped automatically:
# deleting by a reused pathname cannot be made race-free without platform-specific
# locking. Release removes only a lock whose owner record matches this process.
acquire_setup_transaction_lock() {
  local transaction_dir="$1" pending_dir
  _setup_transaction_path_is_safe "$transaction_dir" || return 2
  pending_dir="${transaction_dir}.pending"
  [ ! -e "$pending_dir" ] && [ ! -L "$pending_dir" ] || return 4
  mkdir -m 700 "$pending_dir" || return 4
  if ! printf '%s' "$$" > "$pending_dir/owner_pid" \
      || ! chmod 600 "$pending_dir/owner_pid"; then
    rm -rf -- "$pending_dir"
    return 1
  fi
}

release_setup_transaction_lock() {
  local transaction_dir="$1" pending_dir owner_pid
  _setup_transaction_path_is_safe "$transaction_dir" || return 2
  pending_dir="${transaction_dir}.pending"
  [ -d "$pending_dir" ] && [ ! -L "$pending_dir" ] \
    && [ -f "$pending_dir/owner_pid" ] \
    && [ ! -L "$pending_dir/owner_pid" ] || return 1
  owner_pid="$(cat "$pending_dir/owner_pid")" || return 1
  [ "$owner_pid" = "$$" ] || return 1
  rm -rf -- "$pending_dir"
}

_snapshot_optional_setup_secret() {
  local source="$1" snapshot_dir="$2" name="$3"
  if [ -e "$source" ]; then
    [ -f "$source" ] && [ ! -L "$source" ] || return 1
    cp "$source" "${snapshot_dir}/${name}" || return 1
    chmod 600 "${snapshot_dir}/${name}" || return 1
    printf 'present' > "${snapshot_dir}/${name}.state"
  else
    printf 'absent' > "${snapshot_dir}/${name}.state"
  fi
  chmod 600 "${snapshot_dir}/${name}.state"
}

# begin_setup_transaction DIR ENV_FILE SECRETS_DIR OLD_MODE OLD_PROFILES
#   OLD_TAILSCALE_PORT OLD_ORIGIN OLD_DASHBOARD_PORT NEW_MODE NEW_PROFILES
#   NEW_TAILSCALE_PORT
#
# Persist the entire setup-owned credential/config rollback boundary before the
# first mutation. The fixed, gitignored directory survives SIGKILL; an atomic
# rename means a rerun sees either no transaction or a complete one.
begin_setup_transaction() {
  local transaction_dir="$1" env_file="$2" secrets_dir="$3"
  local old_mode="$4" old_profiles="$5" old_tailscale_port="$6"
  local old_origin="$7" old_dashboard_port="$8" new_mode="$9"
  local new_profiles="${10}" new_tailscale_port="${11}"
  local pending_dir key value

  _setup_transaction_path_is_safe "$transaction_dir" || return 2
  [ -f "$env_file" ] || return 1
  [ ! -e "$transaction_dir" ] || return 3
  pending_dir="${transaction_dir}.pending"
  acquire_setup_transaction_lock "$transaction_dir" || return $?
  # The pending directory is the mutation lock. Recheck the journal after taking
  # it so a setup that promoted its own lock between our first two tests cannot
  # be snapshotted over.
  if [ -e "$transaction_dir" ] || [ -L "$transaction_dir" ]; then
    release_setup_transaction_lock "$transaction_dir" || true
    return 3
  fi
  if ! mkdir -m 700 "$pending_dir/metadata" "$pending_dir/secrets"; then
    release_setup_transaction_lock "$transaction_dir" || true
    return 1
  fi
  if ! cp "$env_file" "$pending_dir/old.env" \
      || ! chmod 600 "$pending_dir/old.env" \
      || ! _snapshot_optional_setup_secret \
        "$secrets_dir/cloudflare_tunnel_token.txt" "$pending_dir/secrets" \
        cloudflare_tunnel_token.txt \
      || ! _snapshot_optional_setup_secret \
        "$secrets_dir/smtp_pass.txt" "$pending_dir/secrets" smtp_pass.txt \
      || ! _snapshot_optional_setup_secret \
        "$secrets_dir/telegram_bot_token.txt" "$pending_dir/secrets" \
        telegram_bot_token.txt; then
    release_setup_transaction_lock "$transaction_dir" || true
    return 1
  fi

  for key in old_mode old_profiles old_tailscale_port old_origin \
      old_dashboard_port new_mode new_profiles new_tailscale_port \
      tailscale_attempted; do
    case "$key" in
      old_mode) value="$old_mode" ;;
      old_profiles) value="$old_profiles" ;;
      old_tailscale_port) value="$old_tailscale_port" ;;
      old_origin) value="$old_origin" ;;
      old_dashboard_port) value="$old_dashboard_port" ;;
      new_mode) value="$new_mode" ;;
      new_profiles) value="$new_profiles" ;;
      new_tailscale_port) value="$new_tailscale_port" ;;
      tailscale_attempted) value=0 ;;
    esac
    printf '%s' "$value" > "$pending_dir/metadata/$key" || {
      release_setup_transaction_lock "$transaction_dir" || true; return 1;
    }
    chmod 600 "$pending_dir/metadata/$key" || {
      release_setup_transaction_lock "$transaction_dir" || true; return 1;
    }
  done
  printf 'active' > "$pending_dir/active" \
    || { release_setup_transaction_lock "$transaction_dir" || true; return 1; }
  chmod 600 "$pending_dir/active" \
    || { release_setup_transaction_lock "$transaction_dir" || true; return 1; }
  if [ -e "$transaction_dir" ] || [ -L "$transaction_dir" ] \
      || ! mv "$pending_dir" "$transaction_dir"; then
    release_setup_transaction_lock "$transaction_dir" || true
    return 1
  fi
}

# setup_transaction_value DIR KEY -> validated one-line metadata value.
setup_transaction_value() {
  local transaction_dir="$1" key="$2" value
  _setup_transaction_path_is_safe "$transaction_dir" || return 2
  case "$key" in
    old_mode|old_profiles|old_tailscale_port|old_origin|old_dashboard_port|\
    new_mode|new_profiles|new_tailscale_port|tailscale_attempted) ;;
    *) return 2 ;;
  esac
  [ -f "$transaction_dir/active" ] \
    && [ -f "$transaction_dir/metadata/$key" ] || return 1
  value="$(cat "$transaction_dir/metadata/$key")" || return 1
  case "$value" in *$'\n'*|*$'\r'*) return 1 ;; esac
  printf '%s' "$value"
}

mark_setup_transaction_tailscale_attempted() {
  local transaction_dir="$1" tmp
  _setup_transaction_path_is_safe "$transaction_dir" || return 2
  [ -f "$transaction_dir/active" ] || return 1
  tmp="$transaction_dir/metadata/tailscale_attempted.tmp.$$"
  printf '1' > "$tmp" && chmod 600 "$tmp" \
    && mv "$tmp" "$transaction_dir/metadata/tailscale_attempted"
}

_restore_optional_setup_secret() {
  local snapshot_dir="$1" target_dir="$2" name="$3" state tmp
  [ -f "$snapshot_dir/${name}.state" ] || return 1
  state="$(cat "$snapshot_dir/${name}.state")" || return 1
  case "$state" in
    present)
      [ -f "$snapshot_dir/$name" ] || return 1
      tmp="$(mktemp "$target_dir/${name}.restore.XXXXXX")" || return 1
      if ! cp "$snapshot_dir/$name" "$tmp" || ! chmod 644 "$tmp" \
          || ! mv "$tmp" "$target_dir/$name"; then
        rm -f "$tmp"
        return 1
      fi
      ;;
    absent) rm -f "$target_dir/$name" ;;
    *) return 1 ;;
  esac
}

# restore_setup_secret_snapshot SNAPSHOT_DIR TARGET_SECRETS_DIR
restore_setup_secret_snapshot() {
  local snapshot_dir="$1" target_dir="$2" name failed=0
  mkdir -p "$target_dir" || return 1
  chmod 700 "$target_dir" || return 1
  for name in cloudflare_tunnel_token.txt smtp_pass.txt telegram_bot_token.txt; do
    _restore_optional_setup_secret "$snapshot_dir" "$target_dir" "$name" \
      || failed=1
  done
  [ "$failed" -eq 0 ]
}

# setup_secret_snapshot_is_complete SNAPSHOT_DIR
# Require an explicit present/absent record for every setup-owned credential.
# Rollback callers use this before stopping any live edge, so an incomplete
# journal cannot turn a recoverable route failure into a partial restoration.
setup_secret_snapshot_is_complete() {
  local snapshot_dir="$1" name state state_path secret_path
  [ -d "$snapshot_dir" ] && [ ! -L "$snapshot_dir" ] || return 1
  for name in cloudflare_tunnel_token.txt smtp_pass.txt telegram_bot_token.txt; do
    state_path="${snapshot_dir}/${name}.state"
    secret_path="${snapshot_dir}/${name}"
    [ -f "$state_path" ] && [ ! -L "$state_path" ] || return 1
    state="$(cat "$state_path")" || return 1
    case "$state" in
      present)
        [ -f "$secret_path" ] && [ ! -L "$secret_path" ] || return 1
        ;;
      absent)
        [ ! -e "$secret_path" ] && [ ! -L "$secret_path" ] || return 1
        ;;
      *) return 1 ;;
    esac
  done
}

discard_setup_transaction() {
  local transaction_dir="$1"
  _setup_transaction_path_is_safe "$transaction_dir" || return 2
  [ ! -e "$transaction_dir" ] && return 0
  [ -f "$transaction_dir/active" ] || return 1
  rm -rf -- "$transaction_dir"
}

# restore_env_snapshot SNAPSHOT TARGET
# Replace TARGET atomically from SNAPSHOT without consuming the snapshot.
# The temporary file lives next to TARGET, so the final rename is same-filesystem.
restore_env_snapshot() {
  local snapshot="$1" target="$2" tmp
  [ -f "$snapshot" ] || return 1
  tmp="$(mktemp "${target}.restore.XXXXXX")" || return $?
  if ! cp "$snapshot" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  chmod 600 "$tmp" || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$target"
}

# restore_secret_from_env ENV_FILE KEY TARGET
# Reconcile one file-backed credential after an .env rollback without writing
# the value to stdout or argv. An empty/absent restored value removes the
# replacement credential; a non-empty value is replaced atomically.
restore_secret_from_env() {
  local env_file="$1" key="$2" target="$3" value tmp
  case "$key" in ''|*[!A-Z0-9_]*) return 2 ;; esac
  [ -f "$env_file" ] || return 1
  value="$(awk -v key="$key" '
    index($0, key "=") == 1 {
      value = substr($0, length(key) + 2)
      sub(/\r$/, "", value)
      if (length(value) > 0) print value
      exit
    }
  ' "$env_file")"
  if [ -z "$value" ]; then
    rm -f "$target"
    return 0
  fi
  tmp="$(mktemp "${target}.restore.XXXXXX")" || return $?
  if ! printf '%s' "$value" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  chmod 644 "$tmp" || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$target"
}

# wait_for_jarvis_marker URL [ATTEMPTS] [DELAY_SECONDS] [CA_FILE]
# Return only after the exact, bounded JARVIS marker probe succeeds. This is used
# by access rollback as well as setup verification: a container being "up" is
# not evidence that its restored origin reaches this installation.
wait_for_jarvis_marker() {
  local url="$1" attempts="${2:-12}" delay_seconds="${3:-5}"
  local ca_file="${4:-}" attempt=0
  while [ "$attempt" -lt "$attempts" ]; do
    if [ "$(probe_external_app "$url" "$ca_file")" = "verified" ]; then
      return 0
    fi
    attempt=$((attempt + 1))
    [ "$attempt" -lt "$attempts" ] && sleep "$delay_seconds"
  done
  return 1
}

# wait_for_local_https_marker URL [ATTEMPTS] [DELAY_SECONDS]
# Local HTTPS has two independent requirements: the endpoint must chain to this
# checkout's mkcert CA, and the host trust store used by ordinary clients must
# accept it. Keep both checks inside the bounded retry loop.
wait_for_local_https_marker() {
  local url="$1" attempts="${2:-12}" delay_seconds="${3:-2}" attempt=0
  while [ "$attempt" -lt "$attempts" ]; do
    if [ "$(probe_local_https_app "$url")" = "verified" ]; then
      return 0
    fi
    attempt=$((attempt + 1))
    [ "$attempt" -lt "$attempts" ] && sleep "$delay_seconds"
  done
  return 1
}

# access_rollback_compose PROJECT_DIR ENV_FILE ARGS...
# Compose's control variables can be exported by a caller and override the
# checkout's .env. Rollback must target the JARVIS project that setup owns, so
# isolate it from ambient selectors and pin both the working tree and env file.
access_rollback_compose() (
  local project_dir="$1" env_file="$2"
  shift 2
  sanitize_compose_environment
  cd "$project_dir" || exit 1
  docker compose --project-directory "$project_dir" --env-file "$env_file" "$@"
)

# remove_attempted_access_runtime NEW_MODE NEW_PROFILES NEW_TAILSCALE_PORT
#   NEW_TAILSCALE_ATTEMPTED NON_INTERACTIVE PROJECT_DIR ENV_FILE
# Stop only the JARVIS-owned edge selected by the failed attempt. A Tailscale
# reset remains subject to the strict whole-config ownership check; an invocation
# that returned nonzero is still inspected because the daemon may have applied it.
remove_attempted_access_runtime() {
  local new_mode="$1" new_profiles="$2" new_tailscale_port="$3"
  local new_tailscale_attempted="$4" non_interactive="$5"
  local project_dir="$6" env_file="$7" edge service failed=0

  while IFS='|' read -r edge service; do
    [ -n "$edge" ] || continue
    case "$edge" in
      tailscale)
        if [ "$new_tailscale_attempted" -eq 1 ]; then
          tailscale_serve_https_off "$new_tailscale_port" "$non_interactive" \
            || failed=1
        fi
        ;;
      tunnel|caddy-local|letsencrypt)
        access_rollback_compose "$project_dir" "$env_file" \
          --profile "$edge" rm -sf "$service" || failed=1
        ;;
    esac
  done < <(access_edge_retirements "$new_mode" "$new_profiles" '' '')

  [ "$failed" -eq 0 ]
}

# rollback_access_runtime OLD_MODE OLD_PROFILES OLD_TAILSCALE_PORT OLD_ORIGIN
#   OLD_DASHBOARD_PORT NEW_MODE NEW_PROFILES NEW_TAILSCALE_PORT
#   NEW_TAILSCALE_ATTEMPTED NON_INTERACTIVE ENV_SNAPSHOT ENV_TARGET SECRET_TARGET_DIR
#   PROJECT_DIR SECRET_SNAPSHOT_DIR
#
# Undo a failed access replacement at the JARVIS-owned runtime boundary. Order is
# deliberate: stop only the replacement edge, restore persisted inputs, recreate
# the dashboard and previous Compose edge from those inputs, then reapply the old
# Tailscale route. The snapshot is never consumed. Return nonzero if any cleanup,
# restore, recreation, or exact-marker verification fails so the caller can print
# manual recovery without claiming that the previous route works.
rollback_access_runtime() {
  [ "$#" -eq 15 ] || return 2
  local old_mode="$1" old_profiles="$2" old_tailscale_port="$3"
  local old_origin="$4" old_dashboard_port="$5" new_mode="$6"
  local new_profiles="$7" new_tailscale_port="$8"
  local new_tailscale_attempted="$9" non_interactive="${10}"
  local env_snapshot="${11}" env_target="${12}" secret_target_dir="${13}"
  local project_dir="${14}" secret_snapshot_dir="${15}"
  local compose_env="$env_target"
  local edge service old_route env_restored=0 failed=0 has_old_tailscale=0
  local seen_services=" dashboard "
  local -a previous_profile_args=() previous_services=(dashboard)

  [ -f "$env_snapshot" ] && [ ! -L "$env_snapshot" ] \
    && setup_secret_snapshot_is_complete "$secret_snapshot_dir" || return 2

  case "$compose_env" in
    /*) ;;
    *) compose_env="${project_dir%/}/${compose_env}" ;;
  esac
  case "$secret_target_dir" in
    /*) ;;
    *) secret_target_dir="${project_dir%/}/${secret_target_dir}" ;;
  esac

  remove_attempted_access_runtime "$new_mode" "$new_profiles" \
    "$new_tailscale_port" "$new_tailscale_attempted" "$non_interactive" \
    "$project_dir" "$compose_env" || failed=1

  if restore_env_snapshot "$env_snapshot" "$env_target"; then
    env_restored=1
    restore_setup_secret_snapshot "$secret_snapshot_dir" \
      "$secret_target_dir" || failed=1
  else
    failed=1
  fi

  if [ "$env_restored" -eq 1 ]; then
    while IFS='|' read -r edge service; do
      [ -n "$edge" ] || continue
      case "$edge" in
        tailscale) has_old_tailscale=1 ;;
        tunnel|caddy-local|letsencrypt)
          previous_profile_args+=(--profile "$edge")
          case "$seen_services" in
            *" $service "*) ;;
            *) previous_services+=("$service"); seen_services="${seen_services}${service} " ;;
          esac
          ;;
      esac
    done < <(access_edge_retirements "$old_mode" "$old_profiles" '' '')

    access_rollback_compose "$project_dir" "$compose_env" \
      ${previous_profile_args[@]+"${previous_profile_args[@]}"} up -d \
      --no-build --force-recreate --no-deps "${previous_services[@]}" || failed=1

    if [ "$has_old_tailscale" -eq 1 ]; then
      tailscale_serve_https "$old_tailscale_port" "$non_interactive" || failed=1
    fi

    wait_for_jarvis_marker \
      "http://127.0.0.1:${old_dashboard_port}/health/jarvis" 15 2 || failed=1

    old_route="$(selected_https_route "$old_mode" "$old_profiles" "$old_origin")"
    case "$old_route" in
      none) ;;
      local-https)
        wait_for_local_https_marker "https://localhost:3443/health/jarvis" 12 2 \
          || failed=1
        ;;
      *)
        if [[ "$old_origin" == https://* ]]; then
          wait_for_jarvis_marker "${old_origin%/}/health/jarvis" 12 5 || failed=1
        else
          failed=1
        fi
        ;;
    esac
  fi

  [ "$failed" -eq 0 ]
}

# Docker's apt repo serves UBUNTU codename dists: Mint/Pop set VERSION_CODENAME
# to their own release name ('wilma' 404s), so the sources line derives
# ${UBUNTU_CODENAME:-$VERSION_CODENAME} from /etc/os-release when it executes.
# shellcheck disable=SC2016  # plan lines expand at execution, not planning
_prereq_plan_apt() {
  local os_id="$1" needs_docker="$2" needs_compose="$3" needs_openssl="$4" needs_toolkit="$5" needs_python3="${6:-0}"
  local needs_curl="${7:-0}" needs_mkcert="${8:-0}"
  local repo_base=ubuntu
  [ "$os_id" = "debian" ] && repo_base=debian

  printf 'sudo apt-get update\n'
  if [ "$needs_docker" = "1" ] || [ "$needs_compose" = "1" ] || [ "$needs_toolkit" = "1" ]; then
    printf 'sudo apt-get install -y ca-certificates curl gnupg\n'
  fi
  if [ "$needs_docker" = "1" ] || [ "$needs_compose" = "1" ]; then
    # Fetch the signing key straight to the root-owned keyring (Docker's own
    # documented apt method) and write the repo list through a root shell — no
    # world-writable /tmp staging that a later sudo would read back (CWE-377).
    printf 'sudo install -m 0755 -d /etc/apt/keyrings\n'
    printf 'sudo curl -fsSL https://download.docker.com/linux/%s/gpg -o /etc/apt/keyrings/docker.asc\n' "$repo_base"
    printf 'sudo chmod a+r /etc/apt/keyrings/docker.asc\n'
    printf 'sudo sh -c '\''echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" > /etc/apt/sources.list.d/docker.list'\''\n' "$repo_base"
    printf 'sudo apt-get update\n'
  fi

  local packages=()
  if [ "$needs_docker" = "1" ]; then
    packages+=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
  elif [ "$needs_compose" = "1" ]; then
    packages+=(docker-compose-plugin)
  fi
  if [ "$needs_openssl" = "1" ]; then
    packages+=(openssl)
  fi
  if [ "$needs_python3" = "1" ]; then
    packages+=(python3)
  fi
  if [ "$needs_curl" = "1" ] \
     && [ "$needs_docker" != "1" ] && [ "$needs_compose" != "1" ] \
     && [ "$needs_toolkit" != "1" ]; then
    packages+=(curl)
  fi
  if [ "$needs_mkcert" = "1" ]; then
    packages+=(mkcert libnss3-tools)
  fi
  if [ "${#packages[@]}" -gt 0 ]; then
    printf 'sudo apt-get install -y %s\n' "${packages[*]}"
  fi
  if [ "$needs_docker" = "1" ]; then
    printf 'sudo systemctl enable --now docker\n'
    printf 'sudo usermod -aG docker "$USER"\n'
  fi
  if [ "$needs_toolkit" = "1" ]; then
    # Dearmor the key through a root pipe (data into gpg, never a remote script
    # into a shell) and transform the fetched list in place at its root-owned
    # destination — again no predictable /tmp file a later sudo reads back.
    printf 'sudo sh -c '\''curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg'\''\n'
    printf 'sudo curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list -o /etc/apt/sources.list.d/nvidia-container-toolkit.list\n'
    printf 'sudo sed -i "s#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g" /etc/apt/sources.list.d/nvidia-container-toolkit.list\n'
    printf 'sudo apt-get update\n'
    printf 'sudo apt-get install -y nvidia-container-toolkit\n'
    printf 'sudo nvidia-ctk runtime configure --runtime=docker\n'
    printf 'sudo systemctl restart docker\n'
  fi
}

# Fedora mirror of _prereq_plan_apt. The repo file is fetched unprivileged and
# installed with one sudo (avoids the dnf4/dnf5 config-manager syntax split).
# shellcheck disable=SC2016  # plan lines expand at execution, not planning
_prereq_plan_dnf() {
  local needs_docker="$1" needs_compose="$2" needs_openssl="$3" needs_toolkit="$4" needs_python3="${5:-0}"
  local needs_curl="${6:-0}" needs_mkcert="${7:-0}"

  if [ "$needs_docker" = "1" ] || [ "$needs_compose" = "1" ] || [ "$needs_toolkit" = "1" ]; then
    printf 'sudo dnf install -y ca-certificates curl\n'
  fi
  if [ "$needs_docker" = "1" ] || [ "$needs_compose" = "1" ]; then
    # Fetch the repo file straight to its root-owned destination — no /tmp hop.
    printf 'sudo curl -fsSL https://download.docker.com/linux/fedora/docker-ce.repo -o /etc/yum.repos.d/docker-ce.repo\n'
  fi

  local packages=()
  if [ "$needs_docker" = "1" ]; then
    packages+=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
  elif [ "$needs_compose" = "1" ]; then
    packages+=(docker-compose-plugin)
  fi
  if [ "$needs_openssl" = "1" ]; then
    packages+=(openssl)
  fi
  if [ "$needs_python3" = "1" ]; then
    packages+=(python3)
  fi
  if [ "$needs_curl" = "1" ] \
     && [ "$needs_docker" != "1" ] && [ "$needs_compose" != "1" ] \
     && [ "$needs_toolkit" != "1" ]; then
    packages+=(curl)
  fi
  if [ "$needs_mkcert" = "1" ]; then
    packages+=(mkcert nss-tools)
  fi
  if [ "${#packages[@]}" -gt 0 ]; then
    printf 'sudo dnf install -y %s\n' "${packages[*]}"
  fi
  if [ "$needs_docker" = "1" ]; then
    printf 'sudo systemctl enable --now docker\n'
    printf 'sudo usermod -aG docker "$USER"\n'
  fi
  if [ "$needs_toolkit" = "1" ]; then
    printf 'sudo curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo -o /etc/yum.repos.d/nvidia-container-toolkit.repo\n'
    printf 'sudo dnf install -y nvidia-container-toolkit\n'
    printf 'sudo nvidia-ctk runtime configure --runtime=docker\n'
    printf 'sudo systemctl restart docker\n'
  fi
}

# prereq_manual_guidance MISSING... -> human-readable fallback for unsupported
# or non-mutating paths. Keep this free of private host paths and secrets.
prereq_manual_guidance() {
  local item wsl=0
  _wsl_without_systemd && wsl=1
  # On WSL without systemd, docker-ce is the wrong answer for any docker/compose
  # gap — point at Docker Desktop's WSL integration once, then suppress the
  # docker-ce lines below.
  if [ "$wsl" -eq 1 ]; then
    case " $* " in
      *" docker "*|*" docker-compose "*)
        printf 'On WSL, enable Docker Desktop for Windows and turn on its WSL integration (Docker Desktop > Settings > Resources > WSL integration) rather than installing Docker Engine as a package, which starts a second daemon: https://docs.docker.com/desktop/wsl/\n' ;;
    esac
  fi
  for item in "$@"; do
    case "$item" in
      docker)         [ "$wsl" -eq 1 ] || printf 'Install Docker Engine (or review-then-run the convenience script from https://get.docker.com): https://docs.docker.com/engine/install/\n' ;;
      docker-compose) [ "$wsl" -eq 1 ] || printf 'Install the Docker Compose v2 plugin: https://docs.docker.com/compose/install/linux/\n' ;;
      openssl) printf 'Install openssl with your OS package manager.\n' ;;
      curl) printf 'Install curl with your OS package manager.\n' ;;
      mkcert) printf 'Install mkcert plus browser trust tooling (libnss3-tools on Debian/Ubuntu, nss-tools on Fedora, or nss with Homebrew).\n' ;;
      nvidia-toolkit) printf 'Install the NVIDIA Container Toolkit: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html\n' ;;
      python3) printf 'Install Python 3 with your OS package manager (setup uses it for model selection and disk sizing).\n' ;;
    esac
  done
  printf 'After installing Docker, start the daemon and re-run ./setup.sh --check.\n'
}

# _gpu_present_for_prereqs -> 0 when the host has a usable NVIDIA GPU but the
# Docker daemon lacks the nvidia runtime (or is unreachable/not installed yet),
# i.e. the prereq plan should include the NVIDIA Container Toolkit. Reuses the
# WSL2-aware nvidia-smi probe; GPU-presence test mirrors detect_hw_tier.
_gpu_present_for_prereqs() {
  local smi
  smi="$(resolve_nvidia_smi)" || return 1
  "$smi" -L 2>/dev/null | grep -q . || return 1
  ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'
}

# _default_model_for_tier TIER BACKEND -> echoes the default model id for the
# tier/backend pair. Reads config/llm-tier-candidates.yaml (relative — setup.sh
# cd's to the repo root) when host python3 has PyYAML; without PyYAML or without
# the file it falls back to _OLLAMA_FALLBACK, which mirrors the YAML's per-tier
# ollama answers — keep the dict in sync when the YAML changes.
# stdout is command-substituted into .env, so diagnostics go to stderr ONLY.
# Malformed YAML must still fail loudly (no bare except).
_default_model_for_tier() {
  python3 - "$1" "$2" <<'PY'
import sys

try:
    import yaml
except ImportError:
    yaml = None

tier, backend = sys.argv[1:3]
_OLLAMA_FALLBACK = {
    "cpu": "qwen3:1.7b", "lt-8": "qwen3:1.7b",
    "8-16": "qwen2.5:7b-instruct", "16-24": "qwen2.5:7b-instruct",
    "24-48": "qwen3:14b", "ge-48": "qwen3:30b-a3b",
}


def _fallback() -> None:
    print(_OLLAMA_FALLBACK.get(tier, "qwen3:1.7b"))
    sys.exit(0)


if yaml is None:
    print(
        "[WARN] host python3 has no PyYAML — using built-in tier defaults",
        file=sys.stderr,
    )
    _fallback()
try:
    with open("config/llm-tier-candidates.yaml") as f:
        data = yaml.safe_load(f)
except (ImportError, FileNotFoundError):
    _fallback()
for c in data["tiers"].get(tier, {}).get("candidates", []):
    if c["backend"] == backend:
        print(c["model"])
        sys.exit(0)
fb = data["tiers"][tier]["fallback_for_tier"]
print(fb["model"])
PY
}

# compute_compose_file OVERLAY OVERRIDE_PRESENT -> echoes colon-joined COMPOSE_FILE.
# OVERLAY is the accelerator overlay basename ("gpu", "rocm", "vulkan") or ""
# for the CPU base; setup.sh picks it from GPU vendor + runtime detection (or
# --gpu). override.yml is appended LAST (an explicit COMPOSE_FILE suppresses
# Compose's implicit override auto-load, and overlay-before-override lets a dev
# override's `deploy: !reset null` win).
compute_compose_file() {
  local overlay="$1" override="$2" joined="docker-compose.yml"
  [ -n "$overlay" ] && joined="${joined}:docker-compose.${overlay}.yml"
  [ "$override" = "1" ] && joined="${joined}:docker-compose.override.yml"
  printf '%s' "$joined"
}

# compute_ollama_models SMART_MODEL -> echoes the comma-set ollama-bootstrap must
# pull: the chosen smart model + the fast + embed defaults, de-duplicated. Keeps
# the pulled set ⊇ the routed models so no LiteLLM alias 404s.
# _FAST/_EMBED mirror the fast/embed tags in .env.example:142 (OLLAMA_MODELS) and
# litellm/config.yaml — keep them in sync if those defaults ever change.
compute_ollama_models() {
  local smart="${1:-qwen3:8b}"
  local _FAST="qwen3:4b" _EMBED="qwen3-embedding:4b"
  case "$smart" in
    "$_FAST"|"$_EMBED") printf '%s,%s' "$_FAST" "$_EMBED" ;;
    *)                  printf '%s,%s,%s' "$smart" "$_FAST" "$_EMBED" ;;
  esac
}

# _image_budget_gb VARIANT -> GB of disk needed to ACQUIRE the app images for
# one install variant. Measured 2026-07 (build peak + 20% headroom) on the
# containerd image store — the Docker fresh-install default, which retains
# compressed blobs on top of the unpacked layers. cpu-pull is a conservative
# floor for the registry-pull install path. cuda-pull is pinned to the cuda
# build peak until a real anonymous registry pull can be measured (the packages
# are private): fail-safe — it only ever over-provisions, never ENOSPCs a pull.
_image_budget_gb() {
  case "$1" in
    cpu-pull)   printf '6' ;;
    cuda-pull)  printf '17' ;;
    cpu-build)  printf '9' ;;
    *)          printf '17' ;;  # cuda-build — the largest variant, safe default
  esac
}

# compute_model_disk_gb SMART_MODEL -> echoes the whole-GB disk the Ollama model
# set (compute_ollama_models: smart + fast + embed, de-duped) pulls, from the
# model catalog's per-tag disk_gb (tags missing from the catalog assume 18 GB
# each). The model pull runs on EVERY install — a warm re-run whose app images
# are already cached still (re-)pulls this set — so the disk preflight must
# charge it even when the app-image budget is already on disk. Returns 0 when
# catalog-derived; echoes a worst-case model-set constant and returns 3 when
# host python3 or the catalog is unusable. stdout is ONLY the number —
# diagnostics go to stderr. JARVIS_MODEL_CATALOG overrides the catalog path.
compute_model_disk_gb() {
  local smart="${1:-qwen3:8b}" worst_models_gb=22
  local catalog="${JARVIS_MODEL_CATALOG:-libs/jarvis_common/jarvis_common/data/model_catalog.json}"
  local models_gb
  models_gb="$(python3 - "$catalog" "$(compute_ollama_models "$smart")" 2>/dev/null <<'PY'
import json
import math
import sys

catalog_path, tags_csv = sys.argv[1:3]
UNKNOWN_MODEL_GB = 18.0
with open(catalog_path) as f:
    disk_by_tag = {
        entry["ollama_tag"]: float(entry.get("disk_gb") or UNKNOWN_MODEL_GB)
        for entry in json.load(f)
        if entry.get("ollama_tag")
    }
tags = [t for t in tags_csv.split(",") if t]
print(math.ceil(sum(disk_by_tag.get(t, UNKNOWN_MODEL_GB) for t in tags)))
PY
)" || models_gb=""
  if [ -n "$models_gb" ] && [ "$models_gb" -eq "$models_gb" ] 2>/dev/null; then
    printf '%s' "$models_gb"
    return 0
  fi
  printf '[WARN] model catalog unreadable (%s) — assuming a worst-case %s GB model set\n' \
    "$catalog" "$worst_models_gb" >&2
  printf '%s' "$worst_models_gb"
  return 3
}

# compute_required_disk_gb SMART_MODEL [VARIANT] -> echoes the whole-GB disk a
# cold install writes to the Docker data root: the app-image budget
# (variant-keyed, see _image_budget_gb) + infra image pulls (postgres/qdrant/
# ollama/litellm/vector, incl. containerd blob retention) + the Ollama model set
# (compute_model_disk_gb). Returns 0 when the model sum is catalog-derived; when
# host python3 or the catalog is unusable it echoes a worst-case total and
# returns 3 so callers can soften a fatal check. stdout is ONLY the number.
compute_required_disk_gb() {
  local smart="${1:-qwen3:8b}" variant="${2:-cuda-build}"
  local infra_gb=14 base_gb models_gb rc=0
  base_gb=$(( $(_image_budget_gb "$variant") + infra_gb ))
  models_gb="$(compute_model_disk_gb "$smart")" || rc=$?
  printf '%s' "$((base_gb + models_gb))"
  return "$rc"
}

# resolve_docker_data_root -> echoes the Docker data root, where image layers,
# build cache and named volumes actually land. Falls back to the Linux default
# when the daemon cannot be queried.
resolve_docker_data_root() {
  local root
  root="$(docker info -f '{{ .DockerRootDir }}' 2>/dev/null || true)"
  [ -n "$root" ] || root="/var/lib/docker"
  printf '%s' "$root"
}

# preflight_disk_lib REQUIRED_GB -> measures free space on the Docker data
# root (df -Pk there, NOT `df .` — the install dir and the data root are
# different filesystems on split-mount hosts) and compares it to REQUIRED_GB.
# stdout: "<free_gb> <data_root>". Returns 0 when free >= required, 1 on a
# shortfall, 2 when free space cannot be measured. Never hard-fails: setup.sh
# owns the fatal/warn policy around this shared core.
preflight_disk_lib() {
  local required_gb="$1" data_root free_kb
  data_root="$(resolve_docker_data_root)"
  free_kb="$(df -Pk "$data_root" 2>/dev/null | awk 'NR==2{print $4}' || true)"
  if [ -z "$free_kb" ] || ! [ "$free_kb" -eq "$free_kb" ] 2>/dev/null; then
    printf '0 %s' "$data_root"
    return 2
  fi
  printf '%s %s' "$((free_kb / 1048576))" "$data_root"
  [ "$((free_kb / 1048576))" -ge "$required_gb" ]
}

# -----------------------------------------------------------------------------
# The application images published to GHCR — single source of truth.
# -----------------------------------------------------------------------------
# Shared by every entry point that starts the stack (setup.sh and update.sh),
# because each of them must materialise these images
# BEFORE bringing the stack up. They all keep a `build:` block so contributors can
# still build from source, and that is exactly why they must be pulled BY NAME:
# `docker compose pull --ignore-buildable` skips every buildable service and would
# pull none of them. Worse, `pull_policy: missing` + `build:` means a service whose
# image is merely absent gets SILENTLY BUILT by `up` — the multi-GB torch build
# (and the ENOSPC) this release exists to eliminate. Hence: pull these, then bring
# up with `--no-build`.
#
# telegram_bot is profile-gated, so callers append it only when that profile is
# active; langfuse is never published (local build only).
# tests/test_docker_compose_invariants.py asserts this list still matches the
# published set declared in docker-compose.yml, and that every entry point uses it.
# shellcheck disable=SC2034  # consumed by the scripts that source this library
PUBLISHED_SERVICES_BASE=(paper_ingestion learning_engine dashboard restore-uploader)
# shellcheck disable=SC2034  # consumed by the scripts that source this library
PUBLISHED_SERVICE_TELEGRAM=telegram_bot
# The image repositories behind that set, used to recognise a warm re-run on disk.
# shellcheck disable=SC2034  # consumed by the scripts that source this library
PUBLISHED_IMAGE_REPOS=(
  ghcr.io/limitcycle-oss/jarvis-paper-ingestion
  ghcr.io/limitcycle-oss/jarvis-learning-engine
  ghcr.io/limitcycle-oss/jarvis-dashboard
)

# -----------------------------------------------------------------------------
# PROFILE_REGISTRY — single source of truth for every optional service group.
# -----------------------------------------------------------------------------
# setup.sh reads this instead of keeping separate hand-maintained profile lists
# for persistence, health, and delivery behavior.
# One pipe-delimited row per group; columns, in order:
#   name              group identifier (== the compose --profile flag)
#   overlay_file      compose file the group's services live in
#   profile_flag      the `docker compose --profile <flag>` name (== name)
#   persist           yes = written to COMPOSE_PROFILES so a bare `up` re-engages it
#   extra_health_svcs space-separated services that join the mandatory health gate
#                     when the group is active (empty = none; TLS edges are gated
#                     by their own cert/reachability probes instead)
#   cert_owner        who owns/terminates TLS (none|mkcert|letsencrypt|cloudflare)
#   tier              supported | manual | experimental
#   delivery          published | upstream-pinned | local-build — which image
#                     source the group's distinctive image comes from. A
#                     local-build image (jarvis/langfuse-hardened, pull_policy:
#                     build) is never on GHCR, so an update/pull gate keying off
#                     this column can exclude it.
# shellcheck disable=SC2034  # consumed by the accessors below and both entry points
PROFILE_REGISTRY=(
  "telegram|docker-compose.yml|telegram|yes|telegram_bot|none|supported|published"
  "tunnel|docker-compose.yml|tunnel|yes||cloudflare|supported|upstream-pinned"
  "observability|docker-compose.yml|observability|no|langfuse|none|manual|local-build"
  "caddy-local|docker-compose.yml|caddy-local|yes||mkcert|experimental|upstream-pinned"
  "letsencrypt|docker-compose.yml|letsencrypt|yes||letsencrypt|supported|upstream-pinned"
  "vllm|docker-compose.vllm.yml|vllm|no||none|manual|upstream-pinned"
  "perf|docker-compose.perf.yml|perf|no||none|manual|published"
)

# MANDATORY_HEALTH_BASE — the always-on services every setup path must wait on.
# Shared here so fresh and existing-install paths cannot drift.
# shellcheck disable=SC2034  # consumed by the scripts that source this library
MANDATORY_HEALTH_BASE="postgres ollama litellm paper_ingestion learning_engine dashboard restore-uploader"

# registry_profiles_to_persist -> space-separated profile flags whose rows are
# persist=yes. setup.sh intersects this with the run's active profiles to build
# COMPOSE_PROFILES, so a bare `docker compose up` re-engages the same set.
registry_profiles_to_persist() {
  local row name overlay flag persist rest out=""
  for row in "${PROFILE_REGISTRY[@]}"; do
    IFS='|' read -r name overlay flag persist rest <<< "$row"
    [ "$persist" = "yes" ] && out="${out:+$out }$flag"
  done
  printf '%s' "$out"
}

# mandatory_health_services BASE [ACTIVE_PROFILE...] -> the health-gate service
# list: BASE (space-separated) plus each active profile's extra_health_svcs from
# the registry, de-duplicated and order-stable. An active group's service is
# health-checked because it was deliberately started.
mandatory_health_services() {
  local base="$1"; shift
  local out="$base" p row name overlay flag persist health rest svc
  for p in "$@"; do
    for row in "${PROFILE_REGISTRY[@]}"; do
      IFS='|' read -r name overlay flag persist health rest <<< "$row"
      [ "$name" = "$p" ] || continue
      for svc in $health; do
        _env_key_in_list "$svc" "$out" || out="$out $svc"
      done
    done
  done
  printf '%s' "$out"
}

# route_claims -> the FIXED set of host-level ingress routes JARVIS advertises,
# one pipe-delimited row per route. These are transport planes, NOT compose
# profiles (a route has no --profile), so they live apart from PROFILE_REGISTRY.
# Columns, in order:
#   route|scheme|port|host_allowlist|setup_token_transport|cookie_policy|
#   passkey_origin|cert_owner|tier
# A docs-parity test consumes this set to check the deployment docs describe
# every route's real transport, token handoff, and WebAuthn behaviour.
route_claims() {
  cat <<'ROUTES'
localhost-http|http|3001|localhost|fragment|secure|localhost|none|supported
raw-ip-lan|http|3001|lan-ip|none|none|none|none|diagnostics-only
named-private-https|https|443|origin-host|fragment|secure|origin-host|external|manual
tailscale-serve|https|443|tailnet-host|fragment|secure|tailnet-host|tailscale|supported
local-https|https|3443|localhost|fragment|secure|localhost|mkcert|experimental
letsencrypt|https|443|domain|fragment|secure|domain|letsencrypt|supported
tunnel|https|443|tunnel-host|fragment|secure|tunnel-host|cloudflare|supported
ROUTES
}

# selected_https_route MODE PROFILES APP_BASE_URL
# Classify whether setup selected an off-host HTTPS route whose exact JARVIS
# marker must be reachable before success. Profiles cover older .env files that
# predate JARVIS_ACCESS_MODE; a named HTTPS origin layered onto localhost/LAN is
# still a required route.
selected_https_route() {
  local mode="$1" profiles="$2" app_base_url="$3"
  case "$mode" in
    tailscale|tunnel|letsencrypt) printf '%s' "$mode"; return 0 ;;
  esac
  case ",${profiles}," in
    *,tunnel,*)      printf 'tunnel'; return 0 ;;
    *,letsencrypt,*) printf 'letsencrypt'; return 0 ;;
    *,caddy-local,*) printf 'local-https'; return 0 ;;
  esac
  case "$app_base_url" in
    https://*) printf 'private' ;;
    *)         printf 'none' ;;
  esac
}

# environment_for_access_route MODE PROFILES APP_BASE_URL
# Loopback-only routes keep developer tolerance. Every off-host authenticated
# HTTPS origin, including a manual --public-origin layered onto localhost/LAN,
# receives production startup and readiness enforcement.
environment_for_access_route() {
  case "$(selected_https_route "$1" "$2" "$3")" in
    none|local-https) printf 'development' ;;
    *)                printf 'production' ;;
  esac
}

# selected_https_is_verified ROUTE PROBE_STATE
# Plain localhost needs no HTTPS probe. Every selected HTTPS route, including
# loopback mkcert, needs its route-specific exact-marker/trust state.
selected_https_is_verified() {
  [ "$1" = "none" ] || [ "$2" = "verified" ]
}

# allocate_ingress_ips SUBNET -> gateway, Telegram bot, Caddy, local Caddy,
# dashboard, and cloudflared addresses as one space-separated row. Docker keeps
# its usual low dynamic addresses; pinned peers use the highest five usable
# addresses so existing networks can adopt the pins without an IPAM/network
# migration. The row is ordered lowest-to-highest after the gateway, so the
# four addresses pinned before the bot keep the values they were assigned.
allocate_ingress_ips() {
  python3 - "$1" <<'PY'
import ipaddress
import sys

try:
    network = ipaddress.ip_network(sys.argv[1], strict=True)
except ValueError:
    raise SystemExit(1)
if network.version != 4 or network.prefixlen > 27:
    raise SystemExit(1)
gateway = network.network_address + 1
edges = [network.broadcast_address - offset for offset in range(5, 0, -1)]
print(" ".join(str(address) for address in [gateway, *edges]))
PY
}

# classify_external_app_probe CURL_RC HTTP LOCATION SERVER CF_MITIGATED BODY
# -> verified | access | waf | wrong-app | dns-tls | unavailable.
classify_external_app_probe() {
  local curl_rc="$1" code="$2" location="$3" server="$4" mitigated="$5" body="$6"
  local metadata
  metadata="$(printf '%s %s %s %s' "$location" "$server" "$mitigated" "$body" | tr '[:upper:]' '[:lower:]')"
  case "$curl_rc" in 6|35|51|58|60) printf 'dns-tls'; return 0 ;; esac
  if [ "$curl_rc" -ne 0 ]; then printf 'unavailable'; return 0; fi
  case "$metadata" in
    *cloudflareaccess.com*|*cdn-cgi/access*) printf 'access'; return 0 ;;
  esac
  case "$mitigated:$metadata" in
    challenge:*|*:*.cloudflare*just\ a\ moment*|*:cloudflare*just\ a\ moment*)
      printf 'waf'; return 0 ;;
  esac
  if [ "$code" = "401" ] && printf '%s' "$metadata" | grep -q cloudflare; then
    printf 'access'; return 0
  fi
  case "$code" in
    2??)
      [ "$body" = "jarvis-rd-assistant" ] && printf 'verified' || printf 'wrong-app'
      ;;
    403|429|503)
      printf '%s' "$metadata" | grep -q cloudflare && printf 'waf' || printf 'unavailable'
      ;;
    *) printf 'unavailable' ;;
  esac
}

# probe_external_app URL [CA_FILE] -> one classifier state. Redirects are deliberately
# not followed: an Access login redirect is edge-reachable, not proof that the
# hostname serves this JARVIS instance. The response body is capped at 4 KiB.
probe_external_app() {
  local url="$1" ca_file="${2:-}" body_file headers_file code rc
  local location server mitigated body
  local -a curl_args=(
    -sS --connect-timeout 5 --max-time 10 --range 0-4095
  )
  if [ -n "$ca_file" ]; then
    [ -r "$ca_file" ] || { printf 'dns-tls'; return 0; }
    curl_args+=(--cacert "$ca_file")
  fi
  body_file="$(mktemp "${TMPDIR:-/tmp}/jarvis-probe-body.XXXXXX")" || return 1
  headers_file="$(mktemp "${TMPDIR:-/tmp}/jarvis-probe-headers.XXXXXX")" || {
    rm -f "$body_file"; return 1;
  }
  if code="$(curl "${curl_args[@]}" \
      --dump-header "$headers_file" --output "$body_file" --write-out '%{http_code}' "$url" 2>/dev/null)"; then
    rc=0
  else
    rc=$?
    code="${code:-000}"
  fi
  location="$(awk 'BEGIN{IGNORECASE=1} /^location:/{sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit}' "$headers_file")"
  server="$(awk 'BEGIN{IGNORECASE=1} /^server:/{sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit}' "$headers_file")"
  mitigated="$(awk 'BEGIN{IGNORECASE=1} /^cf-mitigated:/{sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit}' "$headers_file")"
  body="$(head -c 4096 "$body_file")"
  rm -f "$body_file" "$headers_file"
  classify_external_app_probe "$rc" "$code" "$location" "$server" "$mitigated" "$body"
}

# mkcert_ca_file -> absolute root CA path, when mkcert and its CA are present.
mkcert_ca_file() {
  local ca_root ca_file
  command -v mkcert >/dev/null 2>&1 || return 1
  ca_root="$(mkcert -CAROOT 2>/dev/null)" || return 1
  ca_file="${ca_root%/}/rootCA.pem"
  [ -r "$ca_file" ] || return 1
  printf '%s' "$ca_file"
}

# probe_local_https_app URL -> the same classifier states as probe_external_app.
# First bind the certificate to this installation's mkcert CA, then repeat with
# the normal host trust store so setup never advertises a browser-facing URL
# solely because a private CA file could validate it explicitly.
probe_local_https_app() {
  local url="$1" ca_file chain_state
  ca_file="$(mkcert_ca_file)" || { printf 'dns-tls'; return 0; }
  chain_state="$(probe_external_app "$url" "$ca_file")"
  if [ "$chain_state" != "verified" ]; then
    printf '%s' "$chain_state"
    return 0
  fi
  probe_external_app "$url"
}

# cloudflared's process can be alive without a tunnel connection. Its metrics
# listener exposes /ready=200 only after at least one active edge connection.
cloudflared_ready() {
  docker compose exec -T dashboard \
    curl -fsS --max-time 5 http://cloudflared:2000/ready
}

# tailscale_dns_name -> the current node's MagicDNS name without its trailing
# dot. Returns non-zero until the daemon is connected and has a usable name.
tailscale_dns_name() {
  tailscale status --json 2>/dev/null | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
    name = str(data.get("Self", {}).get("DNSName", "")).rstrip(".")
except (ValueError, TypeError):
    raise SystemExit(1)
if not name:
    raise SystemExit(1)
print(name)
'
}

# registry_profile_host_ports PROFILE... -> the extra HOST TCP ports the named
# active profiles publish, space-separated and de-duplicated. The always-on
# services have a fixed default port list in setup.sh, but an active TLS edge or
# optional service binds MORE host ports; a port pre-check that ignored them
# would green-light a port that then collides at `up`. Values mirror the
# published `ports:` of each group's service (docker-compose.yml / overlays):
#   letsencrypt   caddy ACME edge -> 80, 443
#   caddy-local   local mkcert HTTPS terminator -> 3443
#   observability langfuse -> 3002
#   vllm          vLLM overlay -> 8080
#   tunnel/telegram  cloudflared/telegram dial OUT — no host port published
registry_profile_host_ports() {
  local p out=""
  for p in "$@"; do
    case "$p" in
      letsencrypt)   out="$out 80 443" ;;
      caddy-local)   out="$out 3443" ;;
      observability) out="$out 3002" ;;
      vllm)          out="$out 8080" ;;
    esac
  done
  # De-duplicate, order-stable (letsencrypt could otherwise repeat 443).
  local seen="" port final=""
  for port in $out; do
    _env_key_in_list "$port" "$seen" && continue
    seen="$seen $port"; final="${final:+$final }$port"
  done
  printf '%s' "$final"
}

# readiness_verdict RC ENVIRONMENT -> the action setup.sh's readiness wrapper
# takes for a production-readiness-check.sh exit code, under its 0/2/1 contract:
#   0             -> "ok"    all checks passed
#   2             -> "warn"  warnings present; NEVER fatal, in any environment
#   1 + production -> "abort" HIGH issues; fatal only on the production/letsencrypt path
#   1 + other     -> "warn"  HIGH issues are advisory off the production path (dev tolerance)
#   any other rc  -> "warn"  unknown nonzero: surface it, never silently abort
# Pairing this with the script's exit-code flip in ONE change is what stops a
# routine warning (e.g. missing SMTP, now exit 2) from aborting a production
# install the moment the flip lands.
readiness_verdict() {
  local rc="$1" env="$2"
  case "$rc" in
    0) printf 'ok' ;;
    2) printf 'warn' ;;
    1) [ "$env" = "production" ] && printf 'abort' || printf 'warn' ;;
    *) printf 'warn' ;;
  esac
}

# backfill_torch_variant_from_env — give a pre-1.1 .env the TORCH_VARIANT pair it
# never had, echoing the variant when it writes one (nothing when there is already
# a value, or no .env).
#
# The published paper_ingestion image ships in a CUDA and a CPU flavour and the
# image tag is selected by TORCH_VARIANT_SUFFIX. Without it, `${TORCH_VARIANT_SUFFIX:-}`
# resolves empty and a CUDA host would silently pull — or build — the CPU image
# while its GPU overlay still reserves the NVIDIA device. The installer already
# recorded its effective GPU decision in COMPOSE_FILE (the gpu overlay is listed
# exactly when it resolved to CUDA), so derive the flavour from that rather than
# re-probing hardware. Anything else, a missing COMPOSE_FILE included, means cpu —
# correct on every host, merely slower.
backfill_torch_variant_from_env() {
  [ -f .env ] || return 0
  grep -q '^TORCH_VARIANT=' .env && return 0
  local variant="cpu" suffix=""
  if grep -E '^COMPOSE_FILE=' .env | grep -q 'docker-compose\.gpu\.yml'; then
    variant="cuda"; suffix="-cuda"
  fi
  upsert_env_var TORCH_VARIANT "$variant" || return 1
  upsert_env_var TORCH_VARIANT_SUFFIX "$suffix" || return 1
  # The published :X.Y.Z-cuda image bakes the reranker extras (INSTALL_OPTIONAL);
  # a --build-local rebuild after this backfill must reproduce that, or an
  # upgrading CUDA host silently gets a reranker-less image under the same tag.
  # Mirror the fresh-install invariant (setup.sh). Only add it when absent, so a
  # user who deliberately turned it off is not overridden.
  if [ "$variant" = "cuda" ] && ! grep -q '^INSTALL_OPTIONAL=' .env; then
    upsert_env_var INSTALL_OPTIONAL true || return 1
  fi
  printf '%s' "$variant"
}

# upsert_env_var KEY VALUE — idempotent in-place .env upsert (no duplicate lines).
# Mirrors scripts/init-secrets.sh::upsert_env_var (bash 3.2-portable awk).
upsert_env_var() {
  local k="$1" v="$2" tmp
  # Colocate the temp with .env so the final mv is an atomic same-filesystem
  # rename rather than a cross-filesystem copy a concurrent reader could observe.
  tmp="$(mktemp .env.XXXXXX)" || { printf 'upsert_env_var: mktemp failed\n' >&2; return 1; }
  awk -v k="$k" -v v="$v" '
    index($0, k "=") == 1 { if (!seen) { print k "=" v; seen = 1 } ; next }
    { print }
    END { if (!seen) print k "=" v }
  ' .env > "$tmp" || { rm -f "$tmp"; printf 'upsert_env_var: awk rewrite of .env failed\n' >&2; return 1; }
  mv "$tmp" .env || { rm -f "$tmp"; printf 'upsert_env_var: mv to .env failed\n' >&2; return 1; }
}

# upsert_app_identity VERSION IMAGE_TAG — validate and atomically persist the
# application identity pair so readers can never observe a mixed generation.
upsert_app_identity() {
  local version="$1" image_tag="$2" tmp
  app_version_is_valid "$version" && image_tag_is_valid "$image_tag" || return 2
  tmp="$(mktemp .env.XXXXXX)" \
    || { printf 'upsert_app_identity: mktemp failed\n' >&2; return 1; }
  awk -v version="$version" -v image_tag="$image_tag" '
    /^JARVIS_VERSION=/ {
      if (!seen_version) { print "JARVIS_VERSION=" version; seen_version = 1 }
      next
    }
    /^JARVIS_IMAGE_TAG=/ {
      if (!seen_image_tag) { print "JARVIS_IMAGE_TAG=" image_tag; seen_image_tag = 1 }
      next
    }
    { print }
    END {
      if (!seen_version) print "JARVIS_VERSION=" version
      if (!seen_image_tag) print "JARVIS_IMAGE_TAG=" image_tag
    }
  ' .env > "$tmp" \
    || { rm -f "$tmp"; printf 'upsert_app_identity: awk rewrite of .env failed\n' >&2; return 1; }
  mv "$tmp" .env \
    || { rm -f "$tmp"; printf 'upsert_app_identity: mv to .env failed\n' >&2; return 1; }
}

# sync_ingress_ips_from_env — derive and persist the exact trusted ingress peers
# from the effective JARVIS_NET_SUBNET. This is the upgrade bridge for installs
# created before v1.2, which recorded only the subnet. Exporting the same values
# ensures this process cannot be redirected by stale per-address shell variables
# after the durable .env has been corrected.
sync_ingress_ips_from_env() {
  [ -f .env ] || return 0
  local subnet resolved gateway bot caddy caddy_local dashboard cloudflared
  subnet="${JARVIS_NET_SUBNET:-}"
  if [ -z "$subnet" ]; then
    subnet="$(sed -n 's/^JARVIS_NET_SUBNET=//p' .env | head -n 1)"
  fi
  subnet="${subnet:-10.137.241.0/24}"
  resolved="$(allocate_ingress_ips "$subnet")" || return 1
  read -r gateway bot caddy caddy_local dashboard cloudflared <<< "$resolved"

  upsert_env_var JARVIS_NET_SUBNET "$subnet" || return 1
  upsert_env_var JARVIS_NET_GATEWAY_IP "$gateway" || return 1
  upsert_env_var JARVIS_TELEGRAM_BOT_IP "$bot" || return 1
  upsert_env_var JARVIS_CADDY_IP "$caddy" || return 1
  upsert_env_var JARVIS_CADDY_LOCAL_IP "$caddy_local" || return 1
  upsert_env_var JARVIS_DASHBOARD_IP "$dashboard" || return 1
  upsert_env_var JARVIS_CLOUDFLARED_IP "$cloudflared" || return 1

  export JARVIS_NET_SUBNET="$subnet"
  export JARVIS_NET_GATEWAY_IP="$gateway"
  export JARVIS_TELEGRAM_BOT_IP="$bot"
  export JARVIS_CADDY_IP="$caddy"
  export JARVIS_CADDY_LOCAL_IP="$caddy_local"
  export JARVIS_DASHBOARD_IP="$dashboard"
  export JARVIS_CLOUDFLARED_IP="$cloudflared"
}

# print_setup_link -> print the click-to-finish wizard link when a setup token
# exists. $1 = dashboard base URL (trailing slash optional). Reads
# secrets/jarvis_setup_token.txt relative to CWD (the repo root both entry
# points cd into). When a token is present it prints the "Finish setup:" line
# and sets SETUP_LINK so setup.sh can best-effort open it in a browser; with no
# token it clears SETUP_LINK and prints nothing.
# The token rides a URL FRAGMENT (#setup_token=), never a query string: a
# fragment is never sent to the server, so it stays out of access logs, the
# Referer header, and reverse-proxy request lines. The wizard reads it from
# window.location.hash.
print_setup_link() {
  local base="${1%/}" token
  token="$(cat secrets/jarvis_setup_token.txt 2>/dev/null || true)"
  SETUP_LINK=""
  if [ -n "$token" ]; then
    SETUP_LINK="${base}/setup#setup_token=${token}"
    printf '  Finish setup: %s\n' "$SETUP_LINK"
  fi
}

# headless_setup_route BASE DASHBOARD_HTTP_PORT
#
# Print "tunnel-port|browser-base" for a loopback finish-setup route. A local
# HTTPS certificate is trusted only in the OS where setup installed its CA; an
# outside browser reached through SSH cannot use it. In that case, forward the
# dashboard's loopback HTTP listener instead. SSH protects the traffic between
# the browser machine and the server, while localhost remains a browser secure
# context for passkeys. Existing localhost HTTP routes keep their exact port.
headless_setup_route() {
  local base dashboard_http_port="$2" source_port tunnel_port browser_base

  base="$(printf '%s' "${1%/}" | tr '[:upper:]' '[:lower:]')"

  case "$dashboard_http_port" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$dashboard_http_port" -ge 1 ] 2>/dev/null \
    && [ "$dashboard_http_port" -le 65535 ] 2>/dev/null \
    || return 1

  case "$base" in
    https://localhost|https://*.localhost|https://127.0.0.1)
      tunnel_port="$dashboard_http_port"
      browser_base="http://localhost:${dashboard_http_port}"
      ;;
    https://localhost:*|https://*.localhost:*|https://127.0.0.1:*)
      source_port="${base##*:}"
      case "$source_port" in
        ''|*[!0-9]*) return 1 ;;
      esac
      [ "$source_port" -ge 1 ] 2>/dev/null \
        && [ "$source_port" -le 65535 ] 2>/dev/null \
        || return 1
      tunnel_port="$dashboard_http_port"
      browser_base="http://localhost:${dashboard_http_port}"
      ;;
    http://localhost|http://*.localhost|http://127.0.0.1)
      tunnel_port=80
      browser_base="$base"
      ;;
    http://localhost:*|http://*.localhost:*|http://127.0.0.1:*)
      tunnel_port="${base##*:}"
      case "$tunnel_port" in
        ''|*[!0-9]*) return 1 ;;
      esac
      [ "$tunnel_port" -ge 1 ] 2>/dev/null \
        && [ "$tunnel_port" -le 65535 ] 2>/dev/null \
        || return 1
      browser_base="$base"
      ;;
    *) return 1 ;;
  esac

  printf '%s|%s' "$tunnel_port" "$browser_base"
}

# parse_setup_status_json -> print "configured setup_completed setup_mode" for
# the public /api/setup/status response read on stdin. Values are type-checked so
# malformed or partial responses remain unknown instead of reopening bootstrap
# guidance for an already configured installation.
parse_setup_status_json() {
  python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)

configured = payload.get("configured")
completed = payload.get("setup_completed")
mode = payload.get("setup_mode")
if type(configured) is not bool or type(completed) is not bool:
    raise SystemExit(1)
if mode not in {"single", "multi"}:
    raise SystemExit(1)
print(str(configured).lower(), str(completed).lower(), mode)
'
}

# materialize_api_key_file KEY -> atomically write the local single-user API
# key and print its path. The secret is handled by shell builtins and file I/O;
# it is never passed to an external command as an argument.
materialize_api_key_file() {
  local api_key="$1" key_dir key_file tmp
  [ -n "$api_key" ] || {
    printf 'materialize_api_key_file: API key is empty\n' >&2
    return 1
  }
  key_dir="${HOME}/.config/jarvis"
  key_file="${key_dir}/api-key"
  mkdir -p "$key_dir" || return 1
  chmod 700 "$key_dir" || return 1
  tmp="$(mktemp "${key_dir}/.api-key.XXXXXX")" || return 1
  if ! printf '%s' "$api_key" > "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  chmod 600 "$tmp" || { rm -f "$tmp"; return 1; }
  mv "$tmp" "$key_file" || { rm -f "$tmp"; return 1; }
  printf '%s' "$key_file"
}

# ---------------------------------------------------------------------------
# Non-destructive .env rebuild
# ---------------------------------------------------------------------------
# JARVIS_MANAGED_SECRET_KEYS — the keys a re-run must NEVER silently rotate or
# drop. It is the union of scripts/init-secrets.sh's generator table (the
# openssl-minted secrets) and the operator SMTP relay settings. The merge below
# preserves EVERY existing key regardless; this list names the ones whose loss
# would brick a live deployment (LiteLLM credential decryption, backup
# decryption, model HMAC, Langfuse, magic-link email) and is the canonical
# walk-list for the byte-preservation checks. Keep in sync with the generator
# names in scripts/init-secrets.sh.
# shellcheck disable=SC2034  # consumed by scripts/tests + as project documentation
JARVIS_MANAGED_SECRET_KEYS=(
  POSTGRES_PASSWORD JARVIS_API_KEY JARVIS_CONFIG_KEY LITELLM_MASTER_KEY QDRANT_API_KEY
  LITELLM_SALT_KEY BACKUP_ENCRYPT_KEY JARVIS_MODEL_HMAC_KEY INFRA_INGEST_KEY JARVIS_SETUP_TOKEN
  LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT LANGFUSE_PG_PASSWORD LANGFUSE_INIT_USER_PASSWORD
  SMTP_HOST SMTP_USER SMTP_PASS SMTP_PORT SMTP_FROM
)

# _env_key_in_list KEY "space separated list" -> 0 if KEY is a member.
_env_key_in_list() {
  case " $2 " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

# merge_env_file OLD_ENV TEMPLATE UPSERTS RETIRED -> merged .env on stdout.
#
# Rebuild an .env WITHOUT discarding operator state:
#   * every assignment in OLD_ENV is carried forward BYTE-FOR-BYTE (unknown and
#     operator-added keys included) unless this run owns it or it is retired;
#   * keys this run owns (UPSERTS holds one KEY=VALUE per line — only keys whose
#     flag/prompt was genuinely supplied) are written with the supplied value, so
#     re-running to CHANGE a setting takes effect while its neighbours survive
#     untouched;
#   * keys present in TEMPLATE but absent from OLD_ENV are appended (new-in-
#     release keys arrive), owned ones with their supplied value;
#   * keys named in RETIRED (space-separated) are dropped — unless this run still
#     owns them, in which case the owner wins and the key is re-emitted.
# Values may hold =, #, +, /, spaces and quotes; a value read from UPSERTS keeps
# every byte after the first '=', a carried-forward line is emitted verbatim.
merge_env_file() {
  local old_env="$1" template="$2" upserts="$3" retired="$4"
  local line key

  # 1. Carry OLD_ENV forward: verbatim, except owned upserts and retired drops.
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
      key="${BASH_REMATCH[1]}"
      if grep -qE "^${key}=" "$upserts" 2>/dev/null; then
        printf '%s=%s\n' "$key" "$(grep -E "^${key}=" "$upserts" | head -n 1 | cut -d= -f2-)"
      elif _env_key_in_list "$key" "$retired"; then
        :  # retired and not owned this run — drop
      else
        printf '%s\n' "$line"  # preserve operator/unknown value verbatim
      fi
    else
      printf '%s\n' "$line"  # comment / blank — verbatim
    fi
  done < "$old_env"

  # 2. Append TEMPLATE keys absent from OLD_ENV (keys new in this release).
  local header_done=0
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]] || continue
    key="${BASH_REMATCH[1]}"
    grep -qE "^${key}=" "$old_env" 2>/dev/null && continue
    _env_key_in_list "$key" "$retired" && continue
    if [ "$header_done" -eq 0 ]; then
      printf '\n# Added by this release (absent from your previous .env):\n'
      header_done=1
    fi
    if grep -qE "^${key}=" "$upserts" 2>/dev/null; then
      printf '%s=%s\n' "$key" "$(grep -E "^${key}=" "$upserts" | head -n 1 | cut -d= -f2-)"
    else
      printf '%s\n' "$line"  # template default, verbatim
    fi
  done < "$template"
}

# ---------------------------------------------------------------------------
# Release helpers (shared by setup.sh, update.sh, and the CLI installer)
# ---------------------------------------------------------------------------

# app_version_is_valid VERSION — accept the release and pre-release version
# grammar used for application image tags. The length cap bounds checkout
# metadata before it is used as a Docker tag.
app_version_is_valid() {
  local version="${1:-}"
  [ "${#version}" -le 128 ] || return 1
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$ ]]
}

# image_tag_is_valid TAG — accept a semantic application version or the
# lowercase 40-hex identity used by commit-addressed verification images.
image_tag_is_valid() {
  local tag="${1:-}"
  [ "${#tag}" -le 128 ] || return 1
  app_version_is_valid "$tag" || [[ "$tag" =~ ^[0-9a-f]{40}$ ]]
}

# resolve_checkout_app_version — print the application image version represented
# by the current checkout. An exact release tag is authoritative for tagged
# checkouts; otherwise [project].version in pyproject.toml is used.
resolve_checkout_app_version() {
  local exact_tag="" version=""
  if command -v git >/dev/null 2>&1; then
    exact_tag="$(git describe --tags --exact-match HEAD 2>/dev/null || true)"
  fi

  case "$exact_tag" in
    v[0-9]*) version="${exact_tag#v}" ;;
    *)
      [ -r pyproject.toml ] || return 1
      version="$(awk '
        /^\[project\][[:space:]]*$/ { in_project = 1; next }
        in_project && /^\[/ { exit }
        in_project && /^[[:space:]]*version[[:space:]]*=/ {
          line = $0
          if (line !~ /^[[:space:]]*version[[:space:]]*=[[:space:]]*"[^"]+"[[:space:]]*$/) exit
          sub(/^[[:space:]]*version[[:space:]]*=[[:space:]]*"/, "", line)
          sub(/"[[:space:]]*$/, "", line)
          print line
          exit
        }
      ' pyproject.toml 2>/dev/null)"
      ;;
  esac

  app_version_is_valid "$version" || return 1
  printf '%s' "$version"
}

# latest_stable_tag [REMOTE] -> the highest STABLE release tag on REMOTE
# (default: origin), or empty when there are none. Stable means vMAJOR.MINOR.PATCH
# with no pre-release suffix, so vX.Y.Z-rc1 and other candidates are excluded, and
# integer-tuple comparison orders them versionally rather than lexically without
# GNU-only `sort -V`. Pure: it only reads `git ls-remote`, so it is unit-testable
# behind a git stub.
latest_stable_tag() {
  local remote="${1:-origin}"
  git ls-remote --tags --refs "$remote" 2>/dev/null \
    | python3 -c '
import re
import sys

best = None
for line in sys.stdin:
    match = re.search(r"refs/tags/(v([0-9]+)\.([0-9]+)\.([0-9]+))$", line.strip())
    if match is None:
        continue
    candidate = (tuple(int(part) for part in match.groups()[1:]), match.group(1))
    if best is None or candidate[0] > best[0]:
        best = candidate
if best is not None:
    print(best[1])
'
}

# _cli_shim_body -> the fixed launcher installed as jarvis-research. Logic-free by
# design: it resolves the most recently installed JARVIS repo from the installs
# registry and hands straight off to that repo's tracked CLI script, so upgrades
# to the CLI ship with the repo, never with this shim.
_cli_shim_body() {
  cat <<'SHIM'
#!/usr/bin/env bash
# jarvis-research — launcher for the JARVIS research CLI. Generated by the JARVIS
# installer; do not edit. Runs the CLI from the most recently installed JARVIS
# repo (the top entry of the installs registry).
set -euo pipefail
installs="${XDG_CONFIG_HOME:-${HOME}/.config}/jarvis-research/installs"
if [ ! -s "$installs" ]; then
  printf 'jarvis-research: no JARVIS install is registered (%s).\n' "$installs" >&2
  exit 1
fi
repo="$(head -n 1 "$installs")"
exec "${repo}/scripts/jarvis-research.sh" --repo "$repo" "$@"
SHIM
}

# recorded_state_dir [REPO_DIR] -> the JARVIS_STATE_DIR value recorded in .env,
# empty when the file or the line is absent. The quote strip is the same
# untrusted-value idiom the other .env readers use; it lives here once so the
# creating side (ensure_state_dir) and the removing side (uninstall) can never
# disagree about which path was recorded.
recorded_state_dir() {
  local value
  value="$(sed -n 's/^JARVIS_STATE_DIR=//p' "${1:-$PWD}/.env" 2>/dev/null | head -1)"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  printf '%s' "$value"
}

# ensure_state_dir [REPO_DIR] -> create the durable lifecycle-state directory and
# record it in .env as JARVIS_STATE_DIR. Idempotent; never moves existing state;
# prints one line when it writes something. Compose bind-mounts this path into the
# backup sidecar, so the mkdir runs unconditionally and BEFORE any early return: a
# missing bind-mount source is created by Docker as root:root, which a later
# host-user removal could not unlink. A RECORDED value always wins over the computed
# one, or compose would mount a path this never created.
ensure_state_dir() {
  local repo="${1:-$PWD}" project state_dir recorded
  recorded="$(recorded_state_dir "$repo")"
  if [ -n "$recorded" ]; then
    mkdir -p "$recorded" || return 1
    chmod 700 "$recorded" 2>/dev/null || true
    return 0
  fi
  project="$(_lifecycle_compose_project_name "$repo")" || return 1
  state_dir="${XDG_STATE_HOME:-${HOME}/.local/state}/jarvis-research/${project}"
  mkdir -p "$state_dir" || return 1
  chmod 700 "$state_dir" 2>/dev/null || true
  # upsert_env_var rewrites ./.env through a colocated temp, so it must run with the
  # repo as the working directory.
  ( cd "$repo" && upsert_env_var JARVIS_STATE_DIR "$state_dir" ) || return 1
  printf 'Recorded durable state directory: %s\n' "$state_dir"
}

# install_cli_shim [REPO_DIR] -> install/refresh the jarvis-research launcher and
# register REPO_DIR (default: $PWD) at the TOP of the installs registry, so the
# shim always targets the most recently installed repo. Idempotent: a re-run with
# an unchanged launcher and an already-top registry entry changes nothing and
# prints nothing; it prints exactly one line whenever it writes something.
# Target locations are overridable for testing:
#   JARVIS_CLI_BIN_DIR     (default ~/.local/bin)
#   JARVIS_CLI_CONFIG_DIR  (default ${XDG_CONFIG_HOME:-~/.config}/jarvis-research)
install_cli_shim() {
  local repo="${1:-$PWD}"
  local bin_dir="${JARVIS_CLI_BIN_DIR:-${HOME}/.local/bin}"
  local cfg_dir="${JARVIS_CLI_CONFIG_DIR:-${XDG_CONFIG_HOME:-${HOME}/.config}/jarvis-research}"
  local shim="${bin_dir}/jarvis-research"
  local installs="${cfg_dir}/installs"
  local changed=0

  mkdir -p "$bin_dir" "$cfg_dir" || return 1

  # 1. Write the launcher when it is absent or stale.
  if [ ! -f "$shim" ] || ! _cli_shim_body | cmp -s - "$shim"; then
    _cli_shim_body > "$shim" || return 1
    chmod +x "$shim" || return 1
    changed=1
  fi

  # 2. Prepend REPO_DIR to the registry, de-duplicated and order-stable.
  local rest="" tmp
  [ -f "$installs" ] && rest="$(grep -vxF "$repo" "$installs" 2>/dev/null || true)"
  tmp="$(mktemp)" || return 1
  { printf '%s\n' "$repo"; [ -n "$rest" ] && printf '%s\n' "$rest"; } > "$tmp"
  if [ ! -f "$installs" ] || ! cmp -s "$tmp" "$installs"; then
    mv "$tmp" "$installs" || { rm -f "$tmp"; return 1; }
    changed=1
  else
    rm -f "$tmp"
  fi

  [ "$changed" -eq 1 ] && printf 'Installed jarvis-research launcher: %s\n' "$shim"
  return 0
}

# warn_if_launcher_unreachable -> after installing the launcher, confirm THIS
# shell can find it, and offer to add the PATH line once. Installing a command a
# shell cannot resolve is the same as not installing it. The check reads the
# setup process's own PATH, which is the honest limit of what it can observe: a
# run under a temporarily augmented PATH passes here while a fresh login shell
# may still not resolve the command, so the message says "will not be found",
# never "is now permanently on your PATH".
# Bare printf, not the caller's warn/ok: this library is presentation-free.
# The prompt is offered only when the caller has a terminal to answer it with —
# an unanswerable prompt in a piped or CI install must never edit a startup file.
warn_if_launcher_unreachable() {
  local bin_dir="${JARVIS_CLI_BIN_DIR:-${HOME}/.local/bin}" rc_file line reply=""
  case ":$PATH:" in *":${bin_dir}:"*) return 0 ;; esac
  line="export PATH=\"${bin_dir}:\$PATH\""
  case "$(basename "${SHELL:-}")" in
    zsh)  rc_file="${HOME}/.zshrc" ;;
    bash) rc_file="${HOME}/.bashrc" ;;
    *)    rc_file="" ;;
  esac
  printf 'The jarvis-research command was installed to %s, but that directory is not on your PATH, so the command will not be found.\n' \
    "$bin_dir" >&2
  if [ -n "$rc_file" ] && grep -qxF "$line" "$rc_file" 2>/dev/null; then
    printf '%s already carries that PATH line. Open a new terminal (or run: source %s) and jarvis-research will work.\n' \
      "$rc_file" "$rc_file" >&2
    return 0
  fi
  if [ -n "$rc_file" ] && [ "${NON_INTERACTIVE:-0}" -eq 0 ] && [ -t 0 ]; then
    read -rp "Add it to ${rc_file} now? (Y/n): " reply || reply=""
    case "$reply" in
      [nN]*) ;;
      *)
        if printf '\n%s\n' "$line" >> "$rc_file"; then
          printf 'Added. Open a new terminal (or run: source %s) and jarvis-research will work.\n' \
            "$rc_file" >&2
          return 0
        fi
        ;;
    esac
  fi
  printf 'To fix it, add this line to your shell startup file and open a new terminal:\n  %s\n' \
    "$line" >&2
}

# verify_release_manifests TARGET_REF [ACTIVE_PROFILE...] -> confirm every
# registry-backed image a release needs already exists in the registry, closing
# the window where a tag is visible but its images are not yet published.
# TARGET_REF is either a v-prefixed release tag or a lowercase commit SHA.
# Release tags are normalized once; commit identities remain unchanged.
# Inspected set, for the ACTIVE topology:
#   * application images at the target version (paper_ingestion carries
#     TORCH_VARIANT_SUFFIX), plus telegram_bot when that profile is active;
#   * the third-party pins the target ref's versions.env declares;
#   * each active profile's distinctive third-party image whose registry
#     `delivery` is not local-build.
# A local-build image (jarvis/langfuse-hardened) is never on the registry, so it
# is reported SKIPPED, never inspected. Prints PRESENT/MISSING per inspected image
# and SKIPPED per excluded local-build image; returns 0 only when every inspected
# image is present. Runs docker under a throwaway DOCKER_CONFIG so it never reads
# the caller's registry credentials.
verify_release_manifests() {
  local target_ref="$1"; shift
  local target_version="${target_ref#v}"
  local ns="ghcr.io/limitcycle-oss/jarvis-"
  local svc suffix p row name delivery
  local versions_env img
  local -a images=() skipped_images=()

  # Application images (registry-backed at the target version).
  for svc in "${PUBLISHED_SERVICES_BASE[@]}"; do
    suffix=""
    [ "$svc" = "paper_ingestion" ] && suffix="${TORCH_VARIANT_SUFFIX:-}"
    images+=("${ns}${svc//_/-}:${target_version}${suffix}")
  done
  for p in "$@"; do
    [ "$p" = "telegram" ] && images+=("${ns}${PUBLISHED_SERVICE_TELEGRAM//_/-}:${target_version}")
  done

  # Third-party pins declared by the target ref's versions.env.
  versions_env="$(git show "${target_ref}:versions.env" 2>/dev/null || true)"
  for name in POSTGRES_IMAGE OLLAMA_IMAGE QDRANT_IMAGE LITELLM_IMAGE CLOUDFLARED_IMAGE; do
    img="$(printf '%s\n' "$versions_env" | sed -n "s/^${name}=//p" | head -n 1)"
    [ -n "$img" ] && images+=("$img")
  done

  # Active profiles: a registry-backed profile image joins the inspected set; a
  # local-build profile image is skipped because it is never published.
  for p in "$@"; do
    for row in "${PROFILE_REGISTRY[@]}"; do
      IFS='|' read -r name _ _ _ _ _ _ delivery <<< "$row"
      [ "$name" = "$p" ] || continue
      if [ "$delivery" = "local-build" ]; then
        skipped_images+=("jarvis/langfuse-hardened:${target_version}")
      else
        case "$p" in
          caddy-local|letsencrypt)
            img="$(printf '%s\n' "$versions_env" | sed -n 's/^CADDY_IMAGE=//p' | head -n 1)"
            [ -n "$img" ] && images+=("$img") ;;
        esac
      fi
    done
  done

  local docker_cfg rc=0 ref
  docker_cfg="$(mktemp -d)" || return 1
  for ref in "${images[@]}"; do
    if DOCKER_CONFIG="$docker_cfg" docker manifest inspect "$ref" >/dev/null 2>&1; then
      printf 'PRESENT %s\n' "$ref"
    else
      printf 'MISSING %s\n' "$ref"
      rc=1
    fi
  done
  if [ "${#skipped_images[@]}" -gt 0 ]; then
    for ref in "${skipped_images[@]}"; do
      printf 'SKIPPED %s (local build, not published)\n' "$ref"
    done
  fi
  rm -rf "$docker_cfg"
  return "$rc"
}
