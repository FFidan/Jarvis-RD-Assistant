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

# Default the WSL probe to a NON-WSL kernel string so prereq planning is
# host-independent (a WSL dev box would otherwise flip the docker-plan cases);
# individual cases override JARVIS_PROC_VERSION as needed.
export JARVIS_PROC_VERSION="${FIXTURES}/proc-version-native"
printf 'Linux version 6.8.0-generic\n' > "$JARVIS_PROC_VERSION"

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

# === compute_model_disk_gb ===================================================
# The model-only figure the cached-image disk escape hatch checks against (the
# model set is pulled on EVERY run). Pull set for qwen3:14b is 14b+fast+embed =
# 9.2+2.5+2.6 -> ceil 15; catalog-derived is rc 0, unreadable is worst-case 22
# + rc 3. compute_required_disk_gb = _image_budget + infra(14) + this.
got="$(export JARVIS_MODEL_CATALOG="$CATALOG"; compute_model_disk_gb qwen3:14b)" && rc=0 || rc=$?
expect_eq "compute_model_disk_gb qwen3:14b = ceil(model set) (rc 0)" "${got}/${rc}" "15/0"
got="$(export JARVIS_MODEL_CATALOG="${FIXTURES}/nope.json"; compute_model_disk_gb qwen3:14b 2>/dev/null)" && rc=0 || rc=$?
expect_eq "compute_model_disk_gb unreadable catalog -> worst-case 22 (rc 3)" "${got}/${rc}" "22/3"
# compute_required_disk_gb still equals image_budget + infra + model set.
got="$(export JARVIS_MODEL_CATALOG="$CATALOG"; compute_required_disk_gb qwen3:14b cuda-build)" && rc=0 || rc=$?
expect_eq "compute_required_disk_gb = 17(cuda) + 14(infra) + 15(models)" "${got}/${rc}" "46/0"

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

run_preflight() {  # <skip> <req_gb> <req_rc> <lib_out> <lib_rc> <images_out> [model_gb]
  SKIP="$1" REQ_GB="$2" REQ_RC="$3" LIB_OUT="$4" LIB_RC="$5" IMAGES_OUT="$6" MODEL_GB="${7:-8}" \
  LIB_SRC="${SCRIPT_DIR}/../setup_lib.sh" bash -c '
    set -euo pipefail
    # The real lib provides PUBLISHED_IMAGE_REPOS (the wrapper iterates it, and
    # a private copy here would drift). Source it FIRST: the stubs below must
    # clobber its real compute_required_disk_gb/compute_model_disk_gb/
    # preflight_disk_lib.
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
    compute_model_disk_gb() { printf "%s" "$MODEL_GB"; }
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

# Cached app images + the model pull STILL fits free space -> warn, proceed.
out="$(run_preflight 0 45 0 '10 /var/lib/docker' 1 'abc123' 8)" && rc=0 || rc=$?
case "${rc}:${out}" in
  0:*WARN*model?pull?fits*) pass "cached app images + model pull fits -> warning, proceed" ;;
  0:*WARN*) pass "cached app images downgrade the shortfall to a warning" ;;
  *) printf 'FAIL: cached-image re-run was blocked (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac

# Cached app images but the model pull does NOT fit -> still fatal (models are
# pulled every run, so the escape hatch cannot ignore the model-set space).
out="$(run_preflight 0 45 0 '10 /var/lib/docker' 1 'abc123' 25)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*DIE*model?set*) pass "cached images but model pull won't fit stays fatal (model-set space)" ;;
  *) printf 'FAIL: cached-image model shortfall not fatal (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
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
  0:*WARN*Docker?Desktop*) pass "unmeasurable free space explains Docker Desktop and proceeds" ;;
  *) printf 'FAIL: unmeasurable df did not explain Docker Desktop / blocked the install (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
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

# === strip_gpu_args ==========================================================
# The CPU-retry re-exec argv is the original invocation minus any --gpu
# selection (both the `--gpu VALUE` pair and the `--gpu=VALUE` form), so the
# appended `--gpu cpu` is the only GPU flag and the retry cannot loop back into
# the overlay path. Output is one surviving arg per line.

expect_eq "strip_gpu_args drops the --gpu VALUE pair, keeps the rest" \
  "$(strip_gpu_args --gpu vulkan --non-interactive)" "--non-interactive"
expect_eq "strip_gpu_args drops the --gpu=VALUE form" \
  "$(strip_gpu_args --gpu=rocm)" ""
expect_eq "strip_gpu_args passes non-gpu args through unchanged (order preserved)" \
  "$(strip_gpu_args --domain example.com --non-interactive --backend ollama)" \
  "$(printf '%s\n' --domain example.com --non-interactive --backend ollama)"
expect_eq "strip_gpu_args on empty args echoes nothing" \
  "$(strip_gpu_args)" ""

# === print_setup_link ========================================================
# Prints the click-to-finish wizard link and sets SETUP_LINK when a setup token
# exists under secrets/ relative to CWD; with no token it clears SETUP_LINK and
# prints nothing. A trailing slash on the base must not double up before /setup.
# The token rides a URL FRAGMENT (#setup_token=), never a query string, so it
# never reaches server access logs / the Referer header / a proxy request line.

