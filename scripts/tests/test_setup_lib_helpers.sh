#!/usr/bin/env bash
# test_setup_lib_helpers.sh — behavioral unit tests for the pure helpers in
# scripts/setup_lib.sh (sourced directly), plus static checks that setup.sh
# wires the disk preflight correctly. No docker daemon or network is needed:
# where a helper shells out to docker, a stub is placed on a private PATH.
#
# Run: bash scripts/tests/test_setup_lib_helpers.sh   (exit 0 = pass)
#
# shellcheck disable=SC2030,SC2031  # PATH/JARVIS_MODEL_CATALOG exports are
# deliberately subshell-scoped: each case gets a private stub environment.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_SCRIPT="${SCRIPT_DIR}/../../setup.sh"
# shellcheck source=../setup_lib.sh
# shellcheck disable=SC1091  # resolved at runtime relative to this file
source "${SCRIPT_DIR}/../setup_lib.sh"

FIXTURES="$(mktemp -d)"
trap 'rm -rf "$FIXTURES"' EXIT

fail=0
pass_n=0
pass() { pass_n=$((pass_n + 1)); printf 'PASS: %s\n' "$1"; }
expect_eq() {  # expect_eq <description> <got> <want>
  if [ "$2" = "$3" ]; then
    pass "$1"
  else
    printf 'FAIL: %s (got=%s want=%s)\n' "$1" "$2" "$3" >&2
    fail=1
  fi
}

# fake_docker <body> -> creates an executable `docker` stub in a private dir
# and echoes that dir (export it onto PATH inside a subshell to use it).
fake_docker() {
  local dir
  dir="$(mktemp -d "${FIXTURES}/bin.XXXXXX")"
  printf '#!/usr/bin/env bash\n%s\n' "$1" > "${dir}/docker"
  chmod +x "${dir}/docker"
  printf '%s' "$dir"
}

# === compute_required_disk_gb ================================================
# Formula under test: IMAGE_BUDGET_GB(variant) + infra image pulls (14) +
# ceil(sum of catalog disk_gb over compute_ollama_models' pull set).
# Budgets (measured 2026-07, containerd image store): cpu-pull=6, cpu-build=9,
# cuda-build=17.

CATALOG="${FIXTURES}/model_catalog.json"
cat > "$CATALOG" <<'JSON'
[
  {"id": "qwen3:14b", "ollama_tag": "qwen3:14b", "disk_gb": 9.2},
  {"id": "qwen3:4b", "ollama_tag": "qwen3:4b", "disk_gb": 2.5},
  {"id": "qwen3-embedding:4b", "ollama_tag": "qwen3-embedding:4b", "disk_gb": 2.6},
  {"id": "cloud/model", "ollama_tag": null, "disk_gb": 0.0}
]
JSON

# Pull set for smart=qwen3:14b is 14b + fast + embed = 9.2+2.5+2.6 -> ceil 15.
for vw in "cuda-build 46" "cpu-build 38" "cpu-pull 35"; do
  variant="${vw% *}"
  want="${vw#* }"
  got="$(export JARVIS_MODEL_CATALOG="$CATALOG"; compute_required_disk_gb qwen3:14b "$variant")" && rc=0 || rc=$?
  expect_eq "compute_required_disk_gb qwen3:14b ${variant} = budget+infra+ceil(models)" "${got}/${rc}" "${want}/0"
done

# smart == fast: the pull set de-dupes to fast+embed (2.5+2.6=5.1 -> ceil 6).
got="$(export JARVIS_MODEL_CATALOG="$CATALOG"; compute_required_disk_gb qwen3:4b cuda-build)" && rc=0 || rc=$?
expect_eq "smart==fast de-duped pull set" "${got}/${rc}" "37/0"

# Tag missing from the catalog -> conservative 18 GB for that tag
# (18 + 2.5 + 2.6 -> ceil 24), still catalog-derived (rc 0).
got="$(export JARVIS_MODEL_CATALOG="$CATALOG"; compute_required_disk_gb custom-model:7b cuda-build)" && rc=0 || rc=$?
expect_eq "missing catalog entry assumes 18 GB for that tag" "${got}/${rc}" "55/0"

# Unreadable catalog -> worst-case model-set constant (22) + rc 3; never a hard fail.
got="$(export JARVIS_MODEL_CATALOG="${FIXTURES}/nope.json"; compute_required_disk_gb qwen3:14b cuda-build 2>/dev/null)" && rc=0 || rc=$?
expect_eq "unreadable catalog degrades to the worst-case constant (rc 3)" "${got}/${rc}" "53/3"

