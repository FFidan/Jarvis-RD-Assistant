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
#   - resolve_amd_smi            : locate amd-smi (the stable AMD interface)
#   - detect_gpu_vendor          : nvidia | amd | intel | none probe
#   - resolve_gpu_vram_mb        : vendor-neutral total-VRAM (MB) probe
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

# resolve_amd_smi -> echoes a usable amd-smi path, or returns 1 if none.
# amd-smi (ROCm >= 5.7) is AMD's stable machine interface; rocm-smi's JSON
# output is explicitly unstable across releases, so it is never parsed here.
resolve_amd_smi() {
  command -v amd-smi 2>/dev/null || return 1
}

# detect_gpu_vendor -> echoes nvidia | amd | intel | none.
# Probe order nvidia -> amd -> intel: discrete vendor tools first (nvidia-smi
# enumerates a GPU; amd-smi static reports one), then a bare /dev/dri render
# node (Intel iGPU/dGPU, or an AMD card without any vendor tool installed).
# JARVIS_DRI_DIR overrides the /dev/dri location for tests.
detect_gpu_vendor() {
  local smi
  if smi="$(resolve_nvidia_smi)" && "$smi" -L 2>/dev/null | grep -q .; then
    printf 'nvidia'
    return 0
  fi
  if smi="$(resolve_amd_smi)" && "$smi" static --json 2>/dev/null | grep -qi '"gpu"'; then
    printf 'amd'
    return 0
  fi
  if ls "${JARVIS_DRI_DIR:-/dev/dri}"/renderD* >/dev/null 2>&1; then
    printf 'intel'
    return 0
  fi
  printf 'none'
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



# prereq_install_plan OS OS_ID HAS_APT HAS_BREW HAS_DNF MISSING...
# Prints explicit package-manager commands for supported hosts, one command per
# line. Docker comes from Docker's official repository (docker-ce +
# docker-compose-plugin): stock distro packages miss the compose plugin on
# Debian/Ubuntu and lag Engine releases. Root-escalation contract (setup.sh
# prints the plan verbatim for consent and rewrites only LINE-LEADING sudo to
# `sudo -n` for non-interactive runs): every line is unprivileged or starts
# with exactly one sudo, and remote content is fetched to a temp file as the
# user — never piped into a privileged command. `nvidia-toolkit` in MISSING...
# appends the NVIDIA Container Toolkit + docker runtime wiring. Returns
# non-zero when the host cannot be installed safely. This function only plans;
# setup.sh decides whether to prompt and execute the plan.
prereq_install_plan() {
  local os="$1" os_id="$2" has_apt="$3" has_brew="$4" has_dnf="$5"
  shift 5
  local needs_docker=0 needs_compose=0 needs_openssl=0 needs_toolkit=0 item
  for item in "$@"; do
    case "$item" in
      docker) needs_docker=1 ;;
      docker-compose) needs_compose=1 ;;
      openssl) needs_openssl=1 ;;
      nvidia-toolkit) needs_toolkit=1 ;;
    esac
  done

  case "$os" in
    Linux)
      case "$os_id" in
        debian|ubuntu|linuxmint|pop|popos)
          [ "$has_apt" = "1" ] || return 1
          _prereq_plan_apt "$os_id" "$needs_docker" "$needs_compose" "$needs_openssl" "$needs_toolkit"
          ;;
        fedora)
          [ "$has_dnf" = "1" ] || return 1
          _prereq_plan_dnf "$needs_docker" "$needs_compose" "$needs_openssl" "$needs_toolkit"
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