LINK_DIR="$(mktemp -d "${FIXTURES}/setuplink.XXXXXX")"
mkdir -p "${LINK_DIR}/secrets"
printf 'tok123' > "${LINK_DIR}/secrets/jarvis_setup_token.txt"

got="$(cd "$LINK_DIR"; print_setup_link "https://jarvis.example" >/dev/null; printf '%s' "$SETUP_LINK")"
expect_eq "print_setup_link builds SETUP_LINK from base + token" \
  "$got" "https://jarvis.example/setup#setup_token=tok123"
case "$got" in
  *"?setup_token="*) printf 'FAIL: setup token rode a query string, not a fragment (got=%s)\n' "$got" >&2; fail=1 ;;
  *) pass "print_setup_link keeps the token in a fragment, never a query string" ;;
esac
printed="$(cd "$LINK_DIR"; print_setup_link "https://jarvis.example")"
case "$printed" in
  *"Finish setup: https://jarvis.example/setup#setup_token=tok123"*)
    pass "print_setup_link prints the finish-setup line with the link" ;;
  *) printf 'FAIL: print_setup_link output missing the link (got=%s)\n' "$printed" >&2; fail=1 ;;
esac

got="$(cd "$LINK_DIR"; print_setup_link "https://jarvis.example/" >/dev/null; printf '%s' "$SETUP_LINK")"
expect_eq "print_setup_link: trailing slash on base does not double up before /setup" \
  "$got" "https://jarvis.example/setup#setup_token=tok123"

NOTOK_DIR="$(mktemp -d "${FIXTURES}/setuplink-notok.XXXXXX")"
mkdir -p "${NOTOK_DIR}/secrets"
got="$(cd "$NOTOK_DIR"; print_setup_link "https://jarvis.example" >/dev/null; printf '%s' "$SETUP_LINK")"
expect_eq "print_setup_link clears SETUP_LINK when no token file exists" "$got" ""
printed="$(cd "$NOTOK_DIR"; print_setup_link "https://jarvis.example")"
case "$printed" in
  *setup_token*) printf 'FAIL: print_setup_link leaked a setup_token with no token file (got=%s)\n' "$printed" >&2; fail=1 ;;
  *) pass "print_setup_link prints no setup_token when the token file is absent" ;;
esac

# === access-mode / ingress helpers ===========================================
# _is_lan_ipv4 accepts the RFC1918 address other LAN devices reach this host by and
# rejects docker/CGNAT/link-local ranges. _append_server_name / _append_csv build
# the accumulating nginx Host allowlist and CORS list (LAN + origin keep BOTH
# hostnames). _public_origin_host accepts an origin-only https:// DNS URL and
# refuses IP literals or URL components beyond an optional port. The resolver
# prefers an explicit origin, then a valid origin persisted in APP_BASE_URL.
# These pure helpers live in setup.sh (before the flag parser); extract and eval
# them.
ingress_src="$(sed -n '/^_is_lan_ipv4()/,/^}/p;/^_append_server_name()/,/^}/p;/^_append_csv()/,/^}/p;/^_public_origin_host()/,/^}/p;/^_resolve_public_origin_layer()/,/^}/p' "$SETUP_SCRIPT")"
eval "$ingress_src"

_is_lan_ipv4 192.168.1.5 && rc=0 || rc=$?; expect_eq "_is_lan_ipv4 accepts 192.168.x" "$rc" "0"
_is_lan_ipv4 10.0.0.5     && rc=0 || rc=$?; expect_eq "_is_lan_ipv4 accepts 10.x" "$rc" "0"
_is_lan_ipv4 172.20.0.5   && rc=0 || rc=$?; expect_eq "_is_lan_ipv4 accepts 172.16/12" "$rc" "0"
_is_lan_ipv4 172.17.0.2   && rc=0 || rc=$?; expect_eq "_is_lan_ipv4 rejects docker bridge 172.17.x" "$rc" "1"
_is_lan_ipv4 100.100.0.1  && rc=0 || rc=$?; expect_eq "_is_lan_ipv4 rejects CGNAT 100.64/10" "$rc" "1"
_is_lan_ipv4 169.254.1.1  && rc=0 || rc=$?; expect_eq "_is_lan_ipv4 rejects link-local" "$rc" "1"
_is_lan_ipv4 8.8.8.8      && rc=0 || rc=$?; expect_eq "_is_lan_ipv4 rejects a public IP" "$rc" "1"

expect_eq "_append_server_name seeds an empty list" "$(_append_server_name '' 192.168.1.5)" "192.168.1.5"
expect_eq "_append_server_name accumulates LAN + origin (both hostnames)" \
  "$(_append_server_name "$(_append_server_name '' 192.168.1.5)" jarvis.example.ts.net)" \
  "192.168.1.5 jarvis.example.ts.net"
expect_eq "_append_server_name de-dupes" "$(_append_server_name '192.168.1.5' 192.168.1.5)" "192.168.1.5"
expect_eq "_append_server_name skips an empty name" "$(_append_server_name 'localhost' '')" "localhost"

