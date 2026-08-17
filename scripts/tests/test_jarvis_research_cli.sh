#!/usr/bin/env bash
# Behavioral contract tests for scripts/jarvis-research.sh. Git, Docker, and
# Compose are replaced with recording stubs, and the CLI runs against a temporary
# installation. State, backup, and polling paths are redirected to isolated
# fixtures, so the suite needs no network, Docker daemon, or persistent repository.
#
# The refusal matrix and the transaction ordering checks are the specification:
# every `update` refusal must exit 1 and leave the stub log free of a mutating
# git verb (merge/checkout/reset) or a compose mutation (pull/up/build), and the
# pending-transaction file must be on disk before the fast-forward merge runs.
#
# Run: bash scripts/tests/test_jarvis_research_cli.sh   (exit 0 = pass)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CLI="${REPO_ROOT}/scripts/jarvis-research.sh"
LIB="${REPO_ROOT}/scripts/setup_lib.sh"
UPDATE_SCRIPT="${REPO_ROOT}/update.sh"
LIFECYCLE_HELPER="${REPO_ROOT}/scripts/backup-lifecycle.sh"
BACKUP_SCRIPT="${REPO_ROOT}/scripts/backup.sh"

fail=0
pass_n=0
pass() { pass_n=$((pass_n + 1)); printf 'PASS: %s\n' "$1"; }
check_fail() { printf 'FAIL: %s\n' "$1" >&2; fail=1; }
has()  { grep -q -- "$2" <<<"$1"; }
hasF() { grep -qF -- "$2" <<<"$1"; }
want() { if has "$1" "$2"; then pass "$3"; else check_fail "$3 :: missing /$2/ in <<<$1>>>"; fi; }
lack() { if has "$1" "$2"; then check_fail "$3 :: unexpected /$2/ in <<<$1>>>"; else pass "$3"; fi; }

# Exercise the recovery epilogues without the wider transaction fixture. This
# keeps their user-facing contract independently testable when another lane is
# changing lifecycle-helper integration.
for recovery_fn in _rollback_pin_lines _schema_not_safe_notice _failure_epilogue _success_epilogue; do
  recovery_src="$(sed -n "/^${recovery_fn}() {/,/^}/p" "$CLI")"
  if [ -z "$recovery_src" ]; then
    printf 'FAIL: could not extract %s from %s\n' "$recovery_fn" "$CLI" >&2
    exit 1
  fi
  eval "$recovery_src"
done
REPO=/srv/jarvis-family
PUBLISHED_SERVICES_BASE=(platform_api paper_ingestion learning_engine dashboard restore-uploader)
PUBLISHED_SERVICE_TELEGRAM=telegram_bot
TXN_FROM_VERSION=1.1.2
MIGRATIONS_RAN=1
C_BOLD=""; C_RESET=""; C_YELLOW=""; C_RED=""
err() { printf '[ERROR] %s\n' "$*" >&2; }
_active_profiles() { printf 'tunnel telegram'; }
_txn_field() { printf '1.1.2'; }
cmd_doctor() { :; }

out="$(_failure_epilogue v1.1.3 2>&1)"
data_line="$(printf '%s\n' "$out" | grep -nF 'Admin > Backups' | cut -d: -f1)"
image_line="$(printf '%s\n' "$out" | grep -nF 'Application-image recovery (not a full release rollback)' | cut -d: -f1)"
if has "$out" 'JARVIS_IMAGE_TAG=1.1.2 docker compose --profile tunnel --profile telegram pull' \
   && has "$out" 'platform_api paper_ingestion learning_engine dashboard restore-uploader telegram_bot' \
   && has "$out" 'Repository: /srv/jarvis-family' \
   && has "$out" 'do not move the Git checkout or restore stored data' \
   && has "$out" 'A data-changing migration may have run' \
   && [ -n "$data_line" ] && [ -n "$image_line" ] && [ "$data_line" -lt "$image_line" ] \
   && has "$out" 'cd /srv/jarvis-family && jarvis-research update --resume v1.1.3' \
   && ! has "$out" '<previous-version>' \
   && ! has "$out" 'scripts/restore.sh'; then
  pass "failure_epilogue_contract_is_exact_scoped_ordered_and_resumable"
else
  check_fail "failure epilogue contract: data=$data_line image=$image_line out=<<<$out>>>"
fi

out="$(_success_epilogue v1.1.3 2>&1)"
if ! has "$out" '<previous-version>' \
   && ! has "$out" 'full release rollback' \
   && ! has "$out" 'If you need to roll back' \
   && has "$out" 'Now running v1.1.3'; then
  pass "success_epilogue_omits_speculative_release_rollback"
else
  check_fail "success epilogue printed unsupported rollback guidance: out=<<<$out>>>"
fi

# =============================================================================
# The cleanliness gate, against real Git. `update` refuses on this function, and
# the recording git stub below cannot model pathspecs, so its policy is proven
# here with real repositories instead.
# =============================================================================
clean_src="$(sed -n '/^_require_clean_main_checkout() {/,/^}/p' "$CLI")"
[ -n "$clean_src" ] || { printf 'FAIL: could not extract _require_clean_main_checkout\n' >&2; exit 1; }
eval "$clean_src"
# The extracted function refuses by calling die, which exits. Every case below
# runs it in a subshell, so a refusal is observable as a non-zero status.
die() { err "$1"; printf '        %s\n' "${2:-}" >&2; exit 1; }

CLEAN_FIXTURE="$(mktemp -d)"
(
  cd "$CLEAN_FIXTURE" || exit 1
  git init -q -b main; git config user.email t@t; git config user.name t
  mkdir -p secrets; touch docker-compose.yml secrets/.gitkeep
  printf 'secrets/*.txt\n' > .gitignore
  git add -A; git commit -qm init
)

# marker-only must be accepted by the installed updater
: > "$CLEAN_FIXTURE/secrets/manifest-hmac-required"
out="$( cd "$CLEAN_FIXTURE" && _require_clean_main_checkout 2>&1 )"; rc=$?
if [ "$rc" -eq 0 ]; then
  pass "the installed updater accepts a marker-only checkout"
else
  check_fail "installed updater marker-only: rc=$rc out=<<<$out>>>"
fi

# a second untracked file must still be refused, and named
touch "$CLEAN_FIXTURE/OTHER-DIRT.bin"
out="$( cd "$CLEAN_FIXTURE" && _require_clean_main_checkout 2>&1 )"; rc=$?
rm -f "$CLEAN_FIXTURE/OTHER-DIRT.bin"
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'OTHER-DIRT.bin'; then
  pass "the installed updater still refuses unrelated dirt, and names it"
else
  check_fail "installed updater unrelated dirt: rc=$rc out=<<<$out>>>"
fi

# Git inspection failure must fail closed. Only the status query is broken here;
# an unusable GIT_DIR would refuse at the branch check and prove nothing.
out="$( cd "$CLEAN_FIXTURE" \
  && GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=status.showUntrackedFiles GIT_CONFIG_VALUE_0=bogus \
     _require_clean_main_checkout 2>&1 )"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'Could not inspect'; then
  pass "the installed updater fails closed when Git inspection fails"
else
  check_fail "installed updater fail-closed: rc=$rc out=<<<$out>>>"
fi

# The status query is not the only Git call that can fail. An unreadable index
# breaks the ls-files fences, which run first and would otherwise report "no
# hidden flags" for an index Git cannot parse.
# The scratch index lives in its own directory: a fixed path under TMPDIR is
# shared between concurrent runs of this suite, which corrupt each other.
BOGUS_INDEX_DIR="$(mktemp -d)"
printf 'not-an-index\n' > "$BOGUS_INDEX_DIR/index"
out="$( cd "$CLEAN_FIXTURE" \
  && GIT_INDEX_FILE="$BOGUS_INDEX_DIR/index" \
     _require_clean_main_checkout 2>&1 )"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "index flags"; then
  pass "the installed updater fails closed when the index is unreadable"
else
  check_fail "installed updater unreadable index: rc=$rc out=<<<$out>>>"
fi
rm -rf "$BOGUS_INDEX_DIR"

# A Git too old to know --absolute-git-dir echoes the unrecognised flag and exits
# 0, so the in-progress fence would probe a path that cannot exist. The shim
# reproduces that behaviour on any Git.
OLDGIT_DIR="$(mktemp -d)"
cat > "$OLDGIT_DIR/git" <<SHIM
#!/usr/bin/env bash
for arg in "\$@"; do
  if [ "\$arg" = --absolute-git-dir ]; then printf '%s\n' --absolute-git-dir; exit 0; fi
done
exec "$(command -v git)" "\$@"
SHIM
chmod +x "$OLDGIT_DIR/git"
out="$( cd "$CLEAN_FIXTURE" \
  && PATH="$OLDGIT_DIR:$PATH" _require_clean_main_checkout 2>&1 )"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'Upgrade Git'; then
  pass "the installed updater refuses a Git without --absolute-git-dir"
else
  check_fail "installed updater old git: rc=$rc out=<<<$out>>>"
fi
rm -rf "$OLDGIT_DIR"
rm -rf "$CLEAN_FIXTURE"

# Differential oracle. legacy() is the policy we are replacing, verbatim.
legacy() { [ -z "$(git status --porcelain 2>/dev/null)" ]; }
shipped() { ( _require_clean_main_checkout ) >/dev/null 2>&1; }

ORACLE_ROOT="$(mktemp -d)"
oracle_reset() {
  rm -rf "$ORACLE_ROOT/w"; mkdir -p "$ORACLE_ROOT/w/secrets"
  (
    cd "$ORACLE_ROOT/w" || exit 1
    git init -q -b main; git config user.email t@t; git config user.name t
    mkdir -p sub; touch docker-compose.yml secrets/.gitkeep sub/keep.txt
    printf 'secrets/*.txt\n' > .gitignore
    git add -A; git commit -qm init
  )
}
M=secrets/manifest-hmac-required

# name:setup — every state the two policies could disagree on.
ORACLE_STATES=(
  "clean:true"
  "marker_only::> \$W/$M"
  "marker_dir:mkdir -p \$W/$M/d && echo x > \$W/$M/d/p"
  "marker_symlink:ln -s /etc/hostname \$W/$M"
  "untracked_root:touch \$W/ROOT.bin"
  "untracked_subdir:touch \$W/sub/S.bin"
  "marker_plus_untracked::> \$W/$M; touch \$W/ROOT.bin"
  "modified_tracked:echo x >> \$W/docker-compose.yml"
  "staged_add:touch \$W/N.bin && git -C \$W add N.bin"
  "staged_delete:git -C \$W rm -q --cached sub/keep.txt"
  "deleted_tracked:rm \$W/sub/keep.txt"
  "type_change:rm \$W/docker-compose.yml && ln -s /etc/hostname \$W/docker-compose.yml"
  "ignored_only:touch \$W/secrets/api.txt"
  "marker_tracked:: > \$W/$M; git -C \$W add -f $M; git -C \$W -c user.email=t@t -c user.name=t commit -qm t"
  "rebase_detaches_head:echo z > \$W/n.txt; git -C \$W add n.txt; git -C \$W -c user.email=t@t -c user.name=t commit -qm n; GIT_SEQUENCE_EDITOR='sed -i \"1s/^pick/break/\"' git -C \$W rebase -qi HEAD~1"
  "merge_in_progress:git -C \$W rev-parse HEAD > \$W/.git/MERGE_HEAD"
  "skip_worktree:git -C \$W update-index --skip-worktree docker-compose.yml && echo h >> \$W/docker-compose.yml"
  "assume_unchanged:git -C \$W update-index --assume-unchanged docker-compose.yml && echo h >> \$W/docker-compose.yml"
)
# States where the NEW policy may accept what the old one refused. Exactly one.
ORACLE_NARROWED="marker_only"
# States where the NEW policy may refuse what the old one accepted. Declared strengthening.
ORACLE_STRENGTHENED="rebase_detaches_head merge_in_progress skip_worktree assume_unchanged marker_tracked"

oracle_fail=0
observed_narrowed=""
observed_strengthened=""
for entry in "${ORACLE_STATES[@]}"; do
  name="${entry%%:*}"; setup="${entry#*:}"
  oracle_reset; W="$ORACLE_ROOT/w"; eval "$setup" >/dev/null 2>&1 || true
  if ( cd "$W" && legacy ); then old=ACCEPT; else old=REFUSE; fi
  if ( cd "$W" && shipped ); then new=ACCEPT; else new=REFUSE; fi
  case "$old->$new" in
    "REFUSE->ACCEPT")
      observed_narrowed="${observed_narrowed}${name} "
      printf '%s\n' "$ORACLE_NARROWED" | grep -qx "$name" \
        || { printf 'ORACLE: undeclared narrowing at state %s\n' "$name" >&2; oracle_fail=1; } ;;
    "ACCEPT->REFUSE")
      observed_strengthened="${observed_strengthened}${name} "
      printf '%s\n' $ORACLE_STRENGTHENED | grep -qx "$name" \
        || { printf 'ORACLE: undeclared strengthening at state %s\n' "$name" >&2; oracle_fail=1; } ;;
  esac
done
# Every declared narrowing and strengthening must actually occur, or the list is
# stale and would silently license a divergence nobody measured.
for name in $ORACLE_NARROWED; do
  printf '%s\n' $observed_narrowed | grep -qx "$name" \
    || { printf 'ORACLE: declared narrowing %s never occurred\n' "$name" >&2; oracle_fail=1; }
done
for name in $ORACLE_STRENGTHENED; do
  printf '%s\n' $observed_strengthened | grep -qx "$name" \
    || { printf 'ORACLE: declared strengthening %s never occurred\n' "$name" >&2; oracle_fail=1; }
done
rm -rf "$ORACLE_ROOT"
if [ "$oracle_fail" -eq 0 ]; then
  pass "the new cleanliness policy is a minimal, declared narrowing of the old one"
else
  check_fail "differential oracle reported undeclared policy divergence"
fi

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
STUB="$ROOT/stub"
mkdir -p "$STUB"
FAST_SLEEP_BIN="$ROOT/fast-sleep-bin"
REAL_SLEEP_BIN="$(command -v sleep)"
mkdir -p "$FAST_SLEEP_BIN"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [ "${1:-}" = 3 ]; then exit 0; fi' \
  'exec "$JARVIS_TEST_REAL_SLEEP" "$@"' \
  > "$FAST_SLEEP_BIN/sleep"
chmod +x "$FAST_SLEEP_BIN/sleep"
SOURCE_SHA="1111111111111111111111111111111111111111"
TARGET_SHA="2222222222222222222222222222222222222222"
OTHER_SHA="3333333333333333333333333333333333333333"

# =============================================================================
# Stubs: git + docker + docker compose, logging every call to $STUB_LOG.
# =============================================================================
cat > "$STUB/git" <<'GIT'
#!/usr/bin/env bash
log() { [ -n "${STUB_LOG:-}" ] && printf 'git %s\n' "$*" >> "$STUB_LOG"; }
case "${1:-} ${2:-}" in
  "symbolic-ref --short") printf '%s\n' "${STUB_BRANCH:-main}"; exit 0 ;;
  "status --porcelain")
    # The updater's status query excludes one product-managed path. Honour that
    # pathspec, so a checkout dirty with nothing else is observably clean here.
    dirt="${STUB_DIRTY:-}"
    for arg in "$@"; do
      case "$arg" in
        ":(top,exclude)"*)
          dirt="$(printf '%s' "$dirt" | grep -vF -- "${arg#:(top,exclude)}" || true)" ;;
      esac
    done
    printf '%s' "$dirt"; [ -n "$dirt" ] && printf '\n'; exit 0 ;;
  "remote get-url")       printf '%s\n' "${STUB_REMOTE:-git@github.com:limitcycle-oss/jarvis-rd-assistant.git}"; exit 0 ;;
esac
if [ "${1:-}" = -C ]; then
  repo="$2"; shift 2
  case "${1:-}" in
    rev-parse)
      case "${2:-}" in
        HEAD) cat "$repo/.stub-head" 2>/dev/null || exit 1 ;;
        *) printf '%s\n' "${STUB_TARGET_SHA:-2222222222222222222222222222222222222222}" ;;
      esac
      exit 0 ;;
    *) exit 1 ;;
  esac
fi
case "${1:-}" in
  describe)
    [ -n "${STUB_EXACT_TAG:-}" ] || exit 1
    printf '%s\n' "$STUB_EXACT_TAG"
    exit 0 ;;
  rev-parse)
    # HEAD is durable across the CLI's post-merge exec. Every fixture release tag
    # peels to the same deterministic target commit unless a test overrides it.
    case "${2:-}" in
      HEAD) cat "$STUB_HEAD_FILE" ;;
      # Answer only with a real Git directory, and fail like Git does when there
      # is none. Substituting the working tree would make the guard's directory
      # assertion unfailable and would point its marker probe at the wrong place.
      --absolute-git-dir) [ -d "$PWD/.git" ] || exit 128; printf '%s\n' "$PWD/.git" ;;
      *)    printf '%s\n' "${STUB_TARGET_SHA:-2222222222222222222222222222222222222222}" ;;
    esac
    exit 0 ;;
  fetch) log "$*"; exit 0 ;;
  ls-remote)
    # latest_stable_tag reads this.
    printf 'sha refs/tags/%s\n' "${STUB_TAGS:-v1.1.3}"
    exit 0 ;;
  merge-base)
    # merge-base --is-ancestor HEAD <ref>
    log "$*"
    exit "${STUB_ANCESTOR:-0}" ;;
  diff)
    # diff --name-only HEAD..<ref> -- db/migrations/
    [ -n "${STUB_MIGRATIONS:-}" ] && printf '%s\n' "${STUB_MIGRATIONS}"
    exit 0 ;;
  show)
    # show <ref>:<path>
    case "${2:-}" in
      *:versions.env)
        cat <<'VE'