# Corrupt catalog JSON degrades the same way.
printf 'not json' > "${FIXTURES}/corrupt.json"
got="$(export JARVIS_MODEL_CATALOG="${FIXTURES}/corrupt.json"; compute_required_disk_gb qwen3:14b cpu-build 2>/dev/null)" && rc=0 || rc=$?
expect_eq "corrupt catalog degrades to the worst-case constant (rc 3)" "${got}/${rc}" "45/3"

# The real repo catalog resolves the default pull set without falling back.
got="$(cd "${SCRIPT_DIR}/../.." && compute_required_disk_gb qwen3:8b cuda-build)" && rc=0 || rc=$?
expect_eq "repo catalog: default smart model is catalog-derived (rc 0)" "$rc" "0"
case "$got" in
  ''|*[!0-9]*) printf 'FAIL: repo catalog result not numeric (%s)\n' "$got" >&2; fail=1 ;;
  *) pass "repo catalog: result is numeric (${got} GB)" ;;
esac

# === resolve_docker_data_root ================================================

bin="$(fake_docker 'printf "/custom/docker-root\n"')"
got="$(export PATH="${bin}:${PATH}"; resolve_docker_data_root)"
expect_eq "resolve_docker_data_root honours docker info DockerRootDir" "$got" "/custom/docker-root"

bin="$(fake_docker 'exit 1')"
got="$(export PATH="${bin}:${PATH}"; resolve_docker_data_root)"
expect_eq "resolve_docker_data_root falls back to /var/lib/docker" "$got" "/var/lib/docker"

# === preflight_disk_lib ======================================================
# df must run against the resolved Docker data root, never `df .` (the install
# dir and the data root are different filesystems on split-mount hosts) — pin
# that by pointing the stub daemon at a private dir and checking the reported
# path.

DATA_ROOT="${FIXTURES}/data-root"
mkdir -p "$DATA_ROOT"
bin="$(fake_docker "printf '%s\n' '${DATA_ROOT}'")"

out="$(export PATH="${bin}:${PATH}"; preflight_disk_lib 0)" && rc=0 || rc=$?
expect_eq "preflight_disk_lib passes (rc 0) when free >= required" "$rc" "0"
expect_eq "preflight_disk_lib measures the docker data root (not \$PWD)" "${out#* }" "$DATA_ROOT"
case "${out%% *}" in
  ''|*[!0-9]*) printf 'FAIL: free_gb not numeric (%s)\n' "$out" >&2; fail=1 ;;
  *) pass "preflight_disk_lib reports a numeric free_gb (${out%% *})" ;;
esac

out="$(export PATH="${bin}:${PATH}"; preflight_disk_lib 999999)" && rc=0 || rc=$?
expect_eq "preflight_disk_lib fails (rc 1) on a shortfall" "$rc" "1"

bin="$(fake_docker "printf '%s\n' '${FIXTURES}/missing-root'")"
out="$(export PATH="${bin}:${PATH}"; preflight_disk_lib 1)" && rc=0 || rc=$?
expect_eq "preflight_disk_lib returns 2 when df cannot measure the root" "$rc" "2"
expect_eq "unmeasurable root still reports the path with 0 free" "$out" "0 ${FIXTURES}/missing-root"

# === setup.sh wiring (static) ================================================

scheck() {  # scheck <description> <grep -E pattern>
  if grep -Eq -e "$2" "$SETUP_SCRIPT"; then
    pass "$1"
  else
    printf 'FAIL: %s (pattern: %s)\n' "$1" "$2" >&2
    fail=1
  fi
}
sline() { grep -nE -e "$1" "$SETUP_SCRIPT" | head -1 | cut -d: -f1; }

scheck "setup.sh parses --skip-disk-check" '--skip-disk-check\)'
scheck "setup.sh defaults SKIP_DISK_CHECK=0" '^SKIP_DISK_CHECK=0'
scheck "setup.sh defines preflight_disk()" '^preflight_disk\(\)'
scheck "the shortfall die names the --skip-disk-check escape" 'skip-disk-check to proceed'

# The preflight call must come after the smart-model resolution (the required
# figure depends on it) and before anything pulls or builds.
resolve_line="$(grep -nE -e ' prompt_ai_backend$' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
call_line="$(sline '^preflight_disk$')"
pull_line="$(sline 'up -d ollama')"
if [ -n "$resolve_line" ] && [ -n "$call_line" ] && [ -n "$pull_line" ] \
   && [ "$resolve_line" -lt "$call_line" ] && [ "$call_line" -lt "$pull_line" ]; then
  pass "preflight_disk runs after model resolution and before the first pull"
else
  printf 'FAIL: preflight_disk call (%s) is not between model resolution (%s) and the first pull (%s)\n' \
    "$call_line" "$resolve_line" "$pull_line" >&2
  fail=1
fi