expect_eq "_append_csv seeds an empty list" "$(_append_csv '' https://a)" "https://a"
expect_eq "_append_csv appends comma-separated" "$(_append_csv 'https://a' https://b)" "https://a,https://b"
expect_eq "_append_csv de-dupes" "$(_append_csv 'https://a,https://b' https://a)" "https://a,https://b"

expect_eq "_public_origin_host extracts a DNS hostname" \
  "$(_public_origin_host https://jarvis.example.ts.net)" "jarvis.example.ts.net"
expect_eq "_public_origin_host accepts and strips a numeric port" \
  "$(_public_origin_host https://jarvis.example.ts.net:8443)" "jarvis.example.ts.net"
expect_eq "_public_origin_host canonicalizes DNS case like browsers" \
  "$(_public_origin_host https://Jarvis.Example.TS.Net:8443)" "jarvis.example.ts.net"
for bad_origin in \
  'https://jarvis.example.ts.net/x' \
  'https://jarvis.example.ts.net?x=1' \
  'https://jarvis.example.ts.net#fragment' \
  'https://user@jarvis.example.ts.net' \
  'https://10.0.0.5' \
  'https://[::1]' \
  'https://2001:db8::1' \
  'https://0x7f000001' \
  'https://0x7f.1' \
  'https://0177.0.0.1' \
  'https://127.1' \
  'https://jarvis.123' \
  'https://jarvis.0x' \
  'https://jarvis.0X' \
  'http://jarvis.example.ts.net' \
  'https://-jarvis.example.ts.net' \
  'https://jarvis..example.ts.net' \
  'https://jarvis_example.ts.net' \
  'https://jarvis.example.ts.net:' \
  'https://jarvis.example.ts.net:not-a-port' \
  'https://jarvis.example.ts.net:0' \
  'https://jarvis.example.ts.net:65536'; do
  _public_origin_host "$bad_origin" >/dev/null && rc=0 || rc=$?
  expect_eq "_public_origin_host refuses ${bad_origin}" "$rc" "1"
done

if declare -F _resolve_public_origin_layer >/dev/null; then
  persisted_origin="$(_resolve_public_origin_layer '' https://family.example.ts.net)"
  expect_eq "persisted APP_BASE_URL becomes the named-origin layer when no replacement is supplied" \
    "$persisted_origin" "https://family.example.ts.net"
  expect_eq "explicit --public-origin wins over persisted APP_BASE_URL" \
    "$(_resolve_public_origin_layer https://new.example.ts.net https://family.example.ts.net)" \
    "https://new.example.ts.net"
  expect_eq "the resolved origin canonicalizes DNS case like browsers" \
    "$(_resolve_public_origin_layer https://Family.Example.TS.Net:8443 '')" \
    "https://family.example.ts.net:8443"
  _resolve_public_origin_layer '' 'https://family.example.ts.net/path' >/dev/null && rc=0 || rc=$?
  expect_eq "an invalid persisted APP_BASE_URL is not restored as a named-origin layer" "$rc" "1"

  persisted_host="$(_public_origin_host "$persisted_origin")"
  expect_eq "LAN CORS keeps its HTTP routes and appends the persisted named origin" \
    "$(_append_csv 'http://localhost:3001,http://10.0.0.17:3001' "$persisted_origin")" \
    "http://localhost:3001,http://10.0.0.17:3001,https://family.example.ts.net"
  expect_eq "LAN Host allowlist keeps its IP and appends the persisted named origin" \
    "$(_append_server_name '10.0.0.17' "$persisted_host")" \
    "10.0.0.17 family.example.ts.net"
  expect_eq "tunnel CORS keeps its HTTPS route and appends the persisted named origin" \
    "$(_append_csv 'https://tunnel.example.net,https://localhost:3001' "$persisted_origin")" \
    "https://tunnel.example.net,https://localhost:3001,https://family.example.ts.net"
  expect_eq "tunnel Host allowlist keeps both named origins" \
    "$(_append_server_name 'tunnel.example.net' "$persisted_host")" \
    "tunnel.example.net family.example.ts.net"
else
  printf 'FAIL: setup.sh does not define _resolve_public_origin_layer\n' >&2
  fail=1
fi

# === setup.sh access-mode / ingress wiring (static) ==========================

scheck "sub_value emits DASHBOARD_SERVER_NAME" 'DASHBOARD_SERVER_NAME\)'
scheck "the LAN arm adds the LAN IP to the Host allowlist" '_append_server_name "\$DASHBOARD_SERVER_NAME_VALUE" "\$LAN_IP"'
scheck "the tunnel arm adds the tunnel hostname to the Host allowlist" '_append_server_name "\$DASHBOARD_SERVER_NAME_VALUE" "\$TUNNEL_HOSTNAME"'
scheck "the LAN reachability probe targets http, not https" '_lan_probe_url="http://'
scheck "setup.sh parses --address" '\-\-address\)'
scheck "setup.sh parses --public-origin" '\-\-public-origin\)'
scheck "public-origin feeds APP_BASE_URL" 'APP_BASE_URL_VALUE="\$NI_PUBLIC_ORIGIN"'
scheck "public-origin feeds CORS_ORIGINS" '_append_csv "\$CORS_ORIGINS_OVERRIDE" "\$NI_PUBLIC_ORIGIN"'
scheck "public-origin feeds the Host allowlist" '_append_server_name "\$DASHBOARD_SERVER_NAME_VALUE" "\$PUBLIC_ORIGIN_HOST"'
scheck "a reconfigure resolves its named-origin layer from persisted APP_BASE_URL" '_resolve_public_origin_layer "\$NI_PUBLIC_ORIGIN" "\$_EXISTING_APP_BASE_URL"'
scheck "the LAN wizard truthfully identifies its plain-HTTP route" 'dashboard view uses plain HTTP'
if grep -Fq 'certificate warning' "$SETUP_SCRIPT"; then
  printf 'FAIL: LAN wizard still promises a certificate warning although the route is plain HTTP\n' >&2
  fail=1