POSTGRES_IMAGE=postgres:16.8
OLLAMA_IMAGE=ollama/ollama:0.31.2
QDRANT_IMAGE=qdrant/qdrant:v1.13.2
LITELLM_IMAGE=ghcr.io/berriai/litellm:main-stable
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2025.1.0
CADDY_IMAGE=caddy:2.9-alpine
TARGET_EDGE_IMAGE=registry.example/target-edge:2.0
VE
        ;;
      *:docker-compose.yml)
        printf 'services:\n  dashboard:\n    image: ghcr.io/limitcycle-oss/jarvis-dashboard:1.1.3\n'
        ;;
      *:docker-compose.override.yml) exit 1 ;;
      *) printf '%s\n' "${STUB_MIG_CONTENT:-}" ;;
    esac
    exit 0 ;;
  merge)
    # merge --ff-only <ref>  — the ONLY branch advance.
    if [ -n "${PENDING_FILE:-}" ] && [ -f "$PENDING_FILE" ]; then
      printf 'PENDING_EXISTS_AT_MERGE\n' >> "$STUB_LOG"
      printf 'PENDING_JSON_AT_MERGE=%s\n' "$(cat "$PENDING_FILE")" >> "$STUB_LOG"
    fi
    if [ -n "${UPDATE_PIN_FILE:-}" ] && [ -f "$UPDATE_PIN_FILE" ]; then
      printf 'UPDATE_PIN_AT_MERGE=%s\n' "$(cat "$UPDATE_PIN_FILE")" >> "$STUB_LOG"
    fi
    if [ -n "${STUB_BACKUP_DIR:-}" ] && [ -s "$STUB_BACKUP_DIR/.lifecycle/update.guard" ]; then
      printf 'LIFECYCLE_GUARD_AT_MERGE=%s\n' "$(cat "$STUB_BACKUP_DIR/.lifecycle/update.guard")" >> "$STUB_LOG"
    fi
    log "$*"
    # Simulate the CLI state directory losing write permission mid-update: the
    # first transaction write has already landed, the next one cannot.
    if [ -n "${STUB_FREEZE_STATE_DIR:-}" ] && [ -d "${STUB_FREEZE_STATE_DIR:-}" ]; then
      chmod 500 "$STUB_FREEZE_STATE_DIR"
    fi
    if [ "${STUB_MERGE_RC:-0}" = 0 ] || [ "${STUB_MERGE_CRASH:-0}" = 1 ]; then
      printf '%s\n' "${STUB_TARGET_SHA:-2222222222222222222222222222222222222222}" > "$STUB_HEAD_FILE"
      printf '%s\n' "${STUB_TARGET_SHA:-2222222222222222222222222222222222222222}" > "$STUB_REPO/.stub-head"
    fi
    if [ "${STUB_MERGE_CRASH:-0}" = 1 ]; then
      kill -KILL "$PPID"
      exit 137
    fi
    exit "${STUB_MERGE_RC:-0}" ;;
  *) log "$*"; exit 0 ;;
esac
GIT
chmod +x "$STUB/git"

cat > "$STUB/docker" <<'DOCKER'
#!/usr/bin/env bash
log() { [ -n "${STUB_LOG:-}" ] && printf 'docker %s\n' "$*" >> "$STUB_LOG"; }
BACKUP_CID="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
sidecar_state() { cat "$STUB_SIDECAR_STATE_FILE" 2>/dev/null || printf 'absent'; }
health_sample() {
  local sample="" version image_tag
  if [ -n "${STUB_HEALTH_SEQUENCE:-}" ] && [ -f "$STUB_HEALTH_SEQUENCE" ]; then
    sample="$(sed -n '1p' "$STUB_HEALTH_SEQUENCE")"
    if [ -n "$sample" ]; then
      sed '1d' "$STUB_HEALTH_SEQUENCE" > "${STUB_HEALTH_SEQUENCE}.next"
      mv "${STUB_HEALTH_SEQUENCE}.next" "$STUB_HEALTH_SEQUENCE"
    fi
  fi
  if [ -z "$sample" ]; then
    sample="${STUB_HEALTH-healthy}|${STUB_RUN_STATE-running}"
  fi
  version="$(sed -n 's/^JARVIS_VERSION=//p' "$STUB_REPO/.env" 2>/dev/null | head -1)"
  image_tag="$(sed -n 's/^JARVIS_IMAGE_TAG=//p' "$STUB_REPO/.env" 2>/dev/null | head -1)"
  log "health sample=${sample} version=${version:-missing} image=${image_tag:-missing}"
  printf '%s\n' "$sample"
}
running_svc() {
  case "$1" in
    postgres|ollama|qdrant|litellm|cloudflared|postgres-backup) return 0 ;;
    platform_api|paper_ingestion|learning_engine|dashboard|restore-uploader|telegram_bot) return 0 ;;
    *) return 1 ;;
  esac
}
case "${1:-}" in
  info) [ "${STUB_NO_DAEMON:-0}" = 1 ] && exit 1; exit 0 ;;
  volume)
    case "${2:-}" in
      inspect)
        if printf '%s\n' "$@" | grep -q -- '--format'; then
          project="${STUB_COMPOSE_LABEL_PROJECT:-$(basename "$STUB_REPO" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}"
          printf '%s|postgres_backups\n' "$project"
        fi
        exit 0 ;;
      create) exit 0 ;;
      *) exit 1 ;;
    esac ;;
  run)
    raw_args=("$@")
    detached=0
    for arg in "${raw_args[@]}"; do [ "$arg" = -d ] && detached=1; done
    for ((i=0; i<${#raw_args[@]}; i++)); do
      if [ "${raw_args[$i]}" = /tmp/backup-lifecycle.sh ]; then
        helper_args=("${raw_args[@]:$((i + 1))}")
        log "lifecycle ${helper_args[*]}"
        if [ "$detached" -eq 1 ]; then
          (
            exec 8>&-
            exec env JARVIS_BACKUP_TRIGGER_DIR="$STUB_TRIGGER_DIR" \
              JARVIS_BACKUP_DIR="$STUB_BACKUP_DIR" \
              JARVIS_BACKUP_KEY_FILE="$STUB_BACKUP_KEY_FILE" \
              bash "$STUB_LIFECYCLE_HELPER" "${helper_args[@]}"
          ) >/dev/null 2>&1 &
          printf '0123456789abcdef0123456789abcdef\n'
          exit 0
        fi
        JARVIS_BACKUP_TRIGGER_DIR="$STUB_TRIGGER_DIR" JARVIS_BACKUP_DIR="$STUB_BACKUP_DIR" \
          JARVIS_BACKUP_KEY_FILE="$STUB_BACKUP_KEY_FILE" \
          bash "$STUB_LIFECYCLE_HELPER" "${helper_args[@]}"
        exit $?
      fi
    done
    exit 0 ;;
  manifest)
    # manifest inspect <ref>
    ref="${3:-}"
    log "manifest inspect $ref"
    if [ -n "${MANIFEST_MISS:-}" ] && printf '%s' "$ref" | grep -q -- "$MANIFEST_MISS"; then exit 1; fi
    exit 0 ;;
  pull)
    log "image pull ${2:-}"
    if [ "${STUB_FAIL_STAGE_PULL:-0}" = 1 ] \
       || { [ -n "${STUB_FAIL_STAGE_PULL:-}" ] \
            && [ "${STUB_FAIL_STAGE_PULL:-0}" != 0 ] \
            && printf '%s' "${2:-}" | grep -qF -- "$STUB_FAIL_STAGE_PULL"; }; then
      exit 1
    fi
    exit 0 ;;
  inspect)
    shift; fmt=""
    while [ $# -gt 0 ]; do case "$1" in --format) fmt="$2"; shift 2 ;; *) shift ;; esac; done
    case "$fmt" in
      *State.Paused*State.Running*State.Pid*)
        case "$(sidecar_state)" in
          paused) printf 'true|true|4242\n' ;;
          running) printf 'false|true|4242\n' ;;
          *) printf 'false|false|0\n' ;;
        esac
        ;;
      *State.Health*State.Status*) health_sample ;;
      *com.docker.compose.project.working_dir*)
        project="${STUB_COMPOSE_LABEL_PROJECT:-$(basename "$STUB_REPO" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}"
        printf '%s|%s|%s/docker-compose.yml\n' "$project" "$STUB_REPO" "$STUB_REPO"
        ;;
      *Config.Image*) printf 'oldimage:running\n' ;;
      *State.Health*) printf '%s\n' "${STUB_HEALTH-healthy}" ;;
      *State.Status*) printf '%s\n' "${STUB_RUN_STATE-running}" ;;
      *) : ;;
    esac
    exit 0 ;;
  top)
    printf 'PID COMMAND\n'
    printf '4242 sh -uc sidecar-loop\n'
    if [ -n "${STUB_SIDECAR_CHILD:-}" ]; then
      printf '4243 %s\n' "$STUB_SIDECAR_CHILD"
    else
      printf '4243 sleep 5\n'
    fi
    exit 0 ;;
  rm)
    log "rm ${*:2}"
    printf 'absent\n' > "$STUB_SIDECAR_STATE_FILE"
    exit 0 ;;
  restart) log "restart ${*:2}"; exit 0 ;;
  compose)
    log "compose-env file=${COMPOSE_FILE-<unset>} project=${COMPOSE_PROJECT_NAME-<unset>} profiles=${COMPOSE_PROFILES-<unset>} separator=${COMPOSE_PATH_SEPARATOR-<unset>} envfiles=${COMPOSE_ENV_FILES-<unset>} disable=${COMPOSE_DISABLE_ENV_FILE-<unset>}"
    raw_args=("$@")
    helper_source=""
    producer_source=""
    pdf_source=""
    request_id=""
    for arg in "${raw_args[@]}"; do
      case "$arg" in
        *:/tmp/backup-lifecycle.sh:ro)
          helper_source="${arg%:/tmp/backup-lifecycle.sh:ro}"
          ;;
        *:/tmp/jarvis-target-backup.sh:ro)
          producer_source="${arg%:/tmp/jarvis-target-backup.sh:ro}"
          ;;
        *:/pdf-storage:rw)
          pdf_source="${arg%:/pdf-storage:rw}"
          ;;
        BACKUP_RUN_ID=*)
          request_id="${arg#BACKUP_RUN_ID=}"
          ;;
      esac
    done
    for arg in "${raw_args[@]}"; do
      if [ "$arg" = /tmp/jarvis-target-backup.sh ]; then
        log "target-backup request=$request_id source=$producer_source pdf=$pdf_source"
        # The real producer narrates on stdout (scripts/backup.sh); a silent
        # stub would hide any caller that captures stdout it does not own.
        printf '[2026-07-31T00:00:00+00:00] Starting backup...\n'
        [ -z "${STUB_TARGET_BACKUP_SLEEP:-}" ] || sleep "$STUB_TARGET_BACKUP_SLEEP"
        exit "${STUB_TARGET_BACKUP_RC:-0}"
      fi
    done
    for ((i=0; i<${#raw_args[@]}; i++)); do
      if [ "${raw_args[$i]}" = /tmp/backup-lifecycle.sh ]; then
        helper_args=("${raw_args[@]:$((i + 1))}")
        log "lifecycle ${helper_args[*]}"
        helper_script="${helper_source:-$STUB_LIFECYCLE_HELPER}"
        log "lifecycle-source $helper_script"
        if [ "${helper_args[0]:-}" = wait-update ] \
           && [ -n "${STUB_UPDATE_WAIT_FAIL_ONCE_FILE:-}" ] \
           && [ ! -e "$STUB_UPDATE_WAIT_FAIL_ONCE_FILE" ]; then
          : > "$STUB_UPDATE_WAIT_FAIL_ONCE_FILE"
          exit 75
        fi
        if [ "${helper_args[0]:-}" = hold-update ]; then
          (
            exec 8>&-
            exec env JARVIS_BACKUP_TRIGGER_DIR="$STUB_TRIGGER_DIR" JARVIS_BACKUP_DIR="$STUB_BACKUP_DIR" \
              JARVIS_HOST_SECRETS_DIR="$STUB_REPO/secrets" \
              JARVIS_BACKUP_KEY_FILE="$STUB_BACKUP_KEY_FILE" \
              bash "$helper_script" "${helper_args[@]}"
          ) >/dev/null 2>&1 &
          printf '0123456789abcdef0123456789abcdef\n'
          exit 0
        fi
        if [ "${helper_args[0]:-}" = acknowledge-quarantine ] \
           && [ -n "${STUB_QUARANTINE_REPLACE_ON_ACK:-}" ]; then
          printf '{"version":1,"restore_id":"%s","source":"inbox","requested_at":"2026-07-21T20:00:00+00:00","completed_at":"2026-07-21T20:05:00+00:00","review_state":"awaiting_review"}\n' \
            "$STUB_QUARANTINE_REPLACE_ON_ACK" \
            > "$STUB_TRIGGER_DIR/.outbound-quarantine.json"
        fi
        JARVIS_BACKUP_TRIGGER_DIR="$STUB_TRIGGER_DIR" JARVIS_BACKUP_DIR="$STUB_BACKUP_DIR" \
          JARVIS_HOST_SECRETS_DIR="$STUB_REPO/secrets" \
          JARVIS_BACKUP_KEY_FILE="$STUB_BACKUP_KEY_FILE" \
          bash "$helper_script" "${helper_args[@]}"
        exit $?
      fi
    done
    shift; args=()
    while [ $# -gt 0 ]; do case "$1" in
      --profile|--env-file|--project-directory|-p|-f) shift 2 ;;
      *) args+=("$1"); shift ;;
    esac; done
    set -- "${args[@]:-}"
    case "${1:-}" in
      version) exit 0 ;;
      config)
        if [ "${JARVIS_TARGET_COHORT_RENDER:-0}" = 1 ]; then
          log "target config ${raw_args[*]}"
          if [ -n "${STUB_TARGET_CONFIG_JSON:-}" ]; then
            printf '%s\n' "$STUB_TARGET_CONFIG_JSON"
          else
            printf '{"services":{"dashboard":{"image":"ghcr.io/limitcycle-oss/jarvis-dashboard:%s","pull_policy":"missing","build":{"context":"frontend"}},"target_worker":{"image":"ghcr.io/limitcycle-oss/jarvis-target-worker:%s","pull_policy":"missing"},"target_edge":{"image":"%s"},"langfuse":{"image":"jarvis/langfuse-hardened:%s","pull_policy":"build","build":{"context":"langfuse"}}}}\n' \
              "${JARVIS_IMAGE_TAG:-missing}" "${JARVIS_IMAGE_TAG:-missing}" \
              "${TARGET_EDGE_IMAGE:-registry.example/current-edge:1.0}" \
              "${JARVIS_VERSION:-missing}"
          fi
        else
          project="$(basename "$STUB_REPO" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
          printf '{"volumes":{"postgres_backups":{"name":"%s_postgres_backups"}}}\n' "$project"
        fi
        exit 0 ;;
      exec)
        shift
        [ "${1:-}" = -T ] && shift
        service="${1:-}"
        shift || true
        log "compose exec ${service} $*"
        case "$service" in
          paper_ingestion)
            printf '%s' "${STUB_OWNER_ENV:-}"
            exit 0 ;;
          postgres)
            payload="$(cat)"
            if [ -n "${STUB_PSQL_INPUT_FILE:-}" ]; then
              printf '%s\n' "$payload" > "$STUB_PSQL_INPUT_FILE"
            fi
            if printf '%s' "$payload" | grep -q -- '-- jarvis-owner-status'; then
              printf '%s\n' "${STUB_OWNER_DB_RESULT:-none|missing||}"
              exit 0
            fi
            if printf '%s' "$payload" | grep -q -- '-- jarvis-owner-set'; then
              exit "${STUB_OWNER_SET_RC:-0}"
            fi
            exit 1 ;;
          *) exit 1 ;;
        esac ;;
      run)
        # Record the whole invocation, plus whether the compose timeout wrapper
        # was armed around it (the interactive restore leg must run without one).
        log "compose-run timeout=${BACKUP_COMPOSE_TIMEOUT_SECONDS-<unset>} ${raw_args[*]}"
        run_script=""
        for ((i=0; i<${#raw_args[@]}; i++)); do
          [ "${raw_args[$i]}" = -c ] || continue
          run_script="${raw_args[$((i + 1))]:-}"
          break
        done
        case "$run_script" in
          *"> /backup-trigger/.restore_request.json"*)
            cat > "$STUB_TRIGGER_DIR/.restore_request.json"
            cp "$STUB_TRIGGER_DIR/.restore_request.json" \
              "$STUB_BACKUP_DIR/.captured_restore_request.json"
            exit 0 ;;
          *"rm -f /backup-trigger/.restore_request.json"*)
            rm -f "$STUB_TRIGGER_DIR/.restore_request.json"
            exit 0 ;;
          *"cat /backup-trigger/.restore_request.json"*)
            cat "$STUB_TRIGGER_DIR/.restore_request.json" 2>/dev/null || true
            exit 0 ;;
          *"cat /backup-trigger/.restore_status.json"*)
            cat "$STUB_TRIGGER_DIR/.restore_status.json" 2>/dev/null || printf '{}\n'
            exit 0 ;;
        esac
        if printf '%s\n' "${raw_args[@]}" | grep -qx -- '--complete-authority'; then
          restore_id="$(grep -oE '"restore_id":"[0-9a-f]{32}"' "$STUB_TRIGGER_DIR/.restore_status.json" 2>/dev/null | head -1 | cut -d'"' -f4)"
          source="$(grep -oE '"source":"(local|inbox)"' "$STUB_TRIGGER_DIR/.restore_status.json" 2>/dev/null | head -1 | cut -d'"' -f4)"
          printf '{"state":"done","current_step":"Finishing up","steps":[],"safety_backup_ts":null,"started_at":"1","finished_at":"2","error":null,"manual_steps_required":false,"phase":"finalize","restore_id":"%s","source":"%s"}\n' \
            "$restore_id" "$source" > "$STUB_TRIGGER_DIR/.restore_status.json"
          exit 0
        fi
        if printf '%s\n' "${raw_args[@]}" | grep -qx -- '--run-request' \
           || printf '%s\n' "${raw_args[@]}" | grep -qx -- '/usr/local/bin/restore.sh'; then
          [ "${STUB_RESTORE_LEGACY_RC:-0}" = 0 ] || exit "${STUB_RESTORE_LEGACY_RC}"
          restore_id="$(grep -oE '"restore_id":"[0-9a-f]{32}"' "$STUB_TRIGGER_DIR/.restore_request.json" 2>/dev/null | head -1 | cut -d'"' -f4)"
          source="$(grep -oE '"source":"(local|inbox)"' "$STUB_TRIGGER_DIR/.restore_request.json" 2>/dev/null | head -1 | cut -d'"' -f4)"
          state="${STUB_RESTORE_STATUS_AFTER_REQUEST:-running}"
          printf '{"state":"%s","current_step":"Reconstructing database authority","steps":[],"safety_backup_ts":null,"started_at":"1","finished_at":null,"error":null,"manual_steps_required":false,"phase":"database_authority","restore_id":"%s","source":"%s"}\n' \
            "$state" "$restore_id" "$source" > "$STUB_TRIGGER_DIR/.restore_status.json"
          rm -f "$STUB_TRIGGER_DIR/.restore_request.json"
          exit 0
        fi
        if printf '%s\n' "${raw_args[@]}" | grep -qx -- '/usr/local/bin/restore.sh'; then
          exit "${STUB_RESTORE_LEGACY_RC:-0}"
        fi
        exit 0 ;;
      ps)
        if [ "${2:-}" = "-a" ] && [ "${3:-}" = "-q" ]; then
          # -a includes stopped containers, so only an install whose containers
          # were never created resolves nothing.
          [ "${STUB_NO_CONTAINERS:-0}" = 1 ] && exit 0
          printf 'cid-%s\n' "${4:-}"
          exit 0
        fi
        if [ "${2:-}" = "-q" ]; then
          # A stopped stack resolves no RUNNING container ids at all.
          [ "${STUB_STACK_DOWN:-0}" = 1 ] && exit 0
          if [ "${3:-}" = postgres-backup ]; then
            [ "$(sidecar_state)" = absent ] || printf '%s\n' "$BACKUP_CID"
          elif running_svc "${3:-}"; then
            printf 'cid-%s\n' "${3:-}"
          fi
          exit 0
        fi
        # bare `ps` (status/doctor table)
        [ "${STUB_COMPOSE_PS_FAIL:-0}" = 1 ] && exit 1
        printf 'NAME                 STATUS\n'
        printf 'jarvis-dashboard-1   Up 3 minutes (healthy)\n'
        exit 0 ;;
      pull)
        log "compose pull ${*:2} version=${JARVIS_VERSION:-missing} image=${JARVIS_IMAGE_TAG:-missing}"
        if [ -n "${STUB_BACKUP_DIR:-}" ] && [ -s "$STUB_BACKUP_DIR/.lifecycle/update.guard" ]; then
          printf 'LIFECYCLE_GUARD_AT_PULL=%s\n' "$(cat "$STUB_BACKUP_DIR/.lifecycle/update.guard")" >> "$STUB_LOG"
        fi
        [ "${STUB_FAIL_STAGE_PULL:-0}" = 1 ] && exit 1
        exit 0 ;;
      up)
        log "compose up ${*:2} version=${JARVIS_VERSION:-missing} image=${JARVIS_IMAGE_TAG:-missing}"
        if printf '%s\n' "$*" | grep -qw postgres-backup; then
          printf 'running\n' > "$STUB_SIDECAR_STATE_FILE"
        fi
        exit 0 ;;
      build) log "compose build ${*:2} version=${JARVIS_VERSION:-missing} image=${JARVIS_IMAGE_TAG:-missing}"; exit 0 ;;
      pause)
        log "compose $*"
        printf 'paused\n' > "$STUB_SIDECAR_STATE_FILE"
        exit 0 ;;
      unpause)
        log "compose $*"
        printf 'running\n' > "$STUB_SIDECAR_STATE_FILE"
        exit 0 ;;
      start|stop|restart|logs) log "compose $*"; exit 0 ;;
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
DOCKER
chmod +x "$STUB/docker"