# run_doctor's --check disk advisory stays warn-only (never a die).
doctor_start="$(sline '^run_doctor\(\)')"
doctor_end="$(awk "NR>${doctor_start} && /^}/{print NR; exit}" "$SETUP_SCRIPT")"
if [ -n "$doctor_start" ] && [ -n "$doctor_end" ] \
   && ! sed -n "${doctor_start},${doctor_end}p" "$SETUP_SCRIPT" | grep -qE 'die "|preflight_disk'; then
  pass "run_doctor keeps the disk check advisory (no die / no fatal preflight)"
else
  printf 'FAIL: run_doctor (%s-%s) gained a fatal disk path\n' "$doctor_start" "$doctor_end" >&2
  fail=1
fi

# === preflight_disk policy (behavioral, extracted from setup.sh) ============
# The wrapper's fatal/warn policy is the regression surface: a shortfall is
# fatal ONLY on a first install; cached app images, an unmeasurable df, or a
# catalog-fallback estimate with >=20 GB free must all soften to a warning.

pf_src="$(sed -n '/^preflight_disk()/,/^}/p' "$SETUP_SCRIPT")"

run_preflight() {  # <skip> <req_gb> <req_rc> <lib_out> <lib_rc> <images_out>
  SKIP="$1" REQ_GB="$2" REQ_RC="$3" LIB_OUT="$4" LIB_RC="$5" IMAGES_OUT="$6" \
  LIB_SRC="${SCRIPT_DIR}/../setup_lib.sh" bash -c '
    set -euo pipefail
    # The real lib provides PUBLISHED_IMAGE_REPOS (the wrapper iterates it, and
    # a private copy here would drift). Source it FIRST: the stubs below must
    # clobber its real compute_required_disk_gb/preflight_disk_lib.
    source "$LIB_SRC"
    info() { printf "INFO %s\n" "$*"; }
    ok()   { printf "OK %s\n" "$*"; }
    warn() { printf "WARN %s\n" "$*"; }
    die()  { printf "DIE %s\n" "$1"; printf "HINT %s\n" "$2"; exit 1; }
    docker() {
      case "$1" in
        images) printf "%s" "$IMAGES_OUT" ;;
        *) return 1 ;;   # docker info fails -> no nvidia runtime (cpu-build)
      esac
    }
    compute_required_disk_gb() { printf "%s" "$REQ_GB"; return "$REQ_RC"; }
    preflight_disk_lib() { printf "%s" "$LIB_OUT"; return "$LIB_RC"; }
    SKIP_DISK_CHECK="$SKIP"
    NI_SMART_MODEL="qwen3:8b"
    '"$pf_src"'
    preflight_disk
  '
}

out="$(run_preflight 0 45 0 '10 /var/lib/docker' 1 '')" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*DIE*--skip-disk-check*) pass "first-install shortfall is fatal and names the escape flag" ;;
  *) printf 'FAIL: first-install shortfall not fatal (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac

out="$(run_preflight 0 45 0 '10 /var/lib/docker' 1 'abc123')" && rc=0 || rc=$?
case "${rc}:${out}" in
  0:*WARN*) pass "cached app images downgrade the shortfall to a warning" ;;
  *) printf 'FAIL: cached-image re-run was blocked (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac

out="$(run_preflight 0 45 3 '25 /var/lib/docker' 1 '')" && rc=0 || rc=$?
case "${rc}:${out}" in
  0:*WARN*) pass "fallback estimate with >=20 GB free downgrades to a warning" ;;
  *) printf 'FAIL: fallback estimate above the hard floor was blocked (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac

out="$(run_preflight 0 45 3 '15 /var/lib/docker' 1 '')" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*DIE*) pass "fallback estimate below the 20 GB hard floor stays fatal" ;;
  *) printf 'FAIL: fallback below the hard floor did not die (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac

out="$(run_preflight 0 45 0 '0 /var/lib/docker' 2 '')" && rc=0 || rc=$?
case "${rc}:${out}" in
  0:*WARN*) pass "unmeasurable free space proceeds with a warning" ;;
  *) printf 'FAIL: unmeasurable df blocked the install (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac

out="$(run_preflight 1 45 0 '10 /var/lib/docker' 1 '')" && rc=0 || rc=$?
case "${rc}:${out}" in
  0:*Skipping*) pass "--skip-disk-check bypasses the check entirely" ;;
  *) printf 'FAIL: --skip-disk-check did not bypass (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac

# === prereq_install_plan =====================================================
# Cold-box contract: Docker comes from Docker's official repository (docker-ce
# + docker-compose-plugin) — stock docker.io misses the compose plugin on
# Debian/Ubuntu and skips the daemon/group steps. Root-escalation discipline:
# every plan line is unprivileged or starts with exactly ONE line-leading sudo
# (_run_prereq_plan rewrites only line-leading sudo to `sudo -n`, so a
# mid-pipeline sudo would hang a no-tty non-interactive run); root-consumed
# files are fetched straight to their root-owned destinations rather than staged
# in a predictable /tmp path (CWE-377); and remote content is never piped into a
# shell (piping fetched data into a tool such as `gpg --dearmor` is fine).

plan_has() {  # plan_has <description> <plan> <grep -E pattern>
  if printf '%s\n' "$2" | grep -Eq -e "$3"; then
    pass "$1"
  else
    printf 'FAIL: %s (missing pattern: %s)\n' "$1" "$3" >&2
    fail=1
  fi
}
plan_lacks() {  # plan_lacks <description> <plan> <grep -E pattern>
  if printf '%s\n' "$2" | grep -Eq -e "$3"; then
    printf 'FAIL: %s (unwanted pattern: %s)\n' "$1" "$3" >&2
    fail=1
  else
    pass "$1"
  fi
}

plan="$(prereq_install_plan Linux ubuntu 1 0 0 docker docker-compose openssl nvidia-toolkit)" || plan=""
plan_has   "ubuntu: docker-ce repo from download.docker.com"       "$plan" 'download\.docker\.com/linux/ubuntu'
plan_has   "ubuntu: installs docker-ce + official compose plugin"  "$plan" 'apt-get install -y docker-ce docker-ce-cli containerd\.io docker-buildx-plugin docker-compose-plugin'
plan_lacks "ubuntu: stock docker.io is gone"                       "$plan" 'docker\.io'
plan_has   "ubuntu: signing key fetched straight to the root keyring (no /tmp)" "$plan" '^sudo curl -fsSL https://download\.docker\.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker\.asc$'
plan_has   "ubuntu: repo written by a root shell, not staged in /tmp"          "$plan" '^sudo sh -c .* /etc/apt/sources\.list\.d/docker\.list'
plan_has   "ubuntu: repo pinned to the fetched .asc key via signed-by"         "$plan" 'signed-by=/etc/apt/keyrings/docker\.asc'
plan_has   "ubuntu: enables + starts the daemon"                   "$plan" '^sudo systemctl enable --now docker$'
plan_has   "ubuntu: adds the user to the docker group"             "$plan" '^sudo usermod -aG docker'
plan_has   "ubuntu: installs nvidia-container-toolkit"             "$plan" 'apt-get install -y nvidia-container-toolkit'
plan_has   "ubuntu: wires the nvidia runtime into docker"          "$plan" '^sudo nvidia-ctk runtime configure --runtime=docker$'
plan_has   "ubuntu: restarts the daemon after runtime configure"   "$plan" '^sudo systemctl restart docker$'
# No root-consumed file is staged at a predictable /tmp path (CWE-377): a local
# attacker on a multi-user host could pre-plant it before the sudo reads it back.
plan_lacks "ubuntu: no root-consumed file staged in a predictable /tmp path"   "$plan" '/tmp/'
# A remote fetch is never piped into a SHELL (curl|sh); piping fetched data into
# a tool (curl | gpg --dearmor) is fine and used for the nvidia key.
plan_lacks "ubuntu: no remote content piped into a shell"          "$plan" '\| *(sh|bash)([[:space:]]|$)'
plan_lacks "ubuntu: every sudo is line-leading (exactly one)"      "$plan" '.sudo '

# nvidia-ctk configure edits /etc/docker/daemon.json — the engine must be
# installed first.
install_ln="$(printf '%s\n' "$plan" | grep -n 'install -y docker-ce' | head -1 | cut -d: -f1 || true)"
ctk_ln="$(printf '%s\n' "$plan" | grep -n 'nvidia-ctk runtime configure' | head -1 | cut -d: -f1 || true)"
if [ -n "$install_ln" ] && [ -n "$ctk_ln" ] && [ "$install_ln" -lt "$ctk_ln" ]; then
  pass "ubuntu: toolkit configure ordered after docker install"
else
  printf 'FAIL: toolkit configure (line %s) not after docker install (line %s)\n' "$ctk_ln" "$install_ln" >&2
  fail=1
fi

# Mint/Pop: Docker's apt dists are UBUNTU codenames — a derivative's own
# VERSION_CODENAME ('wilma', 'jolnir') 404s against download.docker.com.
mint_plan="$(prereq_install_plan Linux linuxmint 1 0 0 docker)" || mint_plan=""
# shellcheck disable=SC2016  # the plan must carry the UNEXPANDED derivation
if printf '%s\n' "$mint_plan" | grep -qF '${UBUNTU_CODENAME:-$VERSION_CODENAME}'; then
  pass "mint: codename derives from UBUNTU_CODENAME with VERSION_CODENAME fallback"