else
  pass "LAN wizard does not promise a certificate warning for its plain-HTTP route"
fi
scheck "the setup link uses a loopback/verified base" 'print_setup_link "\$SETUP_LINK_BASE"'
# PROFILE_ARGS + COMPOSE_PROFILES are now registry-driven (no per-profile literal
# list in setup.sh): assert the group is engaged as an ACTIVE_PROFILE, and let the
# PROFILE_REGISTRY accessor tests below prove it persists.
scheck "letsencrypt is engaged as an active profile" 'ACTIVE_PROFILES\+=\(letsencrypt\)'
scheck "local-https engages the caddy-local profile" 'ACTIVE_PROFILES\+=\(caddy-local\)'
scheck "setup.sh drives COMPOSE_PROFILES from the registry persist set" 'registry_profiles_to_persist'
scheck "setup.sh derives the health gate from the shared registry accessor" 'mandatory_health_services'
scheck "letsencrypt waits for the cert before advertising the URL" 'Waiting for the public certificate'
# The setup token must never ride a raw-IP HTTP link: print_setup_link is fed a
# loopback/verified base, never the LAN IP.
if grep -Eq 'print_setup_link[^#]*LAN_IP' "$SETUP_SCRIPT"; then
  printf 'FAIL: print_setup_link is called with the LAN IP (the setup token must stay on loopback)\n' >&2; fail=1
else
  pass "print_setup_link is never called with the LAN IP"
fi

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

# --public-origin is validated at parse time: an IP literal is refused (never a
# valid WebAuthn origin / public-cert host); a DNS https hostname is accepted (and
# --help then exits before any docker work, so this is safe as a subprocess).
out="$(run_setup --public-origin https://10.0.0.5 --help)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*"exactly https://DNS-host[:port]"*) pass "--public-origin https://10.0.0.5 is refused (an IP is not a valid origin)" ;;
  *) printf 'FAIL: --public-origin IP literal not refused (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
out="$(run_setup --public-origin https://jarvis.example.ts.net/path --help)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*"exactly https://DNS-host[:port]"*) pass "--public-origin path is refused with the origin-only shape" ;;
  *) printf 'FAIL: --public-origin path did not report the origin-only shape (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
out="$(run_setup --public-origin https://jarvis.example.ts.net --help)" && rc=0 || rc=$?
if [ "$rc" -eq 0 ]; then
  pass "--public-origin https://jarvis.example.ts.net --help exits 0 (DNS origin accepted)"
else
  printf 'FAIL: valid --public-origin rejected (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1
fi

# === merge_env_file — non-destructive .env rebuild ===========================
# A reconfigure must carry EVERY existing value forward unchanged (no rotated
# secret, no dropped operator key), update only keys this run genuinely supplied,
# add keys new in the release, and drop retired keys. Values here deliberately
# carry a space, #, =, +, /, a double-quote, a single-quote, leading+trailing
# spaces, and a CRLF terminator — the exact bytes a naive cut/quote-capturing
# read mangles.
MERGE_DIR="$(mktemp -d "${FIXTURES}/merge.XXXXXX")"
MOLD="${MERGE_DIR}/old.env"
MTMPL="${MERGE_DIR}/template.env"
MUPS="${MERGE_DIR}/upserts.env"
MOUT="${MERGE_DIR}/merged.env"

cat > "$MOLD" <<'EOF'
# machine-generated header — must survive verbatim
SMTP_HOST=mail server.example
SMTP_USER=user#name
SMTP_PORT=587
SMTP_FROM=jarvis@verified.dev
LITELLM_SALT_KEY=a=b=c
BACKUP_ENCRYPT_KEY=Zm9v+bar/baz==
JARVIS_MODEL_HMAC_KEY=he said "hi"
INFRA_INGEST_KEY=it's fine
QDRANT_API_KEY=plainvalue
MY_CUSTOM_FLAG="a b=c"
CORS_ORIGINS=https://old.example
JARVIS_ACCESS_MODE=localhost
JARVIS_CERT_SAN=DNS:localhost,IP:127.0.0.1
EOF
printf 'LANGFUSE_SALT=  spaced  \n' >> "$MOLD"
printf 'POSTGRES_PASSWORD=crlfvalue\r\n' >> "$MOLD"

cat > "$MTMPL" <<'EOF'
SMTP_HOST=
CORS_ORIGINS=
NEW_RELEASE_KEY=default_new
EOF