# A minimal setup.sh stub for `doctor` (shells `./setup.sh --check`) is NOT used;
# the fixture symlinks the real setup.sh only where needed. doctor tests below use
# a fixture setup.sh that just prints a PASS line.

# =============================================================================
# Fixture repo builder.
# =============================================================================
make_repo() {
  local dir="$1"
  mkdir -p "$dir/scripts/tests" "$dir/db/migrations" "$dir/shared/pdf_storage" "$dir/.git"
  ln -sf "$CLI" "$dir/scripts/jarvis-research.sh"
  ln -sf "$LIB" "$dir/scripts/setup_lib.sh"
  ln -sf "$UPDATE_SCRIPT" "$dir/update.sh"
  cp "$LIFECYCLE_HELPER" "$dir/scripts/backup-lifecycle.sh"
  chmod +x "$dir/scripts/backup-lifecycle.sh"
  # update.sh materializes the Docker-secret source files before it stages any
  # image, so this fixture repo has to offer that script. A no-op stub is the
  # right stand-in: these cases exercise the update transaction machinery, and
  # the real script would generate keys on every one of them and rewrite this
  # fixture's .env. The secrets phase itself is covered, with an ordering
  # assertion, in scripts/tests/test_update_coverage.sh.
  printf '#!/usr/bin/env bash\nexit 0\n' > "$dir/scripts/init-secrets.sh"
  chmod +x "$dir/scripts/init-secrets.sh"
  cat > "$dir/pyproject.toml" <<'PYPROJECT'
[project]
name = "jarvis-rd-assistant"
version = "1.1.3"
PYPROJECT
  cat > "$dir/versions.env" <<'VE'
POSTGRES_IMAGE=postgres:16.8
OLLAMA_IMAGE=ollama/ollama:0.31.2
QDRANT_IMAGE=qdrant/qdrant:v1.13.2
LITELLM_IMAGE=ghcr.io/berriai/litellm:main-stable
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2025.1.0
CADDY_IMAGE=caddy:2.9-alpine
VE
  printf 'services:\n  dashboard:\n    image: x\n' > "$dir/docker-compose.yml"
  printf 'JARVIS_VERSION=1.1.2\nJARVIS_IMAGE_TAG=1.1.2\nTORCH_VARIANT=cpu\nTORCH_VARIANT_SUFFIX=\n' > "$dir/.env"
  mkdir -p "$dir/secrets"
  printf 'FIXTURE-BACKUP-KEY\n' > "$dir/secrets/backup_encrypt_key.txt"
  # A setup.sh that doctor can shell for `--check`.
  cat > "$dir/setup.sh" <<'SETUP'
#!/usr/bin/env bash
[ "${1:-}" = "--check" ] && { printf 'PREFLIGHT: PASS\n'; exit 0; }
exit 0
SETUP
  chmod +x "$dir/setup.sh"
}

make_target_runtime() {
  local dir="$1"
  mkdir -p "$dir"
  cp "$CLI" "$dir/jarvis-research.sh"
  cp "$LIB" "$dir/setup_lib.sh"
  cp "$LIFECYCLE_HELPER" "$dir/backup-lifecycle.sh"
  cp "$BACKUP_SCRIPT" "$dir/backup.sh"
  chmod +x "$dir/jarvis-research.sh" "$dir/setup_lib.sh" \
    "$dir/backup-lifecycle.sh" "$dir/backup.sh"
}

# Fresh per-test environment: a repo, a CLI state dir, a backup dir + trigger dir,
# a clean stub log. Echoes nothing; exports globals the run_cli helper consumes.
new_env() {
  # Reset every stub tunable so a value set by one test can never leak into the
  # next through run_cli's ${VAR:-default} passthrough.
  unset STUB_MIGRATIONS STUB_MIG_CONTENT MANIFEST_MISS STUB_ANCESTOR STUB_MERGE_RC \
        STUB_DIRTY STUB_BRANCH STUB_REMOTE STUB_HEALTH STUB_FAIL_STAGE_PULL \
        STUB_NO_DAEMON STUB_TAGS STUB_RUN_STATE STUB_EXACT_TAG STUB_MERGE_CRASH \
        STUB_HEALTH_SEQUENCE STUB_FAST_SLEEP \
        STUB_TARGET_SHA STUB_COMPOSE_LABEL_PROJECT STUB_UPDATE_WAIT_FAIL_ONCE_FILE \
        STUB_TARGET_CONFIG_JSON STUB_OWNER_ENV STUB_OWNER_DB_RESULT STUB_OWNER_SET_RC \
        STUB_PSQL_INPUT_FILE STUB_QUARANTINE_REPLACE_ON_ACK STUB_TARGET_BACKUP_RC \
        STUB_TARGET_BACKUP_SLEEP STUB_SIDECAR_CHILD \
        STUB_COMPOSE_PS_FAIL STUB_FREEZE_STATE_DIR \
        STUB_STACK_DOWN STUB_NO_CONTAINERS STUB_RESTORE_LEGACY_RC STUB_RESTORE_STATUS_AFTER_REQUEST BACKUP_COMPOSE_TIMEOUT_SECONDS \
        CLI_STDIN_FILE RUN_CLI_PATH \
        JARVIS_UPDATE_GUARD_TIMEOUT JARVIS_UPDATE_GUARD_READY_ATTEMPTS \
        JARVIS_UPDATE_GUARD_READY_INTERVAL RUN_CLI_EXEC 2>/dev/null || true
  REPO="$ROOT/repo.$RANDOM.$RANDOM"
  CFG="$ROOT/cfg.$RANDOM.$RANDOM"
  BK="$ROOT/backups.$RANDOM.$RANDOM"
  TRIG="$ROOT/trigger.$RANDOM.$RANDOM"
  mkdir -p "$CFG" "$BK/.lifecycle" "$TRIG"
  make_repo "$REPO"
  STUB_LOG="$ROOT/log.$RANDOM"
  : > "$STUB_LOG"
  install_key="$(printf '%s' "$(realpath "$REPO")" | sha256sum | cut -d' ' -f1)"
  PENDING_FILE="$CFG/pending-update-${install_key}.json"
  SIDECAR_MARKER="$CFG/pending-update-${install_key}.backup-sidecar-quiesced"
  LEGACY_PENDING_FILE="$CFG/pending-update.json"
  UPDATE_PIN_FILE="$BK/.lifecycle/update-backup-pin.json"
  STUB_HEAD_FILE="$CFG/head"
  STUB_PSQL_INPUT_FILE="$CFG/psql-input.sql"
  STUB_SIDECAR_STATE_FILE="$CFG/backup-sidecar.state"
  printf 'running\n' > "$STUB_SIDECAR_STATE_FILE"
  printf '%s\n' "$SOURCE_SHA" > "$STUB_HEAD_FILE"
  printf '%s\n' "$SOURCE_SHA" > "$REPO/.stub-head"
}

# run_cli [--norepo] <args...>  — invoke the CLI with the stub PATH and the
# fixture env overrides. Default passes --repo "$REPO"; --norepo omits it and
# cd's into the repo (exercising cwd-ancestor resolution + registry).
run_cli() {
  local norepo=0
  if [ "${1:-}" = "--norepo" ]; then norepo=1; shift; fi
  local cli_path="${RUN_CLI_PATH:-$REPO/scripts/jarvis-research.sh}"
  local stub_path="$STUB:$PATH"
  [ "${STUB_FAST_SLEEP:-0}" != 1 ] || stub_path="$FAST_SLEEP_BIN:$stub_path"
  local -a pre=(env "PATH=$stub_path" "STUB_LOG=$STUB_LOG" "PENDING_FILE=$PENDING_FILE"
    "JARVIS_TEST_REAL_SLEEP=$REAL_SLEEP_BIN"
    "UPDATE_PIN_FILE=$UPDATE_PIN_FILE"
    "STUB_TRIGGER_DIR=$TRIG" "STUB_BACKUP_DIR=$BK"
    "STUB_BACKUP_KEY_FILE=$REPO/secrets/backup_encrypt_key.txt"
    "STUB_LIFECYCLE_HELPER=$LIFECYCLE_HELPER"
    "STUB_REPO=$REPO" "STUB_COMPOSE_LABEL_PROJECT=${STUB_COMPOSE_LABEL_PROJECT:-}"
    "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin"
    "JARVIS_BACKUP_DIR=$BK" "JARVIS_BACKUP_TRIGGER_DIR=$TRIG"
    "JARVIS_BACKUP_POLL_TIMEOUT=1" "JARVIS_BACKUP_POLL_INTERVAL=1"
    "JARVIS_UPDATE_GUARD_TIMEOUT=${JARVIS_UPDATE_GUARD_TIMEOUT:-21600}"
    "JARVIS_UPDATE_GUARD_READY_ATTEMPTS=${JARVIS_UPDATE_GUARD_READY_ATTEMPTS:-100}"
    "JARVIS_UPDATE_GUARD_READY_INTERVAL=${JARVIS_UPDATE_GUARD_READY_INTERVAL:-0.1}"
    "STUB_UPDATE_WAIT_FAIL_ONCE_FILE=${STUB_UPDATE_WAIT_FAIL_ONCE_FILE:-}"
    "STUB_BRANCH=${STUB_BRANCH:-main}" "STUB_DIRTY=${STUB_DIRTY:-}"
    "STUB_REMOTE=${STUB_REMOTE:-git@github.com:limitcycle-oss/jarvis-rd-assistant.git}"
    "STUB_ANCESTOR=${STUB_ANCESTOR:-0}" "STUB_MERGE_RC=${STUB_MERGE_RC:-0}"
    "STUB_MIGRATIONS=${STUB_MIGRATIONS:-}" "STUB_MIG_CONTENT=${STUB_MIG_CONTENT:-}"
    "MANIFEST_MISS=${MANIFEST_MISS:-}" "STUB_HEALTH=${STUB_HEALTH-healthy}"
    "STUB_RUN_STATE=${STUB_RUN_STATE-running}"
    "STUB_HEALTH_SEQUENCE=${STUB_HEALTH_SEQUENCE:-}"
    "STUB_FAIL_STAGE_PULL=${STUB_FAIL_STAGE_PULL:-0}" "STUB_TAGS=${STUB_TAGS:-v1.1.3}"
    "STUB_TARGET_CONFIG_JSON=${STUB_TARGET_CONFIG_JSON:-}"
    "STUB_NO_DAEMON=${STUB_NO_DAEMON:-0}" "STUB_EXACT_TAG=${STUB_EXACT_TAG:-}"
    "STUB_MERGE_CRASH=${STUB_MERGE_CRASH:-0}" "STUB_HEAD_FILE=$STUB_HEAD_FILE"
    "STUB_OWNER_ENV=${STUB_OWNER_ENV:-}" "STUB_OWNER_DB_RESULT=${STUB_OWNER_DB_RESULT:-}"
    "STUB_OWNER_SET_RC=${STUB_OWNER_SET_RC:-0}"
    "STUB_TARGET_BACKUP_RC=${STUB_TARGET_BACKUP_RC:-0}"
    "STUB_TARGET_BACKUP_SLEEP=${STUB_TARGET_BACKUP_SLEEP:-}"
    "STUB_SIDECAR_CHILD=${STUB_SIDECAR_CHILD:-}"
    "STUB_COMPOSE_PS_FAIL=${STUB_COMPOSE_PS_FAIL:-0}"
    "STUB_STACK_DOWN=${STUB_STACK_DOWN:-0}"
    "STUB_NO_CONTAINERS=${STUB_NO_CONTAINERS:-0}"
    "STUB_RESTORE_LEGACY_RC=${STUB_RESTORE_LEGACY_RC:-0}"
    "STUB_RESTORE_STATUS_AFTER_REQUEST=${STUB_RESTORE_STATUS_AFTER_REQUEST:-}"
    "STUB_FREEZE_STATE_DIR=${STUB_FREEZE_STATE_DIR:-}"
    "STUB_SIDECAR_STATE_FILE=$STUB_SIDECAR_STATE_FILE"
    "STUB_QUARANTINE_REPLACE_ON_ACK=${STUB_QUARANTINE_REPLACE_ON_ACK:-}"
    "STUB_PSQL_INPUT_FILE=$STUB_PSQL_INPUT_FILE"
    "STUB_TARGET_SHA=${STUB_TARGET_SHA:-$TARGET_SHA}")
  local stdin_path="${CLI_STDIN_FILE:-/dev/null}"
  if [ "${RUN_CLI_EXEC:-0}" = 1 ]; then
    if [ "$norepo" -eq 1 ]; then
      cd "$REPO" || return 1
      exec "${pre[@]}" bash "$cli_path" "$@" <"$stdin_path" 2>&1
    fi
    exec "${pre[@]}" bash "$cli_path" --repo "$REPO" "$@" <"$stdin_path" 2>&1
  elif [ "$norepo" -eq 1 ]; then
    ( cd "$REPO" && "${pre[@]}" bash "$cli_path" "$@" ) <"$stdin_path" 2>&1
  else
    "${pre[@]}" bash "$cli_path" --repo "$REPO" "$@" <"$stdin_path" 2>&1
  fi
}

run_cli_with_input() {
  local input="$1" input_file rc
  shift
  input_file="$CFG/cli-input.$RANDOM"
  printf '%s' "$input" > "$input_file"
  CLI_STDIN_FILE="$input_file" run_cli "$@"
  rc=$?
  rm -f "$input_file"
  return "$rc"
}

# register REPO in the state file (so the managed-install guard's (a) leg passes).
register_repo() { printf '%s\n' "$REPO" > "$CFG/installs"; }

new_staged_update_env() {
  new_env
  register_repo
  STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
  STUB_MIG_CONTENT="DELETE FROM telegram_user_pairings WHERE chat_id < 0;"
  TARGET_RUNTIME="$ROOT/target-runtime.$RANDOM.$RANDOM"
  make_target_runtime "$TARGET_RUNTIME"
  RUN_CLI_PATH="$TARGET_RUNTIME/jarvis-research.sh"
}

RESTORE_REVIEW_ID=0123456789abcdef0123456789abcdef
OTHER_RESTORE_REVIEW_ID=fedcba9876543210fedcba9876543210

seed_restore_review() {
  local restore_id="${1:-$RESTORE_REVIEW_ID}"
  printf '{"version":1,"restore_id":"%s","source":"inbox","requested_at":"2026-07-21T20:00:00+00:00","completed_at":"2026-07-21T20:05:00+00:00","review_state":"awaiting_review"}\n' \
    "$restore_id" > "$TRIG/.outbound-quarantine.json"
  printf '{"version":2,"sha256":"%064d","expires_at":"2099-07-21T22:00:00+00:00","restore_id":"%s","source":"inbox","requested_at":"2026-07-21T20:00:00+00:00"}\n' \
    0 "$restore_id" > "$TRIG/.restore_status_token.json"
}

write_pending() {
  local phase="$1" from_sha="${2:-$SOURCE_SHA}" target_sha="${3:-$TARGET_SHA}"
  local target="${4:-v1.1.3}" target_version="${5:-1.1.3}"
  printf '{"schema_version":1,"from_sha":"%s","from_version":"1.1.2","target":"%s","target_sha":"%s","target_version":"%s","phase":"%s","started_at":"1","backup_id":"","backup_run_id":"","legacy_recovery":false}\n' \
    "$from_sha" "$target" "$target_sha" "$target_version" "$phase" > "$PENDING_FILE"
}

write_pending_backup() {
  local phase="$1" run_id="$2" legacy="${3:-false}"
  printf '{"schema_version":1,"from_sha":"%s","from_version":"1.1.2","target":"v1.1.3","target_sha":"%s","target_version":"1.1.3","phase":"%s","started_at":"1","backup_id":"20991231_235959","backup_run_id":"%s","legacy_recovery":%s}\n' \
    "$SOURCE_SHA" "$TARGET_SHA" "$phase" "$run_id" "$legacy" > "$PENDING_FILE"
}