else
  # shellcheck disable=SC2016
  printf 'FAIL: mint plan lacks the ${UBUNTU_CODENAME:-$VERSION_CODENAME} derivation\n' >&2
  fail=1
fi
plan_has "mint: uses the ubuntu repo path" "$mint_plan" 'download\.docker\.com/linux/ubuntu'

debian_plan="$(prereq_install_plan Linux debian 1 0 0 docker)" || debian_plan=""
plan_has "debian: uses the debian repo path" "$debian_plan" 'download\.docker\.com/linux/debian'

# Fedora mirrors the apt branch via dnf + Docker's repo file.
fedora_plan="$(prereq_install_plan Linux fedora 0 0 1 docker openssl nvidia-toolkit)" || fedora_plan=""
plan_has   "fedora: fetches Docker's repo file straight to the root repo dir (no /tmp)" "$fedora_plan" '^sudo curl -fsSL https://download\.docker\.com/linux/fedora/docker-ce\.repo -o /etc/yum\.repos\.d/docker-ce\.repo$'
plan_has   "fedora: installs docker-ce via dnf"               "$fedora_plan" '^sudo dnf install -y docker-ce docker-ce-cli containerd\.io docker-buildx-plugin docker-compose-plugin openssl$'
plan_has   "fedora: enables + starts the daemon"              "$fedora_plan" '^sudo systemctl enable --now docker$'
plan_has   "fedora: adds the user to the docker group"        "$fedora_plan" '^sudo usermod -aG docker'
plan_has   "fedora: installs nvidia-container-toolkit"        "$fedora_plan" '^sudo dnf install -y nvidia-container-toolkit$'
plan_lacks "fedora: no root-consumed file staged in a predictable /tmp path" "$fedora_plan" '/tmp/'
plan_lacks "fedora: every sudo is line-leading (exactly one)" "$fedora_plan" '.sudo '

prereq_install_plan Linux fedora 0 0 0 docker >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "fedora without dnf refuses to plan" "$rc" "1"

# openssl-only stays a plain package install (no Docker repo bootstrap).
ssl_plan="$(prereq_install_plan Linux ubuntu 1 0 0 openssl)" || ssl_plan=""
plan_has   "openssl-only: plain apt install"    "$ssl_plan" '^sudo apt-get install -y openssl$'
plan_lacks "openssl-only: no docker repo setup" "$ssl_plan" 'download\.docker\.com'

# === _gpu_present_for_prereqs ================================================
# NVIDIA GPU visible but docker lacks the nvidia runtime (or is absent) -> the
# prereq plan must add the container toolkit. Stubs shadow any real
# nvidia-smi/docker on PATH.

GPU_BIN="$(mktemp -d "${FIXTURES}/bin.XXXXXX")"
cat > "${GPU_BIN}/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
echo "GPU 0: Stub GPU"
EOF
cat > "${GPU_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "${GPU_BIN}/nvidia-smi" "${GPU_BIN}/docker"

(export PATH="${GPU_BIN}:${PATH}"; _gpu_present_for_prereqs) && rc=0 || rc=$?
expect_eq "GPU present + docker unreachable -> toolkit needed (rc 0)" "$rc" "0"

cat > "${GPU_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' '{"nvidia":{"path":"nvidia-container-runtime"}}'
EOF
chmod +x "${GPU_BIN}/docker"
(export PATH="${GPU_BIN}:${PATH}"; _gpu_present_for_prereqs) && rc=0 || rc=$?
expect_eq "GPU present + nvidia runtime already wired -> no toolkit (rc 1)" "$rc" "1"

cat > "${GPU_BIN}/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "${GPU_BIN}/nvidia-smi"
(export PATH="${GPU_BIN}:${PATH}"; _gpu_present_for_prereqs) && rc=0 || rc=$?
expect_eq "no usable GPU -> no toolkit (rc 1)" "$rc" "1"

# === detect_gpu_vendor / resolve_amd_smi / resolve_gpu_vram_mb ===============
# Probe order is nvidia -> amd -> intel -> none. amd-smi's JSON is the stable
# AMD machine interface (rocm-smi's explicitly is not) and is parsed
# missing-field-tolerant. Stubs shadow any real vendor tools on PATH;
# JARVIS_WSL_NVIDIA_SMI/JARVIS_DRI_DIR point probes at private fixtures.

AMD_JSON="${FIXTURES}/amd-smi-static.json"
cat > "$AMD_JSON" <<'JSON'
[
  {
    "gpu": 0,
    "asic": {"market_name": "AMD Radeon RX 7800 XT", "vendor_id": "0x1002"},
    "vram": {"type": "GDDR6", "vendor": "SAMSUNG", "size": {"value": 16368, "unit": "MB"}}
  }
]
JSON