# Owned this run: CORS_ORIGINS changes (and its new value carries a `=`, so the
# owned read must keep every byte after the first `=`); JARVIS_ACCESS_MODE flips.
cat > "$MUPS" <<'EOF'
CORS_ORIGINS=https://a:1?x=1,https://b
JARVIS_ACCESS_MODE=lan
EOF

merge_env_file "$MOLD" "$MTMPL" "$MUPS" "JARVIS_CERT_SAN" > "$MOUT"

mval() { grep "^$1=" "$MOUT" | head -n 1 | cut -d= -f2- | tr -d '\r'; }

# Carried-forward values survive byte-for-byte.
expect_eq "merge preserves a value with a space"        "$(mval SMTP_HOST)"             "mail server.example"
expect_eq "merge preserves a value with #"              "$(mval SMTP_USER)"             "user#name"
expect_eq "merge preserves a value with ="              "$(mval LITELLM_SALT_KEY)"      "a=b=c"
expect_eq "merge preserves a value with + and /"        "$(mval BACKUP_ENCRYPT_KEY)"    "Zm9v+bar/baz=="
expect_eq "merge preserves a value with a double-quote" "$(mval JARVIS_MODEL_HMAC_KEY)" 'he said "hi"'
expect_eq "merge preserves a value with a single-quote" "$(mval INFRA_INGEST_KEY)"      "it's fine"
expect_eq "merge preserves leading+trailing spaces"     "$(mval LANGFUSE_SALT)"         "  spaced  "
expect_eq "merge preserves a CRLF-terminated value"     "$(mval POSTGRES_PASSWORD)"     "crlfvalue"
expect_eq "merge preserves an untouched neighbour"      "$(mval QDRANT_API_KEY)"        "plainvalue"

# An unknown operator-added key survives byte-identical (whole line).
expect_eq "merge keeps an unknown operator key verbatim" \
  "$(grep '^MY_CUSTOM_FLAG=' "$MOUT")" 'MY_CUSTOM_FLAG="a b=c"'

# SMTP_PORT / SMTP_FROM survive (dropped by the old carry-forward-list rebuild).
expect_eq "merge keeps SMTP_PORT" "$(mval SMTP_PORT)" "587"
expect_eq "merge keeps SMTP_FROM" "$(mval SMTP_FROM)" "jarvis@verified.dev"

# An owned key updates — its separator-laden `=` value round-trips intact.
expect_eq "merge applies an owned update with an = in the value" \
  "$(mval CORS_ORIGINS)" "https://a:1?x=1,https://b"
expect_eq "merge applies an owned update (access mode)" "$(mval JARVIS_ACCESS_MODE)" "lan"

# A key new in the release (absent from the old .env) is appended.
expect_eq "merge appends a template key new in this release" \
  "$(mval NEW_RELEASE_KEY)" "default_new"

# A retired key not owned this run is dropped.
expect_eq "merge drops a retired, un-owned key" \
  "$(grep -c '^JARVIS_CERT_SAN=' "$MOUT")" "0"

# A retired key this run STILL owns wins (re-emitted) — the owner-over-retire
# rule that lets a writer keep a key alive until the release that removes it.
printf 'JARVIS_CERT_SAN=DNS:new\n' > "${MERGE_DIR}/ups2.env"
merge_env_file "$MOLD" "$MTMPL" "${MERGE_DIR}/ups2.env" "JARVIS_CERT_SAN" > "${MERGE_DIR}/out2.env"
expect_eq "merge re-emits a retired key the run still owns" \
  "$(grep '^JARVIS_CERT_SAN=' "${MERGE_DIR}/out2.env" | cut -d= -f2-)" "DNS:new"

# No-op merge (nothing owned, nothing retired, template == old) is byte-identical
# — the guarantee the keep path relies on.
merge_env_file "$MOLD" "$MOLD" /dev/null "" > "${MERGE_DIR}/noop.env"
if diff -q "$MOLD" "${MERGE_DIR}/noop.env" >/dev/null; then
  pass "no-op merge leaves the file byte-identical"
else
  printf 'FAIL: no-op merge changed the file\n' >&2
  diff "$MOLD" "${MERGE_DIR}/noop.env" >&2 || true
  fail=1
fi

# Every entry in the canonical walk-list survives a merge — the keys whose loss
# would brick a live deployment (LiteLLM/backup decryption, model HMAC, Langfuse,
# magic-link email). Guards the list against silent shrinkage too.
MOLD3="${MERGE_DIR}/managed.env"
: > "$MOLD3"
for _mk in "${JARVIS_MANAGED_SECRET_KEYS[@]}"; do
  printf '%s=sentinel-%s\n' "$_mk" "$_mk" >> "$MOLD3"
done
merge_env_file "$MOLD3" /dev/null /dev/null "" > "${MERGE_DIR}/managed.out"
for _mk in "${JARVIS_MANAGED_SECRET_KEYS[@]}"; do
  _mv="$(grep "^${_mk}=" "${MERGE_DIR}/managed.out" | head -n 1 | cut -d= -f2- | tr -d '\r')"
  expect_eq "merge preserves managed key ${_mk}" "$_mv" "sentinel-${_mk}"
