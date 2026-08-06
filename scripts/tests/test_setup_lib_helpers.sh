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
JARVIS_SETUP_SCRIPT="${SCRIPT_DIR}/../jarvis-setup.sh"
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

# === application version resolution =========================================

# These synthetic values exercise the accepted grammar independently of the
# repository's current release version.
for valid_version in 0.0.1 7.8.9-alpha.1 10.20.30-preview-7; do
  app_version_is_valid "$valid_version" && rc=0 || rc=$?
  expect_eq "app version accepts ${valid_version}" "$rc" "0"
done
for invalid_version in "" v0.0.1 7.8 7.8.9+local "7.8.9 bad"; do
  app_version_is_valid "$invalid_version" && rc=0 || rc=$?
  expect_eq "app version rejects '${invalid_version}'" "$rc" "1"
done

commit_tag="0123456789abcdef0123456789abcdef01234567"
for valid_tag in 0.0.1 7.8.9-alpha.1 "$commit_tag"; do
  image_tag_is_valid "$valid_tag" && rc=0 || rc=$?
  expect_eq "image tag accepts ${valid_tag}" "$rc" "0"
done
for invalid_tag in "" v0.0.1 \
  0123456789ABCDEF0123456789ABCDEF01234567 \
  "${commit_tag%?}" "7.8.9 bad"; do
  image_tag_is_valid "$invalid_tag" && rc=0 || rc=$?
  expect_eq "image tag rejects '${invalid_tag}'" "$rc" "1"
done
app_version_is_valid "$commit_tag" && rc=0 || rc=$?
expect_eq "semantic application version rejects a commit image tag" "$rc" "1"

VERSION_DIR="$(mktemp -d "${FIXTURES}/version.XXXXXX")"
cat > "${VERSION_DIR}/pyproject.toml" <<'PYPROJECT'
[project]
name = "jarvis-rd-assistant"
version = "2.3.4"
PYPROJECT
got="$(cd "$VERSION_DIR"; resolve_checkout_app_version)" && rc=0 || rc=$?
expect_eq "checkout version falls back to [project].version" "${got}/${rc}" "2.3.4/0"

VERSION_GIT_BIN="$(mktemp -d "${FIXTURES}/version-git.XXXXXX")"
cat > "${VERSION_GIT_BIN}/git" <<'GIT'
#!/usr/bin/env bash
if [ "$*" = "describe --tags --exact-match HEAD" ]; then
  printf 'v2.3.4-preview.2\n'
  exit 0
fi
exit 1
GIT
chmod +x "${VERSION_GIT_BIN}/git"
got="$(cd "$VERSION_DIR"; PATH="${VERSION_GIT_BIN}:${PATH}" resolve_checkout_app_version)" \
  && rc=0 || rc=$?
expect_eq "an exact release tag overrides [project].version" \
  "${got}/${rc}" "2.3.4-preview.2/0"

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
scheck "setup.sh parses an explicit Compose project" '--compose-project-name\)'
scheck "setup.sh parses an explicit application image tag" '--image-tag\)'
scheck "fresh lifecycle admission receives the explicit Compose project" \
  'claim_lifecycle_operation "\$SCRIPT_DIR" setup "\$NI_COMPOSE_PROJECT_NAME"'
scheck "setup resolves the checkout application version once" \
  'CHECKOUT_APP_VERSION="\$\(resolve_checkout_app_version\)"'
scheck "fresh setup persists the checkout application version" \
  'upsert_env_var JARVIS_VERSION "\$CHECKOUT_APP_VERSION"'
scheck "fresh setup persists the selected application image tag" \
  'upsert_env_var JARVIS_IMAGE_TAG "\$SELECTED_IMAGE_TAG"'

if grep -Fq 'upsert_env_var JARVIS_VERSION "$_installed_app_version"' \
    "$JARVIS_SETUP_SCRIPT" \
   && grep -Fq 'export JARVIS_VERSION="$_installed_app_version"' \
    "$JARVIS_SETUP_SCRIPT" \
   && grep -Fq 'upsert_env_var JARVIS_IMAGE_TAG "$_installed_image_tag"' \
    "$JARVIS_SETUP_SCRIPT" \
   && grep -Fq 'export JARVIS_IMAGE_TAG="$_installed_image_tag"' \
    "$JARVIS_SETUP_SCRIPT"; then
  pass "the compatibility bootstrap persists separate application and image identities"
else
  printf 'FAIL: compatibility bootstrap does not persist separate application and image identities\n' >&2
  fail=1
fi

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

# Absence of .env does not prove that no previous volume or lifecycle holder
# exists. Both setup entry points must claim the shared volume before creating
# fresh config or secret state.
lease_line="$(awk -v after="$call_line" 'NR > after && /^claim_setup_volume_lease$/ { print NR; exit }' "$SETUP_SCRIPT")"
env_write_line="$(grep -nE '^mv "\$TMP_ENV" \.env$' "$SETUP_SCRIPT" | head -1 | cut -d: -f1)"
if [ -n "$lease_line" ] && [ -n "$env_write_line" ] \
   && [ "$call_line" -lt "$lease_line" ] && [ "$lease_line" -lt "$env_write_line" ]; then
  pass "setup.sh claims the shared lifecycle volume after disk preflight and before config mutation"
else
  printf 'FAIL: setup.sh fresh lifecycle claim (%s) is not between disk preflight (%s) and .env mutation (%s)\n' \
    "$lease_line" "$call_line" "$env_write_line" >&2
  fail=1
fi

jarvis_lease_line="$(grep -nE '^claim_setup_volume_lease$' "$JARVIS_SETUP_SCRIPT" | head -1 | cut -d: -f1 || true)"
jarvis_env_copy_line="$(grep -nF '  cp .env.example .env' "$JARVIS_SETUP_SCRIPT" | head -1 | cut -d: -f1)"
if [ -n "$jarvis_lease_line" ] && [ -n "$jarvis_env_copy_line" ] \
   && [ "$jarvis_lease_line" -lt "$jarvis_env_copy_line" ]; then
  pass "jarvis-setup claims the shared lifecycle volume before fresh config mutation"
else
  printf 'FAIL: jarvis-setup lifecycle claim (%s) does not precede .env creation (%s)\n' \
    "$jarvis_lease_line" "$jarvis_env_copy_line" >&2
  fail=1
fi

# run_doctor's --check disk advisory stays warn-only (never a die).
doctor_start="$(sline '^run_doctor\(\)')"
doctor_end="$(awk "NR>${doctor_start} && /^}/{print NR; exit}" "$SETUP_SCRIPT")"
if [ -n "$doctor_start" ] && [ -n "$doctor_end" ] \
   && ! sed -n "${doctor_start},${doctor_end}p" "$SETUP_SCRIPT" \
        | grep -qE 'die "|^[[:space:]]*preflight_disk$'; then
  pass "run_doctor keeps the disk check advisory (no die / no fatal preflight)"
else
  printf 'FAIL: run_doctor (%s-%s) gained a fatal disk path\n' "$doctor_start" "$doctor_end" >&2
  fail=1
fi

# The project option is validated before Docker work, matches an existing
# checkout identity exactly, and persists through the production env writer.
project_validate_src="$(sed -n '/^_validate_compose_project_request()/,/^}/p' "$SETUP_SCRIPT")"
project_persist_src="$(sed -n '/^_persist_compose_project_request()/,/^}/p' "$SETUP_SCRIPT")"
existing_env_src="$(sed -n '/^existing_env_value()/,/^}/p' "$SETUP_SCRIPT")"
eval "$project_validate_src"

PROJECT_FRESH="${FIXTURES}/project-fresh"
mkdir -p "$PROJECT_FRESH"
_validate_compose_project_request "$PROJECT_FRESH" smoke-project \
  && rc=0 || rc=$?
expect_eq "a valid explicit project is accepted for a fresh install" "$rc" "0"
_validate_compose_project_request "$PROJECT_FRESH" 'Invalid/Project' \
  >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "an invalid explicit project is rejected before admission" "$rc" "2"

PROJECT_MATCH="${FIXTURES}/project-match"
mkdir -p "$PROJECT_MATCH"
printf 'EXISTING=value\n' > "$PROJECT_MATCH/.env"
_validate_compose_project_request "$PROJECT_MATCH" project-match \
  && rc=0 || rc=$?
expect_eq "an old env without a project accepts its directory-derived identity" "$rc" "0"
_validate_compose_project_request "$PROJECT_MATCH" different-project \
  >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "an existing install refuses a different explicit project" "$rc" "3"

got="$(
  cd "$PROJECT_FRESH"
  printf 'EXISTING=value\n' > .env
  eval "$existing_env_src"
  eval "$project_persist_src"
  _persist_compose_project_request smoke-project
  grep -c '^COMPOSE_PROJECT_NAME=smoke-project$' .env
)"
expect_eq "the explicit project is persisted exactly once" "$got" "1"

PROJECT_PARSE_LOG="${FIXTURES}/project-parse-docker.log"
: > "$PROJECT_PARSE_LOG"
PROJECT_PARSE_BIN="$(fake_docker '
printf "%s\n" "$*" >> "$STUB_PROJECT_PARSE_LOG"
exit 1
')"
out="$(PATH="$PROJECT_PARSE_BIN:$PATH" STUB_PROJECT_PARSE_LOG="$PROJECT_PARSE_LOG" \
  bash "$SETUP_SCRIPT" --non-interactive \
    --compose-project-name 'Invalid/Project' 2>&1)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*Invalid?--compose-project-name*) pass "invalid project fails through the real setup parser" ;;
  *) printf 'FAIL: real setup parser accepted an invalid project (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
if [ ! -s "$PROJECT_PARSE_LOG" ]; then
  pass "invalid project fails before any Docker probe"
else
  printf 'FAIL: invalid project reached Docker before rejection\n' >&2; fail=1
fi

: > "$PROJECT_PARSE_LOG"
out="$(PATH="$PROJECT_PARSE_BIN:$PATH" STUB_PROJECT_PARSE_LOG="$PROJECT_PARSE_LOG" \
  bash "$SETUP_SCRIPT" --non-interactive --compose-project-name 2>&1)" \
  && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*requires?a?value*) pass "missing spaced project value gets the central parser error" ;;
  *) printf 'FAIL: missing project value did not get an actionable error (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
if [ ! -s "$PROJECT_PARSE_LOG" ]; then
  pass "missing project value fails before any Docker probe"
else
  printf 'FAIL: missing project value reached Docker before rejection\n' >&2; fail=1
fi

# --check executes the production calculator and Docker-data-root resolver but
# never creates config or starts a service.
DOCTOR_DATA_ROOT="${FIXTURES}/doctor-data-root"
DOCTOR_DOCKER_LOG="${FIXTURES}/doctor-docker.log"
mkdir -p "$DOCTOR_DATA_ROOT"
: > "$DOCTOR_DOCKER_LOG"
DOCTOR_BIN="$(fake_docker '
printf "%s\n" "$*" >> "$STUB_DOCTOR_LOG"
case "$*" in
  "compose version") exit 0 ;;
  "compose version --short") printf "2.24.4\n"; exit 0 ;;
  "info") exit 0 ;;
  "info --format {{json .Runtimes}}") printf "{}\n"; exit 0 ;;
  "info --format {{.DockerRootDir}}") printf "%s\n" "$STUB_DOCTOR_ROOT"; exit 0 ;;
esac
exit 1
')"
if [ -f "${SCRIPT_DIR}/../../.env" ]; then
  doctor_env_before="$(cksum < "${SCRIPT_DIR}/../../.env")"
else
  doctor_env_before=absent
fi
doctor_expected_gb="$(compute_required_disk_gb qwen3:4b cpu-pull)"
out="$(PATH="$DOCTOR_BIN:$PATH" STUB_DOCTOR_LOG="$DOCTOR_DOCKER_LOG" \
  STUB_DOCTOR_ROOT="$DOCTOR_DATA_ROOT" \
  bash "$SETUP_SCRIPT" --check --smart-model qwen3:4b --gpu cpu 2>&1)" \
  && rc=0 || rc=$?
expect_eq "setup.sh --check succeeds with its required tools available" "$rc" "0"
case "$out" in
  *"Install disk requirement: ~${doctor_expected_gb} GB (cpu-pull, model qwen3:4b)."*)
    pass "setup.sh --check reports the production calculator result" ;;
  *) printf 'FAIL: setup.sh --check omitted the exact calculator result\n' >&2; fail=1 ;;
esac
if [ -f "${SCRIPT_DIR}/../../.env" ]; then
  doctor_env_after="$(cksum < "${SCRIPT_DIR}/../../.env")"
else
  doctor_env_after=absent
fi
expect_eq "setup.sh --check leaves .env byte-identical or absent" \
  "$doctor_env_after" "$doctor_env_before"
if grep -Eq 'compose .* (up|pull|build|run)( |$)' "$DOCTOR_DOCKER_LOG"; then
  printf 'FAIL: setup.sh --check attempted a Compose mutation\n' >&2; fail=1
else
  pass "setup.sh --check does not start, pull, build, or run services"
fi

# === preflight_disk policy (behavioral, extracted from setup.sh) ============
# The wrapper's fatal/warn policy is the regression surface: a shortfall is
# fatal ONLY on a first install; cached app images, an unmeasurable df, or a
# catalog-fallback estimate with >=20 GB free must all soften to a warning.

variant_src="$(sed -n '/^setup_disk_variant()/,/^}/p' "$SETUP_SCRIPT")"
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
    '"$variant_src"'
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

# curl is used by setup itself and by repository-install plans. A cold host must
# install it before the first generated command that invokes it. Selecting local
# HTTPS also brings the browser trust helper alongside mkcert.
local_https_apt_plan="$(prereq_install_plan Linux ubuntu 1 0 0 curl mkcert)" || local_https_apt_plan=""
plan_has "local HTTPS apt plan installs curl, mkcert, and browser trust tooling" \
  "$local_https_apt_plan" '^sudo apt-get install -y curl mkcert libnss3-tools$'
local_https_dnf_plan="$(prereq_install_plan Linux fedora 0 0 1 curl mkcert)" || local_https_dnf_plan=""
plan_has "local HTTPS Fedora plan installs curl, mkcert, and browser trust tooling" \
  "$local_https_dnf_plan" '^sudo dnf install -y curl mkcert nss-tools$'
local_https_brew_plan="$(prereq_install_plan Darwin macos 0 1 0 curl mkcert)" || local_https_brew_plan=""
plan_has "local HTTPS Homebrew plan installs curl, mkcert, and Firefox trust tooling" \
  "$local_https_brew_plan" '^brew install curl mkcert nss$'
compose_brew_plan="$(prereq_install_plan Darwin macos 0 1 0 docker-compose)" || compose_brew_plan=""
plan_has "Homebrew upgrades Docker Desktop when its Compose plugin is too old" \
  "$compose_brew_plan" '^brew upgrade --cask docker$'

curl_docker_dnf_plan="$(prereq_install_plan Linux fedora 0 0 1 docker curl)" || curl_docker_dnf_plan=""
curl_install_ln="$(printf '%s\n' "$curl_docker_dnf_plan" | grep -n '^sudo dnf install -y ca-certificates curl$' | head -1 | cut -d: -f1 || true)"
first_curl_ln="$(printf '%s\n' "$curl_docker_dnf_plan" | grep -n '^sudo curl ' | head -1 | cut -d: -f1 || true)"
if [ -n "$curl_install_ln" ] && [ -n "$first_curl_ln" ] \
   && [ "$curl_install_ln" -lt "$first_curl_ln" ]; then
  pass "Fedora installs curl before a prerequisite-plan command invokes it"
else
  printf 'FAIL: Fedora curl install (%s) does not precede first curl use (%s)\n' \
    "$curl_install_ln" "$first_curl_ln" >&2
  fail=1
fi

# Tailscale is the private-HTTPS route. Its guided installer must use the
# signed stable repositories and show explicit package-manager commands.
tailscale_ubuntu_plan="$(tailscale_install_plan Linux ubuntu noble 1 0 1)" || tailscale_ubuntu_plan=""
plan_has "tailscale ubuntu: installs the stable signing key" "$tailscale_ubuntu_plan" \
  '^sudo curl -fsSL https://pkgs\.tailscale\.com/stable/ubuntu/noble\.noarmor\.gpg -o /usr/share/keyrings/tailscale-archive-keyring\.gpg$'
plan_has "tailscale ubuntu: installs its fetch prerequisites first" "$tailscale_ubuntu_plan" \
  '^sudo apt-get install -y ca-certificates curl$'
plan_has "tailscale ubuntu: installs the signed repository list" "$tailscale_ubuntu_plan" \
  '^sudo curl -fsSL https://pkgs\.tailscale\.com/stable/ubuntu/noble\.tailscale-keyring\.list -o /etc/apt/sources\.list\.d/tailscale\.list$'
plan_has "tailscale ubuntu: installs the package" "$tailscale_ubuntu_plan" \
  '^sudo apt-get install -y tailscale$'
plan_has "tailscale ubuntu: enables the daemon" "$tailscale_ubuntu_plan" \
  '^sudo systemctl enable --now tailscaled$'
plan_lacks "tailscale ubuntu: no remote content piped into a shell" "$tailscale_ubuntu_plan" \
  '\| *(sh|bash)([[:space:]]|$)'
plan_lacks "tailscale ubuntu: no predictable /tmp handoff" "$tailscale_ubuntu_plan" '/tmp/'
plan_lacks "tailscale ubuntu: every sudo is line-leading" "$tailscale_ubuntu_plan" '.sudo '

tailscale_mint_plan="$(tailscale_install_plan Linux linuxmint noble 1 0 1)" || tailscale_mint_plan=""
plan_has "tailscale mint: uses the matching Ubuntu repository" "$tailscale_mint_plan" \
  'pkgs\.tailscale\.com/stable/ubuntu/noble\.noarmor\.gpg'

tailscale_debian_plan="$(tailscale_install_plan Linux debian trixie 1 0 1)" || tailscale_debian_plan=""
plan_has "tailscale debian: uses the matching Debian repository" "$tailscale_debian_plan" \
  'pkgs\.tailscale\.com/stable/debian/trixie\.noarmor\.gpg'

tailscale_fedora_plan="$(tailscale_install_plan Linux fedora unused 0 1 1)" || tailscale_fedora_plan=""
plan_has "tailscale fedora: installs the stable repository" "$tailscale_fedora_plan" \
  '^sudo curl -fsSL https://pkgs\.tailscale\.com/stable/fedora/tailscale\.repo -o /etc/yum\.repos\.d/tailscale\.repo$'
plan_has "tailscale fedora: installs its fetch prerequisites first" "$tailscale_fedora_plan" \
  '^sudo dnf install -y ca-certificates curl$'
plan_has "tailscale fedora: installs the package" "$tailscale_fedora_plan" \
  '^sudo dnf install -y tailscale$'
plan_has "tailscale fedora: enables the daemon" "$tailscale_fedora_plan" \
  '^sudo systemctl enable --now tailscaled$'

tailscale_install_plan Linux ubuntu '../../bad' 1 0 1 >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "tailscale apt plan rejects an unsafe codename" "$rc" "1"
tailscale_install_plan Darwin macos unused 0 0 1 >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "tailscale installer refuses hosts without a supported package plan" "$rc" "1"
tailscale_install_plan Linux ubuntu noble 1 0 0 >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "tailscale installer refuses a host without running systemd" "$rc" "1"

runner_src="$(sed -n '/^_run_prereq_plan()/,/^}/p' "$SETUP_SCRIPT")"

expect_eq "root prerequisite execution strips one line-leading sudo" \
  "$(rewrite_prereq_command 'sudo sudo test -f /tmp/example' 0 0)" \
  "sudo test -f /tmp/example"
expect_eq "root non-interactive execution also strips sudo instead of nesting it" \
  "$(rewrite_prereq_command 'sudo apt-get update' 1 0)" \
  "apt-get update"
expect_eq "interactive non-root execution keeps line-leading sudo" \
  "$(rewrite_prereq_command 'sudo apt-get update' 0 1000)" \
  "sudo apt-get update"
expect_eq "non-interactive non-root execution makes line-leading sudo non-blocking" \
  "$(rewrite_prereq_command 'sudo apt-get update' 1 1000)" \
  "sudo -n apt-get update"
expect_eq "the prerequisite rewrite never changes embedded sudo text" \
  "$(rewrite_prereq_command 'printf sudo' 1 1000)" \
  "printf sudo"

runner_out="$(
  eval "$runner_src"
  info() { :; }
  _run_prereq_plan $'printf first\nfalse\nprintf third\n' 0
)" && rc=0 || rc=$?
expect_eq "the prerequisite runner returns the first failed command" "$rc" "1"
expect_eq "the prerequisite runner stops before later commands" "$runner_out" "first"

os_release_src="$(sed -n \
  '/^_os_release_value()/,/^}/p; /^_host_os_id()/,/^}/p; /^_host_os_codename()/,/^}/p' \
  "$SETUP_SCRIPT")"
eval "$os_release_src"
OS_RELEASE_FIXTURE="${FIXTURES}/os-release"
cat > "$OS_RELEASE_FIXTURE" <<'EOF'
ID=linuxmint
VERSION_CODENAME=wilma
UBUNTU_CODENAME="noble"
EOF
expect_eq "host OS parser reads ID as data" \
  "$(JARVIS_OS_RELEASE_FILE="$OS_RELEASE_FIXTURE" _host_os_id)" "linuxmint"
expect_eq "host OS parser selects an Ubuntu derivative's upstream codename" \
  "$(JARVIS_OS_RELEASE_FILE="$OS_RELEASE_FIXTURE" _host_os_codename)" "noble"

OS_RELEASE_MARKER="${FIXTURES}/os-release-executed"
export OS_RELEASE_MARKER
cat > "$OS_RELEASE_FIXTURE" <<'EOF'
ID=$(touch "$OS_RELEASE_MARKER")
VERSION_CODENAME=noble
EOF
expect_eq "host OS parser rejects executable ID text" \
  "$(JARVIS_OS_RELEASE_FILE="$OS_RELEASE_FIXTURE" _host_os_id)" "unknown"
if [ -e "$OS_RELEASE_MARKER" ]; then
  printf 'FAIL: host OS parser executed /etc/os-release content\n' >&2
  fail=1
else
  pass "host OS parser never executes file content"
fi

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

# === headless_setup_route ====================================================
# A certificate trusted inside a VM is not trusted by a browser outside that
# VM. For a loopback HTTPS setup link, a headless install must therefore tunnel
# the dashboard's HTTP listener and print an exact localhost HTTP link. The SSH
# transport protects that loopback traffic. Plain localhost HTTP keeps its
# existing port and address.

got="$(headless_setup_route "https://localhost:3443" 3001 2>/dev/null)" && rc=0 || rc=$?
expect_eq "headless local HTTPS uses HTTP inside the SSH tunnel" \
  "${got}/${rc}" "3001|http://localhost:3001/0"

got="$(headless_setup_route "https://127.0.0.1" 43101 2>/dev/null)" && rc=0 || rc=$?
expect_eq "headless loopback HTTPS honours the configured dashboard HTTP port" \
  "${got}/${rc}" "43101|http://localhost:43101/0"

got="$(headless_setup_route "http://localhost:43001" 3001 2>/dev/null)" && rc=0 || rc=$?
expect_eq "headless localhost HTTP preserves its exact browser address" \
  "${got}/${rc}" "43001|http://localhost:43001/0"

got="$(headless_setup_route "https://jarvis.example" 3001 2>/dev/null)" && rc=0 || rc=$?
expect_eq "headless route helper rejects non-loopback origins" "${got}/${rc}" "/1"

got="$(headless_setup_route "https://localhost:3443" invalid 2>/dev/null)" && rc=0 || rc=$?
expect_eq "headless route helper rejects an invalid dashboard port" "${got}/${rc}" "/1"

RESOLVE_SETUP_BROWSER_ROUTE_FN="$(sed -n '/^resolve_setup_browser_route() {/,/^}/p' "$SETUP_SCRIPT")"
PRESENT_SETUP_LINK_FN="$(sed -n '/^present_setup_link() {/,/^}/p' "$SETUP_SCRIPT")"
headless_output="$(
  cd "$LINK_DIR"
  unset DISPLAY WAYLAND_DISPLAY SSH_TTY
  export SSH_CONNECTION='client 12345 server 22'
  C_BOLD='' C_RESET=''
  eval "$RESOLVE_SETUP_BROWSER_ROUTE_FN"
  eval "$PRESENT_SETUP_LINK_FN"
  present_setup_link 'https://localhost:3443' 3001
)"
_headless_output_ok=1
for _headless_required in \
  'Finish setup: http://localhost:3001/setup#setup_token=tok123' \
  'ssh -L 3001:127.0.0.1:3001 <your-ssh-user>@<server-address>' \
  'open this exact address:'; do
  case "$headless_output" in
    *"$_headless_required"*) ;;
    *) _headless_output_ok=0 ;;
  esac
