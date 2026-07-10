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
  SKIP="$1" REQ_GB="$2" REQ_RC="$3" LIB_OUT="$4" LIB_RC="$5" IMAGES_OUT="$6" bash -c '
    set -euo pipefail
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

# =============================================================================

if [ "$fail" -ne 0 ]; then
  printf '\nsetup_lib helpers: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nsetup_lib helpers: all %s checks passed\n' "$pass_n"