done

# === PROFILE_REGISTRY accessors ==============================================
# The registry is the single source of truth for the optional service groups.
# registry_profiles_to_persist drives COMPOSE_PROFILES; mandatory_health_services
# drives both entry points' health gate; route_claims is the fixed ingress-route
# spec a docs-parity test consumes.

persist="$(registry_profiles_to_persist)"
for _p in tunnel telegram caddy-local letsencrypt; do
  if _env_key_in_list "$_p" "$persist"; then
    pass "registry_profiles_to_persist includes ${_p}"
  else
    printf 'FAIL: registry_profiles_to_persist missing %s (got=%s)\n' "$_p" "$persist" >&2; fail=1
  fi
done
# observability / vllm / perf are deliberately opt-in-per-run, never persisted.
for _p in observability vllm perf; do
  if _env_key_in_list "$_p" "$persist"; then
    printf 'FAIL: registry_profiles_to_persist should NOT persist %s (got=%s)\n' "$_p" "$persist" >&2; fail=1
  else
    pass "registry_profiles_to_persist excludes ${_p} (not persisted)"
  fi
done

# The base is returned unchanged when no group is active (the wrapper's case).
expect_eq "mandatory_health_services returns the base with no active profiles" \
  "$(mandatory_health_services "$MANDATORY_HEALTH_BASE")" "$MANDATORY_HEALTH_BASE"
# restore-uploader is in the shared base (the drift the wrapper had, now killed).
if _env_key_in_list restore-uploader "$MANDATORY_HEALTH_BASE"; then
  pass "MANDATORY_HEALTH_BASE includes restore-uploader"
else
  printf 'FAIL: MANDATORY_HEALTH_BASE missing restore-uploader (got=%s)\n' "$MANDATORY_HEALTH_BASE" >&2; fail=1
fi
# An active group adds its own service to the gate (deliberately started ->
# health-checked), de-duplicated and appended after the base.
expect_eq "mandatory_health_services adds telegram_bot when telegram is active" \
  "$(mandatory_health_services "$MANDATORY_HEALTH_BASE" telegram)" \
  "${MANDATORY_HEALTH_BASE} telegram_bot"
expect_eq "mandatory_health_services adds langfuse when observability is active" \
  "$(mandatory_health_services "postgres" observability)" "postgres langfuse"
# TLS-edge groups add nothing to the health gate (their own cert probes cover them).
expect_eq "mandatory_health_services adds nothing for a TLS-edge group" \
  "$(mandatory_health_services "postgres" letsencrypt caddy-local tunnel)" "postgres"

# route_claims: the six fixed ingress routes, each a 9-column pipe row.
routes="$(route_claims)"
expect_eq "route_claims emits six ingress routes" "$(printf '%s\n' "$routes" | grep -c .)" "6"
for _r in localhost-http raw-ip-lan named-private-https local-https letsencrypt tunnel; do
  if printf '%s\n' "$routes" | grep -q "^${_r}|"; then
    pass "route_claims declares the ${_r} route"
  else
    printf 'FAIL: route_claims missing the %s route\n' "$_r" >&2; fail=1
  fi
done
# Every route row has exactly 9 columns and every token is bash/env-representable
# (no spaces), and the token never rides a query string.
while IFS= read -r _row; do
  [ -n "$_row" ] || continue
  _ncol="$(awk -F'|' '{print NF}' <<< "$_row")"
  expect_eq "route row '${_row%%|*}' has 9 columns" "$_ncol" "9"
  case "$_row" in
    *" "*) printf 'FAIL: route row has a space (not env-representable): %s\n' "$_row" >&2; fail=1 ;;
    *"?setup_token"*) printf 'FAIL: route row advertises a query-string token: %s\n' "$_row" >&2; fail=1 ;;
    *) pass "route row '${_row%%|*}' is env-representable" ;;
  esac
done <<< "$routes"

# === registry_profile_host_ports ============================================
# The extra host ports an active TLS edge / optional profile publishes, so the
# port pre-check covers them (a static default list would miss 80/443/3443).
expect_eq "letsencrypt publishes 80 and 443"       "$(registry_profile_host_ports letsencrypt)" "80 443"
expect_eq "caddy-local publishes 3443"             "$(registry_profile_host_ports caddy-local)" "3443"
expect_eq "observability publishes langfuse 3002"  "$(registry_profile_host_ports observability)" "3002"
expect_eq "vllm publishes 8080"                    "$(registry_profile_host_ports vllm)" "8080"
expect_eq "tunnel/telegram publish no host port"   "$(registry_profile_host_ports tunnel telegram)" ""
expect_eq "no profiles -> no extra ports"          "$(registry_profile_host_ports)" ""
expect_eq "duplicate host ports are de-duped"      "$(registry_profile_host_ports letsencrypt caddy-local)" "80 443 3443"