done
if [ "$_headless_output_ok" -eq 1 ]; then
  pass "headless local HTTPS prints one usable HTTP-over-SSH finish route"
else
  printf 'FAIL: headless local HTTPS output is incomplete (got=%s)\n' "$headless_output" >&2
  fail=1
fi
case "$headless_output" in
  *'Finish setup: https://localhost:3443'*)
    printf 'FAIL: headless output still prints the VM-only HTTPS finish link\n' >&2
    fail=1 ;;
  *) pass "headless output never asks the outside browser to use the VM certificate" ;;
esac

desktop_output="$(
  cd "$LINK_DIR"
  unset SSH_CONNECTION SSH_TTY WAYLAND_DISPLAY
  export DISPLAY=':99'
  C_BOLD='' C_RESET=''
  eval "$RESOLVE_SETUP_BROWSER_ROUTE_FN"
  eval "$PRESENT_SETUP_LINK_FN"
  present_setup_link 'https://localhost:3443' 3001
)"
case "$desktop_output" in
  *'Finish setup: https://localhost:3443/setup#setup_token=tok123'*)
    pass "same-machine local HTTPS keeps its trusted HTTPS finish link" ;;
  *)
    printf 'FAIL: same-machine local HTTPS did not retain HTTPS (got=%s)\n' "$desktop_output" >&2
    fail=1 ;;
esac
case "$desktop_output" in
  *'ssh -L '*)
    printf 'FAIL: same-machine local HTTPS unnecessarily prints an SSH tunnel\n' >&2
    fail=1 ;;
  *) pass "same-machine local HTTPS does not print SSH guidance" ;;
esac

named_https_output="$(
  cd "$LINK_DIR"
  unset DISPLAY WAYLAND_DISPLAY SSH_CONNECTION SSH_TTY JARVIS_WINDOWS_LAUNCHER
  C_BOLD='' C_RESET=''
  eval "$RESOLVE_SETUP_BROWSER_ROUTE_FN"
  eval "$PRESENT_SETUP_LINK_FN"
  present_setup_link 'https://jarvis.family.example' 3001
  printf '\nSTATE=%s|%s\n' "$SETUP_BROWSER_BASE" "$SETUP_BROWSER_IS_SHARED"
)" && rc=0 || rc=$?
case "${rc}:${named_https_output}" in
  0:*'Finish setup: https://jarvis.family.example/setup#setup_token=tok123'*'STATE=https://jarvis.family.example|1'*)
    pass "a verified named HTTPS route remains the shared family origin" ;;
  *) printf 'FAIL: named HTTPS route lost its shared-origin contract (rc=%s out=%s)\n' "$rc" "$named_https_output" >&2; fail=1 ;;
esac

for loopback_name in 'https://LOCALHOST:3443' 'https://family.localhost:3443'; do
  loopback_name_output="$(
    cd "$LINK_DIR"
    unset DISPLAY WAYLAND_DISPLAY SSH_TTY JARVIS_WINDOWS_LAUNCHER
    export SSH_CONNECTION='client 12345 server 22'
    C_BOLD='' C_RESET=''
    eval "$RESOLVE_SETUP_BROWSER_ROUTE_FN"
    resolve_setup_browser_route "$loopback_name" 3001
    printf 'STATE=%s|%s|%s\n' "$SETUP_BROWSER_BASE" \
      "$SETUP_BROWSER_IS_SHARED" "$SETUP_LINK_USES_SSH_TUNNEL"
  )" && rc=0 || rc=$?
  case "${rc}:${loopback_name_output}" in
    0:*'STATE=http://localhost:3001|0|1'*)
      pass "${loopback_name} remains a local-only SSH route" ;;
    *)
      printf 'FAIL: %s was misclassified as a shared family origin (rc=%s out=%s)\n' \
        "$loopback_name" "$rc" "$loopback_name_output" >&2
      fail=1 ;;
  esac
done

WINDOWS_CURL_BIN="$(mktemp -d "${FIXTURES}/windows-curl.XXXXXX")"
WINDOWS_CURL_LOG="${FIXTURES}/windows-curl.log"
cat > "${WINDOWS_CURL_BIN}/curl.exe" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$WINDOWS_CURL_LOG"
printf '%s' "${WINDOWS_CURL_BODY:-jarvis-rd-assistant}"
exit "${WINDOWS_CURL_RC:-0}"
EOF
chmod +x "${WINDOWS_CURL_BIN}/curl.exe"

: > "$WINDOWS_CURL_LOG"
wsl_output="$(
  cd "$LINK_DIR"
  unset DISPLAY WAYLAND_DISPLAY SSH_CONNECTION SSH_TTY
  export JARVIS_WINDOWS_LAUNCHER=1 WINDOWS_CURL_LOG
  export PATH="${WINDOWS_CURL_BIN}:${PATH}"
  C_BOLD='' C_RESET=''
  eval "$RESOLVE_SETUP_BROWSER_ROUTE_FN"
  eval "$PRESENT_SETUP_LINK_FN"
  present_setup_link 'https://localhost:3443' 3001
  printf '\nSTATE=%s|%s|%s\n' "$SETUP_BROWSER_BASE" \
    "$SETUP_LINK_USES_SSH_TUNNEL" "$SETUP_LINK_USES_WINDOWS_FORWARDING"
)" && rc=0 || rc=$?
case "${rc}:${wsl_output}" in
  0:*'Finish setup: http://localhost:3001/setup#setup_token=tok123'*'STATE=http://localhost:3001|0|1'*)
    pass "WSL launcher advertises Windows localhost only after its exact marker probe" ;;
  *)
    printf 'FAIL: verified WSL browser route is wrong (rc=%s out=%s)\n' "$rc" "$wsl_output" >&2
    fail=1 ;;
esac
case "$wsl_output" in
  *'ssh -L '*) printf 'FAIL: verified Windows localhost route prints a fake SSH detour\n' >&2; fail=1 ;;
  *) pass "verified Windows localhost route does not invent an SSH session" ;;
esac
case "$(cat "$WINDOWS_CURL_LOG")" in
  *'http://localhost:3001/health/jarvis'*) pass "Windows reachability probe uses the exact JARVIS marker endpoint" ;;
  *) printf 'FAIL: WSL route did not probe Windows /health/jarvis (args=%s)\n' "$(cat "$WINDOWS_CURL_LOG")" >&2; fail=1 ;;
esac

: > "$WINDOWS_CURL_LOG"
wsl_failure_output="$(
  cd "$LINK_DIR"
  unset DISPLAY WAYLAND_DISPLAY SSH_CONNECTION SSH_TTY
  export JARVIS_WINDOWS_LAUNCHER=1 WINDOWS_CURL_LOG WINDOWS_CURL_BODY='another-service'
  export PATH="${WINDOWS_CURL_BIN}:${PATH}"
  C_BOLD='' C_RESET=''
  eval "$RESOLVE_SETUP_BROWSER_ROUTE_FN"
  eval "$PRESENT_SETUP_LINK_FN"
  present_setup_link 'http://localhost:3001' 3001
)" && rc=0 || rc=$?
expect_eq "WSL refuses to advertise Windows localhost when another app answers" "$rc" "1"
case "$wsl_failure_output" in
  *'Windows localhost forwarding'*'jarvis-rd-assistant'*)
    pass "failed Windows reachability prints concrete forwarding guidance" ;;
  *) printf 'FAIL: failed Windows reachability lacks lay guidance (out=%s)\n' "$wsl_failure_output" >&2; fail=1 ;;
esac
case "$wsl_failure_output" in
  *'setup_token='*|*'ssh -L '*)
    printf 'FAIL: failed Windows reachability advertised an unverified link or fake SSH route\n' >&2
    fail=1 ;;
  *) pass "failed Windows reachability advertises neither a setup token nor SSH" ;;
esac

wsl_transport_failure_output="$(
  cd "$LINK_DIR"
  unset DISPLAY WAYLAND_DISPLAY SSH_CONNECTION SSH_TTY
  export JARVIS_WINDOWS_LAUNCHER=1 WINDOWS_CURL_LOG
  export WINDOWS_CURL_BODY='jarvis-rd-assistant' WINDOWS_CURL_RC=7
  export PATH="${WINDOWS_CURL_BIN}:${PATH}"
  C_BOLD='' C_RESET=''
  eval "$RESOLVE_SETUP_BROWSER_ROUTE_FN"
  eval "$PRESENT_SETUP_LINK_FN"
  present_setup_link 'http://localhost:3001' 3001
)" && rc=0 || rc=$?
expect_eq "WSL rejects the marker when the Windows probe itself fails" "$rc" "1"
case "$wsl_transport_failure_output" in
  *'setup_token='*|*'ssh -L '*)
    printf 'FAIL: failed Windows probe advertised an unverified link or fake SSH route\n' >&2
    fail=1 ;;
  *) pass "failed Windows probe advertises neither a setup token nor SSH" ;;
esac

: > "$WINDOWS_CURL_LOG"
wsl_ssh_output="$(
  cd "$LINK_DIR"
  unset DISPLAY WAYLAND_DISPLAY SSH_TTY
  export SSH_CONNECTION='client 12345 server 22'
  export JARVIS_WINDOWS_LAUNCHER=1 WINDOWS_CURL_LOG WINDOWS_CURL_RC=99
  export PATH="${WINDOWS_CURL_BIN}:${PATH}"
  C_BOLD='' C_RESET=''
  eval "$RESOLVE_SETUP_BROWSER_ROUTE_FN"
  eval "$PRESENT_SETUP_LINK_FN"
  present_setup_link 'https://localhost:3443' 3001
)" && rc=0 || rc=$?
case "${rc}:${wsl_ssh_output}" in
  0:*'ssh -L 3001:127.0.0.1:3001 '*) pass "a real SSH session takes precedence over WSL forwarding" ;;
  *) printf 'FAIL: SSH did not take precedence over WSL (rc=%s out=%s)\n' "$rc" "$wsl_ssh_output" >&2; fail=1 ;;
esac
expect_eq "SSH precedence skips the Windows curl probe" "$(cat "$WINDOWS_CURL_LOG")" ""

# === existing-install status helpers ========================================
# A re-run must distinguish a genuinely unfinished bootstrap from an already
# configured installation. Invalid or partial JSON is unknown, never
# "unconfigured". Single-user guidance is only safe after the key file exists.

got="$(printf '%s' '{"configured":false,"setup_completed":false,"setup_mode":"multi"}' | parse_setup_status_json)" && rc=0 || rc=$?
expect_eq "parse_setup_status_json reports an unfinished multi-user bootstrap" "${got}/${rc}" "false false multi/0"

got="$(printf '%s' '{"configured":true,"setup_completed":true,"setup_mode":"single"}' | parse_setup_status_json)" && rc=0 || rc=$?
expect_eq "parse_setup_status_json reports a completed single-user install" "${got}/${rc}" "true true single/0"

got="$(printf '%s' '{"configured":"false","setup_completed":false,"setup_mode":"multi"}' | parse_setup_status_json 2>/dev/null)" && rc=0 || rc=$?
expect_eq "parse_setup_status_json rejects type-confused status JSON" "${got}/${rc}" "/1"

got="$(printf '%s' 'not-json' | parse_setup_status_json 2>/dev/null)" && rc=0 || rc=$?
expect_eq "parse_setup_status_json rejects invalid JSON" "${got}/${rc}" "/1"

KEY_HOME="$(mktemp -d "${FIXTURES}/key-home.XXXXXX")"
got="$(HOME="$KEY_HOME" materialize_api_key_file 'local-api-key')" && rc=0 || rc=$?
expect_eq "materialize_api_key_file reports the created path" "${got}/${rc}" "${KEY_HOME}/.config/jarvis/api-key/0"
expect_eq "materialize_api_key_file writes the exact key" "$(cat "$got")" "local-api-key"
expect_eq "materialize_api_key_file makes the parent owner-only" "$(stat -c '%a' "${KEY_HOME}/.config/jarvis")" "700"
expect_eq "materialize_api_key_file makes the key owner-only" "$(stat -c '%a' "$got")" "600"

# === access-mode / ingress helpers ===========================================
# _is_lan_ipv4 accepts the RFC1918 address other LAN devices reach this host by and
# rejects docker/CGNAT/link-local ranges. _append_server_name / _append_csv build
# the accumulating nginx Host allowlist and CORS list (LAN + origin keep BOTH
# hostnames). _public_origin_host accepts an origin-only https:// DNS URL and
# refuses IP literals or URL components beyond an optional port.
# These pure helpers live in setup.sh (before the flag parser); extract and eval
# them.
ingress_src="$(sed -n '/^_is_lan_ipv4()/,/^}/p;/^_append_server_name()/,/^}/p;/^_append_csv()/,/^}/p;/^_public_origin_host()/,/^}/p' "$SETUP_SCRIPT")"
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
  'https://localhost' \
  'https://LOCALHOST:3443' \
  'https://family.localhost' \
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

# A custom bridge subnet must derive every pinned ingress address from one
# source so Compose and nginx cannot drift apart.
got="$(allocate_ingress_ips 10.88.40.0/24 2>/dev/null)" && rc=0 || rc=$?
expect_eq "allocate_ingress_ips derives gateway and five edge addresses" \
  "${got}/${rc}" "10.88.40.1 10.88.40.250 10.88.40.251 10.88.40.252 10.88.40.253 10.88.40.254/0"
got="$(allocate_ingress_ips 10.0.0.0/8 2>/dev/null)" && rc=0 || rc=$?
expect_eq "allocate_ingress_ips handles a large subnet without enumerating it" \
  "${got}/${rc}" "10.0.0.1 10.255.255.250 10.255.255.251 10.255.255.252 10.255.255.253 10.255.255.254/0"
allocate_ingress_ips 10.88.40.0/28 >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "allocate_ingress_ips rejects a subnet too small for all service profiles" "$rc" "1"

# External edge classification is deliberately stricter than "curl got 2xx".
expect_eq "exact JARVIS marker verifies the app" \
  "$(classify_external_app_probe 0 200 '' '' '' 'jarvis-rd-assistant')" "verified"
expect_eq "a different 2xx app is not accepted" \
  "$(classify_external_app_probe 0 200 '' '' '' '<html>other app</html>')" "wrong-app"
expect_eq "Cloudflare Access redirect is edge-only" \
  "$(classify_external_app_probe 0 302 'https://team.cloudflareaccess.com/cdn-cgi/access/login' cloudflare '' '')" "access"
expect_eq "Cloudflare challenge is edge-only" \
  "$(classify_external_app_probe 0 403 '' cloudflare challenge '<html>Just a moment</html>')" "waf"
expect_eq "DNS/TLS curl failures stay distinct from an app response" \
  "$(classify_external_app_probe 60 000 '' '' '' '')" "dns-tls"

expect_eq "persisted Tailscale mode requires HTTPS verification" \
  "$(selected_https_route tailscale '' 'https://node.tailnet.ts.net')" "tailscale"
expect_eq "legacy tunnel profile still requires HTTPS verification" \
  "$(selected_https_route localhost 'tunnel,telegram' 'https://jarvis.example.com')" "tunnel"
expect_eq "persisted Let's Encrypt mode requires HTTPS verification" \
  "$(selected_https_route letsencrypt letsencrypt 'https://jarvis.example.com')" "letsencrypt"
expect_eq "a named private origin requires HTTPS verification" \
  "$(selected_https_route localhost '' 'https://jarvis.home.arpa')" "private"
expect_eq "localhost without a named origin needs no external route" \
  "$(selected_https_route localhost '' '')" "none"
expect_eq "local self-signed HTTPS requires its own trusted marker probe" \
  "$(selected_https_route localhost caddy-local '')" "local-https"
selected_https_is_verified private verified && rc=0 || rc=$?
expect_eq "an exact marker satisfies a selected HTTPS route" "$rc" "0"
selected_https_is_verified tailscale dns-tls && rc=0 || rc=$?
expect_eq "a selected HTTPS route rejects non-marker probe states" "$rc" "1"
selected_https_is_verified none unavailable && rc=0 || rc=$?
expect_eq "a localhost-only install does not require an external probe" "$rc" "0"
expect_eq "localhost-only setup keeps development safeguards" \
  "$(environment_for_access_route localhost '' '')" "development"
expect_eq "loopback local HTTPS remains a development-only route" \
  "$(environment_for_access_route localhost caddy-local '')" "development"
expect_eq "a named HTTPS origin layered onto localhost enables production safeguards" \
  "$(environment_for_access_route localhost '' https://jarvis.home.arpa)" "production"
expect_eq "a named HTTPS origin layered onto LAN enables production safeguards" \
  "$(environment_for_access_route lan '' https://jarvis.home.arpa)" "production"
for production_mode in tailscale tunnel letsencrypt; do
  expect_eq "${production_mode} enables production safeguards" \
    "$(environment_for_access_route "$production_mode" '' https://jarvis.example)" \
    "production"
done

LOCAL_HTTPS_PROBE_LOG="${FIXTURES}/local-https-probe.log"
: > "$LOCAL_HTTPS_PROBE_LOG"
got="$(
  export LOCAL_HTTPS_PROBE_LOG
  mkcert_ca_file() { printf '%s' /trusted/rootCA.pem; }
  probe_external_app() {
    printf '%s|%s\n' "$1" "${2:-system}" >> "$LOCAL_HTTPS_PROBE_LOG"
    printf 'verified'
  }
  probe_local_https_app https://localhost:3443/health/jarvis
)"
expect_eq "local HTTPS verification succeeds only through its mkcert CA and system trust" \
  "$got" "verified"
expect_eq "local HTTPS verification checks the intended CA before browser-facing system trust" \
  "$(cat "$LOCAL_HTTPS_PROBE_LOG")" \
  "https://localhost:3443/health/jarvis|/trusted/rootCA.pem
https://localhost:3443/health/jarvis|system"

: > "$LOCAL_HTTPS_PROBE_LOG"
got="$(
  export LOCAL_HTTPS_PROBE_LOG
  mkcert_ca_file() { printf '%s' /trusted/rootCA.pem; }
  probe_external_app() {
    printf '%s|%s\n' "$1" "${2:-system}" >> "$LOCAL_HTTPS_PROBE_LOG"
    printf 'dns-tls'
  }
  probe_local_https_app https://localhost:3443/health/jarvis
)"
expect_eq "local HTTPS verification stops when the intended mkcert chain is invalid" \
  "$got/$(wc -l < "$LOCAL_HTTPS_PROBE_LOG" | tr -d ' ')" "dns-tls/1"

PROBE_CURL_BIN="$(mktemp -d "${FIXTURES}/probe-curl-bin.XXXXXX")"
PROBE_CURL_LOG="${FIXTURES}/probe-curl-args.log"
PROBE_CA="${FIXTURES}/rootCA.pem"
printf 'test-ca\n' > "$PROBE_CA"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" > "$PROBE_CURL_LOG"' \
  'body=""; headers=""' \
  'while [ "$#" -gt 0 ]; do' \
  '  case "$1" in' \
  '    --output) body="$2"; shift 2 ;;' \
  '    --dump-header) headers="$2"; shift 2 ;;' \
  '    --write-out|--connect-timeout|--max-time|--range|--cacert) shift 2 ;;' \
  '    *) shift ;;' \
  '  esac' \
  'done' \
  ': > "$headers"' \
  'printf jarvis-rd-assistant > "$body"' \
  'printf 200' \
  > "${PROBE_CURL_BIN}/curl"
chmod +x "${PROBE_CURL_BIN}/curl"
got="$(export PATH="${PROBE_CURL_BIN}:${PATH}" PROBE_CURL_LOG; \
  probe_external_app https://localhost:3443/health/jarvis "$PROBE_CA")"
expect_eq "the real endpoint probe accepts the exact marker with an explicit CA" \
  "$got" "verified"
case "$(cat "$PROBE_CURL_LOG")" in
  *"--cacert $PROBE_CA"*) pass "the real local probe passes mkcert's CA to curl" ;;
  *) printf 'FAIL: explicit-CA probe did not pass --cacert to curl\n' >&2; fail=1 ;;
esac
if grep -Eq -- '(^| )(--insecure|-k)( |$)' "$PROBE_CURL_LOG"; then
  printf 'FAIL: local HTTPS verification disables TLS validation\n' >&2
  fail=1
else
  pass "local HTTPS verification never disables TLS validation"
fi
got="$(export PATH="${PROBE_CURL_BIN}:${PATH}" PROBE_CURL_LOG; \
  probe_external_app https://localhost:3443/health/jarvis)"
expect_eq "the browser-facing probe also accepts the marker through system trust" \
  "$got" "verified"
if grep -Fq -- '--cacert' "$PROBE_CURL_LOG"; then
  printf 'FAIL: system-trust probe was pinned to a private CA file\n' >&2
  fail=1
else
  pass "the browser-facing probe uses the normal host trust store"
fi

MKCERT_ROOT="${FIXTURES}/mkcert-existing"
MKCERT_BIN="${MKCERT_ROOT}/bin"
MKCERT_LOG="${MKCERT_ROOT}/mkcert.log"
mkdir -p "$MKCERT_ROOT/scripts" "$MKCERT_ROOT/certs" "$MKCERT_ROOT/caroot" "$MKCERT_BIN"
cp "${SCRIPT_DIR}/../init-mkcert.sh" "$MKCERT_ROOT/scripts/init-mkcert.sh"
printf old-cert > "$MKCERT_ROOT/certs/cert.pem"
printf old-key > "$MKCERT_ROOT/certs/key.pem"
printf old-ca > "$MKCERT_ROOT/caroot/rootCA.pem"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'case "$1" in' \
  '  -CAROOT) printf "%s\n" "$MKCERT_ROOT/caroot" ;;' \
  '  -install) printf "%s\n" "$*" >> "$MKCERT_LOG" ;;' \
  '  *) printf "unexpected-generation %s\n" "$*" >> "$MKCERT_LOG" ;;' \
  'esac' > "$MKCERT_BIN/mkcert"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$MKCERT_BIN/openssl"
chmod +x "$MKCERT_BIN/mkcert" "$MKCERT_BIN/openssl"
(
  export PATH="${MKCERT_BIN}:${PATH}" MKCERT_ROOT MKCERT_LOG
  bash "$MKCERT_ROOT/scripts/init-mkcert.sh" >/dev/null
)
expect_eq "make certs reinstalls host trust even when CA and leaf cert files already exist" \
  "$(cat "$MKCERT_LOG")" "-install"

bin="$(fake_docker 'printf "%s\\n" "$*"; case "$*" in *cloudflared:2000/ready*) exit 0;; *) exit 1;; esac')"
got="$(export PATH="${bin}:${PATH}"; cloudflared_ready)" && rc=0 || rc=$?
expect_eq "cloudflared_ready probes the active-connection endpoint from dashboard" \
  "${got}/${rc}" "compose exec -T dashboard curl -fsS --max-time 5 http://cloudflared:2000/ready/0"

# === tailscale_serve_https privilege behavior ================================

TAILSCALE_BIN="$(mktemp -d "${FIXTURES}/bin.XXXXXX")"
TAILSCALE_COMMAND_LOG="${FIXTURES}/tailscale-command.log"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [ "${1:-}" = "-u" ]; then printf "%s\n" "${STUB_ID_UID:-1000}"; exit 0; fi' \
  'exit 1' > "${TAILSCALE_BIN}/id"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "sudo" >> "$TAILSCALE_COMMAND_LOG"' \
  'printf " %s" "$@" >> "$TAILSCALE_COMMAND_LOG"' \
  'printf "\n" >> "$TAILSCALE_COMMAND_LOG"' \
  '[ "${STUB_SUDO_RC:-0}" -eq 0 ] || exit "$STUB_SUDO_RC"' \
  'case "$*" in' \
  '  "-n tailscale serve status --json"|"tailscale serve status --json") printf "%s\n" "${STUB_TAILSCALE_STATUS_JSON:-}" ;;' \
  '  "-n tailscale serve reset"|"tailscale serve reset") ;;' \
  '  "-n tailscale serve --bg --yes --https=443 http://127.0.0.1:"*) [ "$#" -eq 7 ] || exit 64 ;;' \
  '  "tailscale serve --bg --yes --https=443 http://127.0.0.1:"*) [ "$#" -eq 6 ] || exit 64 ;;' \
  '  *) exit 64 ;;' \
  'esac' > "${TAILSCALE_BIN}/sudo"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "tailscale" >> "$TAILSCALE_COMMAND_LOG"' \
  'printf " %s" "$@" >> "$TAILSCALE_COMMAND_LOG"' \
  'printf "\n" >> "$TAILSCALE_COMMAND_LOG"' \
  '[ "${STUB_TAILSCALE_RC:-0}" -eq 0 ] || exit "$STUB_TAILSCALE_RC"' \
  'case "$*" in' \
  '  "serve status --json") printf "%s\n" "${STUB_TAILSCALE_STATUS_JSON:-}" ;;' \
  '  "serve reset") ;;' \
  '  "serve --bg --yes --https=443 http://127.0.0.1:"*) [ "$#" -eq 5 ] || exit 64 ;;' \
  '  *) exit 64 ;;' \
  'esac' > "${TAILSCALE_BIN}/tailscale"
