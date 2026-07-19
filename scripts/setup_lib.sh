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
#   - upsert_env_var             : idempotent in-place .env key write
#   - print_setup_link           : click-to-finish wizard link when token exists
#   - resolve_nvidia_smi         : locate nvidia-smi (PATH or the WSL2 location)
#   - resolve_amd_smi            : locate amd-smi (the stable AMD interface)
#   - detect_gpu_vendor          : nvidia | amd | intel | none probe
#   - resolve_gpu_vram_mb        : vendor-neutral total-VRAM (MB) probe
#   - strip_gpu_args             : drop --gpu selection for the CPU-retry re-exec
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
# an optional leading 'v' tolerated), 1 when it is older, 2 when VERSION is the
# literal 'unknown' (unreadable — caller decides how to treat it). Used to pin a
# real Compose floor instead of accepting any v2: the accelerator overlays merge
# a dev override's `deploy: !reset null`, and the `!reset`/`!override` merge tags
# require Docker Compose 2.24.4+ (Docker's compose-file merge reference).
compose_meets_floor() {
  local ver="${1#v}" floor="$2"
  [ "$ver" = unknown ] && return 2
  # Version-sort the pair: VERSION >= FLOOR exactly when FLOOR sorts first (ties
  # keep FLOOR first, so an equal version still passes).
  [ "$(printf '%s\n%s\n' "$floor" "$ver" | sort -V | head -n 1)" = "$floor" ]
}

# _wsl_without_systemd -> 0 when running under WSL (a Microsoft kernel) with no
# systemd as PID 1. On such a host the docker-ce + `systemctl` install plan
# starts a SECOND daemon that shadows Docker Desktop's and cannot be enabled
# (systemctl fails without systemd); the correct fix is to turn on Docker
# Desktop's WSL integration instead. Probes are env-overridable for testing:
# JARVIS_PROC_VERSION (the kernel version string) and JARVIS_SYSTEMD_DIR (a
# present directory means systemd is running).
_wsl_without_systemd() {
  grep -qi microsoft "${JARVIS_PROC_VERSION:-/proc/version}" 2>/dev/null || return 1
  [ -d "${JARVIS_SYSTEMD_DIR:-/run/systemd/system}" ] && return 1
  return 0
}

# prereq_install_plan OS OS_ID HAS_APT HAS_BREW HAS_DNF MISSING...
# Prints explicit package-manager commands for supported hosts, one command per
# line. Docker comes from Docker's official repository (docker-ce +
# docker-compose-plugin): stock distro packages miss the compose plugin on
# Debian/Ubuntu and lag Engine releases. A WSL host without systemd gets NO
# docker plan (return 1) — see _wsl_without_systemd; the manual guidance points
# it at Docker Desktop's WSL integration instead. Root-escalation contract (setup.sh
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
  local needs_docker=0 needs_compose=0 needs_openssl=0 needs_toolkit=0 needs_python3=0 item
  for item in "$@"; do
    case "$item" in
      docker) needs_docker=1 ;;
      docker-compose) needs_compose=1 ;;
      openssl) needs_openssl=1 ;;
      nvidia-toolkit) needs_toolkit=1 ;;
      python3) needs_python3=1 ;;
    esac
  done

  # WSL without systemd: refuse to auto-plan a docker-ce install. It would stand
  # up a second, systemctl-less daemon shadowing Docker Desktop's; the caller
  # falls through to prereq_manual_guidance, which points at Docker Desktop's WSL
  # integration instead.
  if [ "$needs_docker" = "1" ] && _wsl_without_systemd; then
    return 1
  fi

  case "$os" in
    Linux)
      case "$os_id" in
        debian|ubuntu|linuxmint|pop|popos)
          [ "$has_apt" = "1" ] || return 1
          _prereq_plan_apt "$os_id" "$needs_docker" "$needs_compose" "$needs_openssl" "$needs_toolkit" "$needs_python3"
          ;;
        fedora)
          [ "$has_dnf" = "1" ] || return 1
          _prereq_plan_dnf "$needs_docker" "$needs_compose" "$needs_openssl" "$needs_toolkit" "$needs_python3"
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
      if [ "$needs_python3" = "1" ]; then
        printf 'brew install python\n'
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
  local os_id="$1" needs_docker="$2" needs_compose="$3" needs_openssl="$4" needs_toolkit="$5" needs_python3="${6:-0}"
  local repo_base=ubuntu
  [ "$os_id" = "debian" ] && repo_base=debian

  printf 'sudo apt-get update\n'
  if [ "$needs_docker" = "1" ] || [ "$needs_compose" = "1" ]; then
    printf 'sudo apt-get install -y ca-certificates curl gnupg\n'
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

# -----------------------------------------------------------------------------
# The application images published to GHCR — single source of truth.
# -----------------------------------------------------------------------------
# Shared by every entry point that starts the stack (setup.sh, update.sh,
# scripts/jarvis-setup.sh), because each of them must materialise these images
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
# Both entry points (setup.sh, scripts/jarvis-setup.sh) read this instead of
# keeping their own hand-maintained profile lists, which is exactly how a group
# came to be persisted-but-not-health-checked (or started-but-not-persisted).
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

# MANDATORY_HEALTH_BASE — the always-on services both entry points must wait on.
# Shared here so setup.sh and scripts/jarvis-setup.sh cannot drift (the drift that
# left restore-uploader started-but-unverified by the wrapper).
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
raw-ip-lan|http|3001|lan-ip|paste|none|none|none|supported
named-private-https|https|443|origin-host|fragment|secure|origin-host|external|supported
local-https|https|3443|localhost|fragment|secure|localhost|mkcert|experimental
letsencrypt|https|443|domain|fragment|secure|domain|letsencrypt|supported
tunnel|https|443|tunnel-host|fragment|secure|tunnel-host|cloudflare|supported
ROUTES
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
  tmp="$(mktemp)" || { printf 'upsert_env_var: mktemp failed\n' >&2; return 1; }
  awk -v k="$k" -v v="$v" '
    index($0, k "=") == 1 { if (!seen) { print k "=" v; seen = 1 } ; next }
    { print }
    END { if (!seen) print k "=" v }
  ' .env > "$tmp" || { rm -f "$tmp"; printf 'upsert_env_var: awk rewrite of .env failed\n' >&2; return 1; }
  mv "$tmp" .env || { rm -f "$tmp"; printf 'upsert_env_var: mv to .env failed\n' >&2; return 1; }
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