# === compose_meets_floor =====================================================
# The overlays merge a dev override's `deploy: !reset null`; the !reset/!override
# tags need Docker Compose 2.24.4+, so the gate must reject older v2 and accept
# newer. rc: 0 meets floor, 1 below, 2 unreadable.
compose_meets_floor 2.24.4 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: exact match passes" "$rc" "0"
compose_meets_floor v2.29.7 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: newer passes (v-prefix tolerated)" "$rc" "0"
compose_meets_floor 2.24.3 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: 2.24.3 is below the floor" "$rc" "1"
compose_meets_floor 2.20.0 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: older minor is below the floor" "$rc" "1"
compose_meets_floor 1.29.2 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: v1 is below the floor" "$rc" "1"
compose_meets_floor unknown 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: unreadable version -> rc 2" "$rc" "2"

# === _wsl_without_systemd (WSL prereq guidance) ==============================
# A WSL host (Microsoft kernel) without systemd must NOT get a docker-ce plan
# (it would start a second daemon that systemctl cannot even enable); the manual
# guidance points at Docker Desktop's WSL integration instead. Probes are
# env-overridable (JARVIS_PROC_VERSION / JARVIS_SYSTEMD_DIR).
WSL_PROC="${FIXTURES}/proc-version-wsl"
printf 'Linux version 5.15.0-microsoft-standard-WSL2\n' > "$WSL_PROC"
NO_SYSTEMD="${FIXTURES}/no-systemd-dir"   # deliberately absent
SYSTEMD_DIR="${FIXTURES}/systemd-dir"; mkdir -p "$SYSTEMD_DIR"

( export JARVIS_PROC_VERSION="$WSL_PROC" JARVIS_SYSTEMD_DIR="$NO_SYSTEMD"; _wsl_without_systemd ) && rc=0 || rc=$?
expect_eq "_wsl_without_systemd: WSL + no systemd -> 0" "$rc" "0"
( export JARVIS_PROC_VERSION="$WSL_PROC" JARVIS_SYSTEMD_DIR="$SYSTEMD_DIR"; _wsl_without_systemd ) && rc=0 || rc=$?
expect_eq "_wsl_without_systemd: WSL WITH systemd -> 1 (docker-ce plan is fine)" "$rc" "1"
( export JARVIS_PROC_VERSION="$JARVIS_PROC_VERSION" JARVIS_SYSTEMD_DIR="$NO_SYSTEMD"; _wsl_without_systemd ) && rc=0 || rc=$?
expect_eq "_wsl_without_systemd: native Linux -> 1" "$rc" "1"

( export JARVIS_PROC_VERSION="$WSL_PROC" JARVIS_SYSTEMD_DIR="$NO_SYSTEMD"
  prereq_install_plan Linux ubuntu 1 0 0 docker >/dev/null 2>&1 ) && rc=0 || rc=$?
expect_eq "WSL-without-systemd: docker plan is refused (rc 1)" "$rc" "1"
wsl_guidance="$(export JARVIS_PROC_VERSION="$WSL_PROC" JARVIS_SYSTEMD_DIR="$NO_SYSTEMD"; prereq_manual_guidance docker)"
plan_has   "WSL manual guidance points at Docker Desktop WSL integration" "$wsl_guidance" 'Docker Desktop.*WSL integration'
plan_lacks "WSL manual guidance does not push docker-ce"                  "$wsl_guidance" 'docker-ce'
( prereq_install_plan Linux ubuntu 1 0 0 docker >/dev/null 2>&1 ) && rc=0 || rc=$?
expect_eq "non-WSL host: docker plan is produced (rc 0)" "$rc" "0"

# === python3 as a first-class prerequisite ===================================
# python3 backs model selection + disk sizing (setup_lib helpers shell out to it
# under set -euo pipefail), so it is a hard install-path prerequisite.
mp_src="$(sed -n '/^missing_prereqs()/,/^}/p' "$SETUP_SCRIPT")"
MP_BIN="$(mktemp -d "${FIXTURES}/mp.XXXXXX")"
printf '#!/usr/bin/env bash\ncase "$1" in compose) exit 0 ;; esac\nexit 0\n' > "${MP_BIN}/docker"
printf '#!/usr/bin/env bash\nexit 0\n' > "${MP_BIN}/openssl"
chmod +x "${MP_BIN}/docker" "${MP_BIN}/openssl"
got="$( eval "$mp_src"; ( export PATH="$MP_BIN"; missing_prereqs ) )"
case "$got" in
  *python3*) pass "missing_prereqs reports python3 when it is absent" ;;
  *) printf 'FAIL: missing_prereqs did not report python3 (got=%s)\n' "$got" >&2; fail=1 ;;
esac
printf '#!/usr/bin/env bash\nexit 0\n' > "${MP_BIN}/python3"; chmod +x "${MP_BIN}/python3"
got="$( eval "$mp_src"; ( export PATH="$MP_BIN"; missing_prereqs ) )"
case "$got" in
  *python3*) printf 'FAIL: missing_prereqs reported python3 though present (got=%s)\n' "$got" >&2; fail=1 ;;
  *) pass "missing_prereqs does not report python3 when present" ;;
esac
py_plan="$(prereq_install_plan Linux ubuntu 1 0 0 python3)" || py_plan=""
plan_has "python3: apt installs python3" "$py_plan" '^sudo apt-get install -y python3$'
py_plan_fedora="$(prereq_install_plan Linux fedora 0 0 1 python3)" || py_plan_fedora=""
plan_has "python3: dnf installs python3" "$py_plan_fedora" '^sudo dnf install -y python3$'
plan_has "python3: manual guidance names Python 3" "$(prereq_manual_guidance python3)" 'Python 3'