chmod +x "${TAILSCALE_BIN}/id" "${TAILSCALE_BIN}/sudo" "${TAILSCALE_BIN}/tailscale"

: > "$TAILSCALE_COMMAND_LOG"
out="$(STUB_ID_UID=1000 STUB_SUDO_RC=0 TAILSCALE_COMMAND_LOG="$TAILSCALE_COMMAND_LOG" \
  PATH="${TAILSCALE_BIN}:${PATH}" tailscale_serve_https 3003 0 2>&1)" && rc=0 || rc=$?
expect_eq "interactive non-root Tailscale Serve succeeds through sudo" "$rc" "0"
expect_eq "interactive non-root Tailscale Serve invokes visible sudo command" \
  "$(cat "$TAILSCALE_COMMAND_LOG")" \
  "sudo tailscale serve --bg --yes --https=443 http://127.0.0.1:3003"
if printf '%s\n' "$out" | grep -Fq \
  'sudo tailscale serve --bg --yes --https=443 http://127.0.0.1:3003'; then
  pass "interactive non-root Tailscale Serve prints the privileged command"
else
  printf 'FAIL: interactive non-root Tailscale Serve hid its command (out=%s)\n' "$out" >&2
  fail=1
fi

: > "$TAILSCALE_COMMAND_LOG"
out="$(STUB_ID_UID=1000 STUB_SUDO_RC=0 TAILSCALE_COMMAND_LOG="$TAILSCALE_COMMAND_LOG" \
  PATH="${TAILSCALE_BIN}:${PATH}" tailscale_serve_https 3003 1 2>&1)" && rc=0 || rc=$?
expect_eq "non-interactive non-root Tailscale Serve succeeds through sudo -n" "$rc" "0"
expect_eq "non-interactive non-root Tailscale Serve cannot prompt" \
  "$(cat "$TAILSCALE_COMMAND_LOG")" \
  "sudo -n tailscale serve --bg --yes --https=443 http://127.0.0.1:3003"

: > "$TAILSCALE_COMMAND_LOG"
out="$(STUB_ID_UID=0 STUB_TAILSCALE_RC=0 TAILSCALE_COMMAND_LOG="$TAILSCALE_COMMAND_LOG" \
  PATH="${TAILSCALE_BIN}:${PATH}" tailscale_serve_https 3003 0 2>&1)" && rc=0 || rc=$?
expect_eq "root Tailscale Serve succeeds without sudo" "$rc" "0"
expect_eq "root Tailscale Serve invokes tailscale directly" \
  "$(cat "$TAILSCALE_COMMAND_LOG")" \
  "tailscale serve --bg --yes --https=443 http://127.0.0.1:3003"

: > "$TAILSCALE_COMMAND_LOG"
out="$(STUB_ID_UID=1000 STUB_SUDO_RC=23 TAILSCALE_COMMAND_LOG="$TAILSCALE_COMMAND_LOG" \
  PATH="${TAILSCALE_BIN}:${PATH}" tailscale_serve_https 3003 1 2>&1)" && rc=0 || rc=$?
expect_eq "a privileged Tailscale Serve failure is returned to setup" "$rc" "23"

: > "$TAILSCALE_COMMAND_LOG"
_owned_serve='{"TCP":{"443":{"HTTPS":true}},"Web":{"node.tailnet.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:3003"}}}}}'
out="$(STUB_ID_UID=1000 STUB_SUDO_RC=0 STUB_TAILSCALE_STATUS_JSON="$_owned_serve" \
  TAILSCALE_COMMAND_LOG="$TAILSCALE_COMMAND_LOG" PATH="${TAILSCALE_BIN}:${PATH}" \
  tailscale_serve_https_off 3003 1 2>&1)" && rc=0 || rc=$?
expect_eq "non-interactive Tailscale Serve removal succeeds through sudo -n" "$rc" "0"
expect_eq "Tailscale Serve removal verifies ownership before a supported reset" \
  "$(cat "$TAILSCALE_COMMAND_LOG")" \
  "sudo -n tailscale serve status --json
sudo -n tailscale serve reset"

# A CLI/sudo error does not prove that tailscaled rejected the mutation. Model a
# nonzero configure followed by status showing the exact attempted route: cleanup
# still inspects ownership and resets that route.
: > "$TAILSCALE_COMMAND_LOG"
STUB_ID_UID=1000 STUB_SUDO_RC=23 TAILSCALE_COMMAND_LOG="$TAILSCALE_COMMAND_LOG" \
  PATH="${TAILSCALE_BIN}:${PATH}" tailscale_serve_https 3003 1 >/dev/null 2>&1 \
  && rc=0 || rc=$?
expect_eq "Tailscale configure can return nonzero after a mutation became possible" "$rc" "23"
STUB_ID_UID=1000 STUB_SUDO_RC=0 STUB_TAILSCALE_STATUS_JSON="$_owned_serve" \
  TAILSCALE_COMMAND_LOG="$TAILSCALE_COMMAND_LOG" PATH="${TAILSCALE_BIN}:${PATH}" \
  tailscale_serve_https_off 3003 1 >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "a possibly-applied failed Tailscale call is ownership-checked and reset" \
  "$(cat "$TAILSCALE_COMMAND_LOG")/${rc}" \
  "sudo -n tailscale serve --bg --yes --https=443 http://127.0.0.1:3003
sudo -n tailscale serve status --json
sudo -n tailscale serve reset/0"

: > "$TAILSCALE_COMMAND_LOG"
_shared_serve='{"TCP":{"443":{"HTTPS":true},"8443":{"HTTPS":true}},"Web":{"node.tailnet.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:3003"}}},"node.tailnet.ts.net:8443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8443"}}}}}'
out="$(STUB_ID_UID=1000 STUB_SUDO_RC=0 STUB_TAILSCALE_STATUS_JSON="$_shared_serve" \
  TAILSCALE_COMMAND_LOG="$TAILSCALE_COMMAND_LOG" PATH="${TAILSCALE_BIN}:${PATH}" \
  tailscale_serve_https_off 3003 1 2>&1)" && rc=0 || rc=$?
expect_eq "Tailscale removal refuses to reset an unrelated Serve route" "$rc" "64"
expect_eq "refused Tailscale removal performs only the read-only status command" \
  "$(cat "$TAILSCALE_COMMAND_LOG")" "sudo -n tailscale serve status --json"

printf '%s' '{}' | tailscale_serve_config_is_jarvis_only 3003 && rc=0 || rc=$?
expect_eq "an already-empty Tailscale Serve config is distinct from owned state" "$rc" "3"
_wrong_target='{"TCP":{"443":{"HTTPS":true}},"Web":{"node.tailnet.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:3999"}}}}}'
printf '%s' "$_wrong_target" | tailscale_serve_config_is_jarvis_only 3003 && rc=0 || rc=$?
expect_eq "a single route to another target is not treated as JARVIS-owned" "$rc" "1"

ROLLBACK_DIR="$(mktemp -d "${FIXTURES}/rollback.XXXXXX")"
printf 'JARVIS_ACCESS_MODE=tailscale\n' > "${ROLLBACK_DIR}/previous.env"
printf 'JARVIS_ACCESS_MODE=tunnel\n' > "${ROLLBACK_DIR}/current.env"
restore_env_snapshot "${ROLLBACK_DIR}/previous.env" "${ROLLBACK_DIR}/current.env" \
  && rc=0 || rc=$?
expect_eq "access rollback atomically restores the previous env" \
  "$(cat "${ROLLBACK_DIR}/current.env")/${rc}" "JARVIS_ACCESS_MODE=tailscale/0"
if [ -f "${ROLLBACK_DIR}/previous.env" ]; then
  pass "access rollback preserves the snapshot for manual recovery"
else
  printf 'FAIL: access rollback consumed its recovery snapshot\n' >&2
  fail=1
fi
printf 'CLOUDFLARE_TUNNEL_TOKEN=old-token-value\n' > "${ROLLBACK_DIR}/current.env"
printf 'new-token-value' > "${ROLLBACK_DIR}/tunnel-secret"
restore_secret_from_env "${ROLLBACK_DIR}/current.env" CLOUDFLARE_TUNNEL_TOKEN \
  "${ROLLBACK_DIR}/tunnel-secret" && rc=0 || rc=$?
expect_eq "access rollback restores the prior tunnel credential without printing it" \
  "$(cat "${ROLLBACK_DIR}/tunnel-secret")/${rc}" "old-token-value/0"
printf 'CLOUDFLARE_TUNNEL_TOKEN=\n' > "${ROLLBACK_DIR}/current.env"
restore_secret_from_env "${ROLLBACK_DIR}/current.env" CLOUDFLARE_TUNNEL_TOKEN \
  "${ROLLBACK_DIR}/tunnel-secret" && rc=0 || rc=$?
if [ "$rc" -eq 0 ] && [ ! -e "${ROLLBACK_DIR}/tunnel-secret" ]; then
  pass "access rollback removes a replacement tunnel credential when the old mode had none"
else
  printf 'FAIL: access rollback retained a tunnel credential absent from the restored env\n' >&2
  fail=1
fi

# === access_edge_retirements =================================================

got="$(access_edge_retirements tunnel 'tunnel,telegram' localhost telegram)"
expect_eq "leaving tunnel retires only cloudflared" "$got" "tunnel|cloudflared"

got="$(access_edge_retirements tailscale '' lan '')"
expect_eq "leaving Tailscale retires its host Serve route" "$got" "tailscale|tailscale"

got="$(access_edge_retirements letsencrypt letsencrypt tunnel tunnel)"
expect_eq "switching from Let's Encrypt retires only Caddy" "$got" "letsencrypt|caddy"

got="$(access_edge_retirements localhost 'caddy-local,telegram' localhost telegram)"
expect_eq "leaving local HTTPS retires only local Caddy" "$got" "caddy-local|caddy_local"

got="$(access_edge_retirements tunnel tunnel tunnel tunnel)"
expect_eq "keeping the same access edge retires nothing" "$got" ""

QUIESCE_COMMAND_LOG="${FIXTURES}/access-quiesce.log"
: > "$QUIESCE_COMMAND_LOG"
(
  access_rollback_compose() {
    shift 2
    printf 'docker compose %s\n' "$*" >> "$QUIESCE_COMMAND_LOG"
  }
  tailscale_serve_https_off() {
    printf 'tailscale-off %s %s\n' "$1" "$2" >> "$QUIESCE_COMMAND_LOG"
  }
  quiesce_previous_access_runtime tailscale '' tunnel tunnel 3017 1 \
    "$FIXTURES" current.env
)
expect_eq "replacement verification first quiesces only the retired old edge" \
  "$(cat "$QUIESCE_COMMAND_LOG")" "tailscale-off 3017 1"

: > "$QUIESCE_COMMAND_LOG"
(
  access_rollback_compose() {
    shift 2
    printf 'docker compose %s\n' "$*" >> "$QUIESCE_COMMAND_LOG"
  }
  tailscale_serve_https_off() { return 0; }
  quiesce_previous_access_runtime tunnel tunnel localhost '' 3003 1 \
    "$FIXTURES" current.env
)
expect_eq "Compose edge quiescing is project-scoped and leaves credentials intact" \
  "$(cat "$QUIESCE_COMMAND_LOG")" "docker compose --profile tunnel rm -sf cloudflared"

(
  access_rollback_compose() { return 23; }
  tailscale_serve_https_off() { return 0; }
  quiesce_previous_access_runtime tunnel tunnel localhost '' 3003 1 \
    "$FIXTURES" current.env
) && rc=0 || rc=$?
expect_eq "a failed old-edge quiesce remains fatal so setup can restore the transaction" \
  "$rc" "1"

# A durable transaction snapshot preserves both bytes and absence. This survives
# SIGKILL and stays outside secrets/ so normal backup collection cannot ingest it.
SETUP_TX_DIR="${FIXTURES}/.jarvis-setup-transaction"
SETUP_TX_SECRETS="${FIXTURES}/live-secrets"
RECOVER_TRANSACTION_FN="$(sed -n '/^recover_interrupted_setup_transaction() {/,/^}/p' "$SETUP_SCRIPT")"
case "$RECOVER_TRANSACTION_FN" in
  *'Do not rename it inside this checkout'*'mktemp -d '*'/jarvis-recovery.XXXXXX'*'chmod 700'*'mv --'*)
    pass "invalid active-journal guidance preserves secret snapshots outside the checkout" ;;
  *)
    printf 'FAIL: invalid active-journal guidance lacks a private external hold path\n' >&2
    fail=1 ;;
esac
case "$RECOVER_TRANSACTION_FN" in
  *'rm -rf'*)
    printf 'FAIL: setup still teaches deleting a credential-bearing staging path\n' >&2
    fail=1 ;;
  *) pass "setup never teaches deleting a credential-bearing staging path" ;;
esac
case "$RECOVER_TRANSACTION_FN" in
  # The PHYSICAL parent, not the symlinked one: a rename off the checkout's own
  # filesystem is a copy, which leaves credential bytes behind on a failure.
  *'dirname "$script_dir_physical"'*'jarvis-abandoned-staging-'*)
    pass "abandoned staging is only ever moved outside the checkout" ;;
  *)
    printf 'FAIL: abandoned staging has no destination outside the checkout\n' >&2
    fail=1 ;;
esac
HOSTILE_STATE_TX="${FIXTURES}/hostile-state/.jarvis-setup-transaction"
SETUP_PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
mkdir -p "$(dirname "$HOSTILE_STATE_TX")"
printf 'invalid-journal-shape' > "$HOSTILE_STATE_TX"
out="$(
  (
    eval "$RECOVER_TRANSACTION_FN"
    _SETUP_TRANSACTION_DIR="$HOSTILE_STATE_TX"
    SCRIPT_DIR="$SETUP_PROJECT_ROOT"
    XDG_STATE_HOME="$SETUP_PROJECT_ROOT/.state-inside-checkout"
    HOME="$SETUP_PROJECT_ROOT"
    warn() { printf '%s\n' "$*"; }
    recover_interrupted_setup_transaction
  ) 2>&1
)" && rc=0 || rc=$?
expect_eq "an invalid journal with hostile state roots stops setup" "$rc" "1"
case "$out" in
  *'.state-inside-checkout'*|*'home-inside-checkout'*)
    printf 'FAIL: hostile state root leaked into private recovery guidance\n%s\n' \
      "$out" >&2
    fail=1 ;;
  *'mktemp -d /tmp/jarvis-recovery.XXXXXX'*)
    pass "hostile state roots cannot redirect credential recovery into the checkout" ;;
  *)
    printf 'FAIL: hostile state fallback did not name the external mktemp root\n%s\n' \
      "$out" >&2
    fail=1 ;;
esac
rm -f "$HOSTILE_STATE_TX"
mkdir -p "$SETUP_TX_SECRETS"
printf 'old-env\n' > "${FIXTURES}/old.env"
printf 'old-cloudflare' > "${SETUP_TX_SECRETS}/cloudflare_tunnel_token.txt"
printf 'old-smtp' > "${SETUP_TX_SECRETS}/smtp_pass.txt"

mkdir -p "${SETUP_TX_DIR}.pending/secrets"
printf 'incomplete-secret-copy' > "${SETUP_TX_DIR}.pending/secrets/smtp_pass.txt"
printf '99999999' > "${SETUP_TX_DIR}.pending/owner_pid"
setup_transaction_owner_state "$SETUP_TX_DIR" pending >/dev/null 2>&1 \
  && rc=0 || rc=$?
expect_eq "an abandoned pending transaction is distinguishable from a live owner" \
  "$rc" "1"
if [ -d "${SETUP_TX_DIR}.pending" ]; then
  pass "an abandoned pending transaction is retained for race-free manual inspection"
else
  printf 'FAIL: abandoned setup transaction staging was deleted automatically\n' >&2
  fail=1
fi
# === abandoned setup staging is quarantined, never deleted ===================
# The staging directory is the setup mutex and holds credential copies. A dead
# owner's staging folder is renamed into the checkout's PARENT: the same
# filesystem (so the rename is atomic), outside every ignore rule, build context
# and repository-cleanliness fence. Every other owner state keeps refusing —
# state 2 covers a live owner whose pid file is not written yet, and releasing
# that mutex would let two setups interleave .env rewrites and secret rotation.
new_staging_repo() {  # new_staging_repo NAME -> echoes a fresh git checkout path
  local repo="${FIXTURES}/${1}/clone"
  mkdir -p "$repo"
  git init -q "$repo"
  printf 'tracked\n' > "${repo}/README"
  git -C "$repo" add README
  git -C "$repo" -c user.email=setup@example.invalid -c user.name=setup \
    commit -qm 'initial' >/dev/null
  printf '%s' "$repo"
}
add_abandoned_staging() {  # add_abandoned_staging REPO [OWNER_PID]
  local pending="${1}/.jarvis-setup-transaction.pending"
  # Mirror acquire_setup_transaction_lock: the lock directory itself is 700.
  mkdir -m 700 "$pending"
  mkdir -m 700 "${pending}/secrets"
  printf 'credential-copy' > "${pending}/secrets/smtp_pass.txt"
  [ -z "${2:-}" ] || printf '%s' "$2" > "${pending}/owner_pid"
}
drive_recovery() {  # drive_recovery REPO NON_INTERACTIVE -> output, stdin closed
  (
    eval "$RECOVER_TRANSACTION_FN"
    SCRIPT_DIR="$1"
    _SETUP_TRANSACTION_DIR="${1}/.jarvis-setup-transaction"
    NON_INTERACTIVE="$2"
    warn() { printf '%s\n' "$*"; }
    info() { printf '%s\n' "$*"; }
    ok()   { printf '%s\n' "$*"; }
    rollback_access_runtime() { cp "${11}" "${12}" && printf 'ROLLBACK_CALLED\n'; }
    cd "$1" || return 1
    recover_interrupted_setup_transaction
  ) < /dev/null 2>&1
}
staging_hold_path() {  # staging_hold_path REPO -> the quarantined path, if any
  find "$(dirname "$1")" -maxdepth 1 -name 'jarvis-abandoned-staging-*' 2>/dev/null \
    | head -1
}

# A piped or CI install has no terminal to answer with, so it moves nothing and
# prints the rename as the operator remedy. It must not abort under set -e.
STAGE_PIPED="$(new_staging_repo staging-piped)"
add_abandoned_staging "$STAGE_PIPED" 99999999
out="$(drive_recovery "$STAGE_PIPED" 0)" && rc=0 || rc=$?
expect_eq "an abandoned pending transaction stops setup" "$rc" "1"
case "$out" in
  *'private credential copies'*'no terminal to ask at'*'mv -- '*'.jarvis-setup-transaction.pending'*'jarvis-abandoned-staging-'*)
    pass "a prompt-less install names the secret boundary and the exact rename remedy" ;;
  *)
    printf 'FAIL: abandoned staging guidance is vague, deletes, or can move secrets into the checkout\n%s\n' \
      "$out" >&2
    fail=1 ;;
esac
expect_eq "the staging folder a prompt-less install cannot ask about stays in place" \
  "$([ -d "${STAGE_PIPED}/.jarvis-setup-transaction.pending" ] && printf present || printf gone)" \
  "present"
expect_eq "an unanswerable prompt never moves credential copies anywhere" \
  "$(staging_hold_path "$STAGE_PIPED")" ""

# --non-interactive is the operator asking for the safe default without a prompt.
STAGE_HELD="$(new_staging_repo staging-held)"
add_abandoned_staging "$STAGE_HELD" 99999999
out="$(drive_recovery "$STAGE_HELD" 1)" && rc=0 || rc=$?
expect_eq "quarantining an abandoned staging folder lets setup continue" "$rc" "0"
STAGE_HOLD="$(staging_hold_path "$STAGE_HELD")"
expect_eq "the quarantined folder lands in the checkout's parent directory" \
  "$(dirname "$STAGE_HOLD")" "$(dirname "$STAGE_HELD")"