write_update_pin() {
  local run_id="$1"
  printf '{"timestamp":"20991231_235959","run_id":"%s"}\n' "$run_id" > "$UPDATE_PIN_FILE"
}

log_lacks_mutations() {  # $1 = description
  local l; l="$(cat "$STUB_LOG" 2>/dev/null || true)"
  if printf '%s' "$l" | grep -qE 'merge |checkout |reset |compose (pull|up|build)|image pull'; then
    check_fail "$1 :: stub log has a mutation: $(printf '%s' "$l" | grep -E 'merge |checkout |reset |compose (pull|up|build)|image pull' | tr '\n' ';')"
  else
    pass "$1"
  fi
}

# =============================================================================
# 1. Resolution / registration.
# =============================================================================
new_env
out="$(run_cli version)"; rc=$?
want "$out" 'jarvis-research' "resolve_repo_env_override: --repo resolves and version runs (rc=$rc)"

# cwd/ancestor resolution: run from a SUBDIR of the repo, no --repo, registered.
new_env; register_repo
mkdir -p "$REPO/db/migrations/nested"
out="$( cd "$REPO/db/migrations/nested" && env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
  "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
  bash "$REPO/scripts/jarvis-research.sh" version </dev/null 2>&1 )"; rc=$?
want "$out" 'JARVIS_VERSION=1.1.2\|1.1.2' "resolve_repo_cwd_ancestor: finds compose+versions.env walking up (rc=$rc)"

# state-file default: no --repo, not in repo tree; first registry line is the repo.
new_env; register_repo
out="$( cd "$ROOT" && env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
  "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
  bash "$CLI" version </dev/null 2>&1 )"; rc=$?
want "$out" '1.1.2' "resolve_repo_state_file_default: first registry line is the default install (rc=$rc)"

# stale registry lines are skipped with a warning.
new_env
printf '%s\n%s\n' "$ROOT/does-not-exist-$RANDOM" "$REPO" > "$CFG/installs"
out="$( cd "$ROOT" && env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
  "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
  bash "$CLI" version </dev/null 2>&1 )"; rc=$?
if has "$out" '1.1.2' && has "$out" 'skip\|stale\|ignor'; then
  pass "resolve_repo_skips_stale_lines: stale first line skipped with a warning, valid one used"
else
  check_fail "resolve_repo_skips_stale_lines: <<<$out>>>"
fi

# register prepends + de-dups.
new_env
run_cli register >/dev/null 2>&1
run_cli register >/dev/null 2>&1
if [ "$(grep -cxF "$REPO" "$CFG/installs" 2>/dev/null)" = 1 ] && [ "$(head -n1 "$CFG/installs")" = "$REPO" ]; then
  pass "register_prepends_and_dedups: repo at top exactly once after two registers"
else
  check_fail "register_prepends_and_dedups: installs=$(tr '\n' ',' < "$CFG/installs" 2>/dev/null)"
fi

# unknown command -> exit 2.
new_env
out="$(run_cli frobnicate)"; rc=$?
if [ "$rc" -eq 2 ]; then pass "unknown_command_exits_2: usage exit code"; else check_fail "unknown_command_exits_2: rc=$rc out=$out"; fi

# shim (from setup_lib install_cli_shim) execs the repo CLI with --repo. The
# installed shim resolves the registry at ${XDG_CONFIG_HOME}/jarvis-research/installs,
# so wire XDG_CONFIG_HOME to a fixture whose jarvis-research/ dir install_cli_shim
# populates, then run the shim with that same XDG_CONFIG_HOME.
new_env
XDGH="$ROOT/xdg.$RANDOM"; mkdir -p "$XDGH/jarvis-research"
# shellcheck source=../setup_lib.sh
# shellcheck disable=SC1091
( source "$LIB"; JARVIS_CLI_BIN_DIR="$CFG/bin" JARVIS_CLI_CONFIG_DIR="$XDGH/jarvis-research" \
    install_cli_shim "$REPO" >/dev/null )
# Replace the repo CLI with a probe that echoes its args, then run the shim.
# rm the symlink FIRST so the write never clobbers the real CLI source.
rm -f "$REPO/scripts/jarvis-research.sh"
cat > "$REPO/scripts/jarvis-research.sh" <<'PROBE'
#!/usr/bin/env bash
printf 'PROBE-ARGS: %s\n' "$*"
PROBE
chmod +x "$REPO/scripts/jarvis-research.sh"
shim_out="$(XDG_CONFIG_HOME="$XDGH" bash "$CFG/bin/jarvis-research" version 2>&1)"
if has "$shim_out" "PROBE-ARGS: --repo $REPO version"; then
  pass "shim_execs_repo_script: shim execs \$repo/scripts/jarvis-research.sh --repo \$repo \"\$@\""
else
  check_fail "shim_execs_repo_script: <<<$shim_out>>>"
fi
make_repo "$REPO"   # restore the real symlink for later tests

# =============================================================================
# 2. Refusal matrix — each exits 1 and mutates nothing.
# =============================================================================
new_env   # not registered, no --repo -> unregistered
out="$( cd "$REPO" && env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
  "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
  bash "$REPO/scripts/jarvis-research.sh" update --yes </dev/null 2>&1 )"; rc=$?
if [ "$rc" -eq 1 ]; then pass "update_refuses_unregistered_repo: exit 1"; else check_fail "update_refuses_unregistered_repo: rc=$rc out=$out"; fi
want "$out" 'register' "update_refuses_unregistered_repo: names jarvis-research register"
log_lacks_mutations "update_refuses_unregistered_repo: no mutation"

new_env; register_repo; STUB_REMOTE="git@github.com:someone-else/other.git"
out="$(run_cli update --yes)"; rc=$?
unset STUB_REMOTE
if [ "$rc" -eq 1 ]; then pass "update_refuses_wrong_remote: exit 1"; else check_fail "update_refuses_wrong_remote: rc=$rc out=$out"; fi
log_lacks_mutations "update_refuses_wrong_remote: no mutation"

new_env; register_repo; STUB_COMPOSE_LABEL_PROJECT=another-install
out="$(run_cli update --yes)"; rc=$?
unset STUB_COMPOSE_LABEL_PROJECT
if [ "$rc" -eq 1 ] && has "$out" 'ownership\|install'; then
  pass "update_refuses_compose_project_owned_by_another_install"
else
  check_fail "update accepted mismatched Compose ownership: rc=$rc out=<<<$out>>>"
fi
log_lacks_mutations "mismatched Compose ownership: no mutation"

new_env; register_repo; STUB_DIRTY=" M setup.sh"
out="$(run_cli update --yes)"; rc=$?
unset STUB_DIRTY
if [ "$rc" -eq 1 ]; then pass "update_refuses_dirty_tree: exit 1"; else check_fail "update_refuses_dirty_tree: rc=$rc out=$out"; fi
want "$out" 'M setup.sh' "update_refuses_dirty_tree: the refusal names the offending path"
log_lacks_mutations "update_refuses_dirty_tree: no mutation"

new_env; register_repo; STUB_BRANCH="feature/x"
out="$(run_cli update --yes)"; rc=$?
unset STUB_BRANCH
if [ "$rc" -eq 1 ]; then pass "update_refuses_nonmain_branch: exit 1"; else check_fail "update_refuses_nonmain_branch: rc=$rc out=$out"; fi
log_lacks_mutations "update_refuses_nonmain_branch: no mutation"

# diverged (a): merge-base --is-ancestor fails -> abort BEFORE any side effect.
new_env; register_repo; STUB_ANCESTOR=1
out="$(run_cli update --yes)"; rc=$?
unset STUB_ANCESTOR
if [ "$rc" -eq 1 ]; then pass "update_refuses_diverged_main: exit 1"; else check_fail "update_refuses_diverged_main: rc=$rc out=$out"; fi
log_lacks_mutations "update_refuses_diverged_main: no compose/branch mutation"
trigger_state="$(find "$TRIG" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null)"
if [ ! -f "$PENDING_FILE" ] && [ -z "$trigger_state" ]; then
  pass "update_refuses_diverged_main: no pending state or lifecycle trigger"
else
  check_fail "update_refuses_diverged_main: side effect (pending=$([ -f "$PENDING_FILE" ] && echo yes) trig=$(ls -A "$TRIG"))"
fi

# diverged (b): precheck green but merge --ff-only red -> terminal guard aborts.
new_env; register_repo; STUB_MERGE_RC=1
out="$(run_cli update --yes)"; rc=$?
unset STUB_MERGE_RC
if [ "$rc" -ne 0 ]; then pass "update_refuses_diverged_main(b): ff-merge failure aborts nonzero"; else check_fail "update_refuses_diverged_main(b): rc=$rc out=$out"; fi
# The merge WAS attempted (terminal guard), but nothing beyond it ran.
if has "$(cat "$STUB_LOG")" 'merge --ff-only' && ! grep -q 'compose up' "$STUB_LOG"; then
  pass "update_refuses_diverged_main(b): merge attempted, no recreate followed"
else
  check_fail "update_refuses_diverged_main(b): log=$(cat "$STUB_LOG")"
fi

# missing manifest -> refusal BEFORE any pull/merge.
new_env; register_repo; MANIFEST_MISS='jarvis-dashboard'
out="$(run_cli update --yes)"; rc=$?
unset MANIFEST_MISS
if [ "$rc" -eq 1 ]; then pass "update_refuses_missing_manifest: exit 1"; else check_fail "update_refuses_missing_manifest: rc=$rc out=$out"; fi
if ! grep -qE 'merge --ff-only|compose pull|compose up' "$STUB_LOG"; then
  pass "update_refuses_missing_manifest: no pull/merge before the gate"
else
  check_fail "update_refuses_missing_manifest: log=$(cat "$STUB_LOG")"
fi

# destructive migration + no fresh backup -> refusal; with a fresh backup -> proceeds.
new_env; register_repo
STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
STUB_MIG_CONTENT="-- purge
DELETE FROM telegram_user_pairings WHERE chat_id < 0;"
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && has "$out" 'backup'; then
  pass "update_requires_backup_on_destructive_migration: no fresh backup -> refuse"
else
  check_fail "update_requires_backup_on_destructive_migration(no-backup): rc=$rc out=$out"
fi
log_lacks_mutations "update_requires_backup_on_destructive_migration(no-backup): no mutation"

# =============================================================================
# Backup fixture helper: write one encrypted, authenticated restore point using
# the exact request ID observed in the on-demand trigger.
#   $1=backup_dir  $2=mode  $3=run_id
# =============================================================================
seed_fresh_backup() {
  local dir="$1" mode="$2" run_id="$3" ts="20991231_235959"
  local jf="jarvis_${ts}.sql.gz.enc" sf="secrets_${ts}.tar.gz.enc" qf="qdrant_papers_${ts}.snapshot.enc"
  local lf="litellm_${ts}.sql.gz.enc" pf="pdfs_${ts}.tar.gz.enc"
  if [ "$mode" = "renamed" ]; then jf="jarvis_20991231_235958.sql.gz.enc"; fi
  printf 'JARVISDBDATA' > "$dir/$jf"
  [ "$mode" = "missing_litellm" ] || printf 'LITELLMDBDATA' > "$dir/$lf"
  [ "$mode" = "legacy" ] || printf 'PDFARCHIVE' > "$dir/$pf"
  printf 'SECRETSDATA'  > "$dir/$sf"
  printf 'QDRANTSNAP'   > "$dir/$qf"
  local jsha lsha psha ssha qsha jsz lsz psz ssz qsz entries derived
  jsha="$(sha256sum "$dir/$jf" | cut -d' ' -f1)"; jsz="$(stat -c%s "$dir/$jf")"
  if [ -f "$dir/$lf" ]; then lsha="$(sha256sum "$dir/$lf" | cut -d' ' -f1)"; lsz="$(stat -c%s "$dir/$lf")"; fi
  if [ -f "$dir/$pf" ]; then psha="$(sha256sum "$dir/$pf" | cut -d' ' -f1)"; psz="$(stat -c%s "$dir/$pf")"; fi
  ssha="$(sha256sum "$dir/$sf" | cut -d' ' -f1)"; ssz="$(stat -c%s "$dir/$sf")"
  qsha="$(sha256sum "$dir/$qf" | cut -d' ' -f1)"; qsz="$(stat -c%s "$dir/$qf")"
  if [ "$mode" = "bad_db" ]; then jsha="deadbeef"; fi   # DB hash mismatch
  entries="{\"filename\":\"$jf\",\"sha256\":\"$jsha\",\"size_bytes\":$jsz}"
  if [ -f "$dir/$lf" ]; then entries="$entries,{\"filename\":\"$lf\",\"sha256\":\"$lsha\",\"size_bytes\":$lsz}"; fi
  if [ -f "$dir/$pf" ]; then entries="$entries,{\"filename\":\"$pf\",\"sha256\":\"$psha\",\"size_bytes\":$psz}"; fi
  entries="$entries,{\"filename\":\"$sf\",\"sha256\":\"$ssha\",\"size_bytes\":$ssz}"
  entries="$entries,{\"filename\":\"$qf\",\"sha256\":\"$qsha\",\"size_bytes\":$qsz}"
  if [ "$mode" = "legacy" ]; then
    printf '{"timestamp":"%s","app_version":"1.1.3","schema_version":200,"created_at":"2099-12-31T23:59:59+00:00","archives":[%s]}' \
      "$ts" "$entries" > "$dir/manifest_${ts}.json"
  else
    printf '{"timestamp":"%s","run_id":"%s","app_version":"1.1.3","schema_version":200,"created_at":"2099-12-31T23:59:59+00:00","archives":[%s]}' \
      "$ts" "$run_id" "$entries" > "$dir/manifest_${ts}.json"
  fi
  if [ "$mode" = "unsigned" ]; then return 0; fi
  if [ "$mode" = "bad_hmac" ]; then
    printf '%064d\n' 0 > "$dir/manifest_${ts}.json.hmac"
    return 0
  fi
  derived="$(openssl dgst -sha256 -hmac 'jarvis-manifest-v1' -r < "$REPO/secrets/backup_encrypt_key.txt" | cut -d' ' -f1)"
  openssl dgst -sha256 -mac HMAC -macopt "hexkey:${derived}" -r < "$dir/manifest_${ts}.json" \
    | cut -d' ' -f1 > "$dir/manifest_${ts}.json.hmac"
}

respond_to_backup() {
  local mode="$1"
  (
    local request_id=""
    for _ in $(seq 1 100); do
      if [ -e "$TRIG/.backup_now" ]; then
        request_id="$(tr -d '\r\n' < "$TRIG/.backup_now" 2>/dev/null || true)"
        break
      fi
      request_id="$(
        sed -n 's/^docker target-backup request=\([0-9a-f]\{32\}\) .*/\1/p' \
          "$STUB_LOG" 2>/dev/null | tail -1
      )"
      [ -z "$request_id" ] || break
      sleep 0.02
    done
    if [ "$mode" = "replayed" ]; then request_id="00000000000000000000000000000000"; mode="good"; fi
    seed_fresh_backup "$BK" "$mode" "$request_id"
  ) >/dev/null 2>&1 &
}

new_staged_update_env
respond_to_backup good
out="$(run_cli update --yes)"; rc=$?
staged_log="$(cat "$STUB_LOG")"
sidecar_pause_line="$(grep -n 'docker compose pause postgres-backup' "$STUB_LOG" | head -1 | cut -d: -f1)"
target_backup_line="$(grep -n 'docker target-backup request=' "$STUB_LOG" | head -1 | cut -d: -f1)"
if [ "$rc" -eq 0 ] \
   && has "$staged_log" 'target-backup request=[0-9a-f]\{32\}' \
   && has "$staged_log" "source=$TARGET_RUNTIME/backup.sh" \
   && has "$staged_log" "pdf=$REPO/shared/pdf_storage" \
   && has "$staged_log" "lifecycle-source $TARGET_RUNTIME/backup-lifecycle.sh" \
   && ! has "$staged_log" 'lifecycle publish-request ' \
   && has "$staged_log" 'docker compose pause postgres-backup' \
   && [ "$sidecar_pause_line" -lt "$target_backup_line" ] \
   && has "$staged_log" 'merge --ff-only' \
   && has "$staged_log" "docker rm -f -- 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" \
   && has "$staged_log" 'docker compose up -d --no-deps --force-recreate postgres-backup' \
   && [ "$(cat "$STUB_SIDECAR_STATE_FILE")" = running ] \
   && [ ! -e "$SIDECAR_MARKER" ] \
   && [ ! -e "$UPDATE_PIN_FILE" ]; then
  pass "staged target runtime creates a target-format backup and hands off the backup sidecar"
else
  check_fail "staged target runtime backup: rc=$rc out=<<<$out>>> log=$staged_log"
fi

new_staged_update_env
STUB_TARGET_BACKUP_SLEEP=3
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" "backup producer exceeded the 1s limit" \
   && has "$(cat "$STUB_LOG")" 'docker compose pause postgres-backup' \
   && has "$(cat "$STUB_LOG")" 'docker compose unpause postgres-backup' \
   && [ "$(cat "$STUB_SIDECAR_STATE_FILE")" = running ] \
   && [ ! -e "$SIDECAR_MARKER" ]; then
  pass "staged target backup execution is bounded before any update mutation"
else
  check_fail "staged target runtime timeout: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi
log_lacks_mutations "staged target runtime timeout: no branch or image mutation"

new_staged_update_env
STUB_TARGET_BACKUP_RC=9
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" "selected release's backup producer failed" \
   && has "$(cat "$STUB_LOG")" 'target-backup request=[0-9a-f]\{32\}'; then
  pass "staged target runtime stops before branch movement when its backup producer fails"
else
  check_fail "staged target runtime producer failure: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi
log_lacks_mutations "staged target runtime producer failure: no branch or image mutation"

new_staged_update_env
respond_to_backup bad_hmac
out="$(run_cli update --yes)"; rc=$?
producer_calls="$(grep -c 'docker target-backup request=' "$STUB_LOG" 2>/dev/null || true)"
if [ "$rc" -eq 1 ] && [ "$producer_calls" -eq 1 ] \
   && has "$out" "selected release's backup could not be authenticated"; then
  pass "staged target runtime never retries a matching invalid backup"
else
  check_fail "staged target runtime invalid backup: rc=$rc calls=$producer_calls out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi
log_lacks_mutations "staged target runtime invalid backup: no branch or image mutation"