# === readiness_verdict =======================================================
# The readiness exit-code -> wrapper-action map (contract: 0 clean, 2 warn, 1
# HIGH). Pairing the exit-code flip with this consumer is what keeps a routine
# exit-2 warning from aborting a production install.
expect_eq "rc 0 -> all-clear"                        "$(readiness_verdict 0 production)" "ok"
expect_eq "rc 2 -> warn (never fatal) in production" "$(readiness_verdict 2 production)" "warn"
expect_eq "rc 2 -> warn in development"              "$(readiness_verdict 2 development)" "warn"
expect_eq "rc 1 + production -> abort (fatal)"       "$(readiness_verdict 1 production)" "abort"
expect_eq "rc 1 + development -> warn (dev tolerance)" "$(readiness_verdict 1 development)" "warn"
expect_eq "unknown nonzero -> warn (surface, never silently abort)" "$(readiness_verdict 3 production)" "warn"

# === setup.sh preflight/output wiring (static) ===============================
scheck "missing_prereqs treats python3 as a prerequisite" 'missing\+=\(python3\)'
scheck "ensure_prerequisites verifies python3"            'python3 required for model selection'
scheck "run_doctor --check fails when python3 is absent"  'python3 missing — required for model selection'
scheck "the compose gate pins a real 2.24.4 floor"        '^COMPOSE_MIN=2\.24\.4'
scheck "the compose gate uses compose_meets_floor"        'compose_meets_floor "\$COMPOSE_VER" "\$COMPOSE_MIN"'
scheck "the nvidia-toolkit probe is a first-class preflight" '^preflight_nvidia_toolkit$'
scheck "the port pre-check reads .env port values"        '_port_or_default DASHBOARD_HOST_PORT'
scheck "the port pre-check adds active-profile ports"     'registry_profile_host_ports'
scheck "the readiness wrapper consumes readiness_verdict" 'readiness_verdict "\$_rc" "\$_ENV_VALUE"'
scheck "the readiness wrapper is non-fatal on warnings (exit 2)" 'passed with warnings'
scheck "multi-user next-steps lead with the first-admin setup link" 'Bootstrap the first admin: open the "Finish setup" link'

# nvidia-toolkit is hoisted OUT of the missing-prereqs add (now a first-class,
# non-fatal preflight), so the nested `missing+=(nvidia-toolkit)` must be gone.
if grep -Eq 'missing\+=\(nvidia-toolkit\)' "$SETUP_SCRIPT"; then
  printf 'FAIL: nvidia-toolkit is still nested in the missing-prereqs add\n' >&2; fail=1
else
  pass "nvidia-toolkit is hoisted out of the missing-prereqs add"
fi
# preflight_nvidia_toolkit is non-fatal by contract: its body never dies.
pnt_src="$(sed -n '/^preflight_nvidia_toolkit()/,/^}/p' "$SETUP_SCRIPT")"
if printf '%s\n' "$pnt_src" | grep -qE '\bdie \b|\bdie "'; then
  printf 'FAIL: preflight_nvidia_toolkit contains a die (must be non-fatal)\n' >&2; fail=1
else
  pass "preflight_nvidia_toolkit is non-fatal (no die on a missing GPU runtime)"
fi

# The green "Setup complete." banner must be printed AFTER the readiness gate.
_banner_ln="$(grep -nF 'Setup complete.' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_gate_ln="$(sline 'bash "\$_READINESS_SCRIPT"')"
if [ -n "$_banner_ln" ] && [ -n "$_gate_ln" ] && [ "$_gate_ln" -lt "$_banner_ln" ]; then
  pass "the green 'Setup complete.' banner is printed AFTER the readiness gate"
else
  printf 'FAIL: Setup complete banner (%s) not after the readiness gate (%s)\n' "$_banner_ln" "$_gate_ln" >&2; fail=1
fi

# The dependency-reversed lead step (a magic link before any admin exists) is gone.
if grep -Eq 'Request a magic link at the sign-in page' "$SETUP_SCRIPT"; then
  printf 'FAIL: next-steps still leads with a magic-link step that presupposes SMTP/an account\n' >&2; fail=1
else
  pass "next-steps no longer leads with a magic-link step before first-admin bootstrap"
fi
# ...and first-admin bootstrap is ordered before SMTP configuration.
_boot_ln="$(sline 'Bootstrap the first admin')"
_smtp_ln="$(grep -nE 'Configure SMTP in Settings' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$_boot_ln" ] && [ -n "$_smtp_ln" ] && [ "$_boot_ln" -lt "$_smtp_ln" ]; then
  pass "next-steps orders first-admin bootstrap before SMTP configuration"
else
  printf 'FAIL: next-steps SMTP step (%s) not after bootstrap (%s)\n' "$_smtp_ln" "$_boot_ln" >&2; fail=1
fi

# =============================================================================

if [ "$fail" -ne 0 ]; then
  printf '\nsetup_lib helpers: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nsetup_lib helpers: all %s checks passed\n' "$pass_n"
