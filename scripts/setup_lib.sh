#!/usr/bin/env bash
# Shared setup.sh helpers, factored out so they can be unit-tested directly
# (setup.sh itself is not cleanly sourceable). Concerns:
#   - compute_compose_file       : which compose overlays to persist into .env
#   - compute_ollama_models      : which Ollama tags the bootstrap must pull
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
    "24-48": "qwen2.5:7b-instruct", "ge-48": "qwen3:30b-a3b",
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