new_staged_update_env
STUB_SIDECAR_CHILD="/usr/local/bin/restore.sh"
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" "backup, restore, or prune operation is already active" \
   && has "$(cat "$STUB_LOG")" 'docker compose pause postgres-backup' \
   && has "$(cat "$STUB_LOG")" 'docker compose unpause postgres-backup' \
   && ! has "$(cat "$STUB_LOG")" 'target-backup request=' \
   && [ "$(cat "$STUB_SIDECAR_STATE_FILE")" = running ] \
   && [ ! -e "$SIDECAR_MARKER" ]; then
  pass "staged update refuses to race an installed backup-sidecar operation"
else
  check_fail "staged sidecar activity barrier: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi
log_lacks_mutations "staged sidecar activity barrier: no branch or image mutation"

new_staged_update_env
STUB_FAIL_STAGE_PULL=1
respond_to_backup good
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] \
   && [ -f "$PENDING_FILE" ] \
   && [ "$(wc -l < "$PENDING_FILE")" -eq 1 ] \
   && grep -Eq '"backup_id":"[0-9]{8}_[0-9]{6}"' "$PENDING_FILE" \
   && [ -f "$SIDECAR_MARKER" ] \
   && [ "$(cat "$STUB_SIDECAR_STATE_FILE")" = paused ]; then
  pass "staged update retains its sidecar handoff across an interrupted transaction"
else
  check_fail "staged sidecar retained handoff: rc=$rc pending=$([ -f "$PENDING_FILE" ] && cat "$PENDING_FILE") out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi
STUB_FAIL_STAGE_PULL=0
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && has "$(cat "$STUB_LOG")" "docker rm -f -- 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" \
   && has "$(cat "$STUB_LOG")" 'docker compose up -d --no-deps --force-recreate postgres-backup' \
   && [ "$(cat "$STUB_SIDECAR_STATE_FILE")" = running ] \
   && [ ! -e "$SIDECAR_MARKER" ]; then
  pass "a retried staged update completes the recorded sidecar handoff"
else
  check_fail "staged sidecar retry: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

new_env; register_repo
STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
STUB_MIG_CONTENT="DELETE FROM telegram_user_pairings WHERE chat_id < 0;"
respond_to_backup good
out="$(run_cli update --yes)"; rc=$?
if has "$(cat "$STUB_LOG")" 'merge --ff-only' \
   && has "$(cat "$STUB_LOG")" 'UPDATE_PIN_AT_MERGE={"timestamp":"20991231_235959","run_id":"[0-9a-f]\{32\}"}' \
   && has "$(cat "$STUB_LOG")" 'LIFECYCLE_GUARD_AT_MERGE=[0-9a-f]\{32\}' \
   && has "$(cat "$STUB_LOG")" 'LIFECYCLE_GUARD_AT_PULL=[0-9a-f]\{32\}' \
   && ! has "$(cat "$STUB_LOG")" 'target-backup request=' \
   && [ ! -e "$UPDATE_PIN_FILE" ]; then
  pass "update lifecycle flock spans pin publication through merge, pull, health, and commit"
else
  check_fail "update_requires_backup_on_destructive_migration(with-backup): rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

# A real backup/update mutex wait may exceed the old 10-second readiness poll.
# The CLI must keep one detached owner and one in-container observer alive until
# the guard activates, without launching a Compose container for every poll.
new_env; register_repo
JARVIS_UPDATE_GUARD_TIMEOUT=30
mkdir -p "$BK/.lifecycle"
touch "$BK/.lifecycle/update.lock"
exec 9>>"$BK/.lifecycle/update.lock"
flock -n 9 || check_fail "update_guard_waits_past_ten_seconds: fixture lock unavailable"
long_wait_out="$ROOT/update-long-wait.$RANDOM.log"
( RUN_CLI_EXEC=1 run_cli update --yes ) >"$long_wait_out" 2>&1 &
long_wait_pid=$!
sleep 10.5
long_wait_alive=0
kill -0 "$long_wait_pid" 2>/dev/null && long_wait_alive=1
flock -u 9
exec 9>&-
wait "$long_wait_pid"; long_wait_rc=$?
hold_calls="$(grep -c 'docker lifecycle hold-update ' "$STUB_LOG" 2>/dev/null || true)"
wait_calls="$(grep -c 'docker lifecycle wait-update ' "$STUB_LOG" 2>/dev/null || true)"
status_calls="$(grep -c 'docker lifecycle update-status ' "$STUB_LOG" 2>/dev/null || true)"
if [ "$long_wait_alive" -eq 1 ] && [ "$long_wait_rc" -eq 0 ] \
   && [ "$hold_calls" -eq 1 ] && [ "$wait_calls" -eq 1 ] \
   && [ "$status_calls" -lt 10 ]; then
  pass "update_guard_waits_past_ten_seconds_with_one_owner_and_observer"
else
  check_fail "update_guard_waits_past_ten_seconds: alive=$long_wait_alive rc=$long_wait_rc hold=$hold_calls wait=$wait_calls status=$status_calls out=$(cat "$long_wait_out")"
fi

# Killing the host wrapper while its detached owner is waiting must not leak the
# host command lock or launch a second owner. A retry adopts the durable ID.
new_env; register_repo
JARVIS_UPDATE_GUARD_TIMEOUT=30
mkdir -p "$BK/.lifecycle"
touch "$BK/.lifecycle/update.lock"
exec 9>>"$BK/.lifecycle/update.lock"
flock -n 9 || check_fail "update_guard_retry_adopts_owner: fixture lock unavailable"
first_update_out="$ROOT/update-first.$RANDOM.log"
( RUN_CLI_EXEC=1 run_cli update --yes ) >"$first_update_out" 2>&1 &
first_update_pid=$!
reservation="$BK/.lifecycle/update.reservation"
guard_id=""
owner_ready=0
for _ in $(seq 1 200); do
  if [ -s "$reservation" ]; then
    guard_id="$(tr -d '\r\n' < "$reservation")"
    if JARVIS_BACKUP_TRIGGER_DIR="$TRIG" JARVIS_BACKUP_DIR="$BK" \
         bash "$LIFECYCLE_HELPER" update-reservation-status "$guard_id" \
         >/dev/null 2>&1; then
      owner_ready=1
      break
    fi
  fi
  sleep 0.02
done
kill -KILL "$first_update_pid" 2>/dev/null || true
wait "$first_update_pid" 2>/dev/null || true
retry_update_out="$ROOT/update-retry.$RANDOM.log"
( RUN_CLI_EXEC=1 run_cli update --yes ) >"$retry_update_out" 2>&1 &
retry_update_pid=$!
sleep 0.4
retry_alive=0
kill -0 "$retry_update_pid" 2>/dev/null && retry_alive=1
same_id=0
[ -s "$reservation" ] \
  && [ "$(tr -d '\r\n' < "$reservation")" = "$guard_id" ] \
  && same_id=1
hold_calls_before_release="$(grep -c 'docker lifecycle hold-update ' "$STUB_LOG" 2>/dev/null || true)"
flock -u 9
exec 9>&-
wait "$retry_update_pid"; retry_update_rc=$?
hold_calls_after_release="$(grep -c 'docker lifecycle hold-update ' "$STUB_LOG" 2>/dev/null || true)"
if [ "$owner_ready" -eq 1 ] && [ "$retry_alive" -eq 1 ] \
   && [ "$same_id" -eq 1 ] && [ "$retry_update_rc" -eq 0 ] \
   && [ "$hold_calls_before_release" -eq 1 ] \
   && [ "$hold_calls_after_release" -eq 1 ] \
   && [ ! -e "$reservation" ]; then
  pass "update_guard_retry_adopts_one_pending_owner_after_wrapper_death"
else
  check_fail "update_guard_retry_adopts_owner: owner=$owner_ready alive=$retry_alive same_id=$same_id rc=$retry_update_rc holds=$hold_calls_before_release/$hold_calls_after_release first=$(cat "$first_update_out") retry=$(cat "$retry_update_out")"
fi

# A failed post-merge update keeps both its transaction and exact backup pin so
# scheduled retention cannot prune the only schema rollback point.
new_env; register_repo; STUB_HEALTH="unhealthy"; STUB_FAST_SLEEP=1
STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
STUB_MIG_CONTENT="DROP TABLE old_stuff;"
respond_to_backup good
out="$(run_cli update --yes)"; rc=$?
unset STUB_HEALTH
if [ "$rc" -ne 0 ] && [ -f "$PENDING_FILE" ] && [ -f "$UPDATE_PIN_FILE" ] \
   && grep -q '"phase":"health"' "$PENDING_FILE" \
   && grep -Eq '^\{"timestamp":"20991231_235959","run_id":"[0-9a-f]{32}"\}$' "$UPDATE_PIN_FILE"; then
  pass "failed schema update preserves its authenticated rollback pin"
else
  check_fail "failed schema update lost transaction/pin: rc=$rc pending=$(cat "$PENDING_FILE" 2>/dev/null) pin=$(cat "$UPDATE_PIN_FILE" 2>/dev/null)"
fi

# archive-set gate: fresh manifest but DB archive hash-mismatched -> refuse.
new_env; register_repo
STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
STUB_MIG_CONTENT="DROP TABLE old_stuff;"
respond_to_backup bad_db
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && ! grep -q 'merge --ff-only' "$STUB_LOG"; then
  pass "update_backup_gate_requires_archive_set: mismatched DB archive -> refuse, never merges"
else
  check_fail "update_backup_gate_requires_archive_set: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

for backup_mode in unsigned bad_hmac missing_litellm renamed replayed; do
  new_env; register_repo
  STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
  STUB_MIG_CONTENT="DROP TABLE old_stuff;"
  respond_to_backup "$backup_mode"
  out="$(run_cli update --yes)"; rc=$?
  if [ "$rc" -eq 1 ] && ! grep -q 'merge --ff-only' "$STUB_LOG"; then
    pass "update_backup_gate_refuses_${backup_mode}: unauthenticated/incomplete/mismatched point never merges"
  else
    check_fail "update_backup_gate_refuses_${backup_mode}: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
  fi
done

# =============================================================================
# 3. Transaction set.
# =============================================================================
# Pre-merge staging is rendered from the target ref, not from the current
# checkout's service list. This fixture's target adds a registry-backed worker
# and changes an edge image; both exact refs must be present before the merge.
# Its build-only Langfuse service must never be sent to the registry.
new_env; register_repo
printf 'COMPOSE_PROFILES=nextgen\n' >> "$REPO/.env"
printf 'TELEGRAM_BOT_TOKEN=configured\n' >> "$REPO/.env"
out="$(run_cli update --yes)"; rc=$?
target_worker_line="$(grep -nF 'image pull ghcr.io/limitcycle-oss/jarvis-target-worker:1.1.3' "$STUB_LOG" | head -1 | cut -d: -f1)"
target_edge_line="$(grep -nF 'image pull registry.example/target-edge:2.0' "$STUB_LOG" | head -1 | cut -d: -f1)"
target_merge_line="$(grep -nF 'git merge --ff-only v1.1.3' "$STUB_LOG" | head -1 | cut -d: -f1)"
if [ "$rc" -eq 0 ] && [ -n "$target_worker_line" ] && [ -n "$target_edge_line" ] \
   && [ -n "$target_merge_line" ] && [ "$target_worker_line" -lt "$target_merge_line" ] \
   && [ "$target_edge_line" -lt "$target_merge_line" ] \
   && grep -q 'target config .*--profile nextgen.*--profile telegram' "$STUB_LOG" \
   && ! grep -q 'image pull jarvis/langfuse-hardened' "$STUB_LOG"; then
  pass "update_stages_exact_target_ref_registry_images_before_merge"
else
  check_fail "target cohort staging: rc=$rc worker=$target_worker_line edge=$target_edge_line merge=$target_merge_line log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# A target-image pull failure leaves both the source checkout and its durable
# merge intent untouched, so the same install can retry safely.
new_env; register_repo
STUB_FAIL_STAGE_PULL='jarvis-target-worker'
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && [ "$(cat "$STUB_HEAD_FILE")" = "$SOURCE_SHA" ] \
   && [ -f "$PENDING_FILE" ] && grep -q '"phase":"merge_pending"' "$PENDING_FILE" \
   && ! grep -q 'git merge --ff-only' "$STUB_LOG"; then
  pass "target_cohort_pull_failure_keeps_checkout_and_per_install_journal_for_retry"
else
  check_fail "target pull failure boundary: rc=$rc head=$(cat "$STUB_HEAD_FILE") pending=$(cat "$PENDING_FILE" 2>/dev/null) log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi
unset STUB_FAIL_STAGE_PULL

# additive-only migration: no backup needed, full happy path completes.
new_env; register_repo
out="$(run_cli update --yes)"; rc=$?
if has "$(cat "$STUB_LOG")" 'PENDING_EXISTS_AT_MERGE' \
   && has "$(cat "$STUB_LOG")" '"schema_version":1' \
   && has "$(cat "$STUB_LOG")" '"phase":"merge_pending"' \
   && has "$(cat "$STUB_LOG")" "\"target_sha\":\"$TARGET_SHA\""; then
  pass "update_writes_pending_txn_before_merge: atomic schema + merge intent + target HEAD exist at merge"
else
  check_fail "update_writes_pending_txn_before_merge: rc=$rc log=$(cat "$STUB_LOG")"
fi
if ! has "$out" '<previous-version>' \
   && ! has "$out" 'full release rollback' \
   && ! has "$out" 'If you need to roll back'; then
  pass "successful_update_omits_speculative_release_rollback"
else
  check_fail "successful update printed unsupported rollback guidance: out=<<<$out>>>"
fi

# pins the UNPREFIXED version + v-less image refs.
new_env; register_repo
out="$(run_cli update --yes)"; rc=$?
if grep -qx 'JARVIS_VERSION=1.1.3' "$REPO/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.1.3' "$REPO/.env"; then
  pass "update_pins_semantic_version_and_release_image_tag_without_v_prefix"
else
  check_fail "update release identities: env=$(grep -E '^JARVIS_(VERSION|IMAGE_TAG)=' "$REPO/.env")"
fi
if ! grep -q ':v1\.1\.3' "$STUB_LOG" \
   && ! grep -qE 'JARVIS_(VERSION|IMAGE_TAG)=v1\.1\.3' "$STUB_LOG"; then
  pass "update_release_identities_do_not_gain_a_v_prefix"
else
  check_fail "v-prefix leaked into release identity: $(grep -E ':v1|=v1' "$STUB_LOG")"
fi

# A commit-addressed verification update keeps the checkout's semantic
# application version while selecting the exact commit-tagged image cohort.
new_env; register_repo
out="$(run_cli update --to "$TARGET_SHA" --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && grep -q "git merge --ff-only $TARGET_SHA" "$STUB_LOG" \
   && grep -q "image pull ghcr.io/limitcycle-oss/jarvis-target-worker:$TARGET_SHA" "$STUB_LOG" \
   && grep -q "compose pull .*version=1.1.3 image=$TARGET_SHA" "$STUB_LOG" \
   && grep -qx 'JARVIS_VERSION=1.1.3' "$REPO/.env" \
   && grep -qx "JARVIS_IMAGE_TAG=$TARGET_SHA" "$REPO/.env"; then
  pass "commit_addressed_update_keeps_semantic_version_and_exact_image_identity"
else
  check_fail "commit-addressed update identity: rc=$rc env=$(grep -E '^JARVIS_(VERSION|IMAGE_TAG)=' "$REPO/.env") log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# A managed update must keep its journal pending through a recoverable
# unhealthy sample, then commit only after the shared state machine observes
# health. The no-op sleep keeps production's 180/3 arithmetic intact.
new_env; register_repo
HEALTH_SEQUENCE="$CFG/health-sequence"
printf '%s\n' 'unhealthy|running' 'healthy|running' > "$HEALTH_SEQUENCE"
STUB_HEALTH_SEQUENCE="$HEALTH_SEQUENCE"
STUB_FAST_SLEEP=1
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 0 ] && [ ! -e "$PENDING_FILE" ] \
   && grep -q '^docker health sample=unhealthy|running version=1.1.2 image=1.1.2$' "$STUB_LOG" \
   && grep -q '^docker health sample=healthy|running version=1.1.2 image=1.1.2$' "$STUB_LOG" \
   && grep -qx 'JARVIS_VERSION=1.1.3' "$REPO/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.1.3' "$REPO/.env"; then
  pass "managed_transient_unhealthy_commits_only_after_later_health"
else
  check_fail "managed transient health: rc=$rc pending=$(cat "$PENDING_FILE" 2>/dev/null) env=$(grep JARVIS_VERSION "$REPO/.env") log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# commits only after health: failed health -> transaction stays pending.
new_env; register_repo; STUB_HEALTH="unhealthy"; STUB_FAST_SLEEP=1
out="$(run_cli update --yes)"; rc=$?
unset STUB_HEALTH
if [ "$rc" -ne 0 ] && [ -f "$PENDING_FILE" ] \
   && ! grep -q '"committed"' "$PENDING_FILE" 2>/dev/null \
   && grep -qx 'JARVIS_VERSION=1.1.2' "$REPO/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.1.2' "$REPO/.env"; then
  pass "update_commits_txn_only_after_health: failed health leaves txn pending and old pin intact"
else
  check_fail "update_commits_txn_only_after_health: rc=$rc pending=$([ -f "$PENDING_FILE" ] && cat "$PENDING_FILE") env=$(grep JARVIS_VERSION "$REPO/.env")"
fi

# --to targets an rc tag with the same gates.
new_env; register_repo
STUB_EXACT_TAG="v1.1.4-rc.1"
out="$(run_cli update --to v1.1.4-rc.1 --yes)"; rc=$?
if grep -q 'git merge --ff-only v1.1.4-rc.1' "$STUB_LOG" \
   && grep -qx 'JARVIS_VERSION=1.1.4-rc.1' "$REPO/.env" \
   && grep -qx 'JARVIS_IMAGE_TAG=1.1.4-rc.1' "$REPO/.env"; then
  pass "update_to_flag_targets_rc_with_same_gates: rc tag merged + pinned v-less"
else
  check_fail "update_to_flag_targets_rc_with_same_gates: log=$(grep merge "$STUB_LOG") env=$(grep -E '^JARVIS_(VERSION|IMAGE_TAG)=' "$REPO/.env")"
fi

# resume re-enters recorded phase: a pending file at phase=pull resumes pulls, never re-merges.
new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
write_pending pull
out="$(run_cli update --yes)"; rc=$?
if grep -q 'compose pull' "$STUB_LOG" && ! grep -q 'merge --ff-only' "$STUB_LOG"; then
  pass "update_resume_reenters_recorded_phase: pending phase=pull resumes pulls, no re-merge"
else
  check_fail "update_resume_reenters_recorded_phase: log=$(cat "$STUB_LOG")"
