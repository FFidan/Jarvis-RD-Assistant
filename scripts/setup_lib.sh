#!/usr/bin/env bash
# Shared setup.sh helpers, factored out so they can be unit-tested directly
# (setup.sh itself is not cleanly sourceable). Concerns:
#   - compute_compose_file       : which compose overlays to persist into .env
#   - compute_ollama_models      : which Ollama tags the bootstrap must pull
#   - compute_required_disk_gb   : GB a cold install writes to the data root
#   - resolve_docker_data_root   : where Docker keeps images/volumes/cache
#   - preflight_disk_lib         : free-vs-required disk measurement core
#   - upsert_env_var             : idempotent in-place .env key write
#   - resolve_nvidia_smi         : locate nvidia-smi (PATH or the WSL2 location)
#   - _default_model_for_tier    : tier+backend -> default model id
# Sourced by setup.sh (which cd's to the repo root first, so the relative `.env`
# in upsert_env_var resolves correctly).

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



# prereq_install_plan OS OS_ID HAS_APT HAS_BREW MISSING...
# Prints explicit package-manager commands for supported hosts, one command per
# line. Returns non-zero when the host cannot be installed safely. This function
# only plans; setup.sh decides whether to prompt and execute the plan.
prereq_install_plan() {
  local os="$1" os_id="$2" has_apt="$3" has_brew="$4"
  shift 4
  local needs_docker=0 needs_compose=0 needs_openssl=0 item
  for item in "$@"; do
    case "$item" in
      docker) needs_docker=1 ;;
      docker-compose) needs_compose=1 ;;
      openssl) needs_openssl=1 ;;
    esac
  done

  case "$os" in
    Linux)
      case "$os_id" in
        debian|ubuntu|linuxmint|pop|popos)
          [ "$has_apt" = "1" ] || return 1
          local apt_packages=()
          if [ "$needs_docker" = "1" ]; then
            apt_packages+=(docker.io docker-compose-plugin)
          elif [ "$needs_compose" = "1" ]; then
            apt_packages+=(docker-compose-plugin)
          fi
          if [ "$needs_openssl" = "1" ]; then
            apt_packages+=(openssl)
          fi
          [ "${#apt_packages[@]}" -gt 0 ] || return 0
          printf 'sudo apt-get update\n'
          printf 'sudo apt-get install -y %s\n' "${apt_packages[*]}"
          ;;
        *) return 1 ;;
      esac
      ;;
    Darwin)
      [ "$has_brew" = "1" ] || return 1
      if [ "$needs_docker" = "1" ] || [ "$needs_compose" = "1" ]; then
        printf 'brew install --cask docker\n'
      fi
      if [ "$needs_openssl" = "1" ]; then
        printf 'brew install openssl\n'
      fi
      ;;
    *) return 1 ;;
  esac
}

# prereq_manual_guidance MISSING... -> human-readable fallback for unsupported
# or non-mutating paths. Keep this free of private host paths and secrets.
prereq_manual_guidance() {
  local item
  for item in "$@"; do
    case "$item" in
      docker) printf 'Install Docker Engine or Docker Desktop: https://docs.docker.com/engine/install/\n' ;;
      docker-compose) printf 'Install the Docker Compose v2 plugin: https://docs.docker.com/compose/install/linux/\n' ;;
      openssl) printf 'Install openssl with your OS package manager.\n' ;;
    esac
  done
  printf 'After installing Docker, start the daemon and re-run ./setup.sh --check.\n'
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

# compute_compose_file NVIDIA_PRESENT OVERRIDE_PRESENT -> echoes colon-joined COMPOSE_FILE.
# gpu.yml is added only when the Docker nvidia runtime is present; override.yml is
# appended LAST (an explicit COMPOSE_FILE suppresses Compose's implicit override
# auto-load, and gpu-before-override lets a dev override's `deploy: !reset null` win).
compute_compose_file() {
  local nvidia="$1" override="$2" files="docker-compose.yml"
  [ "$nvidia" = "1" ] && files="${files}:docker-compose.gpu.yml"
  [ "$override" = "1" ] && files="${files}:docker-compose.override.yml"
  printf '%s' "$files"
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
# floor for the registry-pull install path.
_image_budget_gb() {
  case "$1" in
    cpu-pull)  printf '6' ;;
    cpu-build) printf '9' ;;
    *)         printf '17' ;;  # cuda-build — the largest variant, safe default
  esac
}

# compute_required_disk_gb SMART_MODEL [VARIANT] -> echoes the whole-GB disk a
# cold install writes to the Docker data root: the app-image budget
# (variant-keyed, see _image_budget_gb) + infra image pulls (postgres/qdrant/
# ollama/litellm/vector, incl. containerd blob retention) + the Ollama model
# set compute_ollama_models will pull (per-model disk_gb from the model
# catalog; tags missing from the catalog assume 18 GB each). Returns 0 when
# the model sum is catalog-derived; when host python3 or the catalog is
# unusable it echoes a worst-case-model-set total and returns 3 so callers can
# soften a fatal check. stdout is ONLY the number — diagnostics go to stderr.
# JARVIS_MODEL_CATALOG overrides the catalog path (testing); the default is
# relative because setup.sh cd's to the repo root.
compute_required_disk_gb() {
  local smart="${1:-qwen3:8b}" variant="${2:-cuda-build}"
  local infra_gb=14 worst_models_gb=22
  local catalog="${JARVIS_MODEL_CATALOG:-libs/jarvis_common/jarvis_common/data/model_catalog.json}"
  local base_gb models_gb
  base_gb=$(( $(_image_budget_gb "$variant") + infra_gb ))
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
    printf '%s' "$((base_gb + models_gb))"
    return 0
  fi
  printf '[WARN] model catalog unreadable (%s) — assuming a worst-case %s GB model set\n' \
    "$catalog" "$worst_models_gb" >&2
  printf '%s' "$((base_gb + worst_models_gb))"
  return 3
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
# and the alternate bootstraps compose their own fatal/warn policy around
# this shared core.
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

# upsert_env_var KEY VALUE — idempotent in-place .env upsert (no duplicate lines).
# Mirrors scripts/init-secrets.sh::upsert_env_var (bash 3.2-portable awk).
upsert_env_var() {
  local k="$1" v="$2" tmp
  tmp="$(mktemp)" || { printf 'upsert_env_var: mktemp failed\n' >&2; return 1; }
  awk -v k="$k" -v v="$v" '
    index($0, k "=") == 1 { if (!seen) { print k "=" v; seen = 1 } ; next }
    { print }
    END { if (!seen) print k "=" v }
  ' .env > "$tmp" || { rm -f "$tmp"; printf 'upsert_env_var: awk rewrite of .env failed\n' >&2; return 1; }
  mv "$tmp" .env || { rm -f "$tmp"; printf 'upsert_env_var: mv to .env failed\n' >&2; return 1; }
}