# Docker's apt repo serves UBUNTU codename dists: Mint/Pop set VERSION_CODENAME
# to their own release name ('wilma' 404s), so the sources line derives
# ${UBUNTU_CODENAME:-$VERSION_CODENAME} from /etc/os-release when it executes.
# shellcheck disable=SC2016  # plan lines expand at execution, not planning
_prereq_plan_apt() {
  local os_id="$1" needs_docker="$2" needs_compose="$3" needs_openssl="$4" needs_toolkit="$5"
  local repo_base=ubuntu
  [ "$os_id" = "debian" ] && repo_base=debian

  printf 'sudo apt-get update\n'
  if [ "$needs_docker" = "1" ] || [ "$needs_compose" = "1" ]; then
    printf 'sudo apt-get install -y ca-certificates curl gnupg\n'
    printf 'curl -fsSL https://download.docker.com/linux/%s/gpg -o /tmp/jarvis-docker.asc\n' "$repo_base"
    printf 'sudo install -m 0755 -d /etc/apt/keyrings\n'
    printf 'sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg /tmp/jarvis-docker.asc\n'
    printf 'sudo chmod a+r /etc/apt/keyrings/docker.gpg\n'
    printf 'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/%s $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" > /tmp/jarvis-docker.list\n' "$repo_base"
    printf 'sudo install -m 0644 /tmp/jarvis-docker.list /etc/apt/sources.list.d/docker.list\n'
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
  if [ "${#packages[@]}" -gt 0 ]; then
    printf 'sudo apt-get install -y %s\n' "${packages[*]}"
  fi
  if [ "$needs_docker" = "1" ]; then
    printf 'sudo systemctl enable --now docker\n'
    printf 'sudo usermod -aG docker "$USER"\n'
  fi
  if [ "$needs_toolkit" = "1" ]; then
    printf 'curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey -o /tmp/jarvis-nvidia-toolkit.asc\n'
    printf 'sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg /tmp/jarvis-nvidia-toolkit.asc\n'
    printf 'curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list -o /tmp/jarvis-nvidia-toolkit.list\n'
    printf 'sed -i "s#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g" /tmp/jarvis-nvidia-toolkit.list\n'
    printf 'sudo install -m 0644 /tmp/jarvis-nvidia-toolkit.list /etc/apt/sources.list.d/nvidia-container-toolkit.list\n'
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
  local needs_docker="$1" needs_compose="$2" needs_openssl="$3" needs_toolkit="$4"

  if [ "$needs_docker" = "1" ] || [ "$needs_compose" = "1" ]; then
    printf 'curl -fsSL https://download.docker.com/linux/fedora/docker-ce.repo -o /tmp/jarvis-docker-ce.repo\n'
    printf 'sudo install -m 0644 /tmp/jarvis-docker-ce.repo /etc/yum.repos.d/docker-ce.repo\n'
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
  if [ "${#packages[@]}" -gt 0 ]; then
    printf 'sudo dnf install -y %s\n' "${packages[*]}"
  fi
  if [ "$needs_docker" = "1" ]; then
    printf 'sudo systemctl enable --now docker\n'
    printf 'sudo usermod -aG docker "$USER"\n'
  fi
  if [ "$needs_toolkit" = "1" ]; then
    printf 'curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo -o /tmp/jarvis-nvidia-toolkit.repo\n'
    printf 'sudo install -m 0644 /tmp/jarvis-nvidia-toolkit.repo /etc/yum.repos.d/nvidia-container-toolkit.repo\n'
    printf 'sudo dnf install -y nvidia-container-toolkit\n'
    printf 'sudo nvidia-ctk runtime configure --runtime=docker\n'
    printf 'sudo systemctl restart docker\n'
  fi
}

# prereq_manual_guidance MISSING... -> human-readable fallback for unsupported
# or non-mutating paths. Keep this free of private host paths and secrets.
prereq_manual_guidance() {
  local item
  for item in "$@"; do
    case "$item" in
      docker) printf 'Install Docker Engine (or review-then-run the convenience script from https://get.docker.com): https://docs.docker.com/engine/install/\n' ;;
      docker-compose) printf 'Install the Docker Compose v2 plugin: https://docs.docker.com/compose/install/linux/\n' ;;
      openssl) printf 'Install openssl with your OS package manager.\n' ;;
      nvidia-toolkit) printf 'Install the NVIDIA Container Toolkit: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html\n' ;;
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
  local overlay="$1" override="$2" files="docker-compose.yml"
  [ -n "$overlay" ] && files="${files}:docker-compose.${overlay}.yml"
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
  printf '%s' "$variant"
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