fi

# explicit --resume skips merge and does not re-exec.
new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
write_pending pull
out="$(run_cli update --resume v1.1.3 --yes)"; rc=$?
if ! grep -q 'merge --ff-only' "$STUB_LOG"; then
  pass "update_resume_skips_merge_and_does_not_reexec: --resume performs no merge"
else
  check_fail "update_resume_skips_merge_and_does_not_reexec: log=$(cat "$STUB_LOG")"
fi

# A resumed commit-addressed transaction must reuse its recorded image identity,
# not derive the image tag from the checkout's semantic application version.
new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
write_pending pull "$SOURCE_SHA" "$TARGET_SHA" "$TARGET_SHA" "$TARGET_SHA"
out="$(run_cli update --resume "$TARGET_SHA" --yes)"; rc=$?
if [ "$rc" -eq 0 ] \
   && ! grep -q 'merge --ff-only' "$STUB_LOG" \
   && grep -q "compose pull .*version=1.1.3 image=$TARGET_SHA" "$STUB_LOG" \
   && grep -qx 'JARVIS_VERSION=1.1.3' "$REPO/.env" \
   && grep -qx "JARVIS_IMAGE_TAG=$TARGET_SHA" "$REPO/.env"; then
  pass "commit_addressed_resume_reuses_recorded_image_identity"
else
  check_fail "commit-addressed resume identity: rc=$rc env=$(grep -E '^JARVIS_(VERSION|IMAGE_TAG)=' "$REPO/.env") log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# Backup-bearing resumes re-authenticate the recorded archive set immediately
# before update.sh. Missing pins or changed bytes fail closed; an intact set runs
# and removes the retention pin only after the committed phase is durable.
resume_run="0123456789abcdef0123456789abcdef"

new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
seed_fresh_backup "$BK" good "$resume_run"
write_pending_backup pull "$resume_run"
write_update_pin "$resume_run"
printf 'CORRUPTED-AFTER-MERGE' > "$BK/jarvis_20991231_235959.sql.gz.enc"
out="$(run_cli update --resume v1.1.3 --yes)"; rc=$?
if [ "$rc" -eq 1 ] && ! grep -q 'compose pull' "$STUB_LOG" \
   && [ -f "$PENDING_FILE" ] && [ -f "$UPDATE_PIN_FILE" ]; then
  pass "resume refuses a changed rollback archive before update.sh and keeps its pin"
else
  check_fail "resume accepted corrupt backup: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
seed_fresh_backup "$BK" good "$resume_run"
write_pending_backup pull "$resume_run"
out="$(run_cli update --resume v1.1.3 --yes)"; rc=$?
if [ "$rc" -eq 1 ] && ! grep -q 'compose pull' "$STUB_LOG" \
   && [ -f "$PENDING_FILE" ]; then
  pass "resume refuses a missing rollback retention pin before update.sh"
else
  check_fail "resume accepted missing backup pin: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
seed_fresh_backup "$BK" good "$resume_run"
write_pending_backup pull "$resume_run"
write_update_pin "$resume_run"
out="$(run_cli update --resume v1.1.3 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && grep -q 'compose pull' "$STUB_LOG" \
   && [ ! -e "$PENDING_FILE" ] && [ ! -e "$UPDATE_PIN_FILE" ]; then
  pass "valid backup-bearing resume re-verifies then clears its pin after commit"
else
  check_fail "valid backup resume contract wrong: rc=$rc pending=$([ -e "$PENDING_FILE" ] && cat "$PENDING_FILE") pin=$([ -e "$UPDATE_PIN_FILE" ] && cat "$UPDATE_PIN_FILE")"
fi

# an explicit --resume tag that disagrees with the pending target is refused
# before any pull/merge (a mistyped tag must not re-pin .env to the wrong version).
new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
write_pending pull
out="$(run_cli update --resume v9.9.9 --yes)"; rc=$?
if [ "$rc" -eq 1 ] && has "$out" 'does not match' \
   && ! grep -qE 'compose pull|merge --ff-only' "$STUB_LOG"; then
  pass "resume_tag_must_match_pending: mismatched --resume tag refused, no pull/merge"
else
  check_fail "resume_tag_must_match_pending: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

# A process death after git moved HEAD but before the CLI advanced its phase must
# resume post-merge work, not report up-to-date or attempt a second merge.
new_env; register_repo; STUB_MERGE_CRASH=1
out="$(run_cli update --yes)"; rc=$?
unset STUB_MERGE_CRASH
first_pending="$(cat "$PENDING_FILE" 2>/dev/null || true)"
out2="$(run_cli update --yes)"; rc2=$?
merge_count="$(grep -c 'git merge --ff-only' "$STUB_LOG" 2>/dev/null || true)"
if [ "$rc" -ne 0 ] && [ "$rc2" -eq 0 ] \
   && has "$first_pending" '"phase":"merge_pending"' \
   && [ "$merge_count" -eq 1 ] && grep -q 'compose pull' "$STUB_LOG"; then
  pass "update_resumes_after_post_merge_process_death: target HEAD completes without a second merge"
else
  check_fail "update_resumes_after_post_merge_process_death: rc=$rc rc2=$rc2 pending=<<<$first_pending>>> merges=$merge_count log=$(cat "$STUB_LOG") out2=<<<$out2>>>"
fi

# A valid transaction is still refused when the checkout is neither its source
# nor target commit.
new_env; register_repo
printf '%s\n' "$OTHER_SHA" > "$STUB_HEAD_FILE"
write_pending pull
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && has "$out" 'HEAD\|checkout\|transaction' \
   && ! grep -qE 'compose pull|merge --ff-only' "$STUB_LOG"; then
  pass "update_pending_refuses_unrelated_head: no post-merge mutation"
else
  check_fail "update_pending_refuses_unrelated_head: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

for bad_state in truncated unknown_phase; do
  new_env; register_repo
  if [ "$bad_state" = truncated ]; then
    printf '{"schema_version":1,"from_sha":"%s"' "$SOURCE_SHA" > "$PENDING_FILE"
  else
    write_pending alien
  fi
  out="$(run_cli update --yes)"; rc=$?
  if [ "$rc" -eq 1 ] && ! grep -qE 'compose pull|merge --ff-only' "$STUB_LOG"; then
    pass "update_refuses_${bad_state}_transaction: malformed state fails closed"
  else
    check_fail "update_refuses_${bad_state}_transaction: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
  fi
done

new_env; register_repo
out="$(run_cli update --resume v1.1.3 --yes)"; rc=$?
if [ "$rc" -eq 1 ] && ! grep -qE 'compose pull|merge --ff-only' "$STUB_LOG"; then
  pass "update_resume_without_pending_refuses: explicit resume cannot invent state"
else
  check_fail "update_resume_without_pending_refuses: rc=$rc out=<<<$out>>> log=$(cat "$STUB_LOG")"
fi

new_env; register_repo
update_lock_id="$(printf '%s' "$(realpath "$REPO")" | sha256sum | cut -d' ' -f1)"
mkdir -p "$CFG/locks"
(
  exec 8>"$CFG/locks/${update_lock_id}.lock"
  flock -n 8 || exit 1
  : > "$CFG/update-lock-held"
  sleep 3
) &
lock_holder=$!
for _ in $(seq 1 40); do [ -e "$CFG/update-lock-held" ] && break; sleep 0.05; done
out="$(run_cli update --yes)"; rc=$?
kill "$lock_holder" 2>/dev/null || true; wait "$lock_holder" 2>/dev/null || true
if [ "$rc" -eq 1 ] && ! grep -qE 'compose pull|merge --ff-only' "$STUB_LOG"; then
  pass "update_refuses_a_concurrent_operation_for_the_same_install"
else
  check_fail "update_refuses_a_concurrent_operation_for_the_same_install: rc=$rc"
fi

# Pending journals share a config directory but not a filename. An interrupted
# update in one clone must neither block nor be overwritten by another clone.
new_env; register_repo
repo_a="$REPO"; pending_a="$PENDING_FILE"
write_pending merge_pending
pending_a_before="$(cat "$pending_a")"
repo_b="$ROOT/repo-b.$RANDOM.$RANDOM"
make_repo "$repo_b"
printf '%s\n' "$SOURCE_SHA" > "$repo_b/.stub-head"
printf '%s\n%s\n' "$repo_a" "$repo_b" > "$CFG/installs"
REPO="$repo_b"
STUB_HEAD_FILE="$CFG/head-b"
printf '%s\n' "$SOURCE_SHA" > "$STUB_HEAD_FILE"
install_key_b="$(printf '%s' "$(realpath "$REPO")" | sha256sum | cut -d' ' -f1)"
PENDING_FILE="$CFG/pending-update-${install_key_b}.json"
STUB_FAIL_STAGE_PULL='jarvis-target-worker'
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && [ "$PENDING_FILE" != "$pending_a" ] \
   && [ "$(cat "$pending_a")" = "$pending_a_before" ] \
   && [ -f "$PENDING_FILE" ] && grep -q '"phase":"merge_pending"' "$PENDING_FILE"; then
  pass "two_installs_keep_independent_interrupted_update_journals"
else
  check_fail "per-install journal isolation: rc=$rc a=$(cat "$pending_a" 2>/dev/null) b=$(cat "$PENDING_FILE" 2>/dev/null) out=<<<$out>>>"
fi
unset STUB_FAIL_STAGE_PULL

# A current-format journal written by the pre-fix CLI at the old global path is
# migrated only after its source/target commit pair identifies this clone.
new_env; register_repo
write_pending merge_pending
mv "$PENDING_FILE" "$LEGACY_PENDING_FILE"
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 0 ] && [ ! -e "$LEGACY_PENDING_FILE" ] \
   && [ ! -e "$PENDING_FILE" ] && grep -q 'git merge --ff-only' "$STUB_LOG"; then
  pass "legacy_global_current_journal_migrates_to_the_attributed_install"
else
  check_fail "current journal migration: rc=$rc legacy=$(cat "$LEGACY_PENDING_FILE" 2>/dev/null) scoped=$(cat "$PENDING_FILE" 2>/dev/null) out=<<<$out>>>"
fi

# Malformed or unrelated global state is never moved or deleted on behalf of
# the selected install.
for legacy_case in malformed unrelated; do
  new_env; register_repo
  if [ "$legacy_case" = malformed ]; then
    printf '{"from_sha":"broken"' > "$LEGACY_PENDING_FILE"
  else
    printf '{"schema_version":1,"from_sha":"%s","from_version":"1.1.2","target":"v1.1.3","target_sha":"%s","target_version":"1.1.3","phase":"merge_pending","started_at":"1","backup_id":"","backup_run_id":"","legacy_recovery":false}\n' \
      "$OTHER_SHA" "$TARGET_SHA" > "$LEGACY_PENDING_FILE"
  fi
  legacy_before="$(cat "$LEGACY_PENDING_FILE")"
  out="$(run_cli update --yes)"; rc=$?
  if [ "$rc" -eq 1 ] && [ "$(cat "$LEGACY_PENDING_FILE")" = "$legacy_before" ] \
     && [ ! -e "$PENDING_FILE" ] && ! grep -qE 'image pull|git merge --ff-only' "$STUB_LOG"; then
    pass "legacy_global_${legacy_case}_journal_fails_closed_without_moving_state"
  else
    check_fail "legacy ${legacy_case} handling: rc=$rc legacy=$(cat "$LEGACY_PENDING_FILE" 2>/dev/null) scoped=$(cat "$PENDING_FILE" 2>/dev/null) log=$(cat "$STUB_LOG") out=<<<$out>>>"
  fi
done

# If two registered clones are at the same recorded target, the old global
# journal cannot identify its owner and must remain untouched.
new_env
repo_a="$REPO"
repo_b="$ROOT/repo-ambiguous.$RANDOM.$RANDOM"
make_repo "$repo_b"
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
printf '%s\n' "$TARGET_SHA" > "$repo_a/.stub-head"
printf '%s\n' "$TARGET_SHA" > "$repo_b/.stub-head"
printf '%s\n%s\n' "$repo_a" "$repo_b" > "$CFG/installs"
printf '{"from_sha":"%s","from_version":"1.1.3","target":"v1.2.0","target_version":"1.2.0","phase":"staging","started_at":"1","backup_id":""}\n' \
  "$SOURCE_SHA" > "$LEGACY_PENDING_FILE"
legacy_before="$(cat "$LEGACY_PENDING_FILE")"
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 1 ] && [ "$(cat "$LEGACY_PENDING_FILE")" = "$legacy_before" ] \
   && [ ! -e "$PENDING_FILE" ] && ! grep -qE 'image pull|git merge --ff-only' "$STUB_LOG"; then
  pass "ambiguous_legacy_global_journal_is_not_claimed_by_either_install"
else
  check_fail "ambiguous legacy attribution: rc=$rc legacy=$(cat "$LEGACY_PENDING_FILE" 2>/dev/null) log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# v1.1.3 can leave phase=staging after HEAD already moved because the new CLI is
# loaded only after that merge. Accept exactly that historical shape once.
new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
printf '%s\n' "$TARGET_SHA" > "$REPO/.stub-head"
printf '{"from_sha":"%s","from_version":"1.1.3","target":"v1.2.0","target_version":"1.2.0","phase":"staging","started_at":"1","backup_id":""}\n' \
  "$SOURCE_SHA" > "$LEGACY_PENDING_FILE"
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 0 ] && grep -q 'compose pull' "$STUB_LOG" \
   && [ ! -e "$PENDING_FILE" ] && [ ! -e "$LEGACY_PENDING_FILE" ]; then
  pass "update_recovers_v113_staging_after_target_head: v1.1.3-era state targeting v1.2.0 reconciled"
else
  check_fail "update_recovers_v113_staging_after_target_head: rc=$rc pending=$(cat "$PENDING_FILE" 2>/dev/null) log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
printf '%s\n' "$TARGET_SHA" > "$REPO/.stub-head"
seed_fresh_backup "$BK" legacy ""
printf '{"from_sha":"%s","from_version":"1.1.3","target":"v1.2.0","target_version":"1.2.0","phase":"merged","started_at":"1","backup_id":"20991231_235959"}\n' \
  "$SOURCE_SHA" > "$LEGACY_PENDING_FILE"
out="$(run_cli update --resume v1.2.0 --yes)"; rc=$?
if [ "$rc" -eq 0 ] && grep -q 'compose pull' "$STUB_LOG" \
   && [ ! -e "$PENDING_FILE" ] && [ ! -e "$LEGACY_PENDING_FILE" ]; then
  if [ ! -e "$UPDATE_PIN_FILE" ]; then
    pass "update_recovers_v113_merged_handoff_with_authenticated_legacy_backup"
  else
    check_fail "legacy recovery left a stale update backup pin"
  fi
else
  check_fail "update_recovers_v113_merged_handoff_with_authenticated_legacy_backup: rc=$rc"
fi

new_env; register_repo
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
write_pending committed
out="$(run_cli update --yes)"; rc=$?
if [ "$rc" -eq 0 ] && [ ! -e "$PENDING_FILE" ] && ! grep -qE 'compose pull|merge --ff-only' "$STUB_LOG"; then
  pass "update_already_complete_transaction_is_clean_noop: stale committed marker removed"
else
  check_fail "update_already_complete_transaction_is_clean_noop: rc=$rc pending=$(cat "$PENDING_FILE" 2>/dev/null) log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# =============================================================================
# 4. Mechanical + misc.
# =============================================================================
if ! grep -qE 'checkout[[:space:]]+-B|reset[[:space:]]+--hard' "$CLI"; then
  pass "update_script_never_contains_checkout_dash_B: no 'checkout -B' / 'reset --hard' in the script"
else
  check_fail "update_script_never_contains_checkout_dash_B: found a banned branch-rewrite verb"
fi

# uninstall dispatches to scripts/uninstall.sh, vouching for the repo with --repo
# and passing the operator's flags through unchanged.
new_env; register_repo
cat > "$REPO/scripts/uninstall.sh" <<'PROBE'
#!/usr/bin/env bash
printf 'UNINSTALL-ARGS: %s\n' "$*"
PROBE
chmod +x "$REPO/scripts/uninstall.sh"
out="$(run_cli uninstall --dry-run --tier 1)"; rc=$?
if has "$out" "UNINSTALL-ARGS: --repo $REPO --dry-run --tier 1"; then
  pass "uninstall_dispatches_to_script: CLI execs scripts/uninstall.sh --repo \$REPO with the flags"
else
  check_fail "uninstall_dispatches_to_script: <<<$out>>>"
fi

# status happy path against stubbed `docker compose ps`.
new_env; register_repo
out="$(run_cli status)"; rc=$?
if [ "$rc" -eq 0 ] && has "$out" 'dashboard'; then
  pass "status_happy_path: renders the compose ps table, exit 0"
else
  check_fail "status_happy_path: rc=$rc out=$out"
fi

# Instance-owner status is read-only and resolves the effective environment
# from the running service, never by sourcing the host .env file.
new_env; register_repo
STUB_OWNER_DB_RESULT='database|valid|7|owner@example.com'
out="$(run_cli owner status)"; rc=$?
if [ "$rc" -eq 0 ] \
   && has "$out" 'Source: database' \
   && has "$out" 'State: valid' \
   && has "$out" 'owner@example.com' \
   && grep -q -- '-- jarvis-owner-status' "$STUB_PSQL_INPUT_FILE" \
   && grep -q 'compose exec paper_ingestion' "$STUB_LOG" \
   && ! grep -q 'lifecycle' "$STUB_LOG"; then
  pass "owner_status_reports_effective_database_owner_without_lifecycle_mutation"
else
  check_fail "owner status database path: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo
STUB_OWNER_ENV=42
STUB_OWNER_DB_RESULT='environment|valid|42|host-owner@example.com'
out="$(run_cli owner status)"; rc=$?
if [ "$rc" -eq 0 ] \
   && has "$out" 'Source: environment' \
   && has "$out" 'host-owner@example.com' \
   && grep -q 'compose exec paper_ingestion' "$STUB_LOG"; then
  pass "owner_status_reads_authoritative_override_from_running_service"
else
  check_fail "owner status environment path: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# Owner repair requires exact typed confirmation before taking a lifecycle lock
# or opening a database transaction.
new_env; register_repo
out="$(run_cli_with_input $'wrong@example.com\n' owner set owner@example.com)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'did not match' \
   && [ ! -e "$STUB_PSQL_INPUT_FILE" ] \
   && [ ! -e "$BK/.lifecycle/operation.state" ]; then
  pass "owner_set_refuses_mismatched_confirmation_before_mutation"
else
  check_fail "owner set confirmation refusal: rc=$rc out=<<<$out>>>"