# make_vendor_bin <nvidia-behavior> <amd-behavior> -> stub dir for PATH.
# behaviors: ok = enumerate a GPU (nvidia also answers --query-gpu), fail = exit 1.
make_vendor_bin() {
  local dir
  dir="$(mktemp -d "${FIXTURES}/bin.XXXXXX")"
  if [ "$1" = "ok" ]; then
    cat > "${dir}/nvidia-smi" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -L) echo "GPU 0: Stub NVIDIA GPU" ;;
  --query-gpu=memory.total) echo "24576" ;;
esac
EOF
  else
    printf '#!/usr/bin/env bash\nexit 1\n' > "${dir}/nvidia-smi"
  fi
  if [ "$2" = "ok" ]; then
    printf '#!/usr/bin/env bash\ncat "%s"\n' "$AMD_JSON" > "${dir}/amd-smi"
  else
    printf '#!/usr/bin/env bash\nexit 1\n' > "${dir}/amd-smi"
  fi
  chmod +x "${dir}/nvidia-smi" "${dir}/amd-smi"
  printf '%s' "$dir"
}

DRI_EMPTY="${FIXTURES}/dri-empty"; mkdir -p "$DRI_EMPTY"

AMD_BIN="$(make_vendor_bin fail ok)"
NV_BIN="$(make_vendor_bin ok ok)"
NOGPU_BIN="$(make_vendor_bin fail fail)"

vendor_env() {  # vendor_env <bin> <dri_dir> <fn...> — run fn under the stub env
  local bin="$1" dri="$2"; shift 2
  (export PATH="${bin}:${PATH}" JARVIS_WSL_NVIDIA_SMI=/nonexistent JARVIS_DRI_DIR="$dri"; "$@")
}

got="$(vendor_env "$NV_BIN" "$DRI_EMPTY" detect_gpu_vendor)"
expect_eq "nvidia wins the probe order even with amd-smi present" "$got" "nvidia"

got="$(vendor_env "$AMD_BIN" "$DRI_EMPTY" detect_gpu_vendor)"
expect_eq "amd-smi enumerating a GPU -> amd" "$got" "amd"

# A bare /dev/dri render node is NOT proof of an Intel GPU: VMs expose a
# virtio-gpu render node, and an ARM SoC render node has no PCI vendor file.
# Classification keys off the render node's PCI vendor id.
# vendor_of <vendor-id-or-empty> -> classification for a render node whose PCI
# vendor file holds <vendor-id> (empty = no vendor file), no discrete tool present.
vendor_of() {
  local d s
  d="$(mktemp -d "${FIXTURES}/dri.XXXXXX")"; touch "${d}/renderD128"
  s="$(mktemp -d "${FIXTURES}/drm.XXXXXX")"
  if [ -n "$1" ]; then
    mkdir -p "${s}/renderD128/device"
    printf '%s\n' "$1" > "${s}/renderD128/device/vendor"
  fi
  (export PATH="${NOGPU_BIN}:${PATH}" JARVIS_WSL_NVIDIA_SMI=/nonexistent \
     JARVIS_DRI_DIR="$d" JARVIS_DRM_SYS_DIR="$s"; detect_gpu_vendor)
}

expect_eq "render node, PCI vendor 0x8086 (Intel) -> intel" "$(vendor_of 0x8086)" "intel"
expect_eq "render node, PCI vendor 0x1af4 (virtio VM) -> none" "$(vendor_of 0x1af4)" "none"
expect_eq "render node, no PCI vendor file (ARM SoC) -> none" "$(vendor_of '')" "none"
expect_eq "render node, PCI vendor 0x1002 (AMD) -> amd" "$(vendor_of 0x1002)" "amd"

got="$(vendor_env "$NOGPU_BIN" "$DRI_EMPTY" detect_gpu_vendor)"
expect_eq "no probe answers -> none" "$got" "none"

# resolve_dri_gids echoes the numeric owning-group ids of the /dev/dri nodes.
# Assert against stat -c %g of the fixture files themselves (tmpdir owner GID —
# no root needed).
DRI_GIDS="${FIXTURES}/dri-gids"; mkdir -p "$DRI_GIDS"
touch "${DRI_GIDS}/card0" "${DRI_GIDS}/renderD128"
want_video="$(stat -c %g "${DRI_GIDS}/card0")"
want_render="$(stat -c %g "${DRI_GIDS}/renderD128")"
got="$(JARVIS_DRI_DIR="$DRI_GIDS" resolve_dri_gids)"
expect_eq "resolve_dri_gids echoes '<video_gid> <render_gid>'" "$got" "${want_video} ${want_render}"