case "${STAGE_HOLD}/" in
  "${STAGE_HELD}"/*)
    printf 'FAIL: quarantined staging stayed inside the checkout: %s\n' "$STAGE_HOLD" >&2
    fail=1 ;;
  *) pass "the quarantined folder is not under the checkout" ;;
esac
expect_eq "the quarantined folder keeps its 700 mode" \
  "$(stat -c '%a' "$STAGE_HOLD")" "700"
expect_eq "the credential copies move with it" \
  "$(cat "${STAGE_HOLD}/secrets/smtp_pass.txt")" "credential-copy"
expect_eq "the staging path is released so setup can lock again" \
  "$([ -e "${STAGE_HELD}/.jarvis-setup-transaction.pending" ] && printf present || printf gone)" \
  "gone"
# git status must EXIT 0 and print nothing: outside a repository it fails, and a
# bare command substitution would read that failure as cleanliness.
staging_status="$(git -C "$STAGE_HELD" status --porcelain)" && rc=0 || rc=$?
expect_eq "the checkout's git status command still succeeds" "$rc" "0"
expect_eq "the quarantine leaves the checkout clean for the update fences" \
  "$staging_status" ""

# frozen_date_bin <stamp> -> dir holding a `date` that always reports <stamp>
# (export it onto PATH inside a subshell to use it).
frozen_date_bin() {
  local dir
  dir="$(mktemp -d "${FIXTURES}/bin.XXXXXX")"
  cat > "${dir}/date" <<EOF
#!/usr/bin/env bash
printf '%s\n' '$1'
EOF
  chmod +x "${dir}/date"
  printf '%s' "$dir"
}

# A destination that already exists must stop the rename: mv would move the
# staging folder INSIDE it, hiding credential copies a level deeper than every
# message here says they are.
#
# The quarantine name is `date +%Y%m%d%H%M%S`, and the recovery derives its own
# copy of it. Reading the real clock twice means a second boundary between the
# two reads produces two different names, no collision at all, and a case that
# passes having exercised nothing. Pin the clock so the collision is the
# behaviour under test rather than a coincidence.
STAGE_TAKEN="$(new_staging_repo staging-taken)"
add_abandoned_staging "$STAGE_TAKEN" 99999999
TAKEN_STAMP="20260101000000"
TAKEN_DATE_BIN="$(frozen_date_bin "$TAKEN_STAMP")"
TAKEN_HOLD="$(dirname "$STAGE_TAKEN")/jarvis-abandoned-staging-${TAKEN_STAMP}"
mkdir -p "$TAKEN_HOLD"
out="$(export PATH="${TAKEN_DATE_BIN}:${PATH}"; drive_recovery "$STAGE_TAKEN" 1)" && rc=0 || rc=$?
expect_eq "a taken quarantine name stops setup instead of nesting the folder" "$rc" "1"
expect_eq "the staging folder stays where it is when the name is taken" \
  "$([ -d "${STAGE_TAKEN}/.jarvis-setup-transaction.pending" ] && printf present || printf gone)" \
  "present"
expect_eq "nothing is nested inside the existing quarantine" \
  "$([ -e "${TAKEN_HOLD}/.jarvis-setup-transaction.pending" ] && printf nested || printf clean)" \
  "clean"
case "$out" in
  *'already holds that name'*) pass "the taken-name refusal names the path to resolve" ;;
  *)
    printf 'FAIL: a taken quarantine name produced no actionable message\n%s\n' "$out" >&2
    fail=1 ;;
esac

# An unreadable owner record includes a LIVE owner: the lock directory exists
# before its pid file is written. Releasing it there is a concurrent-setup bug.
STAGE_NOPID="$(new_staging_repo staging-nopid)"
add_abandoned_staging "$STAGE_NOPID"
out="$(drive_recovery "$STAGE_NOPID" 1)" && rc=0 || rc=$?
expect_eq "staging with no owner record stops setup instead of continuing" "$rc" "1"
expect_eq "staging with no owner record is never moved aside" \
  "$(staging_hold_path "$STAGE_NOPID")" ""
case "$out" in
  *'unreadable owner record'*'mv -- '*)
    pass "an unreadable owner record is reported with a manual remedy" ;;
  *)
    printf 'FAIL: unreadable owner state lacks its refusal or remedy\n%s\n' "$out" >&2
    fail=1 ;;
esac

# A live owner keeps the mutex it holds.
STAGE_LIVE="$(new_staging_repo staging-live)"
add_abandoned_staging "$STAGE_LIVE" "$$"
out="$(drive_recovery "$STAGE_LIVE" 1)" && rc=0 || rc=$?
expect_eq "a live owner's staging folder stops setup" "$rc" "1"
expect_eq "a live owner's staging folder is never moved aside" \
  "$(staging_hold_path "$STAGE_LIVE")" ""
case "$out" in
  *'Another setup process is still running'*)
    pass "a live staging owner is reported as a concurrent setup" ;;
  *)
    printf 'FAIL: live staging owner refusal lacks concurrent-setup guidance\n%s\n' \
      "$out" >&2
    fail=1 ;;
esac

# Quarantining must CONTINUE the recovery, not end it: an interrupted recovery
# leaves a dead-owner staging folder beside an unrecovered journal, and returning
# early there would start setup against a half-mutated install.
STAGE_BOTH="$(new_staging_repo staging-both)"
printf 'old-env-bytes\n' > "${STAGE_BOTH}/old.env"
mkdir -p "${STAGE_BOTH}/live-secrets"
begin_setup_transaction "${STAGE_BOTH}/.jarvis-setup-transaction" \
  "${STAGE_BOTH}/old.env" "${STAGE_BOTH}/live-secrets" \
  tunnel tunnel 3017 https://old.example 3001 letsencrypt letsencrypt 3029
printf '99999999' > "${STAGE_BOTH}/.jarvis-setup-transaction/owner_pid"
add_abandoned_staging "$STAGE_BOTH" 99999999
printf 'interrupted-env\n' > "${STAGE_BOTH}/.env"
out="$(drive_recovery "$STAGE_BOTH" 1)" && rc=0 || rc=$?
expect_eq "a quarantine followed by a live journal recovers it" "$rc" "0"
case "$out" in
  *'Restoring its previous configuration'*ROLLBACK_CALLED*)
    pass "quarantining falls through to the interrupted journal's recovery" ;;
  *)
    printf 'FAIL: the interrupted journal was not recovered after the quarantine\n%s\n' \
      "$out" >&2
    fail=1 ;;
esac
expect_eq "the interrupted run's previous configuration is restored" \
  "$(cat "${STAGE_BOTH}/.env")" "old-env-bytes"

begin_setup_transaction "$SETUP_TX_DIR" "${FIXTURES}/old.env" \
  "$SETUP_TX_SECRETS" tunnel tunnel 3017 https://old.example 3001 \
  letsencrypt letsencrypt 3029 >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "an unresolved pending transaction blocks a new snapshot" "$rc" "4"
rm -rf "${SETUP_TX_DIR}.pending"

RECOVERY_LOCK_DIR="${FIXTURES}/recovery-lock/.jarvis-setup-transaction"
RECOVERY_LOCK_LOG="${FIXTURES}/recovery-lock.log"
mkdir -p "$(dirname "$RECOVERY_LOCK_DIR")"
: > "$RECOVERY_LOCK_LOG"
acquire_setup_transaction_lock "$RECOVERY_LOCK_DIR"
printf 'holder-rollback\n' >> "$RECOVERY_LOCK_LOG"
(
  if acquire_setup_transaction_lock "$RECOVERY_LOCK_DIR"; then
    printf 'contender-rollback\n' >> "$RECOVERY_LOCK_LOG"
    release_setup_transaction_lock "$RECOVERY_LOCK_DIR"
  fi
)
expect_eq "two recovery contenders allow only the atomic lock holder to roll back" \
  "$(cat "$RECOVERY_LOCK_LOG")" "holder-rollback"
release_setup_transaction_lock "$RECOVERY_LOCK_DIR"

mkdir -p "${SETUP_TX_DIR}.pending"
printf '%s' "$$" > "${SETUP_TX_DIR}.pending/owner_pid"
setup_transaction_owner_state "$SETUP_TX_DIR" pending >/dev/null 2>&1 \
  && rc=0 || rc=$?
expect_eq "a transaction staging directory reports its live owner" "$rc" "0"
if [ -d "${SETUP_TX_DIR}.pending" ]; then
  pass "checking a live setup process leaves its staging directory intact"
else
  printf 'FAIL: live setup transaction staging directory was deleted\n' >&2
  fail=1
fi
rm -rf "${SETUP_TX_DIR}.pending"

printf 'not-a-directory' > "${SETUP_TX_DIR}.pending"
setup_transaction_owner_state "$SETUP_TX_DIR" pending >/dev/null 2>&1 \
  && rc=0 || rc=$?
expect_eq "an unexpected pending-transaction file is invalid" "$rc" "2"
expect_eq "checking an unexpected pending-transaction file never deletes it" \
  "$(cat "${SETUP_TX_DIR}.pending")" "not-a-directory"
rm -f "${SETUP_TX_DIR}.pending"

begin_setup_transaction "$SETUP_TX_DIR" "${FIXTURES}/old.env" \
  "$SETUP_TX_SECRETS" tunnel 'tunnel,telegram' 3017 https://old.example 3001 \
  letsencrypt letsencrypt 3029
if [ ! -e "${SETUP_TX_DIR}.pending" ]; then
  pass "completed setup transaction leaves no credential-bearing staging directory"
else
  printf 'FAIL: completed setup transaction retained its staging directory\n' >&2
  fail=1
fi
expect_eq "durable setup transaction records the old route identity" \
  "$(setup_transaction_value "$SETUP_TX_DIR" old_mode)|$(setup_transaction_value "$SETUP_TX_DIR" old_origin)" \
  "tunnel|https://old.example"
setup_transaction_owner_state "$SETUP_TX_DIR" active >/dev/null 2>&1 \
  && rc=0 || rc=$?
expect_eq "an active transaction journal reports its live setup owner" "$rc" "0"
out="$(
  (
    eval "$RECOVER_TRANSACTION_FN"
    _SETUP_TRANSACTION_DIR="$SETUP_TX_DIR"
    NON_INTERACTIVE=1
    warn() { printf '%s\n' "$*"; }
    rollback_access_runtime() { printf 'ROLLBACK_CALLED\n'; return 0; }
    recover_interrupted_setup_transaction
  ) 2>&1
)" && rc=0 || rc=$?
expect_eq "a second setup refuses to recover another live setup's active journal" \
  "$rc" "1"
case "$out" in
  *'Another setup process is still running'* )
    pass "live active-journal refusal explains the concurrent setup" ;;
  *)
    printf 'FAIL: live active-journal refusal lacks concurrent-setup guidance\n%s\n' \
      "$out" >&2
    fail=1 ;;
esac
case "$out" in
  *ROLLBACK_CALLED*)
    printf 'FAIL: a second setup rolled back a live setup transaction\n' >&2
    fail=1 ;;
  *) pass "a live active journal is never rolled back by a second setup" ;;
esac
expect_eq "durable setup transaction starts before any Tailscale mutation" \
  "$(setup_transaction_value "$SETUP_TX_DIR" tailscale_attempted)" "0"
mark_setup_transaction_tailscale_attempted "$SETUP_TX_DIR"
expect_eq "durable setup transaction records a possibly-applied Tailscale mutation" \
  "$(setup_transaction_value "$SETUP_TX_DIR" tailscale_attempted)" "1"
begin_setup_transaction "$SETUP_TX_DIR" "${FIXTURES}/old.env" \
  "$SETUP_TX_SECRETS" localhost '' 3003 '' 3001 tunnel tunnel 3003 \
  >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "an unresolved transaction journal cannot be overwritten by a rerun" \
  "$rc/$(setup_transaction_value "$SETUP_TX_DIR" old_mode)" "3/tunnel"

printf 'new-cloudflare' > "${SETUP_TX_SECRETS}/cloudflare_tunnel_token.txt"
printf 'new-smtp' > "${SETUP_TX_SECRETS}/smtp_pass.txt"
printf 'new-telegram' > "${SETUP_TX_SECRETS}/telegram_bot_token.txt"
restore_setup_secret_snapshot "${SETUP_TX_DIR}/secrets" "$SETUP_TX_SECRETS"
expect_eq "transaction rollback restores present credential bytes" \
  "$(cat "${SETUP_TX_SECRETS}/cloudflare_tunnel_token.txt")|$(cat "${SETUP_TX_SECRETS}/smtp_pass.txt")" \
  "old-cloudflare|old-smtp"
if [ ! -e "${SETUP_TX_SECRETS}/telegram_bot_token.txt" ]; then
  pass "transaction rollback restores an originally absent credential as absent"
else
  printf 'FAIL: transaction rollback retained an originally absent Telegram credential\n' >&2
  fail=1
fi
discard_setup_transaction "$SETUP_TX_DIR"
if [ ! -e "$SETUP_TX_DIR" ]; then
  pass "committing or completing rollback removes duplicate credential snapshots"
else
  printf 'FAIL: setup transaction snapshot was not removed\n' >&2
  fail=1
fi

transaction_path_is_ignored() {
  local repo_root="${SCRIPT_DIR}/../.." path="$1"
  if git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$repo_root" check-ignore --no-index -q "$path"
  else
    grep -Fxq "$path" "${repo_root}/.gitignore"
  fi
}

for ignored_transaction_path in \
    .jarvis-setup-transaction .jarvis-setup-transaction.pending; do
  if transaction_path_is_ignored "$ignored_transaction_path"; then
    pass "git ignores file- or directory-shaped ${ignored_transaction_path}"
  else
    printf 'FAIL: git can stage retained transaction path %s\n' \
      "$ignored_transaction_path" >&2
    fail=1
  fi
done

# Setup, update, backup, restore and key rotation each write machine-local state
# next to the checkout. A single orphaned one makes the tree unclean, and an
# unclean tree refuses every later update, so each must be ignored and untracked.
machine_local_residue=(
  secrets/manifest-hmac-required
  secrets/example.txt.restore.123456
  secrets/jarvis_config_key_rotation_state.txt.123456
  .env.restore.123456
  .env.a1b2c3
  litellm/.config.yaml.123456
  shared/local_pdfs/notes.txt
)
residue_repo_root="${SCRIPT_DIR}/../.."
if git -C "$residue_repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  for residue_path in "${machine_local_residue[@]}"; do
    if ! git -C "$residue_repo_root" check-ignore --no-index -q "$residue_path"; then
      printf 'FAIL: git can stage machine-local residue %s\n' "$residue_path" >&2
      fail=1
    elif git -C "$residue_repo_root" ls-files --error-unmatch "$residue_path" \
        >/dev/null 2>&1; then
      printf 'FAIL: machine-local residue %s is tracked\n' "$residue_path" >&2
      fail=1
    else
      pass "git ignores and does not track ${residue_path}"
    fi
  done
  if git -C "$residue_repo_root" check-ignore --no-index -q .env.example; then
    printf 'FAIL: .env.example is ignored; the residue rules are too broad\n' >&2
    fail=1
  else
    pass ".env.example stays visible to git"
  fi
else
  printf 'NOTE: skipping the machine-local residue contract, no work tree\n' >&2
fi

# === access reconfiguration rollback =========================================
# A failed replacement is a live-runtime transaction, not only a file restore.
# Stub every side effect so the log proves the required order without touching
# Docker, Tailscale, credentials, or the host network.
ROLLBACK_COMMAND_LOG="${FIXTURES}/access-runtime-rollback.log"

run_rollback_order_case() {
  local old_mode="$1" old_profiles="$2" old_port="$3" old_origin="$4"
  local new_mode="$5" new_profiles="$6" new_port="$7" new_tailscale_attempted="$8"
  : > "$ROLLBACK_COMMAND_LOG"
  (
    access_rollback_compose() {
      shift 2
      printf 'docker compose %s\n' "$*" >> "$ROLLBACK_COMMAND_LOG"
    }
    tailscale_serve_https_off() {
      printf 'tailscale-off %s %s\n' "$1" "$2" >> "$ROLLBACK_COMMAND_LOG"
    }
    tailscale_serve_https() {
      printf 'tailscale-on %s %s\n' "$1" "$2" >> "$ROLLBACK_COMMAND_LOG"
    }
    restore_env_snapshot() {
      printf 'restore-env %s %s\n' "$1" "$2" >> "$ROLLBACK_COMMAND_LOG"
    }
    restore_secret_from_env() {
      printf 'restore-secret %s %s %s\n' "$1" "$2" "$3" >> "$ROLLBACK_COMMAND_LOG"
    }
    wait_for_jarvis_marker() {
      printf 'verify %s\n' "$1" >> "$ROLLBACK_COMMAND_LOG"
    }
    rollback_access_runtime "$old_mode" "$old_profiles" "$old_port" \
      "$old_origin" 3001 "$new_mode" "$new_profiles" "$new_port" \
      "$new_tailscale_attempted" 1 previous.env current.env tunnel-secret "$FIXTURES"
  )
}

run_rollback_order_case tunnel tunnel 3003 https://old.example \
  letsencrypt letsencrypt 3003 0 && rc=0 || rc=$?
expect_eq "old tunnel -> failed new route rollback succeeds" "$rc" "0"
expect_eq "old tunnel rollback removes replacement, restores files, then reapplies and verifies old edge" \
  "$(cat "$ROLLBACK_COMMAND_LOG")" \
  "docker compose --profile letsencrypt rm -sf caddy
restore-env previous.env current.env
restore-secret current.env CLOUDFLARE_TUNNEL_TOKEN tunnel-secret
docker compose --profile tunnel up -d --no-build --force-recreate --no-deps dashboard cloudflared
verify http://127.0.0.1:3001/health/jarvis
verify https://old.example/health/jarvis"

run_rollback_order_case tunnel tunnel 3003 https://old.example \
  tailscale '' 3029 1 && rc=0 || rc=$?
expect_eq "failed Tailscale invocation with a possibly-applied route rolls back safely" "$rc" "0"
expect_eq "attempted Tailscale replacement is ownership-checked before persisted state is restored" \
  "$(cat "$ROLLBACK_COMMAND_LOG")" \
  "tailscale-off 3029 1
restore-env previous.env current.env
restore-secret current.env CLOUDFLARE_TUNNEL_TOKEN tunnel-secret
docker compose --profile tunnel up -d --no-build --force-recreate --no-deps dashboard cloudflared
verify http://127.0.0.1:3001/health/jarvis
verify https://old.example/health/jarvis"

run_rollback_order_case tailscale '' 3017 https://old.tailnet.ts.net \
  tunnel tunnel 3003 0 && rc=0 || rc=$?
expect_eq "old Tailscale -> failed tunnel rollback succeeds" "$rc" "0"
expect_eq "old Tailscale rollback removes tunnel, restores dashboard, then reapplies Serve" \
  "$(cat "$ROLLBACK_COMMAND_LOG")" \
  "docker compose --profile tunnel rm -sf cloudflared
restore-env previous.env current.env
restore-secret current.env CLOUDFLARE_TUNNEL_TOKEN tunnel-secret
docker compose up -d --no-build --force-recreate --no-deps dashboard
tailscale-on 3017 1
verify http://127.0.0.1:3001/health/jarvis
verify https://old.tailnet.ts.net/health/jarvis"

run_rollback_order_case tunnel tunnel 3003 https://old.example \
  tunnel tunnel 3003 0 && rc=0 || rc=$?
expect_eq "same-mode failed tunnel rollback succeeds" "$rc" "0"
expect_eq "same-mode rollback replaces the live edge instead of trusting restored files alone" \
  "$(cat "$ROLLBACK_COMMAND_LOG")" \
  "docker compose --profile tunnel rm -sf cloudflared
restore-env previous.env current.env
restore-secret current.env CLOUDFLARE_TUNNEL_TOKEN tunnel-secret
docker compose --profile tunnel up -d --no-build --force-recreate --no-deps dashboard cloudflared
verify http://127.0.0.1:3001/health/jarvis
verify https://old.example/health/jarvis"

# Exercise setup.sh's main-Compose failure wrapper with the real transactional
# helper underneath it. The first docker call represents a partial `up` failure;
# every later effect is the rollback and must precede the terminal error.
: > "$ROLLBACK_COMMAND_LOG"
(
  eval "$(sed -n '/^compose_up_or_recover() {/,/^}/p' "$SETUP_SCRIPT")"
  docker() {
    printf 'main-compose %s\n' "$*" >> "$ROLLBACK_COMMAND_LOG"
    return 23
  }
  access_rollback_compose() {
    shift 2
    printf 'docker compose %s\n' "$*" >> "$ROLLBACK_COMMAND_LOG"
  }
  restore_env_snapshot() {
    printf 'restore-env %s %s\n' "$1" "$2" >> "$ROLLBACK_COMMAND_LOG"
  }
  restore_secret_from_env() {
    printf 'restore-secret %s %s %s\n' "$1" "$2" "$3" >> "$ROLLBACK_COMMAND_LOG"
  }
  wait_for_jarvis_marker() { printf 'verify %s\n' "$1" >> "$ROLLBACK_COMMAND_LOG"; }
  die_enospc_aware() { printf 'terminal-error\n' >> "$ROLLBACK_COMMAND_LOG"; return 1; }
  rollback_unverified_access_config() {
    rollback_access_runtime tunnel tunnel 3003 https://old.example 3001 \
      letsencrypt letsencrypt 3003 0 1 previous.env current.env tunnel-secret "$FIXTURES"
  }
  COMPOSE_OVERLAY=()
  NON_INTERACTIVE=1
  _ACCESS_RECONFIGURATION_APPLY_STARTED=1
  compose_up_or_recover "main apply failed" "inspect logs" up -d
) >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "partial main Compose failure remains nonzero after access rollback" "$rc" "1"
expect_eq "partial main Compose failure restores old files and live edge before terminating" \
  "$(cat "$ROLLBACK_COMMAND_LOG")" \
  "main-compose compose up -d
docker compose --profile letsencrypt rm -sf caddy
restore-env previous.env current.env
restore-secret current.env CLOUDFLARE_TUNNEL_TOKEN tunnel-secret
docker compose --profile tunnel up -d --no-build --force-recreate --no-deps dashboard cloudflared
verify http://127.0.0.1:3001/health/jarvis
verify https://old.example/health/jarvis
terminal-error"

UNVERIFIED_EXIT_FN="$(sed -n '/^unverified_https_exit() {/,/^}/p' "$SETUP_SCRIPT")"
out="$(
  (
    eval "$UNVERIFIED_EXIT_FN"
    C_BOLD=''; C_RESET=''; C_YELLOW=''
    _ENV_SNAPSHOT_TAKEN=1
    rollback_unverified_access_config() { return 1; }
    wait_for_jarvis_marker() { return 0; }
    unverified_https_exit HTTPS https://new.example 3001 ./setup.sh
  ) 2>&1
)" && rc=0 || rc=$?
expect_eq "unverified route with incomplete rollback exits nonzero" "$rc" "1"
case "$out" in
  *'Local access state is uncertain because recovery was not verified.'*)
    pass "incomplete rollback reports uncertain local access" ;;
  *)
    printf 'FAIL: incomplete rollback does not report uncertain local access\n' >&2
    fail=1 ;;
esac
case "$out" in
  *'Local services are healthy and usable'*)
    printf 'FAIL: incomplete rollback still claims healthy local access\n' >&2
    fail=1 ;;
  *) pass "incomplete rollback makes no unverified local-health claim" ;;
esac

REAL_ROLLBACK_GUIDANCE_FN="$(sed -n '/^rollback_unverified_access_config() {/,/^}/p' "$SETUP_SCRIPT")"
MANUAL_TX_DIR="${FIXTURES}/manual/.jarvis-setup-transaction"
mkdir -p "$MANUAL_TX_DIR/secrets"
printf active > "$MANUAL_TX_DIR/active"
printf 'old-env\n' > "$MANUAL_TX_DIR/old.env"
out="$(
  (
    eval "$REAL_ROLLBACK_GUIDANCE_FN"
    warn() { printf '%s\n' "$*"; }
    rollback_access_runtime() { return 1; }
    _ENV_SNAPSHOT_TAKEN=1
    _SETUP_TRANSACTION_DIR="$MANUAL_TX_DIR"
    _PREVIOUS_ACCESS_MODE=localhost
    _PREVIOUS_COMPOSE_PROFILES=caddy-local
    _PREVIOUS_TAILSCALE_PORT=3003
    _PREVIOUS_APP_BASE_URL=''
    _PREVIOUS_DASHBOARD_HOST_PORT=3001
    ACCESS_MODE_LABEL=tunnel
    COMPOSE_PROFILES_VALUE=tunnel
    DASHBOARD_TRUSTED_HOST_PORT_RESOLVED=3003
    _REPLACEMENT_TAILSCALE_ATTEMPTED=0
    NON_INTERACTIVE=1
    SCRIPT_DIR="$FIXTURES"
    rollback_unverified_access_config
  ) 2>&1
)" && rc=0 || rc=$?
expect_eq "real incomplete rollback guidance remains nonzero" "$rc" "1"
case "$out" in
  *'rm -sf cloudflared'*'cp .jarvis-setup-transaction/old.env .env'*'restore_setup_secret_snapshot .jarvis-setup-transaction/secrets ./secrets'*'up -d --no-build --force-recreate --no-deps dashboard'*'--profile caddy-local up -d --no-build --force-recreate --no-deps caddy_local'*'curl -fsS http://127.0.0.1:3001/health/jarvis'*'--cacert "$(mkcert -CAROOT)/rootCA.pem"'*'curl -fsS https://localhost:3443/health/jarvis'*)
    pass "manual recovery orders new-edge cleanup, all credential restore, old edge recreation, and exact probes" ;;
  *)
    printf 'FAIL: real incomplete rollback guidance is missing or out of order\n%s\n' "$out" >&2
    fail=1 ;;
esac

UNVERIFIED_CLEANUP_LOG="${FIXTURES}/unverified-cleanup.log"
: > "$UNVERIFIED_CLEANUP_LOG"
out="$(
  (
    eval "$UNVERIFIED_EXIT_FN"
    C_BOLD=''; C_RESET=''; C_YELLOW=''
    _ENV_SNAPSHOT_TAKEN=0
    _ENV_EXISTED_AT_START=0
    ACCESS_MODE_LABEL=tunnel
    COMPOSE_PROFILES_VALUE=tunnel
    DASHBOARD_TRUSTED_HOST_PORT_RESOLVED=3003
    NON_INTERACTIVE=1
    SCRIPT_DIR="$FIXTURES"
    rollback_unverified_access_config() { return 1; }
    remove_attempted_access_runtime() {
      printf 'cleanup %s %s\n' "$1" "$2" >> "$UNVERIFIED_CLEANUP_LOG"
    }
    wait_for_jarvis_marker() { return 0; }
    unverified_https_exit HTTPS https://new.example 3001 ./setup.sh
  ) 2>&1
)" && rc=0 || rc=$?
case "$out" in
  *'Local services are healthy and usable on this computer.'*)
    pass "fresh install claims local availability only after its exact marker probe" ;;
  *)
    printf 'FAIL: verified fresh-install localhost is not reported usable\n' >&2
    fail=1 ;;
esac
expect_eq "fresh unverified route removes its attempted JARVIS edge before exit" \
  "$(cat "$UNVERIFIED_CLEANUP_LOG")" "cleanup tunnel tunnel"

: > "$UNVERIFIED_CLEANUP_LOG"
(
  eval "$UNVERIFIED_EXIT_FN"
  eval "$(sed -n '/^cleanup_setup_exit() {/,/^}/p' "$SETUP_SCRIPT")"
  C_BOLD=''; C_RESET=''; C_YELLOW=''
  TMP_ENV="${FIXTURES}/does-not-exist"
  _ENV_SNAPSHOT_TAKEN=0
  _ENV_EXISTED_AT_START=0
  _ACCESS_TRANSACTION_COMMITTED=0
  _ACCESS_ROLLBACK_ATTEMPTED=0
  ACCESS_MODE_LABEL=tunnel
  COMPOSE_PROFILES_VALUE=tunnel
  DASHBOARD_TRUSTED_HOST_PORT_RESOLVED=3003
  _REPLACEMENT_TAILSCALE_ATTEMPTED=0
  NON_INTERACTIVE=1
  SCRIPT_DIR="$FIXTURES"
  remove_attempted_access_runtime() {
    printf 'cleanup %s %s\n' "$1" "$2" >> "$UNVERIFIED_CLEANUP_LOG"
  }
  wait_for_jarvis_marker() { return 0; }
  trap cleanup_setup_exit EXIT
  unverified_https_exit HTTPS https://new.example 3001 ./setup.sh
) >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "fresh unverified-route failure does not run edge cleanup twice through EXIT" \
  "$rc/$(cat "$UNVERIFIED_CLEANUP_LOG")" "1/cleanup tunnel tunnel"

: > "$UNVERIFIED_CLEANUP_LOG"
out="$(
  (
    eval "$UNVERIFIED_EXIT_FN"
    C_BOLD=''; C_RESET=''; C_YELLOW=''
    _ENV_SNAPSHOT_TAKEN=0
    _ENV_EXISTED_AT_START=1
    ACCESS_MODE_LABEL=tunnel
    COMPOSE_PROFILES_VALUE=tunnel
    SCRIPT_DIR="$FIXTURES"
    remove_attempted_access_runtime() { printf 'unexpected-cleanup\n' >> "$UNVERIFIED_CLEANUP_LOG"; }
    wait_for_jarvis_marker() { return 0; }
    unverified_https_exit HTTPS https://existing.example 3001 ./setup.sh
  ) 2>&1
)" && rc=0 || rc=$?
expect_eq "keep-existing rerun does not remove a pre-existing unverified edge" \
  "$(cat "$UNVERIFIED_CLEANUP_LOG")" ""

out="$(
  (
    eval "$UNVERIFIED_EXIT_FN"
    C_BOLD=''; C_RESET=''; C_YELLOW=''
    _ENV_SNAPSHOT_TAKEN=0
    _ENV_EXISTED_AT_START=0
    ACCESS_MODE_LABEL=localhost
    COMPOSE_PROFILES_VALUE=''
    DASHBOARD_TRUSTED_HOST_PORT_RESOLVED=3003
    NON_INTERACTIVE=1
    SCRIPT_DIR="$FIXTURES"
    rollback_unverified_access_config() { return 1; }
    remove_attempted_access_runtime() { return 0; }
    wait_for_jarvis_marker() { return 1; }
    unverified_https_exit HTTPS https://new.example 3001 ./setup.sh
  ) 2>&1
)" && rc=0 || rc=$?
case "$out" in
  *'The local dashboard marker could not be verified.'*'docker compose logs --tail=200 dashboard'*)
    pass "fresh install with an unverified localhost marker prints fresh-install diagnostics" ;;
  *)
    printf 'FAIL: fresh install without a local marker prints the wrong recovery guidance\n' >&2
    fail=1 ;;
esac

CLEANUP_EXIT_FN="$(sed -n '/^cleanup_setup_exit() {/,/^}/p' "$SETUP_SCRIPT")"
EXIT_TRANSACTION_LOG="${FIXTURES}/setup-exit-transaction.log"
: > "$EXIT_TRANSACTION_LOG"
(
  eval "$CLEANUP_EXIT_FN"
  warn() { :; }
  TMP_ENV="${FIXTURES}/does-not-exist"
  _ENV_SNAPSHOT_TAKEN=1
  _ACCESS_TRANSACTION_COMMITTED=0
  _ACCESS_ROLLBACK_ATTEMPTED=0
  rollback_unverified_access_config() {
    printf 'rollback\n' >> "$EXIT_TRANSACTION_LOG"
  }
  trap cleanup_setup_exit EXIT
  exit 23
) >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "any failed reconfiguration exit runs rollback and preserves its exit code" \
  "$rc/$(cat "$EXIT_TRANSACTION_LOG")" "23/rollback"

: > "$EXIT_TRANSACTION_LOG"
(
  eval "$CLEANUP_EXIT_FN"
  warn() { :; }
  TMP_ENV="${FIXTURES}/does-not-exist"
  _ENV_SNAPSHOT_TAKEN=0
  _ENV_EXISTED_AT_START=0
  _ACCESS_TRANSACTION_COMMITTED=0
  _ACCESS_ROLLBACK_ATTEMPTED=0
  ACCESS_MODE_LABEL=tunnel
  COMPOSE_PROFILES_VALUE=tunnel
  DASHBOARD_TRUSTED_HOST_PORT_RESOLVED=3003
  _REPLACEMENT_TAILSCALE_ATTEMPTED=0
  NON_INTERACTIVE=1
  SCRIPT_DIR="$FIXTURES"
  remove_attempted_access_runtime() {
    printf 'fresh-cleanup %s %s\n' "$1" "$2" >> "$EXIT_TRANSACTION_LOG"
  }
  trap cleanup_setup_exit EXIT
  exit 17
) >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "any failed fresh install removes its selected edge once and preserves its exit code" \
  "$rc/$(cat "$EXIT_TRANSACTION_LOG")" "17/fresh-cleanup tunnel tunnel"

# Hostile exported Compose selectors must not redirect rollback to another
# project or config. The boundary runs in a subshell, so the caller stays intact.
ROLLBACK_COMPOSE_BIN="$(mktemp -d "${FIXTURES}/rollback-compose-bin.XXXXXX")"
ROLLBACK_COMPOSE_LOG="${FIXTURES}/rollback-compose-env.log"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "cwd=%s file=%s project=%s profiles=%s separator=%s envfiles=%s disable=%s args=%s\n" "$PWD" "${COMPOSE_FILE-unset}" "${COMPOSE_PROJECT_NAME-unset}" "${COMPOSE_PROFILES-unset}" "${COMPOSE_PATH_SEPARATOR-unset}" "${COMPOSE_ENV_FILES-unset}" "${COMPOSE_DISABLE_ENV_FILE-unset}" "$*" > "$ROLLBACK_COMPOSE_LOG"' \
  > "${ROLLBACK_COMPOSE_BIN}/docker"
chmod +x "${ROLLBACK_COMPOSE_BIN}/docker"
(
  export PATH="${ROLLBACK_COMPOSE_BIN}:${PATH}"
  export ROLLBACK_COMPOSE_LOG COMPOSE_FILE=/tmp/hostile.yml
  export COMPOSE_PROJECT_NAME=not-jarvis COMPOSE_PROFILES=hostile
  export COMPOSE_PATH_SEPARATOR=';' COMPOSE_ENV_FILES=/tmp/hostile.env
  export COMPOSE_DISABLE_ENV_FILE=1
  access_rollback_compose "$FIXTURES" "$FIXTURES/current.env" ps
)
expect_eq "access rollback clears ambient Compose selectors and pins the JARVIS project directory" \
  "$(cat "$ROLLBACK_COMPOSE_LOG")" \
  "cwd=${FIXTURES} file=unset project=unset profiles=unset separator=unset envfiles=unset disable=unset args=compose --project-directory ${FIXTURES} --env-file ${FIXTURES}/current.env ps"

got="$(
  export COMPOSE_FILE=/tmp/hostile.yml COMPOSE_PROJECT_NAME=not-jarvis
  export COMPOSE_PROFILES=hostile COMPOSE_PATH_SEPARATOR=';'
  export COMPOSE_ENV_FILES=/tmp/hostile.env COMPOSE_DISABLE_ENV_FILE=1
  sanitize_compose_environment
  printf '%s|%s|%s|%s|%s|%s' "${COMPOSE_FILE-unset}" \
    "${COMPOSE_PROJECT_NAME-unset}" "${COMPOSE_PROFILES-unset}" \
    "${COMPOSE_PATH_SEPARATOR-unset}" "${COMPOSE_ENV_FILES-unset}" \
    "${COMPOSE_DISABLE_ENV_FILE-unset}"
)"
expect_eq "setup Compose sanitizer clears every caller selector" "$got" \
  "unset|unset|unset|unset|unset|unset"

# === setup.sh access-mode / ingress wiring (static) ==========================

scheck "sub_value emits DASHBOARD_SERVER_NAME" 'DASHBOARD_SERVER_NAME\)'
scheck "the LAN arm adds the LAN IP to the Host allowlist" '_append_server_name "\$DASHBOARD_SERVER_NAME_VALUE" "\$LAN_IP"'
scheck "the tunnel arm adds the tunnel hostname to the Host allowlist" '_append_server_name "\$DASHBOARD_SERVER_NAME_VALUE" "\$TUNNEL_HOSTNAME"'
scheck "the LAN reachability probe targets http, not https" '_lan_probe_url="http://'
scheck "the LAN probe checks the exact JARVIS marker" '_lan_probe_url="http://.*health/jarvis'
scheck "setup.sh parses --address" '\-\-address\)'
scheck "setup.sh parses --public-origin" '\-\-public-origin\)'
scheck "setup.sh offers Tailscale as a first-class access choice" '3\) From your private Tailscale network'
scheck "missing Tailscale gets a reviewed package-install plan" 'Guided Tailscale installer would run:'
scheck "the reviewed Tailscale plan uses the shared command runner" '_run_prereq_plan "\$plan"'
scheck "non-interactive Tailscale installation requires explicit prerequisite consent" 'Re-run with --install-prereqs after reviewing the commands above'
scheck "Tailscale target port is resolved through persisted .env precedence" 'DASHBOARD_TRUSTED_HOST_PORT_RESOLVED="\$\(_port_or_default DASHBOARD_TRUSTED_HOST_PORT 3003\)"'
scheck "Tailscale Serve uses the persisted custom trusted-listener port" 'tailscale_serve_https "\$DASHBOARD_TRUSTED_HOST_PORT_RESOLVED" "\$NON_INTERACTIVE"'
scheck "a Tailscale Serve failure preserves the localhost fallback" 'Tailscale Serve could not be configured\. Localhost remains available'
scheck "Tailscale verification requires the exact app marker" 'probe_external_app "https://\$TAILSCALE_HOSTNAME/health/jarvis"'
scheck "public-origin feeds APP_BASE_URL" 'APP_BASE_URL_VALUE="\$NI_PUBLIC_ORIGIN"'
scheck "public-origin feeds CORS_ORIGINS" '_append_csv "\$CORS_ORIGINS_OVERRIDE" "\$NI_PUBLIC_ORIGIN"'
scheck "public-origin feeds the Host allowlist" '_append_server_name "\$DASHBOARD_SERVER_NAME_VALUE" "\$PUBLIC_ORIGIN_HOST"'
scheck "a reconfigure snapshots the prior access mode before replacing .env" '_PREVIOUS_ACCESS_MODE="\$\(existing_env_value JARVIS_ACCESS_MODE \|\| true\)"'
scheck "a reconfigure snapshots prior compose profiles before replacing .env" '_PREVIOUS_COMPOSE_PROFILES="\$\(existing_env_value COMPOSE_PROFILES \|\| true\)"'
scheck "a successful mode change retires the previous access edge" 'access_edge_retirements "\$_PREVIOUS_ACCESS_MODE" "\$_PREVIOUS_COMPOSE_PROFILES"'
scheck "failed replacement rolls back the live JARVIS access runtime" 'rollback_access_runtime '
scheck "an incomplete runtime rollback retains private recovery snapshots" 'Automatic runtime rollback was incomplete; recovery snapshots were retained'
scheck "hard-interrupted setup is recovered before a new baseline is read" '^recover_interrupted_setup_transaction '
scheck "a corrupt transaction journal is retained instead of overwritten" 'journal is incomplete or corrupt; it was not overwritten or deleted'
scheck "the durable journal snapshots every setup-mutated credential" 'begin_setup_transaction "\$_SETUP_TRANSACTION_DIR"'
scheck "same-mode tunnel changes force-recreate cloudflared" '--no-build --force-recreate --no-deps cloudflared'
scheck "fresh local HTTPS uses a trusted exact-marker gate" 'wait_for_local_https_marker '
scheck "fresh local HTTPS feeds the selected-route gate" 'local-https\) _SELECTED_HTTPS_STATE="\$LOCAL_HTTPS_APP_STATE"'
scheck "keep-existing local HTTPS uses the same trust probe" '_keep_edge_state="\$\(probe_local_https_app '
_ts_attempt_ln="$(sline '^[[:space:]]*_REPLACEMENT_TAILSCALE_ATTEMPTED=1$')"
_ts_call_ln="$(sline '^[[:space:]]*if tailscale_serve_https ' )"
if [ -n "$_ts_attempt_ln" ] && [ -n "$_ts_call_ln" ] \
   && [ "$_ts_attempt_ln" -lt "$_ts_call_ln" ]; then
  pass "Tailscale rollback treats any Serve invocation as a possible mutation"
else
  printf 'FAIL: Tailscale mutation-attempt flag is not set before Serve (%s vs %s)\n' \
    "$_ts_attempt_ln" "$_ts_call_ln" >&2
  fail=1
fi
scheck "main Compose apply is marked mutation-unknown before invocation" '_ACCESS_RECONFIGURATION_APPLY_STARTED=1'
scheck "setup sanitizes caller Compose selectors before preflight" '^sanitize_compose_environment$'
_compose_sanitize_ln="$(sline '^sanitize_compose_environment$')"
_compose_probe_ln="$(sline '^COMPOSE_VER=')"
if [ -n "$_compose_sanitize_ln" ] && [ -n "$_compose_probe_ln" ] \
   && [ "$_compose_sanitize_ln" -lt "$_compose_probe_ln" ]; then
  pass "caller Compose selectors are cleared before the first top-level Compose probe"
else
  printf 'FAIL: Compose environment sanitation is too late (%s vs %s)\n' \
    "$_compose_sanitize_ln" "$_compose_probe_ln" >&2
  fail=1
fi
_main_apply_mark_ln="$(sline '^[[:space:]]*\[ "\$_ENV_SNAPSHOT_TAKEN" -eq 1 \] && _ACCESS_RECONFIGURATION_APPLY_STARTED=1$')"
_main_apply_call_ln="$(grep -nF 'compose_up_or_recover "docker compose up failed."' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$_main_apply_mark_ln" ] && [ -n "$_main_apply_call_ln" ] \
   && [ "$_main_apply_mark_ln" -lt "$_main_apply_call_ln" ]; then
  pass "main Compose is marked mutation-unknown before its apply call"
else
  printf 'FAIL: main Compose mutation marker is too late (%s vs %s)\n' \
    "$_main_apply_mark_ln" "$_main_apply_call_ln" >&2
  fail=1
fi
scheck "Cloudflare tunnel tokens are read without terminal echo" 'read -rsp "Paste your tunnel token: " CLOUDFLARE_TUNNEL_TOKEN'
scheck "non-interactive Cloudflare reads its token from a file" 'NI_TUNNEL_TOKEN_FILE'
scheck "setup.sh parses a non-interactive tunnel hostname" '\-\-tunnel-hostname\)'
scheck "setup.sh parses a non-interactive tunnel token file" '\-\-tunnel-token-file\)'
scheck "authenticated off-host access classification drives production safeguards" 'environment_for_access_route "\$ACCESS_MODE_LABEL"'
scheck "leaving a tunnel clears its secret file" 'rm -f secrets/cloudflare_tunnel_token\.txt'
scheck "old edges are quiesced before replacement verification" 'quiesce_previous_access_runtime '
if grep -Fq '_EXISTING_APP_BASE_URL' "$SETUP_SCRIPT"; then
  printf 'FAIL: reconfiguration still silently preserves the old public origin\n' >&2
  fail=1
else
  pass "reconfiguration does not silently preserve an old public origin"
fi
scheck "the LAN wizard truthfully identifies its diagnostics-only route" 'Exposes a plain-HTTP health check'
scheck "LAN mode labels raw-IP HTTP as a diagnostics URL" 'LAN_DIAGNOSTIC_URL="http://\$\{LAN_IP\}:\$\{DASHBOARD_HOST_PORT_RESOLVED\}/health/jarvis"'
if grep -Fq 'DASHBOARD_URL="http://${LAN_IP}:${DASHBOARD_HOST_PORT_RESOLVED}"' "$SETUP_SCRIPT"; then
  printf 'FAIL: LAN mode still advertises raw-IP HTTP as the dashboard\n' >&2
  fail=1
else
  pass "LAN mode never advertises raw-IP HTTP as the dashboard"
fi
if grep -Fq 'view from any device' "$SETUP_SCRIPT"; then
  printf 'FAIL: LAN summary still advertises application viewing on the diagnostics-only port\n' >&2
  fail=1
else
  pass "LAN summary does not advertise application viewing on the diagnostics-only port"
fi
if grep -Fq 'DASHBOARD_URL="Cloudflare configured' "$SETUP_SCRIPT"; then
  printf 'FAIL: Cloudflare pending prose is still stored where a URL is required\n' >&2
  fail=1
else
  pass "Cloudflare keeps Dashboard as a usable URL until verification succeeds"
fi

_cf_menu="$(sed -n '/4) From anywhere — Cloudflare Tunnel/,/5) From anywhere — your own domain/p' "$SETUP_SCRIPT")"
if printf '%s\n' "$_cf_menu" | grep -Fq 'Full features'; then
  printf 'FAIL: Cloudflare is promised full features before the route is verified\n' >&2
  fail=1
else
  pass "Cloudflare is not promised full features before route verification"
fi
if grep -Eq 'tunnel\).*full features' "$SETUP_SCRIPT"; then
  printf 'FAIL: Cloudflare access-mode summary claims full features before verification\n' >&2
  fail=1
else
  pass "Cloudflare access-mode summary waits for route verification"
fi

_cf_guidance="$(sed -n '/^unverified_https_exit()/,/^}/p' "$SETUP_SCRIPT")"
_cf_guidance_ok=1
for _required_cf_guidance in \
  'Local services are healthy and usable on this computer.' \
  'Setup needs attention.' \
  './setup.sh' \
  'exit 1'; do
  printf '%s\n' "$_cf_guidance" | grep -Fq "$_required_cf_guidance" || _cf_guidance_ok=0
done
if [ "$_cf_guidance_ok" -eq 1 ]; then
  pass "unverified Cloudflare guidance is actionable and exits nonzero"
else
  printf 'FAIL: unverified Cloudflare guidance is missing, non-actionable, or exits zero\n' >&2
  fail=1
fi
if printf '%s\n' "$_cf_guidance" | grep -Fq 'Setup complete.'; then
  printf 'FAIL: unverified Cloudflare guidance prints Setup complete\n' >&2
  fail=1
else
  pass "unverified Cloudflare guidance cannot print Setup complete"
fi
_cf_final_gate="$(sed -n '/^# Selected HTTPS completion gate/,/^fi$/p' "$SETUP_SCRIPT")"
case "$_cf_final_gate" in
  *'selected_https_is_verified'*'unverified_https_exit'*)
    pass "fresh setup gates every selected HTTPS route on exact verification" ;;
  *)
    printf 'FAIL: fresh setup does not route an unverified selected HTTPS state to failure\n' >&2
    fail=1 ;;
esac
if grep -Fq 'certificate warning' "$SETUP_SCRIPT"; then
  printf 'FAIL: LAN wizard still promises a certificate warning although the route is plain HTTP\n' >&2
  fail=1
else
  pass "LAN wizard does not promise a certificate warning for its plain-HTTP route"
fi
scheck "the setup link uses a loopback/verified base" \
  'present_setup_link "\$SETUP_LINK_BASE" "\$DASHBOARD_HOST_PORT_RESOLVED"'

# A failed first pull/build leaves .env behind. The next setup run takes the
# keep-existing branch, so that branch must finish the same health and bootstrap
# guidance instead of exiting immediately after `docker compose up`.
KEEP_BRANCH="$(sed -n '/if \[ "\$_do_overwrite" -eq 0 \]; then/,/^[[:space:]]*exit 0$/p' "$SETUP_SCRIPT")"
case "$KEEP_BRANCH" in
  *'upsert_env_var JARVIS_VERSION "$_keep_app_version"'*'upsert_env_var JARVIS_IMAGE_TAG "$_keep_image_tag"'*'export JARVIS_VERSION="$_keep_app_version"'*'export JARVIS_IMAGE_TAG="$_keep_image_tag"'*)
    pass "existing-config setup backfills and exports separate application and image identities" ;;
  *)
    printf 'FAIL: existing-config setup does not maintain separate application and image identities\n' >&2
    fail=1 ;;
esac
case "$KEEP_BRANCH" in
  *mandatory_health_services*) pass "existing-config resume waits for mandatory services" ;;
  *) printf 'FAIL: existing-config resume skips the mandatory health gate\n' >&2; fail=1 ;;
esac
case "$KEEP_BRANCH" in
  *'/api/setup/status'*) pass "existing-config resume asks the live app whether bootstrap is complete" ;;
  *) printf 'FAIL: existing-config resume does not query live setup status\n' >&2; fail=1 ;;
esac
case "$KEEP_BRANCH" in
  *'if [ "$_keep_configured" = "false" ]; then'*present_setup_link*) pass "existing-config resume prints a first-admin link only for an unconfigured install" ;;
  *) printf 'FAIL: existing-config resume does not state-gate the first-admin link\n' >&2; fail=1 ;;
esac
case "$KEEP_BRANCH" in
  *materialize_api_key_file*) pass "existing-config single-user guidance materializes the API-key file" ;;
  *) printf 'FAIL: existing-config single-user guidance can name a missing API-key file\n' >&2; fail=1 ;;
esac
case "$KEEP_BRANCH" in
  *production-readiness-check.sh*) pass "existing-config resume runs production readiness" ;;
  *) printf 'FAIL: existing-config resume skips production readiness\n' >&2; fail=1 ;;
esac
case "$KEEP_BRANCH" in
  *'selected_https_route'*'_keep_edge_state="$(probe_external_app'*'unverified_https_exit'*)
    pass "existing-config resume keeps every unverified selected HTTPS route non-successful" ;;
  *)
    printf 'FAIL: existing-config resume can report success for an unverified selected HTTPS route\n' >&2
    fail=1 ;;
esac
if printf '%s\n' "$KEEP_BRANCH" | grep -Fq 'JARVIS is healthy and setup is complete.'; then
  printf 'FAIL: existing-config resume claims all setup is complete before checking its edge\n' >&2
  fail=1
else
  pass "existing-config resume distinguishes in-app onboarding from edge completion"
fi
_keep_shim_ln="$(printf '%s\n' "$KEEP_BRANCH" | grep -nF 'install_cli_shim "$SCRIPT_DIR"' | tail -1 | cut -d: -f1)"
_keep_cf_gate_ln="$(printf '%s\n' "$KEEP_BRANCH" | grep -nF 'unverified_https_exit' | tail -1 | cut -d: -f1)"
if [ -n "$_keep_shim_ln" ] && [ -n "$_keep_cf_gate_ln" ] \
   && [ "$_keep_shim_ln" -lt "$_keep_cf_gate_ln" ]; then
  pass "existing-config resume installs the local lifecycle CLI before an edge-only failure"
else
  printf 'FAIL: Cloudflare edge failure can skip the local lifecycle CLI (%s vs %s)\n' \
    "$_keep_shim_ln" "$_keep_cf_gate_ln" >&2
  fail=1
fi
_keep_readiness_ln="$(printf '%s\n' "$KEEP_BRANCH" | grep -nF 'bash "$_keep_readiness"' | tail -1 | cut -d: -f1)"
_keep_finish_link_ln="$(printf '%s\n' "$KEEP_BRANCH" | grep -nF 'present_setup_link "$_keep_dashboard_url" "$_keep_dashboard_port"' | tail -1 | cut -d: -f1)"
if [ -n "$_keep_readiness_ln" ] && [ -n "$_keep_cf_gate_ln" ] \
   && [ -n "$_keep_finish_link_ln" ] \
   && [ "$_keep_readiness_ln" -lt "$_keep_cf_gate_ln" ] \
   && [ "$_keep_cf_gate_ln" -lt "$_keep_finish_link_ln" ]; then
  pass "existing-config resume defers the token-bearing finish link until readiness and route verification pass"
else
  printf 'FAIL: existing-config finish link is ordered before final gates (%s, %s, %s)\n' \
    "$_keep_readiness_ln" "$_keep_cf_gate_ln" "$_keep_finish_link_ln" >&2
  fail=1
fi
# PROFILE_ARGS + COMPOSE_PROFILES are now registry-driven (no per-profile literal
# list in setup.sh): assert the group is engaged as an ACTIVE_PROFILE, and let the
# PROFILE_REGISTRY accessor tests below prove it persists.
scheck "letsencrypt is engaged as an active profile" 'ACTIVE_PROFILES\+=\(letsencrypt\)'
scheck "local-https engages the caddy-local profile" 'ACTIVE_PROFILES\+=\(caddy-local\)'
scheck "setup.sh drives COMPOSE_PROFILES from the registry persist set" 'registry_profiles_to_persist'
scheck "setup.sh derives the health gate from the shared registry accessor" 'mandatory_health_services'
scheck "letsencrypt waits for the cert before advertising the URL" 'Waiting for the public certificate'
_le_timeout_block="$(sed -n '/if \[ "\$_le_ok" -eq 1 \]; then/,/^  fi$/p' "$SETUP_SCRIPT")"
if printf '%s\n' "$_le_timeout_block" | grep -Fq "unverified_https_exit \"Let's Encrypt\""; then
  pass "Let's Encrypt timeout enters the transactional rollback path immediately"
else
  printf "FAIL: Let's Encrypt timeout can bypass transactional access rollback\n" >&2
  fail=1
fi
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
            --smtp-pass-file --mode --backend --smart-model --gpu \
            --tunnel-hostname --tunnel-token-file --compose-project-name \
            --image-tag; do
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

case "$out" in
  *'--backend ollama|auto'*'Docker was just installed'*'newgrp docker'*)
    pass "--help is complete and documents only installer-supported backends" ;;
  *) printf 'FAIL: --help is truncated or advertises the wrong backend contract\n%s\n' "$out" >&2; fail=1 ;;
esac
case "$out" in
  *'--backend ollama|vllm'*|*'HuggingFace AWQ repo id'*)
    printf 'FAIL: --help still presents vLLM/HuggingFace as installer choices\n' >&2
    fail=1 ;;
  *) pass "--help does not present the manual vLLM overlay as an installer choice" ;;
esac

out="$(run_setup --backend vllm --help)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'manual benchmark overlay'*)
    pass "--backend vllm is rejected before setup work and explains the manual overlay" ;;
  *) printf 'FAIL: --backend vllm was not rejected clearly (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
out="$(run_setup --backend=vllm --help)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'manual benchmark overlay'*) pass "--backend=vllm is rejected by the same contract" ;;
  *) printf 'FAIL: --backend=vllm was not rejected clearly (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac

_backend_prompt="$(sed -n '/^prompt_ai_backend() {/,/^}/p' "$SETUP_SCRIPT")"
if printf '%s\n' "$_backend_prompt" | grep -Eq '\[[0-9]+\] vLLM|Choice \[1\]'; then
  printf 'FAIL: the interactive installer still offers a vLLM backend choice\n' >&2
  fail=1
else
  pass "the interactive installer configures Ollama without a dead vLLM choice"
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

out="$(run_setup --non-interactive --profile=tunnel)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'requires --tunnel-ack'*) pass "non-interactive tunnel requires explicit exposure consent" ;;
  *) printf 'FAIL: tunnel without acknowledgement was not rejected (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
out="$(run_setup --profile=tunnel)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'requires --non-interactive'*) pass "the tunnel profile cannot silently fall through the interactive menu" ;;
  *) printf 'FAIL: interactive --profile=tunnel was not rejected (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
out="$(run_setup --non-interactive --tailscale --profile=tunnel)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'cannot be combined'*) pass "setup refuses two competing HTTPS routes" ;;
  *) printf 'FAIL: Tailscale and tunnel profiles were not rejected together (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
out="$(run_setup --non-interactive --profile=tunnel --tunnel-ack)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'requires --tunnel-hostname'*) pass "non-interactive tunnel requires its public hostname" ;;
  *) printf 'FAIL: tunnel without hostname was not rejected (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
out="$(run_setup --non-interactive --profile=tunnel --tunnel-ack --tunnel-hostname jarvis.example.com)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'requires --tunnel-token-file'*) pass "non-interactive tunnel requires a token file" ;;
  *) printf 'FAIL: tunnel without token file was not rejected (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
out="$(run_setup --tunnel-token-file "${FIXTURES}/missing-token" --help)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'cannot read'*) pass "an unreadable tunnel token file fails before setup" ;;
  *) printf 'FAIL: unreadable tunnel token file was not rejected (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
out="$(run_setup --tunnel-token-file /dev/stdin --help)" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'regular file'*) pass "non-interactive tunnel token input cannot block on stdin or a device" ;;
  *) printf 'FAIL: non-regular tunnel token source was not rejected (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac
: > "${FIXTURES}/empty-tunnel-token"
out="$(run_setup --non-interactive --profile=tunnel --tunnel-ack --tunnel-hostname 'bad/name' --tunnel-token-file "${FIXTURES}/empty-tunnel-token")" && rc=0 || rc=$?
case "${rc}:${out}" in
  1:*'DNS hostname'*) pass "non-interactive tunnel rejects an invalid public hostname" ;;
  *) printf 'FAIL: invalid tunnel hostname was not rejected (rc=%s out=%s)\n' "$rc" "$out" >&2; fail=1 ;;
esac

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
JARVIS_SKIP_SELFSIGNED_GEN=false
OWNER_USER_ID=73
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

merge_env_file "$MOLD" "$MTMPL" "$MUPS" \
  "JARVIS_CERT_SAN JARVIS_SKIP_SELFSIGNED_GEN" > "$MOUT"

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
expect_eq "merge preserves an advanced owner override"  "$(mval OWNER_USER_ID)"         "73"

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

# Empty is a meaningful replacement for access-owned state. A route change must
# clear the old public origin, tunnel credential, trust flag, and profile rather
# than treating those values as generic operator settings to preserve.
cat > "${MERGE_DIR}/access-old.env" <<'EOF'
APP_BASE_URL=https://old.example
CLOUDFLARE_TUNNEL_TOKEN=old-token
TUNNEL_HOSTNAME=old.example
JARVIS_TRUST_CF_CONNECTING_IP=true
COMPOSE_PROFILES=tunnel,telegram
MY_CUSTOM_FLAG=keep-me
EOF
cat > "${MERGE_DIR}/access-clear.env" <<'EOF'
APP_BASE_URL=
CLOUDFLARE_TUNNEL_TOKEN=
TUNNEL_HOSTNAME=
JARVIS_TRUST_CF_CONNECTING_IP=false
COMPOSE_PROFILES=
EOF
merge_env_file "${MERGE_DIR}/access-old.env" /dev/null \
  "${MERGE_DIR}/access-clear.env" "" > "${MERGE_DIR}/access-out.env"
for _cleared_key in APP_BASE_URL CLOUDFLARE_TUNNEL_TOKEN TUNNEL_HOSTNAME COMPOSE_PROFILES; do
  expect_eq "merge clears access-owned ${_cleared_key}" \
    "$(grep "^${_cleared_key}=" "${MERGE_DIR}/access-out.env")" "${_cleared_key}="
done
expect_eq "merge resets Cloudflare trust when leaving the tunnel" \
  "$(grep '^JARVIS_TRUST_CF_CONNECTING_IP=' "${MERGE_DIR}/access-out.env")" \
  "JARVIS_TRUST_CF_CONNECTING_IP=false"
expect_eq "access reset preserves an unrelated operator key" \
  "$(grep '^MY_CUSTOM_FLAG=' "${MERGE_DIR}/access-out.env")" "MY_CUSTOM_FLAG=keep-me"

# A key new in the release (absent from the old .env) is appended.
expect_eq "merge appends a template key new in this release" \
  "$(mval NEW_RELEASE_KEY)" "default_new"

# A retired key not owned this run is dropped.
expect_eq "merge drops a retired, un-owned key" \
  "$(grep -c '^JARVIS_CERT_SAN=' "$MOUT")" "0"
expect_eq "merge drops the retired self-signed generator switch" \
  "$(grep -c '^JARVIS_SKIP_SELFSIGNED_GEN=' "$MOUT")" "0"

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

# route_claims: the seven fixed ingress routes, each a 9-column pipe row.
routes="$(route_claims)"
expect_eq "route_claims emits seven ingress routes" "$(printf '%s\n' "$routes" | grep -c .)" "7"
for _r in localhost-http raw-ip-lan named-private-https tailscale-serve local-https letsencrypt tunnel; do
  if printf '%s\n' "$routes" | grep -q "^${_r}|"; then
    pass "route_claims declares the ${_r} route"
  else
    printf 'FAIL: route_claims missing the %s route\n' "$_r" >&2; fail=1
  fi
done
expect_eq "raw-IP LAN transports no setup token, cookie, or passkey ceremony" \
  "$(printf '%s\n' "$routes" | grep '^raw-ip-lan|')" \
  "raw-ip-lan|http|3001|lan-ip|none|none|none|none|diagnostics-only"
expect_eq "guided Tailscale has its own supported certificate/origin contract" \
  "$(printf '%s\n' "$routes" | grep '^tailscale-serve|')" \
  "tailscale-serve|https|443|tailnet-host|fragment|secure|tailnet-host|tailscale|supported"
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
compose_meets_floor 2.40.3+ds1-0ubuntu1 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: distro build metadata is tolerated" "$rc" "0"
compose_meets_floor 2.24.4+ds1-1 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: exact-floor build metadata is tolerated" "$rc" "0"
compose_meets_floor 2.24.4-rc.1 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: an exact-floor prerelease is rejected" "$rc" "1"
compose_meets_floor unknown 2.24.4 && rc=0 || rc=$?; expect_eq "compose floor: unreadable version -> rc 2" "$rc" "2"
sort() { return 99; }
compose_meets_floor 2.40.3 2.24.4 && rc=0 || rc=$?
expect_eq "compose floor: comparison does not require GNU sort -V" "$rc" "0"
unset -f sort

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
( export JARVIS_PROC_VERSION="$WSL_PROC" JARVIS_SYSTEMD_DIR="$NO_SYSTEMD"
  prereq_install_plan Linux ubuntu 1 0 0 docker-compose >/dev/null 2>&1 ) && rc=0 || rc=$?
expect_eq "WSL-without-systemd: Compose upgrade plan is refused (rc 1)" "$rc" "1"
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
printf '#!/usr/bin/env bash\ncase "$*" in "compose version --short") printf "2.24.4\\n" ;; "compose version") : ;; esac\nexit 0\n' > "${MP_BIN}/docker"
printf '#!/usr/bin/env bash\nexit 0\n' > "${MP_BIN}/openssl"
printf '#!/usr/bin/env bash\nexec /usr/bin/uname "$@"\n' > "${MP_BIN}/uname"
chmod +x "${MP_BIN}/docker" "${MP_BIN}/openssl" "${MP_BIN}/uname"
got="$( eval "$mp_src"; ( export PATH="$MP_BIN" COMPOSE_MIN=2.24.4 NI_PROFILE=dev; missing_prereqs ) )"
case "$got" in
  *python3*) pass "missing_prereqs reports python3 when it is absent" ;;
  *) printf 'FAIL: missing_prereqs did not report python3 (got=%s)\n' "$got" >&2; fail=1 ;;
esac
case "$got" in
  *curl*) pass "missing_prereqs reports curl when it is absent" ;;
  *) printf 'FAIL: missing_prereqs did not report curl (got=%s)\n' "$got" >&2; fail=1 ;;
esac
printf '#!/usr/bin/env bash\nexit 0\n' > "${MP_BIN}/python3"
printf '#!/usr/bin/env bash\nexit 0\n' > "${MP_BIN}/curl"
chmod +x "${MP_BIN}/python3" "${MP_BIN}/curl"
got="$( eval "$mp_src"; ( export PATH="$MP_BIN" COMPOSE_MIN=2.24.4 NI_PROFILE=dev; missing_prereqs ) )"
case "$got" in
  *python3*) printf 'FAIL: missing_prereqs reported python3 though present (got=%s)\n' "$got" >&2; fail=1 ;;
  *) pass "missing_prereqs does not report python3 when present" ;;
esac
got="$( eval "$mp_src"; ( export PATH="$MP_BIN" COMPOSE_MIN=2.24.4 NI_PROFILE=local-https; missing_prereqs ) )"
case "$got" in
  *mkcert*) pass "local-https reports a missing mkcert/browser-trust toolchain" ;;
  *) printf 'FAIL: local-https did not report missing mkcert tooling (got=%s)\n' "$got" >&2; fail=1 ;;
esac
printf '#!/usr/bin/env bash\nexit 0\n' > "${MP_BIN}/mkcert"
printf '#!/usr/bin/env bash\nexit 0\n' > "${MP_BIN}/certutil"
chmod +x "${MP_BIN}/mkcert" "${MP_BIN}/certutil"
got="$( eval "$mp_src"; ( export PATH="$MP_BIN" COMPOSE_MIN=2.24.4 NI_PROFILE=local-https; missing_prereqs ) )"
case "$got" in
  *mkcert*) printf 'FAIL: local-https reported mkcert though its toolchain is present (got=%s)\n' "$got" >&2; fail=1 ;;
  *) pass "local-https accepts a complete mkcert/browser-trust toolchain" ;;
esac

printf '#!/usr/bin/env bash\ncase "$*" in "compose version --short") printf "2.24.3\\n" ;; "compose version") : ;; esac\nexit 0\n' > "${MP_BIN}/docker"
got="$( eval "$mp_src"; ( export PATH="$MP_BIN" COMPOSE_MIN=2.24.4 NI_PROFILE=dev; missing_prereqs ) )"
case "$got" in
  *docker-compose*) pass "missing_prereqs rejects Compose below 2.24.4" ;;
  *) printf 'FAIL: missing_prereqs accepted old Compose (got=%s)\n' "$got" >&2; fail=1 ;;
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
scheck "missing_prereqs treats curl as a prerequisite"    'missing\+=\(curl\)'
scheck "ensure_prerequisites verifies python3"            'python3 required for model selection'
scheck "ensure_prerequisites verifies curl"               'curl required for downloads and health checks'
scheck "run_doctor --check fails when python3 is absent"  'python3 missing — required for model selection'
scheck "run_doctor --check fails when curl is absent"     'curl missing — required for downloads and health checks'
scheck "the compose gate pins a real 2.24.4 floor"        '^COMPOSE_MIN=2\.24\.4'
scheck "the compose gate rejects rather than warns on an old plugin" 'Docker Compose v.* is too old'
scheck "selected local HTTPS prepares trust before service startup" '^[[:space:]]*prepare_local_https$'
scheck "selected local HTTPS no longer requires manual make certs first" 'Creating and trusting the local HTTPS certificate'
scheck "persisted local HTTPS can use the same consented mkcert installer" 'handle_missing_prereqs mkcert'
_keep_local_https_ln="$(grep -nF '*,caddy-local,*) prepare_local_https' "$SETUP_SCRIPT" | head -1 | cut -d: -f1 || true)"
_keep_mutation_ln="$(grep -nF '_SETUP_MUTATION_STARTED=1' "$SETUP_SCRIPT" | head -1 | cut -d: -f1 || true)"
if [ -n "$_keep_local_https_ln" ] && [ -n "$_keep_mutation_ln" ] \
   && [ "$_keep_local_https_ln" -lt "$_keep_mutation_ln" ]; then
  pass "persisted local HTTPS resolves consent before deployment mutation"
else
  printf 'FAIL: persisted local HTTPS prerequisite handling follows mutation (%s vs %s)\n' \
    "$_keep_local_https_ln" "$_keep_mutation_ln" >&2
  fail=1
fi

jarvis_openssl_ln="$(grep -nF 'command -v openssl' "$JARVIS_SETUP_SCRIPT" | head -1 | cut -d: -f1 || true)"
jarvis_curl_ln="$(grep -nF 'command -v curl' "$JARVIS_SETUP_SCRIPT" | head -1 | cut -d: -f1 || true)"
jarvis_env_copy_ln="$(grep -nF '  cp .env.example .env' "$JARVIS_SETUP_SCRIPT" | head -1 | cut -d: -f1 || true)"
if [ -n "$jarvis_openssl_ln" ] && [ -n "$jarvis_curl_ln" ] \
   && [ -n "$jarvis_env_copy_ln" ] \
   && [ "$jarvis_openssl_ln" -lt "$jarvis_env_copy_ln" ] \
   && [ "$jarvis_curl_ln" -lt "$jarvis_env_copy_ln" ]; then
  pass "jarvis-setup checks openssl and curl before creating .env"
else
  printf 'FAIL: jarvis-setup prerequisite checks do not precede .env mutation (openssl=%s curl=%s env=%s)\n' \
    "$jarvis_openssl_ln" "$jarvis_curl_ln" "$jarvis_env_copy_ln" >&2
  fail=1
fi
if grep -qF 'Docker Compose v2.24.4 or newer is required' "$JARVIS_SETUP_SCRIPT"; then
  pass "jarvis-setup enforces the Compose feature floor"
else
  printf 'FAIL: jarvis-setup does not enforce Compose 2.24.4+\n' >&2
  fail=1
fi
scheck "the nvidia-toolkit probe is a first-class preflight" '^preflight_nvidia_toolkit$'
scheck "the port pre-check reads .env port values"        '_port_or_default DASHBOARD_HOST_PORT'
scheck "the port pre-check adds active-profile ports"     'registry_profile_host_ports'
scheck "the port pre-check explains occupied ports on a running reinstall" 'Existing JARVIS services are expected'
scheck "the readiness wrapper consumes readiness_verdict" 'readiness_verdict "\$_rc" "\$_ENV_VALUE"'
scheck "the readiness wrapper is non-fatal on warnings (exit 2)" 'passed with warnings'
scheck "multi-user next-steps lead with the first-admin setup link" 'Bootstrap the first admin: open the "Finish setup" link'
scheck "multi-user copy says SMTP is optional" 'SMTP is optional'
scheck "multi-user passkey guidance prints the exact address to keep using" \
  'Keep using this exact address for sign-in and passkeys: %s'
scheck "fresh actionable URLs adopt the resolved browser route" \
  '^DASHBOARD_URL="\$SETUP_BROWSER_BASE"$'
scheck "resumed actionable URLs adopt the resolved browser route" \
  '^[[:space:]]*_keep_dashboard_url="\$SETUP_BROWSER_BASE"$'
scheck "temporary bootstrap explicitly defers family/passkey setup" \
  'Do not invite family members or enrol passkeys at this temporary address'
scheck "family/passkey steps require a verified shared browser origin" \
  'if \[ "\$SETUP_BROWSER_IS_SHARED" -eq 1 \]; then'
if grep -qF 'Verify your final origin and APP_BASE_URL' "$SETUP_SCRIPT"; then
  printf 'FAIL: multi-user next steps expose APP_BASE_URL instead of a concrete action\n' >&2
  fail=1
else
  pass "multi-user next steps do not expose APP_BASE_URL jargon"
fi

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

_interrupted_recovery_ln="$(sline '^recover_interrupted_setup_transaction ' )"
_previous_baseline_ln="$(sline '^_PREVIOUS_ACCESS_MODE=')"
if [ -n "$_interrupted_recovery_ln" ] && [ -n "$_previous_baseline_ln" ] \
   && [ "$_interrupted_recovery_ln" -lt "$_previous_baseline_ln" ]; then
  pass "an interrupted journal is reconciled before the current env becomes a new baseline"
else
  printf 'FAIL: interrupted recovery does not precede baseline capture (%s vs %s)\n' \
    "$_interrupted_recovery_ln" "$_previous_baseline_ln" >&2
  fail=1
fi

# The green "Setup complete." banner must be printed AFTER the readiness gate.
_banner_ln="$(grep -nF 'Setup complete.' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_gate_ln="$(grep -nF 'bash "$_READINESS_SCRIPT"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$_banner_ln" ] && [ -n "$_gate_ln" ] && [ "$_gate_ln" -lt "$_banner_ln" ]; then
  pass "the green 'Setup complete.' banner is printed AFTER the readiness gate"
else
  printf 'FAIL: Setup complete banner (%s) not after the readiness gate (%s)\n' "$_banner_ln" "$_gate_ln" >&2; fail=1
fi

_edge_quiesce_ln="$(grep -nF 'if ! quiesce_previous_access_runtime' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_edge_verify_ln="$(grep -nF '# Selected HTTPS completion gate' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_tailscale_probe_ln="$(grep -nF 'if tailscale_serve_https "$DASHBOARD_TRUSTED_HOST_PORT_RESOLVED"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_local_https_probe_ln="$(grep -nF 'if wait_for_local_https_marker' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_tunnel_probe_ln="$(grep -nF 'TUNNEL_APP_STATE="$(probe_external_app' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_public_origin_probe_ln="$(grep -nF 'probe_external_app "${NI_PUBLIC_ORIGIN}/health/jarvis"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_letsencrypt_probe_ln="$(grep -nF 'probe_external_app "https://${NI_DOMAIN}/health/jarvis"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_edge_finalize_ln="$(grep -nF 'finalize_previous_access_edge_retirement' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_journal_discard_ln="$(grep -nF 'discard_setup_transaction "$_SETUP_TRANSACTION_DIR"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_transaction_commit_ln="$(grep -nF '_ACCESS_TRANSACTION_COMMITTED=1' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_browser_route_ln="$(grep -nF 'resolve_setup_browser_route "$SETUP_LINK_BASE" "$DASHBOARD_HOST_PORT_RESOLVED"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_finish_link_ln="$(grep -nF 'present_setup_link "$SETUP_LINK_BASE" "$DASHBOARD_HOST_PORT_RESOLVED"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$_edge_quiesce_ln" ] && [ -n "$_edge_verify_ln" ] \
   && [ -n "$_edge_finalize_ln" ] && [ -n "$_journal_discard_ln" ] \
   && [ -n "$_transaction_commit_ln" ] && [ -n "$_browser_route_ln" ] \
   && [ -n "$_finish_link_ln" ] \
   && [ -n "$_tailscale_probe_ln" ] && [ -n "$_local_https_probe_ln" ] \
   && [ -n "$_tunnel_probe_ln" ] && [ -n "$_public_origin_probe_ln" ] \
   && [ -n "$_letsencrypt_probe_ln" ] \
   && [ "$_edge_quiesce_ln" -lt "$_tailscale_probe_ln" ] \
   && [ "$_edge_quiesce_ln" -lt "$_local_https_probe_ln" ] \
   && [ "$_edge_quiesce_ln" -lt "$_tunnel_probe_ln" ] \
   && [ "$_edge_quiesce_ln" -lt "$_public_origin_probe_ln" ] \
   && [ "$_edge_quiesce_ln" -lt "$_letsencrypt_probe_ln" ] \
   && [ "$_edge_quiesce_ln" -lt "$_edge_verify_ln" ] \
   && [ "$_edge_verify_ln" -lt "$_gate_ln" ] \
   && [ "$_gate_ln" -lt "$_edge_finalize_ln" ] \
   && [ "$_edge_finalize_ln" -lt "$_journal_discard_ln" ] \
   && [ "$_journal_discard_ln" -lt "$_transaction_commit_ln" ] \
   && [ "$_transaction_commit_ln" -lt "$_browser_route_ln" ] \
   && [ "$_browser_route_ln" -lt "$_banner_ln" ] \
   && [ "$_banner_ln" -lt "$_finish_link_ln" ]; then
  pass "access replacement orders quiesce, marker, readiness, commit, browser verification, banner, then finish link"
else
  printf 'FAIL: access transaction order is unsafe (quiesce=%s verify=%s readiness=%s finalize=%s discard=%s commit=%s browser=%s banner=%s link=%s)\n' \
    "$_edge_quiesce_ln" "$_edge_verify_ln" "$_gate_ln" \
    "$_edge_finalize_ln" "$_journal_discard_ln" "$_transaction_commit_ln" \
    "$_browser_route_ln" "$_banner_ln" "$_finish_link_ln" >&2
  fail=1
fi

_exit_trap_ln="$(grep -nF 'trap cleanup_setup_exit EXIT' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_journal_begin_ln="$(grep -nF 'begin_setup_transaction "$_SETUP_TRANSACTION_DIR"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_model_pull_ln="$(grep -nF 'run --rm ollama-bootstrap' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_health_failure_ln="$(grep -nF 'The following service(s) did not become healthy' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_readiness_abort_ln="$(grep -nF 'exit "$_rc"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$_exit_trap_ln" ] && [ -n "$_journal_begin_ln" ] \
   && [ -n "$_model_pull_ln" ] && [ -n "$_health_failure_ln" ] \
   && [ -n "$_readiness_abort_ln" ] \
   && [ "$_exit_trap_ln" -lt "$_journal_begin_ln" ] \
   && [ "$_journal_begin_ln" -lt "$_model_pull_ln" ] \
   && [ "$_journal_begin_ln" -lt "$_health_failure_ln" ] \
   && [ "$_journal_begin_ln" -lt "$_readiness_abort_ln" ] \
   && [ "$_readiness_abort_ln" -lt "$_journal_discard_ln" ]; then
  pass "the EXIT transaction covers model pull, mandatory health, and readiness failures before commit"
else
  printf 'FAIL: EXIT transaction does not span all failure gates (trap=%s begin=%s model=%s health=%s readiness=%s discard=%s)\n' \
    "$_exit_trap_ln" "$_journal_begin_ln" "$_model_pull_ln" \
    "$_health_failure_ln" "$_readiness_abort_ln" "$_journal_discard_ln" >&2
  fail=1
fi

_tunnel_restart_ln="$(grep -nF 'Cloudflare tunnel restart failed.' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_tunnel_ready_ln="$(grep -nF 'if cloudflared_ready' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$_tunnel_restart_ln" ] && [ -n "$_tunnel_ready_ln" ] \
   && [ "$_tunnel_restart_ln" -lt "$_tunnel_ready_ln" ]; then
  pass "same-mode Cloudflare reconfiguration force-recreates its connector before readiness and marker probes"
else
  printf 'FAIL: Cloudflare connector restart does not precede verification (%s vs %s)\n' \
    "$_tunnel_restart_ln" "$_tunnel_ready_ln" >&2
  fail=1
fi

# The dependency-reversed lead step (a magic link before any admin exists) is gone.
if grep -Eq 'Request a magic link at the sign-in page' "$SETUP_SCRIPT"; then
  printf 'FAIL: next-steps still leads with a magic-link step that presupposes SMTP/an account\n' >&2; fail=1
else
  pass "next-steps no longer leads with a magic-link step before first-admin bootstrap"
fi
# ...and first-admin bootstrap is ordered before SMTP configuration.
_boot_ln="$(sline 'Bootstrap the first admin')"
_smtp_ln="$(grep -nE 'SMTP is optional: configure it' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$_boot_ln" ] && [ -n "$_smtp_ln" ] && [ "$_boot_ln" -lt "$_smtp_ln" ]; then
  pass "next-steps orders first-admin bootstrap before SMTP configuration"
else
  printf 'FAIL: next-steps SMTP step (%s) not after bootstrap (%s)\n' "$_smtp_ln" "$_boot_ln" >&2; fail=1
fi

# A loopback-only setup link is reachable from a headless server through an SSH
# forward. This must not be limited to the raw-LAN profile: localhost and local
# HTTPS use the same bootstrap route on a headless VM.
scheck "setup gives headless loopback installs an SSH-forward command" \
  'Finish setup from another computer'
scheck "headless SSH guidance never guesses a server hostname" \
  '<your-ssh-user>@<server-address>'
scheck "headless loopback guidance detects an SSH session" 'SSH_CONNECTION'
scheck "headless loopback guidance detects a missing graphical session" 'WAYLAND_DISPLAY'
scheck "headless finish links are prepared by the tested route helper" \
  'headless_setup_route "\$1" "\$2"'
scheck "headless guidance prints the exact browser address" \
  'In that computer.s browser, open this exact address'
scheck "headless guidance explains why HTTP needs no certificate bypass" \
  'HTTP only inside the encrypted SSH connection'
_present_helper_ln="$(grep -nF 'present_setup_link()' "$SETUP_SCRIPT" | head -1 | cut -d: -f1)"
_keep_present_ln="$(grep -nF 'present_setup_link "$_keep_dashboard_url" "$_keep_dashboard_port"' "$SETUP_SCRIPT" | head -1 | cut -d: -f1)"
_fresh_present_ln="$(grep -nF 'present_setup_link "$SETUP_LINK_BASE" "$DASHBOARD_HOST_PORT_RESOLVED"' "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$_present_helper_ln" ] && [ -n "$_keep_present_ln" ] && [ -n "$_fresh_present_ln" ] \
   && [ "$_present_helper_ln" -lt "$_keep_present_ln" ] \
   && [ "$_keep_present_ln" -lt "$_fresh_present_ln" ]; then
  pass "fresh and resumed installs share the headless finish-link presenter"
else
  printf 'FAIL: fresh/resumed finish-link paths do not share the presenter (%s, %s, %s)\n' \
    "$_present_helper_ln" "$_keep_present_ln" "$_fresh_present_ln" >&2
  fail=1
fi
scheck "multi-user guidance names passkeys and one-time links for family members" \
  'Family members sign in with a passkey or one-time link'
scheck "multi-user guidance preserves owner API-key recovery" \
  'configured owner can use local API-key recovery'
if grep -qF 'at the final origin' "$SETUP_SCRIPT"; then
  printf 'FAIL: multi-user passkey step uses origin jargon after defining a concrete address\n' >&2
  fail=1
else
  pass "multi-user passkey step reuses the concrete address wording"
fi

# Nothing actionable may claim that the dashboard is ready before every final
# route, readiness, and transaction gate has passed and the finish link exists.
_dashboard_guidance_ln="$(grep -nF "Open the dashboard:" "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_dashboard_summary_ln="$(grep -nF "Dashboard:    " "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_signin_guidance_ln="$(grep -nF "Family members sign in with a passkey or one-time link." "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_ready_guidance_ln="$(grep -nF "All mandatory services healthy. You can now open the dashboard." "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
_admin_guidance_ln="$(grep -nF "Admin user management:" "$SETUP_SCRIPT" | tail -1 | cut -d: -f1)"
if [ -n "$_dashboard_guidance_ln" ] && [ -n "$_dashboard_summary_ln" ] && [ -n "$_signin_guidance_ln" ] \
   && [ -n "$_ready_guidance_ln" ] && [ -n "$_admin_guidance_ln" ] \
   && [ "$_finish_link_ln" -lt "$_dashboard_guidance_ln" ] \
   && [ "$_finish_link_ln" -lt "$_dashboard_summary_ln" ] \
   && [ "$_finish_link_ln" -lt "$_signin_guidance_ln" ] \
   && [ "$_finish_link_ln" -lt "$_ready_guidance_ln" ] \
   && [ "$_finish_link_ln" -lt "$_admin_guidance_ln" ]; then
  pass "ready, sign-in, dashboard, and admin guidance follow the finish-link gate"
else
  printf 'FAIL: post-gate operator guidance is out of order (link=%s dashboard=%s dashboard-summary=%s sign-in=%s ready=%s admin=%s)\n' \
    "$_finish_link_ln" "$_dashboard_guidance_ln" "$_dashboard_summary_ln" "$_signin_guidance_ln" \
    "$_ready_guidance_ln" "$_admin_guidance_ln" >&2
  fail=1
fi

# === compose service health convergence =====================================
# The lookup owns Compose-specific container discovery. The shared helper owns
# Docker state transitions and samples each file-backed state exactly once.
HEALTH_SEQUENCE_FILE="${FIXTURES}/health-sequence"
HEALTH_CURRENT_FILE="${FIXTURES}/health-current"
HEALTH_LOOKUP_LOG="${FIXTURES}/health-lookups"
HEALTH_BIN="$(fake_docker '
if [ "${1:-}" = inspect ] && [ "${2:-}" = --format ]; then
  cat "$STUB_HEALTH_CURRENT_FILE"
  exit 0
fi
exit 1
')"

_health_sequence_lookup() {
  local line
  line="$(sed -n '1p' "$HEALTH_SEQUENCE_FILE")"
  sed '1d' "$HEALTH_SEQUENCE_FILE" > "${HEALTH_SEQUENCE_FILE}.next"
  mv "${HEALTH_SEQUENCE_FILE}.next" "$HEALTH_SEQUENCE_FILE"
  printf '%s\n' "${line:-absent}" >> "$HEALTH_LOOKUP_LOG"
  [ -n "$line" ] && [ "$line" != absent ] || return 0
  printf '%s' "$line" > "$HEALTH_CURRENT_FILE"
  printf 'container-1'
}

run_health_sequence() {
  local sequence="$1" budget="$2" rc=0
  printf '%s\n' "$sequence" > "$HEALTH_SEQUENCE_FILE"
  : > "$HEALTH_CURRENT_FILE"
  : > "$HEALTH_LOOKUP_LOG"
  export HEALTH_SEQUENCE_FILE HEALTH_CURRENT_FILE HEALTH_LOOKUP_LOG
  export STUB_HEALTH_CURRENT_FILE="$HEALTH_CURRENT_FILE"
  PATH="$HEALTH_BIN:$PATH" \
    wait_for_compose_service_health test-service "$budget" \
      _health_sequence_lookup 0 || rc=$?
  printf '%s/%s/%s/%s' "$rc" "$COMPOSE_HEALTH_RESULT" \
    "$COMPOSE_HEALTH_LAST_STATE" "$(wc -l < "$HEALTH_LOOKUP_LOG" | tr -d ' ')"
}

got="$(run_health_sequence \
  $'absent\nstarting|running\nunhealthy|running\nhealthy|running' 4)"
expect_eq "service health converges across absent, starting, and unhealthy states" \
  "$got" "0/healthy/healthy/4"

got="$(run_health_sequence \
  $'unhealthy|running\nunhealthy|running\nunhealthy|running' 3)"
expect_eq "permanent unhealthy state exhausts the zero-delay budget" \
  "$got" "1/timeout/unhealthy/3"

got="$(run_health_sequence '|exited' 4)"
expect_eq "an exited container fails on its first sample" \
  "$got" "1/terminal/exited/1"

got="$(run_health_sequence 'unhealthy|dead' 4)"
expect_eq "a dead container fails on its first sample" \
  "$got" "1/terminal/dead/1"

got="$(run_health_sequence '|running' 4)"
expect_eq "a running container without a healthcheck is explicitly unverified" \
  "$got" "0/running-unverified/running/1"

wait_for_compose_service_health test-service 1 invalid-name 0 \
  >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "service health lookup rejects command-like function names" "$rc" "2"

# Both setup entry points provide only Compose lookup and result-wording
# adapters; the shared helper executes the same transient-state sequence.
SETUP_HEALTH_LOOKUP_SRC="$(sed -n '/^_setup_service_container_id()/,/^}/p' "$SETUP_SCRIPT")"
SETUP_HEALTH_WAIT_SRC="$(sed -n '/^_wait_for_setup_service()/,/^}/p' "$SETUP_SCRIPT")"
JARVIS_HEALTH_LOOKUP_SRC="$(sed -n '/^_jarvis_setup_service_container_id()/,/^}/p' "$JARVIS_SETUP_SCRIPT")"
JARVIS_HEALTH_WAIT_SRC="$(sed -n '/^_wait_for_jarvis_setup_service()/,/^}/p' "$JARVIS_SETUP_SCRIPT")"
CALLER_HEALTH_BIN="$(fake_docker '
if [ "${1:-}" = inspect ]; then
  cat "$STUB_CALLER_HEALTH_CURRENT"
  exit 0
fi
case " $* " in
  *" compose "*"ps -q "*)
    line="$(sed -n "1p" "$STUB_CALLER_HEALTH_SEQUENCE")"
    sed "1d" "$STUB_CALLER_HEALTH_SEQUENCE" > "${STUB_CALLER_HEALTH_SEQUENCE}.next"
    mv "${STUB_CALLER_HEALTH_SEQUENCE}.next" "$STUB_CALLER_HEALTH_SEQUENCE"
    printf "%s\n" "${line:-absent}" >> "$STUB_CALLER_HEALTH_LOG"
    [ -n "$line" ] && [ "$line" != absent ] || exit 0
    printf "%s" "$line" > "$STUB_CALLER_HEALTH_CURRENT"
    printf "container-1"
    exit 0
    ;;
esac
exit 1
')"

run_setup_caller_health() {
  local entrypoint="$1" rc=0
  printf '%s\n' \
    $'absent\nstarting|running\nunhealthy|running\nhealthy|running' \
    > "$HEALTH_SEQUENCE_FILE"
  : > "$HEALTH_CURRENT_FILE"
  : > "$HEALTH_LOOKUP_LOG"
  export STUB_CALLER_HEALTH_SEQUENCE="$HEALTH_SEQUENCE_FILE"
  export STUB_CALLER_HEALTH_CURRENT="$HEALTH_CURRENT_FILE"
  export STUB_CALLER_HEALTH_LOG="$HEALTH_LOOKUP_LOG"
  (
    info() { :; }
    ok() { :; }
    warn() { :; }
    err() { :; }
    COMPOSE="docker compose --env-file .env"
    case "$entrypoint" in
      setup)
        eval "$SETUP_HEALTH_LOOKUP_SRC"
        eval "$SETUP_HEALTH_WAIT_SRC"
        PATH="$CALLER_HEALTH_BIN:$PATH" \
          _wait_for_setup_service test-service 4 0 || rc=$?
        ;;
      wrapper)
        eval "$JARVIS_HEALTH_LOOKUP_SRC"
        eval "$JARVIS_HEALTH_WAIT_SRC"
        PATH="$CALLER_HEALTH_BIN:$PATH" \
          _wait_for_jarvis_setup_service test-service 4 0 || rc=$?
        ;;
    esac
    printf '%s/%s/%s' "$rc" "$COMPOSE_HEALTH_RESULT" \
      "$(wc -l < "$HEALTH_LOOKUP_LOG" | tr -d ' ')"
  )
}

got="$(run_setup_caller_health setup)"
expect_eq "setup.sh delegates transient health convergence to the shared helper" \
  "$got" "0/healthy/4"
got="$(run_setup_caller_health wrapper)"
expect_eq "jarvis-setup delegates transient health convergence to the shared helper" \
  "$got" "0/healthy/4"
if grep -qE '^wait_healthy\(\)' "$SETUP_SCRIPT" "$JARVIS_SETUP_SCRIPT"; then
  printf 'FAIL: a setup entry point still owns a health state machine\n' >&2
  fail=1
else
  pass "setup entry points contain no caller-owned wait_healthy state machine"
fi

# === host/shared lifecycle exclusion ========================================
# Host locking must also work on supported macOS/minimal hosts where GNU
# sha256sum and util-linux flock are absent. Python 3 is a setup prerequisite,
# so a deliberately tiny PATH exercises both portable fallbacks.
LIFECYCLE_REPO="${FIXTURES}/lifecycle-repo"
LIFECYCLE_REPO_OTHER="${FIXTURES}/lifecycle-repo-other"
LIFECYCLE_CONFIG="${FIXTURES}/lifecycle-config"
NO_GNU_BIN="${FIXTURES}/no-gnu-bin"
mkdir -p "$LIFECYCLE_REPO" "$LIFECYCLE_REPO_OTHER" "$NO_GNU_BIN"
for _tool in python3 dirname mkdir sleep; do
  ln -s "$(command -v "$_tool")" "${NO_GNU_BIN}/${_tool}"
done

got="$(PATH="$NO_GNU_BIN" JARVIS_CLI_CONFIG_DIR="$LIFECYCLE_CONFIG" \
  host_lifecycle_lock_path "$LIFECYCLE_REPO")" && rc=0 || rc=$?
case "$got" in
  "$LIFECYCLE_CONFIG"/locks/*.lock) _portable_path=1 ;;
  *) _portable_path=0 ;;
esac
expect_eq "host lock identity needs neither sha256sum nor shasum" \
  "${rc}/${_portable_path}" "0/1"

_canon_target="${FIXTURES}/canonical-target"
_canon_alias="${FIXTURES}/canonical-alias"
mkdir -p "$_canon_target/subdir"
ln -s "$_canon_target" "$_canon_alias"
expect_eq "portable canonicalizer resolves symlinks and dot-dot without GNU realpath" \
  "$(canonical_path_portable "$_canon_alias/subdir/../future-file")" \
  "$_canon_target/future-file"

_fallback_marker="${FIXTURES}/fallback-host-held"
(
  export PATH="$NO_GNU_BIN" JARVIS_CLI_CONFIG_DIR="$LIFECYCLE_CONFIG"
  claim_host_lifecycle_lock "$LIFECYCLE_REPO"
  : > "$_fallback_marker"
  sleep 3
) &
_fallback_holder=$!
for _attempt in $(seq 1 100); do
  [ -f "$_fallback_marker" ] && break
  sleep 0.02
done
if [ -f "$_fallback_marker" ]; then
  (
    export PATH="$NO_GNU_BIN" JARVIS_CLI_CONFIG_DIR="$LIFECYCLE_CONFIG"
    claim_host_lifecycle_lock "$LIFECYCLE_REPO"
  ) && rc=0 || rc=$?
  expect_eq "python fcntl fallback keeps the host lease after its child exits" "$rc" "3"
else
  printf 'FAIL: portable host-lock holder did not become ready\n' >&2; fail=1
fi
kill "$_fallback_holder" 2>/dev/null || true
wait "$_fallback_holder" 2>/dev/null || true

# Re-exec inheritance is authenticated inside the Docker named-volume lock
# domain, not through a host descriptor. Stub only the already-live status
# probe: the claim must ask for the exact kind and ID exported by the parent.
(
  _lifecycle_volume_helper_for_project() {
    [ "$2" = lifecycle-repo ] && [ "$3" = host-status ] \
      && [ "$4" = setup ] \
      && [ "$5" = 0123456789abcdef0123456789abcdef ]
  }
  export JARVIS_SHARED_LIFECYCLE_LOCK_HELD=1
  export JARVIS_SHARED_LIFECYCLE_KIND=setup
  export JARVIS_SHARED_LIFECYCLE_ID=0123456789abcdef0123456789abcdef
  export JARVIS_SHARED_LIFECYCLE_PROJECT=lifecycle-repo
  claim_lifecycle_operation "$LIFECYCLE_REPO" setup
) && rc=0 || rc=$?
expect_eq "same-operation re-exec authenticates the named-volume holder" "$rc" "0"

(
  _lifecycle_volume_helper_for_project() { return 1; }
  export JARVIS_SHARED_LIFECYCLE_LOCK_HELD=1
  export JARVIS_SHARED_LIFECYCLE_KIND=setup
  export JARVIS_SHARED_LIFECYCLE_ID=0123456789abcdef0123456789abcdef
  export JARVIS_SHARED_LIFECYCLE_PROJECT=lifecycle-repo
  claim_lifecycle_operation "$LIFECYCLE_REPO" setup
) >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "a dead inherited named-volume holder is rejected" "$rc" "1"

# A claim retains its explicit Compose project so later release and retained
# cleanup cannot be redirected by a newly written or changed .env.
PROJECT_REPO="${FIXTURES}/explicit-project-repo"
PROJECT_LOG="${FIXTURES}/explicit-project.log"
mkdir -p "$PROJECT_REPO"
: > "$PROJECT_LOG"
got="$(
  set +e
  trap '
    original_rc=$?
    set +e
    resolved="$(_lifecycle_compose_project_name "$PROJECT_REPO" smoke-project)"
    resolve_rc=$?
    printf "%s|%s\n" "$resolved" "$resolve_rc"
    exit "$original_rc"
  ' EXIT
  false
)" && rc=0 || rc=$?
expect_eq "explicit lifecycle project resolves successfully inside failure cleanup" \
  "${got}/${rc}" "smoke-project|0/1"
(
  export PROJECT_LOG
  _lifecycle_volume_helper_for_project() {
    local project="$2" command="$3"
    printf '%s|%s\n' "$project" "$command" >> "$PROJECT_LOG"
    case "$command" in
      current-host) return 0 ;;
      reserve-host) printf 'adopt' ;;
      wait-host|release-host|host-release-complete|clear-retained-host) return 0 ;;
      *) return 1 ;;
    esac
  }
  claim_lifecycle_operation "$PROJECT_REPO" setup smoke-project
  printf 'COMPOSE_PROJECT_NAME=redirected\n' > "$PROJECT_REPO/.env"
  finish_lifecycle_operation "$PROJECT_REPO" setup retain
  clear_retained_lifecycle_operation "$PROJECT_REPO" setup \
    "$JARVIS_SHARED_LIFECYCLE_ID"
) && rc=0 || rc=$?
expect_eq "explicit lifecycle project survives release and retained cleanup" "$rc" "0"
if grep -qv '^smoke-project|' "$PROJECT_LOG" \
   || ! grep -qxF 'smoke-project|release-host' "$PROJECT_LOG" \
   || ! grep -qxF 'smoke-project|clear-retained-host' "$PROJECT_LOG"; then
  printf 'FAIL: lifecycle release or cleanup changed its admitted project\n' >&2
  fail=1
else
  pass "lifecycle release and cleanup use the admitted explicit project"
fi

SETUP_LEASE_CLEANUP_FN="$(sed -n '/^cleanup_setup_lifecycle_exit() {/,/^}/p' "$SETUP_SCRIPT")"
JARVIS_SETUP_LEASE_CLEANUP_FN="$(sed -n '/^cleanup_jarvis_setup_lifecycle() {/,/^}/p' "$JARVIS_SETUP_SCRIPT")"
for _mutation_expected in "0 clear" "1 retain"; do
  _mutation="${_mutation_expected%% *}"
  _expected="${_mutation_expected#* }"
  got="$(
    set +e
    eval "$SETUP_LEASE_CLEANUP_FN"
    _SETUP_LIFECYCLE_CLAIMED=1
    _SETUP_MUTATION_STARTED="$_mutation"
    SCRIPT_DIR="$LIFECYCLE_REPO"
    finish_lifecycle_operation() { printf '%s\n' "$3"; }
    false
    cleanup_setup_lifecycle_exit
  )" && rc=0 || rc=$?
  expect_eq "setup.sh failed lifecycle exit uses ${_expected} after mutation=${_mutation}" \
    "${got}/${rc}" "${_expected}/1"

  got="$(
    set +e
    eval "$JARVIS_SETUP_LEASE_CLEANUP_FN"
    _SETUP_LIFECYCLE_CLAIMED=1
    _SETUP_MUTATION_STARTED="$_mutation"
    REPO_ROOT="$LIFECYCLE_REPO"
    finish_lifecycle_operation() { printf '%s\n' "$3"; }
    false
    cleanup_jarvis_setup_lifecycle
  )" && rc=0 || rc=$?
  expect_eq "jarvis-setup failed lifecycle exit uses ${_expected} after mutation=${_mutation}" \
    "${got}/${rc}" "${_expected}/1"
done

# The private lifecycle helper must use the volume name resolved by the exact
# managed Compose model. Overrides may intentionally replace the default name;
# guessing would create a second lock domain on Docker Desktop.
VOLUME_REPO="${FIXTURES}/lifecycle-volume-repo"
VOLUME_LOG="${FIXTURES}/lifecycle-volume-docker.log"
VOLUME_CREATED="${FIXTURES}/lifecycle-volume-created"
mkdir -p "$VOLUME_REPO/scripts"
printf 'services: {}\nvolumes:\n  postgres_backups:\n' > "$VOLUME_REPO/docker-compose.yml"
printf 'volumes:\n  postgres_backups:\n    name: family-backups\n' > "$VOLUME_REPO/docker-compose.custom.yml"
printf 'COMPOSE_PROJECT_NAME=family\nCOMPOSE_FILE=docker-compose.yml:docker-compose.custom.yml\n' \
  > "$VOLUME_REPO/.env"
printf 'POSTGRES_IMAGE=postgres:16.8\n' > "$VOLUME_REPO/versions.env"
cp "${SCRIPT_DIR}/../backup-lifecycle.sh" "$VOLUME_REPO/scripts/backup-lifecycle.sh"
VOLUME_BIN="$(fake_docker '
printf "%s\n" "$*" >> "$STUB_VOLUME_LOG"
case "${1:-} ${2:-}" in
  "compose "*) printf "%s\n" "$STUB_VOLUME_JSON"; exit 0 ;;
  "volume inspect")
    if [ "${STUB_VOLUME_EXISTS:-0}" != 1 ] && [ ! -e "$STUB_VOLUME_CREATED" ]; then exit 1; fi
    if printf "%s\n" "$@" | grep -q -- "--format"; then printf "%s\n" "$STUB_VOLUME_LABELS"; fi
    exit 0 ;;
  "volume create") : > "$STUB_VOLUME_CREATED"; exit 0 ;;
esac
exit 1
')"

: > "$VOLUME_LOG"
got="$(PATH="$VOLUME_BIN:$PATH" STUB_VOLUME_LOG="$VOLUME_LOG" \
  STUB_VOLUME_CREATED="$VOLUME_CREATED" STUB_VOLUME_EXISTS=1 \
  STUB_VOLUME_LABELS='family|postgres_backups' \
  STUB_VOLUME_JSON='{"volumes":{"postgres_backups":{"name":"family-backups"}}}' \
  COMPOSE_PROJECT_NAME=ambient-redirect \
  prepare_lifecycle_volume "$VOLUME_REPO")" && rc=0 || rc=$?
expect_eq "lifecycle volume follows a repo-local Compose override name" \
  "${got}/${rc}" "family-backups/0"
if grep -qF -- "-f $VOLUME_REPO/docker-compose.yml -f $VOLUME_REPO/docker-compose.custom.yml config --format json" "$VOLUME_LOG"; then
  pass "lifecycle volume resolution renders the exact managed Compose file set"
else
  printf 'FAIL: lifecycle volume resolution skipped the managed Compose override\n' >&2
  fail=1
fi

rm -f "$VOLUME_CREATED"
: > "$VOLUME_LOG"
got="$(PATH="$VOLUME_BIN:$PATH" STUB_VOLUME_LOG="$VOLUME_LOG" \
  STUB_VOLUME_CREATED="$VOLUME_CREATED" STUB_VOLUME_EXISTS=0 \
  STUB_VOLUME_LABELS='smoke-project|postgres_backups' \
  STUB_VOLUME_JSON='{"volumes":{"postgres_backups":{"name":"smoke-backups"}}}' \
  COMPOSE_PROJECT_NAME=ambient-redirect \
  prepare_lifecycle_volume "$VOLUME_REPO" smoke-project)" && rc=0 || rc=$?
expect_eq "explicit lifecycle project determines the managed volume" \
  "${got}/${rc}" "smoke-backups/0"
if grep -qF -- "-p smoke-project " "$VOLUME_LOG" \
   && grep -qF 'volume create --label com.docker.compose.project=smoke-project --label com.docker.compose.volume=postgres_backups smoke-backups' \
      "$VOLUME_LOG"; then
  pass "explicit lifecycle project determines Compose rendering and ownership labels"
else
  printf 'FAIL: explicit lifecycle project did not determine Compose ownership\n' >&2
  fail=1
fi

prepare_lifecycle_volume "$VOLUME_REPO" 'Invalid/Project' \
  >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "invalid explicit lifecycle project fails before Docker admission" "$rc" "2"

# Manual .env deletion must not create a lock-free fresh-install path. Compose
# can still resolve the default project/volume from the repository model and a
# sanitized empty env file, so an existing volume remains discoverable.
mv "$VOLUME_REPO/.env" "$VOLUME_REPO/.env.saved"
: > "$VOLUME_LOG"
got="$(PATH="$VOLUME_BIN:$PATH" STUB_VOLUME_LOG="$VOLUME_LOG" \
  STUB_VOLUME_CREATED="$VOLUME_CREATED" STUB_VOLUME_EXISTS=1 \
  STUB_VOLUME_LABELS='lifecycle-volume-repo|postgres_backups' \
  STUB_VOLUME_JSON='{"volumes":{"postgres_backups":{"name":"lifecycle-volume-repo_postgres_backups"}}}' \
  prepare_lifecycle_volume "$VOLUME_REPO")" && rc=0 || rc=$?
expect_eq "lifecycle volume remains resolvable when .env was deleted" \
  "${got}/${rc}" "lifecycle-volume-repo_postgres_backups/0"
if grep -qF -- "--env-file /dev/null -p lifecycle-volume-repo -f $VOLUME_REPO/docker-compose.yml config --format json" "$VOLUME_LOG"; then
  pass "missing .env uses the sanitized base Compose model for lifecycle admission"
else
  printf 'FAIL: missing .env did not resolve lifecycle admission through the sanitized base Compose model\n' >&2
  fail=1
fi
mv "$VOLUME_REPO/.env.saved" "$VOLUME_REPO/.env"

got="$(PATH="$VOLUME_BIN:$PATH" STUB_VOLUME_LOG="$VOLUME_LOG" \
  STUB_VOLUME_CREATED="$VOLUME_CREATED" STUB_VOLUME_EXISTS=1 \
  STUB_VOLUME_LABELS='other|postgres_backups' \
  STUB_VOLUME_JSON='{"volumes":{"postgres_backups":{"name":"family-backups"}}}' \
  prepare_lifecycle_volume "$VOLUME_REPO" 2>/dev/null)" && rc=0 || rc=$?
expect_eq "custom lifecycle volume with foreign ownership fails closed" "$rc" "4"

rm -f "$VOLUME_CREATED"
: > "$VOLUME_LOG"
got="$(PATH="$VOLUME_BIN:$PATH" STUB_VOLUME_LOG="$VOLUME_LOG" \
  STUB_VOLUME_CREATED="$VOLUME_CREATED" STUB_VOLUME_EXISTS=0 \
  STUB_VOLUME_LABELS='family|postgres_backups' \
  STUB_VOLUME_JSON='{"volumes":{"postgres_backups":{"name":"family-backups"}}}' \
  prepare_lifecycle_volume "$VOLUME_REPO")" && rc=0 || rc=$?
expect_eq "missing plain managed lifecycle volume is created with its resolved name" \
  "${got}/${rc}" "family-backups/0"
grep -qF 'volume create --label com.docker.compose.project=family --label com.docker.compose.volume=postgres_backups family-backups' "$VOLUME_LOG" \
  && pass "fresh lifecycle volume creation records exact Compose ownership" \
  || { printf 'FAIL: lifecycle volume creation did not use exact ownership labels\n' >&2; fail=1; }

: > "$VOLUME_LOG"
got="$(PATH="$VOLUME_BIN:$PATH" STUB_VOLUME_LOG="$VOLUME_LOG" \
  STUB_VOLUME_CREATED="$VOLUME_CREATED" STUB_VOLUME_EXISTS=1 \
  STUB_VOLUME_LABELS='family|postgres_backups' \
  STUB_VOLUME_JSON='{"volumes":{"postgres_backups":{"name":"family-backups","external":true}}}' \
  prepare_lifecycle_volume "$VOLUME_REPO" 2>/dev/null)" && rc=0 || rc=$?
expect_eq "external lifecycle volume is never treated as install-owned" "$rc" "4"

rm -f "$VOLUME_CREATED"
: > "$VOLUME_LOG"
got="$(PATH="$VOLUME_BIN:$PATH" STUB_VOLUME_LOG="$VOLUME_LOG" \
  STUB_VOLUME_CREATED="$VOLUME_CREATED" STUB_VOLUME_EXISTS=0 \
  STUB_VOLUME_LABELS='family|postgres_backups' \
  STUB_VOLUME_JSON='{"volumes":{"postgres_backups":{"name":"family-backups","driver_opts":{"type":"tmpfs"}}}}' \
  prepare_lifecycle_volume "$VOLUME_REPO" 2>/dev/null)" && rc=0 || rc=$?
expect_eq "missing specially configured lifecycle volume is not recreated incorrectly" "$rc" "4"
if ! grep -qF 'volume create ' "$VOLUME_LOG"; then
  pass "unsupported lifecycle volume creation settings remain untouched"
else
  printf 'FAIL: configured lifecycle volume was silently recreated\n' >&2
  fail=1
fi

if grep -qF 'secrets/.jarvis-lifecycle-operation' "${SCRIPT_DIR}/../setup_lib.sh" \
   || ! grep -qF 'type=volume,src=${volume},dst=/backups' "${SCRIPT_DIR}/../setup_lib.sh" \
   || ! grep -qF 'docker run --rm -d --network none --read-only' "${SCRIPT_DIR}/../setup_lib.sh"; then
  printf 'FAIL: lifecycle helper is not confined to the private Docker named volume\n' >&2
  fail=1
else
  pass "cross-actor lifecycle ownership uses a detached named-volume helper"
fi

(
  export JARVIS_CLI_CONFIG_DIR="$LIFECYCLE_CONFIG"
  claim_host_lifecycle_lock "$LIFECYCLE_REPO"
  claim_host_lifecycle_lock "$LIFECYCLE_REPO_OTHER"
) >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "inherited host lock cannot be reused for a different install" "$rc" "2"

# Tier-4 uninstall may unlink and recreate the clone. The external install
# mutex remains authoritative across that split.
_split_marker="${FIXTURES}/split-host-held"
(
  export JARVIS_CLI_CONFIG_DIR="$LIFECYCLE_CONFIG"
  claim_host_lifecycle_lock "$LIFECYCLE_REPO"
  : > "$_split_marker"
  sleep 3
) &
_split_holder=$!
for _attempt in $(seq 1 100); do
  [ -f "$_split_marker" ] && break
  sleep 0.02
done
mv "$LIFECYCLE_REPO" "${LIFECYCLE_REPO}.removed"
mkdir -p "$LIFECYCLE_REPO"
(
  exec 7>&- 8>&-
  unset JARVIS_HOST_LIFECYCLE_LOCK_HELD JARVIS_SHARED_LIFECYCLE_LOCK_HELD
  export JARVIS_CLI_CONFIG_DIR="$LIFECYCLE_CONFIG"
  claim_host_lifecycle_lock "$LIFECYCLE_REPO"
) >/dev/null 2>&1 && rc=0 || rc=$?
expect_eq "external host mutex survives clone replacement" "$rc" "3"
kill "$_split_holder" 2>/dev/null || true
wait "$_split_holder" 2>/dev/null || true

# === upsert_env_var (atomic in-place .env write) ============================
# The colocated-temp writer fills an empty placeholder IN PLACE (no duplicate),
# rewrites an existing value in place, appends an absent key, and leaves the
# surrounding lines untouched. It rewrites ./.env in the current directory, so
# each case runs inside a private fixture dir.
UPSERT_DIR="$(mktemp -d "${FIXTURES}/upsert.XXXXXX")"
cat > "${UPSERT_DIR}/.env" <<'ENV'
KEEP_BEFORE=1
JARVIS_API_KEY=
KEEP_AFTER=2
ENV
got="$(cd "$UPSERT_DIR"; upsert_env_var JARVIS_API_KEY deadbeef; upsert_env_var NEW_KEY added; cat .env)"
want="$(printf '%s\n' 'KEEP_BEFORE=1' 'JARVIS_API_KEY=deadbeef' 'KEEP_AFTER=2' 'NEW_KEY=added')"
expect_eq "upsert_env_var fills the placeholder in place and appends absent keys" "$got" "$want"
# Re-upserting an existing key rewrites the value without adding a duplicate line.
got="$(cd "$UPSERT_DIR"; upsert_env_var JARVIS_API_KEY feedface; grep -c '^JARVIS_API_KEY=' .env)"
expect_eq "upsert_env_var never duplicates an existing key" "$got" "1"
got="$(cd "$UPSERT_DIR"; grep '^JARVIS_API_KEY=' .env)"
expect_eq "upsert_env_var rewrites the existing value in place" "$got" "JARVIS_API_KEY=feedface"

# === ensure_state_dir (durable lifecycle-state directory) ====================
# Compose bind-mounts JARVIS_STATE_DIR into the backup sidecar, so the directory
# this helper creates and the path .env records must always be the same one.
STATE_HOME="$(mktemp -d "${FIXTURES}/statehome.XXXXXX")"
new_state_repo() {  # new_state_repo NAME -> a fresh repo dir carrying an .env
  local dir="${FIXTURES}/${1}"
  mkdir -p "$dir"
  printf 'KEEP=1\n' > "${dir}/.env"
  printf '%s' "$dir"
}

ESD_REPO="$(new_state_repo esd-fresh)"
got="$(XDG_STATE_HOME="$STATE_HOME" ensure_state_dir "$ESD_REPO")"
recorded="$(sed -n 's/^JARVIS_STATE_DIR=//p' "${ESD_REPO}/.env")"
expect_eq "ensure_state_dir records the project-scoped state directory" \
  "$recorded" "${STATE_HOME}/jarvis-research/esd-fresh"
expect_eq "ensure_state_dir creates that directory mode 700" \
  "$(stat -c '%a' "$recorded")" "700"
expect_eq "ensure_state_dir reports the directory it recorded" \
  "$got" "Recorded durable state directory: ${recorded}"
XDG_STATE_HOME="$STATE_HOME" ensure_state_dir "$ESD_REPO" >/dev/null
expect_eq "ensure_state_dir never writes a second JARVIS_STATE_DIR line" \
  "$(grep -c '^JARVIS_STATE_DIR=' "${ESD_REPO}/.env")" "1"

# A recorded path that no longer exists — a sibling uninstall, a state cleaner —
# must be recreated AS RECORDED. Creating the computed path instead leaves
# compose's real bind source missing, and Docker then creates it root-owned.
ESD_MOVED="$(new_state_repo esd-moved)"
MOVED_STATE="${FIXTURES}/moved-state-dir"
printf 'JARVIS_STATE_DIR=%s\n' "$MOVED_STATE" >> "${ESD_MOVED}/.env"
XDG_STATE_HOME="$STATE_HOME" ensure_state_dir "$ESD_MOVED" >/dev/null
if [ -d "$MOVED_STATE" ] && [ ! -e "${STATE_HOME}/jarvis-research/esd-moved" ]; then
  pass "ensure_state_dir recreates the recorded path and not the computed one"
else
  printf 'FAIL: ensure_state_dir ignored the recorded path (recorded=%s computed=%s)\n' \
    "$([ -d "$MOVED_STATE" ] && echo present || echo missing)" \
    "$([ -e "${STATE_HOME}/jarvis-research/esd-moved" ] && echo present || echo missing)" >&2
  fail=1
fi
expect_eq "ensure_state_dir hardens a recreated recorded directory to 700" \
  "$(stat -c '%a' "$MOVED_STATE")" "700"

# .env values may be quoted; a literal quoted name is not the path compose mounts.
ESD_QUOTED="$(new_state_repo esd-quoted)"
QUOTED_STATE="${FIXTURES}/quoted-state-dir"
printf 'JARVIS_STATE_DIR="%s"\n' "$QUOTED_STATE" >> "${ESD_QUOTED}/.env"
XDG_STATE_HOME="$STATE_HOME" ensure_state_dir "$ESD_QUOTED" >/dev/null
if [ -d "$QUOTED_STATE" ] && [ ! -e "${FIXTURES}/\"quoted-state-dir\"" ]; then
  pass "ensure_state_dir unquotes a recorded value instead of taking it literally"
else
  printf 'FAIL: ensure_state_dir treated a quoted recorded value literally\n' >&2
  fail=1
fi

# Two clones on one host must never share a state directory: a purge of the first
# would otherwise delete the second's signed-restore ratchet.
ESD_OTHER="$(new_state_repo esd-other-clone)"
XDG_STATE_HOME="$STATE_HOME" ensure_state_dir "$ESD_OTHER" >/dev/null
expect_eq "two clones resolve to different state directories" \
  "$(sed -n 's/^JARVIS_STATE_DIR=//p' "${ESD_OTHER}/.env")" \
  "${STATE_HOME}/jarvis-research/esd-other-clone"

# A clone whose sanitized basename is not a valid Compose project fails rather
# than inventing a shared name — and both callers must survive that failure.
ESD_BAD="$(new_state_repo _esd-invalid)"
rc=0
XDG_STATE_HOME="$STATE_HOME" ensure_state_dir "$ESD_BAD" >/dev/null 2>&1 || rc=$?
expect_eq "ensure_state_dir fails rather than inventing a project name" "$rc" "1"
expect_eq "setup.sh calls ensure_state_dir non-fatally at both launcher-install sites" \
  "$(grep -c '^ *ensure_state_dir "\$SCRIPT_DIR" || warn ' "$SETUP_SCRIPT")" "2"

# === warn_if_launcher_unreachable (installed command must be findable) =======
# A launcher a login shell cannot resolve is not installed. The check never
# prompts without a terminal to answer at, and never edits a startup file it
# could not ask about: a piped or CI install must finish silently and green.
PATHCHK_HOME="$(mktemp -d "${FIXTURES}/pathchk.XXXXXX")"
PATHCHK_BIN="${PATHCHK_HOME}/.local/bin"
PATHCHK_LINE="export PATH=\"${PATHCHK_BIN}:\$PATH\""
mkdir -p "$PATHCHK_BIN"

out="$(
  (
    PATH="${PATHCHK_BIN}:${PATH}"
    HOME="$PATHCHK_HOME" SHELL=/bin/bash JARVIS_CLI_BIN_DIR="$PATHCHK_BIN" \
      NON_INTERACTIVE=0 warn_if_launcher_unreachable
  ) < /dev/null 2>&1
)" && rc=0 || rc=$?
expect_eq "a launcher directory already on PATH says nothing" "${rc}|${out}" "0|"

: > "${PATHCHK_HOME}/.bashrc"
out="$(
  (
    HOME="$PATHCHK_HOME" SHELL=/bin/bash JARVIS_CLI_BIN_DIR="$PATHCHK_BIN" \
      NON_INTERACTIVE=0 warn_if_launcher_unreachable
  ) < /dev/null 2>&1
)" && rc=0 || rc=$?
expect_eq "an unreachable launcher warns without failing the install" "$rc" "0"
case "$out" in
  *'not on your PATH'*"$PATHCHK_LINE"*)
    pass "an unreachable launcher names the directory and the exact PATH line" ;;
  *)
    printf 'FAIL: unreachable-launcher guidance lacks the directory or the PATH line\n%s\n' \
      "$out" >&2
    fail=1 ;;
esac
expect_eq "a prompt-less install never edits the shell startup file" \
  "$(wc -c < "${PATHCHK_HOME}/.bashrc")" "0"

out="$(
  (
    HOME="$PATHCHK_HOME" SHELL=/bin/bash JARVIS_CLI_BIN_DIR="$PATHCHK_BIN" \
      NON_INTERACTIVE=1 warn_if_launcher_unreachable
  ) 2>&1
)" && rc=0 || rc=$?
expect_eq "a non-interactive install still reports the unreachable launcher" \
  "${rc}|$(wc -c < "${PATHCHK_HOME}/.bashrc")" "0|0"
case "$out" in
  *'Add it to '*)
    printf 'FAIL: a non-interactive install prompted for the PATH line\n%s\n' "$out" >&2
    fail=1 ;;
  *) pass "a non-interactive install never prompts for the PATH line" ;;
esac

# A startup file that already carries the line is never given a second copy.
printf '%s\n' "$PATHCHK_LINE" > "${PATHCHK_HOME}/.bashrc"
out="$(
  (
    HOME="$PATHCHK_HOME" SHELL=/bin/bash JARVIS_CLI_BIN_DIR="$PATHCHK_BIN" \
      NON_INTERACTIVE=0 warn_if_launcher_unreachable
  ) < /dev/null 2>&1
)" && rc=0 || rc=$?
expect_eq "an already-recorded PATH line is reported, not appended again" \
  "${rc}|$(grep -cxF "$PATHCHK_LINE" "${PATHCHK_HOME}/.bashrc")" "0|1"
case "$out" in
  *'already carries that PATH line'*)
    pass "an already-recorded PATH line points at opening a new terminal" ;;
  *)
    printf 'FAIL: an already-recorded PATH line was not recognised\n%s\n' "$out" >&2
    fail=1 ;;
esac
expect_eq "setup.sh checks launcher reachability non-fatally at both launcher-install sites" \
  "$(grep -c '^ *warn_if_launcher_unreachable || true$' "$SETUP_SCRIPT")" "2"

# The lifecycle CLI must record the directory on both update routes, and on the
# resume route BEFORE the backup sidecar is recreated — compose reads the value
# from .env at that moment, so a call placed after it would mount nothing.
LIFECYCLE_CLI="${SCRIPT_DIR}/../jarvis-research.sh"
expect_eq "the update command records the state directory on both routes" \
  "$(grep -c '^ *ensure_state_dir "\$REPO" || warn ' "$LIFECYCLE_CLI")" "2"
esd_line="$(grep -n '^ *ensure_state_dir "\$REPO" || warn ' "$LIFECYCLE_CLI" | tail -1 | cut -d: -f1)"
sidecar_line="$(grep -n '_activate_selected_backup_sidecar; then' "$LIFECYCLE_CLI" | head -1 | cut -d: -f1)"
resume_line="$(grep -n '^_resume_transaction() {' "$LIFECYCLE_CLI" | head -1 | cut -d: -f1)"
if [ -n "$esd_line" ] && [ -n "$sidecar_line" ] && [ -n "$resume_line" ] \
   && [ "$resume_line" -lt "$esd_line" ] && [ "$esd_line" -lt "$sidecar_line" ]; then
  pass "the resumed update records the state directory before recreating the sidecar"
else
  printf 'FAIL: state-dir recording is not between the resume entry (%s) and the sidecar recreate (%s): %s\n' \
    "$resume_line" "$sidecar_line" "$esd_line" >&2
  fail=1
fi

# The version and image selector form one identity pair. Their dedicated writer
# replaces duplicate legacy rows through one final rename and rejects invalid
# input without changing the file.
IDENTITY_DIR="$(mktemp -d "${FIXTURES}/identity.XXXXXX")"
cat > "${IDENTITY_DIR}/.env" <<'ENV'
KEEP_BEFORE=1
JARVIS_VERSION=1.0.0
JARVIS_IMAGE_TAG=1.0.0
JARVIS_VERSION=duplicate
KEEP_AFTER=2
ENV
got="$(
  cd "$IDENTITY_DIR"
  upsert_app_identity 2.3.4 0123456789abcdef0123456789abcdef01234567
  cat .env
)"
want="$(printf '%s\n' \
  'KEEP_BEFORE=1' \
  'JARVIS_VERSION=2.3.4' \
  'JARVIS_IMAGE_TAG=0123456789abcdef0123456789abcdef01234567' \
  'KEEP_AFTER=2')"
expect_eq "upsert_app_identity replaces both identities and removes duplicates" \
  "$got" "$want"
identity_before="$(cksum < "${IDENTITY_DIR}/.env")"
(cd "$IDENTITY_DIR"; upsert_app_identity invalid invalid) && rc=0 || rc=$?
expect_eq "upsert_app_identity rejects invalid identities" "$rc" "2"
identity_after="$(cksum < "${IDENTITY_DIR}/.env")"
expect_eq "an invalid identity leaves .env byte-identical" \
  "$identity_after" "$identity_before"

# === _lifecycle_path_inside_repo (shared path-containment helper) ============
# Literal prefix containment on a trailing-slash-normalized path: a true subpath
# is inside; a sibling that only shares a string prefix (/a/bc vs /a/b) is NOT;
# and the repo root equals itself (inside).
_lifecycle_path_inside_repo /a/b/c /a/b && rc=0 || rc=$?
expect_eq "_lifecycle_path_inside_repo: a subpath is inside (rc 0)" "$rc" "0"
_lifecycle_path_inside_repo /a/bc /a/b && rc=0 || rc=$?
expect_eq "_lifecycle_path_inside_repo: a sibling-prefix path is NOT inside (rc 1)" "$rc" "1"
_lifecycle_path_inside_repo /a/b /a/b && rc=0 || rc=$?
expect_eq "_lifecycle_path_inside_repo: the repo root equals itself (inside, rc 0)" "$rc" "0"

# === jarvis-setup ingress-IP derivation ordering (wrapper-leg fix) ============
# jarvis-setup must derive the ingress IP peers into .env before Compose reads
# it, so the wrapper install (which never runs setup.sh's inline
# allocate_ingress_ips) writes the JARVIS_*_IP proxy pins the pull/up consumes.
jarvis_ingress_line="$(grep -nE '^sync_ingress_ips_from_env' "$JARVIS_SETUP_SCRIPT" | head -1 | cut -d: -f1 || true)"
jarvis_compose_line="$(grep -nE '\$\{COMPOSE\}[[:space:]]+(pull|up)' "$JARVIS_SETUP_SCRIPT" | head -1 | cut -d: -f1)"
if [ -n "$jarvis_ingress_line" ] && [ -n "$jarvis_compose_line" ] \
   && [ "$jarvis_ingress_line" -lt "$jarvis_compose_line" ]; then
  pass "jarvis-setup derives ingress IPs before the first Compose pull/up"
else
  printf 'FAIL: jarvis-setup ingress-IP sync (%s) does not precede the first Compose pull/up (%s)\n' \
    "$jarvis_ingress_line" "$jarvis_compose_line" >&2
  fail=1
fi

# === setup.sh consumes every address the allocator emits =====================
# setup.sh reads allocate_ingress_ips into positional variables and persists
# each one. A field added to the allocator without a matching variable here
# silently shifts every later pin and collapses the trailing addresses into the
# final variable, so the count is asserted against the allocator's real output.
ingress_field_n="$(allocate_ingress_ips 10.88.40.0/24 2>/dev/null | wc -w)"
setup_read_n="$(awk '/^read -r JARVIS_NET_GATEWAY_IP_VALUE/{c=1} c{print; if ($0 !~ /\\$/) exit}' \
  "$SETUP_SCRIPT" | tr -d '\\' | sed 's/^read -r //; s/<<<.*//' | wc -w)"
expect_eq "setup.sh reads every address allocate_ingress_ips emits" \
  "$setup_read_n" "$ingress_field_n"
if grep -qE '^upsert_env_var JARVIS_TELEGRAM_BOT_IP ' "$SETUP_SCRIPT"; then
  pass "setup.sh persists the derived Telegram bot address"
else
  printf 'FAIL: setup.sh does not upsert JARVIS_TELEGRAM_BOT_IP\n' >&2
  fail=1
fi

# =============================================================================

if [ "$fail" -ne 0 ]; then
  printf '\nsetup_lib helpers: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nsetup_lib helpers: all %s checks passed\n' "$pass_n"