fi

# An effective host override cannot be shadowed by changing only the database.
new_env; register_repo
STUB_OWNER_ENV=42
out="$(run_cli_with_input $'owner@example.com\n' owner set owner@example.com)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'OWNER_USER_ID' \
   && [ ! -e "$STUB_PSQL_INPUT_FILE" ]; then
  pass "owner_set_refuses_environment_managed_ownership"
else
  check_fail "owner set environment refusal: rc=$rc out=<<<$out>>>"
fi

# A successful repair is one parameter-bound transaction under both lifecycle
# and database locks, with its mandatory audit insert in the same stdin script.
new_env; register_repo
out="$(run_cli_with_input $'owner@example.com\n' owner set owner@example.com)"; rc=$?
if [ "$rc" -eq 0 ] \
   && has "$out" 'owner@example.com' \
   && grep -q -- '-- jarvis-owner-set' "$STUB_PSQL_INPUT_FILE" \
   && grep -q '^BEGIN;' "$STUB_PSQL_INPUT_FILE" \
   && grep -q "pg_advisory_xact_lock(hashtext('admin_role_mutation'))" "$STUB_PSQL_INPUT_FILE" \
   && grep -q ":'target_email'" "$STUB_PSQL_INPUT_FILE" \
   && grep -q 'admin.owner.repair' "$STUB_PSQL_INPUT_FILE" \
   && grep -q '^COMMIT;' "$STUB_PSQL_INPUT_FILE" \
   && ! grep -qF 'owner@example.com' "$STUB_PSQL_INPUT_FILE" \
   && [ ! -e "$BK/.lifecycle/operation.state" ]; then
  pass "owner_set_is_parameter_bound_locked_audited_and_lifecycle_scoped"
else
  check_fail "owner set transaction: rc=$rc sql=$(cat "$STUB_PSQL_INPUT_FILE" 2>/dev/null) log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo
STUB_OWNER_SET_RC=1
out="$(run_cli_with_input $'owner@example.com\n' owner set owner@example.com)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'owner status' \
   && [ ! -e "$BK/.lifecycle/operation.state" ]; then
  pass "owner_set_database_refusal_clears_lifecycle_state"
else
  check_fail "owner set database refusal: rc=$rc state=$(cat "$BK/.lifecycle/operation.state" 2>/dev/null) out=<<<$out>>>"
fi

# Every usage refusal names the exact invocation to run instead of the generic
# help pointer. Each site is driven on its own, so none can be silently skipped.
# Fields: argv @@ the site's own message @@ the invocation it must name.
USAGE_SITES=(
  "update --frobnicate@@update: unknown option '--frobnicate'@@Run: jarvis-research update [--to <tag>] [--resume <tag>] [--yes]"
  "owner set@@owner set takes exactly one email address.@@Run: jarvis-research owner set <email>"
  "owner set not-an-email@@owner set requires one ordinary email address.@@Run: jarvis-research owner set <email>"
  "owner status extra@@owner status takes no arguments.@@Run: jarvis-research owner status"
  "owner bogus@@owner: unknown subcommand 'bogus'.@@Run: jarvis-research owner status   (or: jarvis-research owner set <email>)"
  "restore acknowledge@@restore acknowledge takes exactly one restore ID.@@Run: jarvis-research restore acknowledge <restore-id>"
  "restore acknowledge short@@restore acknowledge requires one lowercase 32-hex restore ID.@@Run: jarvis-research restore acknowledge <restore-id>"
  "restore bogus@@restore: unknown subcommand 'bogus'.@@Run: jarvis-research restore status   (or: restore run|legacy|request <timestamp>, restore acknowledge <restore-id>)"
  "restore legacy@@restore legacy takes exactly one backup timestamp.@@Run: jarvis-research restore legacy <timestamp>"
  "restore legacy nonsense@@restore legacy requires one backup timestamp in YYYYMMDD_HHMMSS form.@@Run: jarvis-research restore legacy <timestamp>"
  "restore legacy --bogus@@restore legacy: unknown option '--bogus'.@@Run: jarvis-research restore legacy <timestamp> [--allow-unknown-schema]"
  "restore request@@restore request takes exactly one backup timestamp.@@Run: jarvis-research restore request <timestamp>"
  "restore request nonsense@@restore request requires one backup timestamp in YYYYMMDD_HHMMSS form.@@Run: jarvis-research restore request <timestamp>"
  "restore status extra@@restore status takes no arguments.@@Run: jarvis-research restore status"
)
for entry in "${USAGE_SITES[@]}"; do
  usage_argv="${entry%%@@*}"; usage_rest="${entry#*@@}"
  usage_msg="${usage_rest%%@@*}"; usage_remedy="${usage_rest#*@@}"
  new_env; register_repo
  # shellcheck disable=SC2086  # a fixed, test-owned argument list
  out="$(run_cli $usage_argv)"; rc=$?
  if [ "$rc" -eq 2 ] \
     && hasF "$out" "$usage_msg" \
     && hasF "$out" "$usage_remedy" \
     && ! hasF "$out" 'Run: jarvis-research help'; then
    pass "usage_error_names_the_correct_invocation: ${usage_argv}"
  else
    check_fail "usage_error_names_the_correct_invocation ${usage_argv}: rc=$rc out=<<<$out>>>"
  fi
done

# Restore acknowledgement binds typed confirmation to the exact quarantine,
# consumes the restore-session token first, and never prints it.
new_env; register_repo; seed_restore_review
out="$(run_cli_with_input "${RESTORE_REVIEW_ID}"$'\n' restore acknowledge "$RESTORE_REVIEW_ID")"; rc=$?
if [ "$rc" -eq 0 ] \
   && has "$out" "$RESTORE_REVIEW_ID" \
   && ! has "$out" 'sha256' \
   && ! has "$(cat "$STUB_LOG")" 'sha256' \
   && [ ! -e "$TRIG/.restore_status_token.json" ] \
   && [ ! -e "$TRIG/.outbound-quarantine.json" ] \
   && [ ! -e "$BK/.lifecycle/operation.state" ] \
   && grep -q "lifecycle inspect-quarantine ${RESTORE_REVIEW_ID}" "$STUB_LOG" \
   && grep -q "lifecycle acknowledge-quarantine ${RESTORE_REVIEW_ID}" "$STUB_LOG"; then
  pass "restore_acknowledge_exact_id_consumes_token_then_quarantine_under_lifecycle"
else
  check_fail "restore acknowledge success: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo; seed_restore_review
out="$(run_cli_with_input "${OTHER_RESTORE_REVIEW_ID}"$'\n' restore acknowledge "$RESTORE_REVIEW_ID")"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'did not match' \
   && [ -e "$TRIG/.restore_status_token.json" ] \
   && [ -e "$TRIG/.outbound-quarantine.json" ] \
   && [ ! -e "$BK/.lifecycle/operation.state" ] \
   && ! grep -q 'lifecycle acknowledge-quarantine' "$STUB_LOG"; then
  pass "restore_acknowledge_refuses_wrong_typed_id_before_lifecycle_or_mutation"
else
  check_fail "restore acknowledge wrong confirmation: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo; seed_restore_review
out="$(run_cli restore acknowledge short)"; rc=$?
if [ "$rc" -eq 2 ] \
   && has "$out" 'lowercase 32-hex restore ID' \
   && [ -e "$TRIG/.restore_status_token.json" ] \
   && [ -e "$TRIG/.outbound-quarantine.json" ] \
   && [ ! -s "$STUB_LOG" ]; then
  pass "restore_acknowledge_rejects_noncanonical_id_before_docker"
else
  check_fail "restore acknowledge invalid id: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

for quarantine_kind in malformed dangling_symlink; do
  new_env; register_repo; seed_restore_review
  case "$quarantine_kind" in
    malformed) printf 'not-json\n' > "$TRIG/.outbound-quarantine.json" ;;
    dangling_symlink)
      rm -f "$TRIG/.outbound-quarantine.json"
      ln -s "$TRIG/missing-quarantine" "$TRIG/.outbound-quarantine.json"
      ;;
  esac
  out="$(run_cli restore acknowledge "$RESTORE_REVIEW_ID")"; rc=$?
  if [ "$rc" -eq 1 ] \
     && has "$out" 'unavailable or does not match' \
     && [ -e "$TRIG/.restore_status_token.json" ] \
     && { [ -e "$TRIG/.outbound-quarantine.json" ] || [ -L "$TRIG/.outbound-quarantine.json" ]; } \
     && [ ! -e "$BK/.lifecycle/operation.state" ]; then
    pass "restore_acknowledge_refuses_${quarantine_kind}_sentinel_fail_closed"
  else
    check_fail "restore acknowledge ${quarantine_kind}: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
  fi
done

new_env; register_repo; seed_restore_review
STUB_QUARANTINE_REPLACE_ON_ACK="$OTHER_RESTORE_REVIEW_ID"
out="$(run_cli_with_input "${RESTORE_REVIEW_ID}"$'\n' restore acknowledge "$RESTORE_REVIEW_ID")"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'changed before acknowledgement' \
   && [ -e "$TRIG/.restore_status_token.json" ] \
   && grep -q "$OTHER_RESTORE_REVIEW_ID" "$TRIG/.outbound-quarantine.json" \
   && [ ! -e "$BK/.lifecycle/operation.state" ]; then
  pass "restore_acknowledge_revalidates_exact_sentinel_after_lifecycle_admission"
else
  check_fail "restore acknowledge race: rc=$rc state=$(cat "$BK/.lifecycle/operation.state" 2>/dev/null) log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo; seed_restore_review
rm -f "$TRIG/.restore_status_token.json"
mkdir "$TRIG/.restore_status_token.json"
out="$(run_cli_with_input "${RESTORE_REVIEW_ID}"$'\n' restore acknowledge "$RESTORE_REVIEW_ID")"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'quarantine remains active' \
   && [ -d "$TRIG/.restore_status_token.json" ] \
   && [ -e "$TRIG/.outbound-quarantine.json" ] \
   && [ ! -e "$BK/.lifecycle/operation.state" ]; then
  pass "restore_acknowledge_token_consume_failure_keeps_quarantine"
else
  check_fail "restore acknowledge token failure: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo; seed_restore_review
printf 'restore\n' > "$BK/.lifecycle/operation.state"
out="$(run_cli_with_input "${RESTORE_REVIEW_ID}"$'\n' restore acknowledge "$RESTORE_REVIEW_ID")"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'lifecycle operation' \
   && [ -e "$TRIG/.restore_status_token.json" ] \
   && [ -e "$TRIG/.outbound-quarantine.json" ] \
   && [ "$(cat "$BK/.lifecycle/operation.state")" = restore ] \
   && ! grep -q 'lifecycle acknowledge-quarantine' "$STUB_LOG"; then
  pass "restore_acknowledge_refuses_foreign_lifecycle_without_mutation"
else
  check_fail "restore acknowledge foreign lifecycle: rc=$rc state=$(cat "$BK/.lifecycle/operation.state" 2>/dev/null) log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# =============================================================================
# Recovery commands: break-glass restore, restore progress, off-host request.
# =============================================================================
# Scheduled backup never consumes restore requests. The explicit host command
# starts a no-listener, transient restore job with the exceptional credential.
new_env; register_repo
printf '{"source":"local","timestamp":"20260101_010101","restore_id":"0123456789abcdef0123456789abcdef","requested_at":"2026-07-21T20:00:00Z"}\n' \
  > "$TRIG/.restore_request.json"
out="$(run_cli restore run)"; rc=$?
restore_run_argv="$(grep 'compose-run .*postgres-restore' "$STUB_LOG" | head -1)"
if [ "$rc" -eq 0 ] \
   && has "$out" 'completed after authority reconstruction and migrations' \
   && hasF "$restore_run_argv" '--rm' \
   && hasF "$restore_run_argv" '--no-deps' \
   && hasF "$restore_run_argv" 'postgres-restore' \
   && grep -q 'compose-run .*cluster-bootstrap restore-prepare' "$STUB_LOG" \
   && grep -q 'compose-run .*jarvis-migrator' "$STUB_LOG" \
   && grep -q 'compose-run .*litellm-migrator' "$STUB_LOG" \
   && grep -q 'compose-run .*cluster-bootstrap restore-finalize' "$STUB_LOG" \
   && grep -q 'compose-run .*postgres-restore --complete-authority' "$STUB_LOG"; then
  pass "restore_run_is_fail_fast_and_completes_authority_in_the_required_order"
else
  check_fail "restore run: rc=$rc argv=<<<$restore_run_argv>>> out=<<<$out>>>"
fi

new_env; register_repo
printf '{"state":"running","current_step":"Reconstructing database authority","steps":[],"safety_backup_ts":null,"started_at":"1","finished_at":null,"error":null,"manual_steps_required":false,"phase":"database_authority","restore_id":"0123456789abcdef0123456789abcdef","source":"local"}\n' \
  > "$TRIG/.restore_status.json"
out="$(run_cli restore run)"; rc=$?
if [ "$rc" -eq 0 ] \
   && has "$out" 'interrupted restore completed after authority reconstruction and migrations' \
   && ! grep -q 'compose-run .*postgres-restore --run-request' "$STUB_LOG" \
   && grep -q 'compose-run .*cluster-bootstrap restore-prepare' "$STUB_LOG" \
   && grep -q 'compose-run .*postgres-restore --complete-authority' "$STUB_LOG"; then
  pass "restore_run_resumes_pending_authority_without_replaying_the_data_swap"
else
  check_fail "restore run authority resume: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo
out="$(run_cli restore run)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'No valid restore request is queued' \
   && ! grep -q 'compose-run .*postgres-restore --run-request' "$STUB_LOG"; then
  pass "restore_run_refuses_a_missing_request_without_claiming_success"
else
  check_fail "restore run missing request: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo
printf '{"source":"local","timestamp":"20260101_010101","restore_id":"0123456789abcdef0123456789abcdef","requested_at":"2026-07-21T20:00:00Z"}\n' \
  > "$TRIG/.restore_request.json"
STUB_RESTORE_STATUS_AFTER_REQUEST=failed
out="$(run_cli restore run)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'did not reach database authority reconstruction' \
   && ! grep -q 'compose-run .*cluster-bootstrap restore-prepare' "$STUB_LOG"; then
  pass "restore_run_refuses_a_failed_durable_status_without_finalizing"
else
  check_fail "restore run failed status: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

new_env; register_repo
out="$(BACKUP_COMPOSE_TIMEOUT_SECONDS=7 run_cli restore legacy 20260101_010101)"; rc=$?
legacy_write="$(grep -n 'compose-run .*restore_request\.json' "$STUB_LOG" | head -1 | cut -d: -f1)"
legacy_run="$(grep -n 'compose-run .*restore\.sh' "$STUB_LOG" | head -1 | cut -d: -f1)"
legacy_write_argv="$(grep 'compose-run .*restore_request\.json' "$STUB_LOG" | head -1)"
legacy_run_argv="$(grep 'compose-run .*restore\.sh' "$STUB_LOG" | head -1)"
legacy_request="$(cat "$BK/.captured_restore_request.json" 2>/dev/null || true)"
if [ "$rc" -eq 0 ] \
   && [ -n "$legacy_write" ] && [ -n "$legacy_run" ] \
   && [ "$legacy_write" -lt "$legacy_run" ] \
   && hasF "$legacy_run_argv" 'postgres-restore' \
   && ! grep -q 'compose \(stop\|start\) postgres-backup' "$STUB_LOG"; then
  pass "restore_legacy_uses_the_transient_restore_job_without_stopping_backups"
else
  check_fail "restore legacy ordering: rc=$rc write=$legacy_write run=$legacy_run log=$(cat "$STUB_LOG")"
fi

# Both one-offs skip dependencies; the direct legacy run overrides the restore
# job's request-consuming entrypoint so its typed acknowledgement reaches stdin.
if hasF "$legacy_write_argv" '--no-deps' && hasF "$legacy_write_argv" '--entrypoint sh' \
   && hasF "$legacy_run_argv" '--no-deps' \
   && hasF "$legacy_run_argv" '--entrypoint /usr/local/bin/restore.sh'; then
  pass "restore_legacy_one_offs_skip_dependencies_and_override_the_poll_entrypoint"
else
  check_fail "restore legacy one-off flags: write=<<<$legacy_write_argv>>> run=<<<$legacy_run_argv>>>"
fi

# No Compose flag forces a pseudo-terminal, so the interactive leg must not carry
# -T (which would make the acceptance prompt unreachable) and must not run under
# the compose timeout wrapper (which would kill the operator mid-prompt). The
# request write is the opposite: its stdin is a pipe, so -T belongs there.
if hasF "$legacy_write_argv" ' -T ' \
   && hasF "$legacy_write_argv" 'compose-run timeout=7 ' \
   && ! hasF "$legacy_run_argv" ' -T ' \
   && hasF "$legacy_run_argv" 'compose-run timeout= '; then
  pass "restore_legacy_pins_T_to_the_request_write_and_runs_the_prompt_untimed"
else
  check_fail "restore legacy tty/timeout policy: write=<<<$legacy_write_argv>>> run=<<<$legacy_run_argv>>>"
fi

if printf '%s' "$legacy_request" | grep -Eq '"source":"local"' \
   && printf '%s' "$legacy_request" | grep -Eq '"timestamp":"20260101_010101"' \
   && printf '%s' "$legacy_request" | grep -Eq '"restore_id":"[0-9a-f]{32}"' \
   && printf '%s' "$legacy_request" | grep -Eq '"requested_at":"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"'; then
  pass "restore_legacy_writes_a_well_formed_same_host_restore_request"
else
  check_fail "restore legacy request json: <<<$legacy_request>>>"
fi

# The unknown-schema acknowledgement is off unless the operator opts in, so the
# schema-0 refusal's "--allow-unknown-schema" advice is followable from the CLI.
if printf '%s' "$legacy_request" | grep -q 'allow_unknown_schema'; then
  check_fail "restore legacy default request must NOT acknowledge unknown schema: <<<$legacy_request>>>"
else
  pass "restore_legacy_default_request_does_not_acknowledge_unknown_schema"
fi
new_env; register_repo
run_cli restore legacy 20260101_010101 --allow-unknown-schema >/dev/null 2>&1
legacy_ack_request="$(cat "$BK/.captured_restore_request.json" 2>/dev/null || true)"
if printf '%s' "$legacy_ack_request" | grep -Eq '"allow_unknown_schema":true' \
   && printf '%s' "$legacy_ack_request" | grep -Eq '"timestamp":"20260101_010101"'; then
  pass "restore_legacy_allow_unknown_schema_flag_sets_the_acknowledgement"