DRI_RONLY="${FIXTURES}/dri-render-only"; mkdir -p "$DRI_RONLY"; touch "${DRI_RONLY}/renderD128"
want_render="$(stat -c %g "${DRI_RONLY}/renderD128")"
got="$(JARVIS_DRI_DIR="$DRI_RONLY" resolve_dri_gids)"
expect_eq "resolve_dri_gids: video falls back to render GID with no card* node" "$got" "${want_render} ${want_render}"

DRI_NORENDER="${FIXTURES}/dri-no-render"; mkdir -p "$DRI_NORENDER"
got="$(JARVIS_DRI_DIR="$DRI_NORENDER" resolve_dri_gids)" && rc=0 || rc=$?
expect_eq "resolve_dri_gids returns 1 with no renderD* node" "$rc" "1"
expect_eq "resolve_dri_gids echoes nothing with no renderD* node" "$got" ""

got="$(vendor_env "$NV_BIN" "$DRI_EMPTY" resolve_gpu_vram_mb nvidia)"
expect_eq "resolve_gpu_vram_mb nvidia reads nvidia-smi memory.total" "$got" "24576"

got="$(vendor_env "$AMD_BIN" "$DRI_EMPTY" resolve_gpu_vram_mb amd)"
expect_eq "resolve_gpu_vram_mb amd reads the amd-smi vram size" "$got" "16368"

vendor_env "$AMD_BIN" "$DRI_EMPTY" resolve_gpu_vram_mb intel >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "intel (shared system RAM) reports no VRAM figure (rc 1)" "$rc" "1"

# Missing-field tolerance: a vram block without a size must fail cleanly (rc 1),
# and a plain-number size (older amd-smi shape) must still parse as MB.
NOSIZE_BIN="$(make_vendor_bin fail ok)"
printf '#!/usr/bin/env bash\nprintf %s\n' "'[{\"gpu\": 0}]'" > "${NOSIZE_BIN}/amd-smi"
chmod +x "${NOSIZE_BIN}/amd-smi"
vendor_env "$NOSIZE_BIN" "$DRI_EMPTY" resolve_gpu_vram_mb amd >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "amd-smi JSON without vram size -> rc 1 (no phantom VRAM)" "$rc" "1"

PLAIN_BIN="$(make_vendor_bin fail ok)"
printf '#!/usr/bin/env bash\nprintf %s\n' "'[{\"gpu\": 0, \"vram\": {\"size\": 8192}}]'" > "${PLAIN_BIN}/amd-smi"
chmod +x "${PLAIN_BIN}/amd-smi"
got="$(vendor_env "$PLAIN_BIN" "$DRI_EMPTY" resolve_gpu_vram_mb amd)"
expect_eq "plain-number vram size parses as MB" "$got" "8192"

# Tier parity: an AMD host must land a non-CPU tier once its VRAM arrives.
# detect_hw_tier lives in setup.sh — extract and run it against the stubs.
tier_src="$(sed -n '/^detect_hw_tier()/,/^}/p' "$SETUP_SCRIPT")"
got="$(export PATH="${AMD_BIN}:${PATH}" JARVIS_WSL_NVIDIA_SMI=/nonexistent JARVIS_DRI_DIR="$DRI_EMPTY"
       eval "$tier_src"; detect_hw_tier)"
expect_eq "amd-smi fixture (16368 MB) yields a non-CPU tier" "$got" "16-24"
got="$(export PATH="${NOGPU_BIN}:${PATH}" JARVIS_WSL_NVIDIA_SMI=/nonexistent JARVIS_DRI_DIR="$DRI_EMPTY"
       eval "$tier_src"; detect_hw_tier)"
expect_eq "no measurable VRAM stays cpu tier" "$got" "cpu"

# === compute_compose_file =====================================================
# Overlay basename ("" | gpu | rocm | vulkan) + override flag -> COMPOSE_FILE.
# override.yml must always come LAST so a dev override's `deploy: !reset null`
# wins over any accelerator overlay.

expect_eq "compute_compose_file: CPU base only" \
  "$(compute_compose_file "" 0)" "docker-compose.yml"
expect_eq "compute_compose_file: gpu overlay before override" \
  "$(compute_compose_file gpu 1)" "docker-compose.yml:docker-compose.gpu.yml:docker-compose.override.yml"
expect_eq "compute_compose_file: rocm overlay" \
  "$(compute_compose_file rocm 0)" "docker-compose.yml:docker-compose.rocm.yml"
expect_eq "compute_compose_file: vulkan overlay before override" \
  "$(compute_compose_file vulkan 1)" "docker-compose.yml:docker-compose.vulkan.yml:docker-compose.override.yml"

# === setup.sh GPU wiring (static) =============================================