else
  check_fail "restore legacy --allow-unknown-schema request: <<<$legacy_ack_request>>>"
fi

# A failed break-glass restore never changes the scheduled backup service.
new_env; register_repo
STUB_RESTORE_LEGACY_RC=1
out="$(run_cli restore legacy 20260101_010101)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'restore status' \
   && ! grep -q 'compose \(stop\|start\) postgres-backup' "$STUB_LOG"; then
  pass "restore_legacy_failure_leaves_scheduled_backup_untouched"
else
  check_fail "restore legacy failure isolation: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# restore status reads the sidecar's status file through its own one-off.
new_env; register_repo
printf '{"state":"failed","current_step":"Restoring database","steps":[],"safety_backup_ts":"20260101_000000","started_at":"1","finished_at":null,"error":"pg_restore failed","drop_started":true,"manual_steps_required":true,"phase":"destructive"}\n' \
  > "$TRIG/.restore_status.json"
out="$(run_cli restore status)"; rc=$?
status_argv="$(grep 'compose-run .*restore_status\.json' "$STUB_LOG" | head -1)"
if [ "$rc" -eq 0 ] \
   && has "$out" 'failed' \
   && has "$out" 'pg_restore failed' \
   && has "$out" 'Restoring database' \
   && has "$out" '20260101_000000' \
   && has "$out" 'required' \
   && hasF "$status_argv" '--no-deps' \
   && hasF "$status_argv" '--entrypoint sh'; then
  pass "restore_status_reports_state_error_manual_steps_and_the_safety_point"
else
  check_fail "restore status read: rc=$rc argv=<<<$status_argv>>> out=<<<$out>>>"
fi

# The recovery commands exist for the disaster where the database will not start,
# so a stopped stack must NOT lock them out: the ownership check reads the labels
# of the stopped container and the command proceeds through its own one-off.
new_env; register_repo
STUB_STACK_DOWN=1
printf '{"state":"failed","current_step":"Restoring database","steps":[],"safety_backup_ts":null,"started_at":"1","finished_at":null,"error":"pg_restore failed","drop_started":false,"manual_steps_required":false,"phase":"pre"}\n' \
  > "$TRIG/.restore_status.json"
out="$(run_cli restore status)"; rc=$?
if [ "$rc" -eq 0 ] \
   && has "$out" 'failed' \
   && grep -q 'compose-run' "$STUB_LOG"; then
  pass "restore_status_still_reads_its_report_when_the_stack_is_stopped"
else
  check_fail "restore status stopped stack: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# A restore replays into a RUNNING database, so a stopped stack is refused at the
# start, by name, and nothing is stopped or written on the way out.
new_env; register_repo
STUB_STACK_DOWN=1
out="$(run_cli restore legacy 20260101_010101)"; rc=$?
if [ "$rc" -ne 0 ] \
   && has "$out" 'database is not running' \
   && has "$out" 'jarvis-research start' \
   && ! grep -q 'compose stop postgres-backup' "$STUB_LOG"; then
  pass "restore_legacy_refuses_by_name_when_the_database_is_not_running"
else
  check_fail "restore legacy stopped stack: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# An install whose containers were never created cannot be ownership-checked at
# all. That fence stays up, and the refusal names the cause and the remedy.
new_env; register_repo
STUB_NO_CONTAINERS=1
out="$(run_cli restore status)"; rc=$?
if [ "$rc" -eq 1 ] \
   && has "$out" 'no Postgres container' \
   && has "$out" 'jarvis-research start' \
   && ! grep -q 'compose-run' "$STUB_LOG"; then
  pass "recovery_refuses_actionably_when_the_containers_do_not_exist"
else
  check_fail "restore status no containers: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# The off-host request is printed for the operator to submit AFTER the archives
# and the one-time key are in place. Submitting it early fails the restore.
new_env; register_repo
out="$(run_cli restore request 20260101_010101)"; rc=$?
if [ "$rc" -eq 0 ] \
   && hasF "$out" 'cp ./offsite/. postgres-backup:/restore-inbox/' \
   && hasF "$out" 'postgres-backup:/restore-inbox/operator_key' \
   && hasF "$out" '"source":"inbox"' \
   && hasF "$out" '"timestamp":"20260101_010101"' \
   && [ ! -e "$TRIG/.restore_request.json" ] \
   && [ ! -s "$STUB_LOG" ]; then
  pass "restore_request_prints_the_off_host_procedure_and_submits_nothing"
else
  check_fail "restore request: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# The printed commands run in the operator's own shell, outside the ownership
# check. Every one of them must therefore name this install's project and files;
# a bare `docker compose` would obey a stray COMPOSE_PROJECT_NAME instead.
request_cmds="$(printf '%s\n' "$out" | grep -E 'docker compose|postgres-backup' | grep -v '^ *jarvis-research')"
request_unscoped="$(printf '%s\n' "$request_cmds" | grep -E '(^|\| *) *docker compose ' | grep -vF -- '-p ' || true)"
if [ -n "$request_cmds" ] && [ -z "$request_unscoped" ] \
   && hasF "$out" "--project-directory" \
   && hasF "$out" "--env-file" \
   && [ "$(printf '%s\n' "$request_cmds" | grep -cF -- '-p ')" -eq 3 ]; then
  pass "restore_request_scopes_every_printed_command_to_this_install"
else
  check_fail "restore request scoping: unscoped=<<<$request_unscoped>>> cmds=<<<$request_cmds>>>"
fi

# Managed CLI commands are install-scoped even when the invoking shell exports
# Compose selectors for a different checkout or project.
new_env; register_repo
export COMPOSE_FILE=/tmp/foreign-compose.yml
export COMPOSE_PROJECT_NAME=foreign-project
export COMPOSE_PROFILES=foreign-profile
export COMPOSE_PATH_SEPARATOR=';'
export COMPOSE_ENV_FILES=/tmp/foreign.env
export COMPOSE_DISABLE_ENV_FILE=1
out="$(run_cli status)"; rc=$?
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_PATH_SEPARATOR
unset COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE
if [ "$rc" -eq 0 ] \
   && grep -qF 'compose-env file=<unset> project=<unset> profiles=<unset> separator=<unset> envfiles=<unset> disable=<unset>' "$STUB_LOG"; then
  pass "managed CLI clears caller Compose selectors before dispatch"
else
  check_fail "managed CLI leaked caller Compose selectors: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# Every mutating day-to-day command shares the same host + sidecar lifecycle
# admission. A durable foreign restore must refuse before even a Docker probe,
# must not queue a later activation, and a deliberate retry after release works.
for control_cmd in start stop restart repair; do
  new_env; register_repo
  printf 'restore\n' > "$BK/.lifecycle/operation.state"
  out="$(run_cli "$control_cmd")"; rc=$?
  if [ "$rc" -eq 1 ] \
     && ! grep -qE 'docker (compose (up|stop|restart)|restart )' "$STUB_LOG" \
     && [ "$(cat "$BK/.lifecycle/operation.state")" = restore ]; then
    pass "${control_cmd}_refuses_foreign_lifecycle_before_service_mutation"
  else
    check_fail "${control_cmd}_foreign_lifecycle_refusal: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
  fi
  rm -f "$BK/.lifecycle/operation.state"
  : > "$STUB_LOG"
  out="$(run_cli "$control_cmd")"; rc=$?
  if [ "$rc" -eq 0 ] && [ -s "$STUB_LOG" ] \
     && [ ! -e "$BK/.lifecycle/operation.state" ]; then
    pass "${control_cmd}_retry_after_lifecycle_release_succeeds"
  else
    check_fail "${control_cmd}_retry_after_release: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
  fi
done

# doctor probes each accelerator overlay by the route that overlay actually uses.
# run_cli cannot carry these host-path overrides, so the three cases share one
# invocation helper that adds them to the same stub environment.
run_cli_doctor_host() {
  env "PATH=$STUB:$PATH" "STUB_LOG=$STUB_LOG" \
    "JARVIS_CLI_CONFIG_DIR=$CFG" "JARVIS_CLI_BIN_DIR=$CFG/bin" \
    "JARVIS_DRI_DIR=$1" "JARVIS_DEV_DIR=$2" \
    bash "$REPO/scripts/jarvis-research.sh" --repo "$REPO" doctor </dev/null 2>&1
}

# doctor warns on a ROCm/Vulkan overlay with no DRI render node, exit unchanged.
new_env; register_repo
printf 'JARVIS_VERSION=1.1.2\nJARVIS_IMAGE_TAG=1.1.2\nTORCH_VARIANT=cpu\nCOMPOSE_FILE=docker-compose.yml:docker-compose.vulkan.yml\n' > "$REPO/.env"
EMPTY_DRI="$ROOT/dri.$RANDOM"; mkdir -p "$EMPTY_DRI"
EMPTY_DEV="$ROOT/dev.$RANDOM"; mkdir -p "$EMPTY_DEV"
out="$(run_cli_doctor_host "$EMPTY_DRI" "$EMPTY_DEV")"; rc=$?
if has "$out" 'render node\|/dev/dri\|render' && [ "$rc" -ne 2 ]; then
  pass "doctor_warns_overlay_without_dri: render-node WARN emitted, exit unchanged"
else
  check_fail "doctor_warns_overlay_without_dri: rc=$rc out=<<<$out>>>"
fi

# WSL2 + NVIDIA is a supported install path and exposes /dev/dxg instead of a DRI
# render node. Its CUDA overlay must not be told the accelerator is unavailable.
new_env; register_repo
printf 'JARVIS_VERSION=1.1.3\nJARVIS_IMAGE_TAG=1.1.3\nTORCH_VARIANT=cpu\nCOMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml\n' > "$REPO/.env"
WSL_DEV="$ROOT/dev.$RANDOM"; mkdir -p "$WSL_DEV"; : > "$WSL_DEV/dxg"
out="$(run_cli_doctor_host "$EMPTY_DRI" "$WSL_DEV")"; rc=$?
if [ "$rc" -ne 2 ] && ! has "$out" 'render node' \
   && ! has "$out" 'accelerator will be unavailable'; then
  pass "doctor_cuda_overlay_without_render_node_is_not_warned"
else
  check_fail "doctor_cuda_overlay_without_render_node: rc=$rc out=<<<$out>>>"
fi

# When the CUDA overlay really has no NVIDIA route, the warning names the paths
# that were probed rather than a render node that was never looked for.
new_env; register_repo
printf 'JARVIS_VERSION=1.1.3\nJARVIS_IMAGE_TAG=1.1.3\nTORCH_VARIANT=cpu\nCOMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml\n' > "$REPO/.env"
out="$(run_cli_doctor_host "$EMPTY_DRI" "$EMPTY_DEV")"; rc=$?
if [ "$rc" -ne 2 ] && hasF "$out" "${EMPTY_DEV}/dxg" \
   && has "$out" 'NVIDIA Container Toolkit' && ! has "$out" 'render node'; then
  pass "doctor_cuda_overlay_warning_names_the_nvidia_paths_it_probed"
else
  check_fail "doctor_cuda_overlay_warning_text: rc=$rc out=<<<$out>>>"
fi

# doctor answers the question both update refusals send the user here to ask.
new_env; register_repo
out="$(run_cli doctor)"; clean_rc=$?
if has "$out" 'update readiness' && has "$out" 'ready to update'; then
  pass "doctor_reports_update_readiness: a clean checkout is reported ready"
else
  check_fail "doctor_reports_update_readiness: rc=$clean_rc out=<<<$out>>>"
fi

new_env; register_repo; STUB_DIRTY=" M setup.sh"
out="$(run_cli doctor)"; rc=$?
unset STUB_DIRTY
# The indentation proves the refusal reached the user through doctor's readiness
# section rather than merely leaking from the guard's own stderr.
if [ "$rc" -ne 0 ] && [ "$clean_rc" -eq 0 ] \
   && hasF "$out" '  [ERROR] Your working tree has uncommitted changes' \
   && has "$out" 'M setup.sh'; then
  pass "doctor_reports_unupdatable_checkout: refusal reported and exit turns non-zero"
else
  check_fail "doctor_reports_unupdatable_checkout: rc=$rc clean_rc=$clean_rc out=<<<$out>>>"
fi

# The signed-manifest marker is exempt from the readiness query, so a checkout
# holding nothing else still reports ready and the marker is never named.
new_env; register_repo; STUB_DIRTY="?? secrets/manifest-hmac-required"
out="$(run_cli doctor)"; rc=$?
unset STUB_DIRTY
if [ "$rc" -eq 0 ] && has "$out" 'ready to update' \
   && ! has "$out" 'uncommitted changes' \
   && ! has "$out" 'manifest-hmac-required'; then
  pass "doctor_reports_marker_only_checkout_ready: the marker is never reported"
else
  check_fail "doctor_reports_marker_only_checkout_ready: rc=$rc out=<<<$out>>>"
fi

# A repairable doctor warning names the product command that repairs it.
new_env; register_repo; STUB_COMPOSE_PS_FAIL=1
out="$(run_cli doctor)"; rc=$?
unset STUB_COMPOSE_PS_FAIL
if has "$out" 'Could not query container status' \
   && hasF "$out" 'jarvis-research repair'; then
  pass "doctor_container_warning_names_repair: the warning names its recovery command"
else
  check_fail "doctor_container_warning_names_repair: rc=$rc out=<<<$out>>>"
fi

# A recorded version that disagrees with the checkout, with no update journal to
# explain it, used to make doctor and update report different installed versions
# and offer no way out. Both must now print the same reconciliation message.
new_env; register_repo
RECONCILE_MSG='This install records version 1.1.2 in .env, but the checkout is version 1.1.3'
doctor_out="$(run_cli doctor)"; rc=$?
printf '%s\n' "$TARGET_SHA" > "$STUB_HEAD_FILE"
update_out="$(run_cli update --yes)"; urc=$?
if [ "$rc" -eq 0 ] && [ "$urc" -eq 0 ] \
   && hasF "$doctor_out" "$RECONCILE_MSG" && hasF "$update_out" "$RECONCILE_MSG" \
   && hasF "$doctor_out" "cd $REPO && ./update.sh --yes" \
   && hasF "$update_out" "cd $REPO && ./update.sh --yes" \
   && has "$update_out" 'Already up to date'; then
  pass "version_mismatch_gets_one_reconciliation_message_from_doctor_and_update"
else
  check_fail "version reconciliation: rc=$rc urc=$urc doctor=<<<$doctor_out>>> update=<<<$update_out>>>"
fi

# The message is a mismatch report, not decoration: agreement silences it, and a
# pending journal means the update path owns the difference.
new_env; register_repo
printf 'JARVIS_VERSION=1.1.3\nJARVIS_IMAGE_TAG=1.1.3\nTORCH_VARIANT=cpu\n' > "$REPO/.env"
matched_out="$(run_cli doctor)"; rc=$?
new_env; register_repo
: > "$PENDING_FILE"
journal_out="$(run_cli doctor)"; jrc=$?
rm -f "$PENDING_FILE"
if [ "$rc" -eq 0 ] && [ "$jrc" -eq 0 ] \
   && ! has "$matched_out" 'no update is in progress' \
   && ! has "$journal_out" 'no update is in progress'; then
  pass "version_reconciliation_is_silent_when_matched_or_journalled"
else
  check_fail "version reconciliation silence: rc=$rc jrc=$jrc matched=<<<$matched_out>>> journal=<<<$journal_out>>>"
fi

# A transaction-journal write that fails after the update has already started
# must stop cleanly and name the record it could not write, rather than letting
# the failure surface later as an unrelated error.
new_staged_update_env
STUB_FREEZE_STATE_DIR="$CFG"
respond_to_backup good
out="$(run_cli update --yes)"; rc=$?
chmod 700 "$CFG"
unset STUB_FREEZE_STATE_DIR
if [ "$rc" -eq 1 ] \
   && has "$out" 'Could not record the update' \
   && hasF "$out" "$PENDING_FILE" \
   && ! has "$(cat "$STUB_LOG")" 'compose pull'; then
  pass "update_dies_cleanly_when_the_journal_cannot_be_written"
else
  check_fail "mid-flow journal write failure: rc=$rc log=$(cat "$STUB_LOG") out=<<<$out>>>"
fi

# Recovery honesty: a migration-bearing update that fails health identifies the
# exact recorded application pin and repository, preserves its configured
# profiles and services, puts data restoration before image recovery, and never
# presents that bounded action as a full release rollback.
new_env; register_repo; STUB_HEALTH="unhealthy"; STUB_FAST_SLEEP=1
STUB_MIGRATIONS="db/migrations/0200_drop_thing.sql"
STUB_MIG_CONTENT="DELETE FROM telegram_user_pairings WHERE chat_id < 0;"
printf 'COMPOSE_PROFILES=tunnel\nTELEGRAM_BOT_TOKEN=configured\n' >> "$REPO/.env"
respond_to_backup good
out="$(run_cli update --yes)"; rc=$?
unset STUB_HEALTH
data_line="$(printf '%s\n' "$out" | grep -nF 'Admin > Backups' | tail -1 | cut -d: -f1)"
image_line="$(printf '%s\n' "$out" | grep -nF 'Application-image recovery (not a full release rollback)' | tail -1 | cut -d: -f1)"
recovery_count="$(printf '%s\n' "$out" | grep -cF 'Application-image recovery (not a full release rollback)' || true)"
if [ "$rc" -ne 0 ] \
   && grep -q '"from_version":"1.1.2"' "$PENDING_FILE" \
   && has "$out" 'Repository:' && has "$out" "$REPO" \
   && has "$out" 'JARVIS_IMAGE_TAG=1.1.2 docker compose --profile tunnel --profile telegram pull' \
   && has "$out" 'platform_api paper_ingestion learning_engine dashboard restore-uploader telegram_bot' \
   && has "$out" 'do not move the Git checkout or restore stored data' \
   && has "$out" 'A data-changing migration may have run' \
   && [ -n "$data_line" ] && [ -n "$image_line" ] && [ "$data_line" -lt "$image_line" ] \
   && [ "$recovery_count" -eq 1 ] \
   && has "$out" "cd $REPO && jarvis-research update --resume v1.1.3" \
   && ! has "$out" '<previous-version>' \
   && ! has "$out" 'scripts/restore.sh' \
   && ! has "$out" 'migration already ran'; then
  pass "failed_transaction_recovery_is_exact_scoped_ordered_and_resumable"
else
  check_fail "failed transaction recovery contract: rc=$rc data=$data_line image=$image_line count=$recovery_count out=<<<$out>>>"
fi

# =============================================================================
if [ "$fail" -ne 0 ]; then
  printf '\njarvis-research CLI: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\njarvis-research CLI: all %s checks passed\n' "$pass_n"