scheck "setup.sh parses --gpu with the four overlay choices" 'cuda\|rocm\|vulkan\|cpu\) NI_GPU_OVERRIDE'
scheck "setup.sh defaults NI_GPU_OVERRIDE empty" '^NI_GPU_OVERRIDE=""'
scheck "invalid --gpu dies with the accepted set" 'Invalid --gpu'
scheck "overlay selection references the ROCm overlay" 'docker-compose\.rocm\.yml'
scheck "overlay selection references the Vulkan overlay" 'docker-compose\.vulkan\.yml'
scheck "AMD ROCm engagement is gated on /dev/kfd" 'JARVIS_KFD_DEV:-/dev/kfd'
scheck "setup.sh persists JARVIS_GPU_VENDOR into .env" 'JARVIS_GPU_VENDOR\)'
# GPU overlay is opt-in (default flip) with working numeric group_add + CPU recovery.
scheck "Intel/AMD default to a CPU install (Vulkan/ROCm opt-in)" 'Vulkan acceleration is experimental; opt in'
scheck "GPU overlay selection resolves numeric /dev/dri GIDs" 'resolve_dri_gids'
scheck "the resolved render GID is persisted to .env" 'upsert_env_var JARVIS_RENDER_GID'
scheck "a failed GPU overlay offers a one-keypress CPU retry via re-exec" 'exec "\$0" --gpu cpu'
scheck "the CPU-retry prompt is gated on an interactive TTY (never non-interactive)" '\-eq 0 \] && \[ -t 0'

# === setup.sh prereq wiring (static) =========================================

scheck "setup.sh refuses snap-packaged docker" 'snap list docker'
scheck "snap refusal names the removal command" 'snap remove docker'
scheck "setup.sh initialises DOCKER_JUST_INSTALLED" '^DOCKER_JUST_INSTALLED=0'
scheck "fresh-install permission denial exits 3 (distinct from failure)" '^ +exit 3$'
scheck "exit-3 path tells the user to re-login or newgrp" 'log out and back in.*newgrp docker'
scheck "usage text documents exit code 3" '^#   3  Docker was just installed'
scheck "host probe passes dnf availability to the planner" 'command -v dnf'

# === setup.sh flag-value validation (behavioral subprocess) ==========
# A value-taking flag followed by ANOTHER recognized flag (instead of its
# value) must die with an actionable message rather than silently swallowing
# the next flag as its value and exiting 0. setup.sh's flag loop (~420-553)
# runs entirely before any docker/prereq code, so every case below exits via
# die/-h before touching docker — safe to run as a real subprocess.

run_setup() {  # run_setup <arg...> -> combined stdout+stderr; rc left in $?
  bash "$SETUP_SCRIPT" "$@" 2>&1
}

for flag in --domain --admin-email --profile --smtp-host --smtp-user \
            --smtp-pass-file --mode --backend --smart-model --gpu; do
  out="$(run_setup "$flag" --non-interactive --help)" && rc=0 || rc=$?
  case "${rc}:${out}" in
    1:*"${flag} requires a value"*)
      pass "${flag} followed by another flag dies (does not swallow it)" ;;
    *)
      printf 'FAIL: %s followed by another flag did not die as expected (rc=%s out=%s)\n' \
        "$flag" "$rc" "$out" >&2
      fail=1 ;;
  esac
done

# No-regression: a real value still works.
out="$(run_setup --domain example.com --help)" && rc=0 || rc=$?
if [ "$rc" -eq 0 ]; then
  pass "--domain example.com --help still exits 0 (no regression)"
else
  printf 'FAIL: --domain example.com --help rc=%s out=%s\n' "$rc" "$out" >&2; fail=1
fi

# No-regression: the --flag=value form tolerates a dash-prefixed value (the
# documented escape hatch — must NOT be affected by the $2-lookahead guard,
# since the guard only fires for the space-separated form).
out="$(run_setup --domain=-example.com --help)" && rc=0 || rc=$?
if [ "$rc" -eq 0 ]; then
  pass "--domain=-example.com --help still exits 0 (= form escape hatch intact)"
else
  printf 'FAIL: --domain=-example.com --help rc=%s out=%s\n' "$rc" "$out" >&2; fail=1
fi

# No-regression: a value-taking flag as the LAST argument still dies (the
# original set -u guard this pre-check exists for).
out="$(run_setup --domain)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'--domain requires a value'*) pass "--domain as the last argument still dies" ;;
  *) printf 'FAIL: --domain as last arg rc=%s out=%s\n' "$rc" "$out" >&2; fail=1 ;;
esac

# =============================================================================

if [ "$fail" -ne 0 ]; then
  printf '\nsetup_lib helpers: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nsetup_lib helpers: all %s checks passed\n' "$pass_n"
