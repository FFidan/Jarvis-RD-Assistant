#!/usr/bin/env bash
# Behavioral contracts for scripts/update-bootstrap.sh.
#
# The fixture uses a local Git origin and dummy lifecycle files. It exercises
# target resolution and handoff without network access or Docker.
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
BOOTSTRAP="${REPO_ROOT}/scripts/update-bootstrap.sh"

fail=0
pass_n=0
pass() { pass_n=$((pass_n + 1)); printf 'PASS: %s\n' "$1"; }
check_fail() { printf 'FAIL: %s\n' "$1" >&2; fail=1; }

ROOT="$(mktemp -d)"
trap 'rm -rf -- "$ROOT"' EXIT
AUTHOR_NAME="JARVIS Test"
AUTHOR_EMAIL="jarvis-test@example.invalid"
SOURCE="$ROOT/source"
ORIGIN="$ROOT/origin.git"
INSTALL="$ROOT/install"
LOG="$ROOT/handoff.log"
RUNTIME_LOG="$ROOT/runtime.log"
mkdir -p "$SOURCE"

git -C "$SOURCE" init -q -b main
git -C "$SOURCE" config user.name "$AUTHOR_NAME"
git -C "$SOURCE" config user.email "$AUTHOR_EMAIL"
printf 'services: {}\n' > "$SOURCE/docker-compose.yml"
printf 'POSTGRES_IMAGE=postgres:16.8\n' > "$SOURCE/versions.env"
git -C "$SOURCE" add docker-compose.yml versions.env
git -C "$SOURCE" commit -qm "source"
SOURCE_SHA="$(git -C "$SOURCE" rev-parse HEAD)"

mkdir -p "$SOURCE/scripts"
cat > "$SOURCE/scripts/jarvis-research.sh" <<'CLI'
#!/usr/bin/env bash
set -euo pipefail
runtime="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
for required in setup_lib.sh backup-lifecycle.sh backup.sh; do
  [ -f "$runtime/$required" ] || exit 40
done
printf '%s\n' "$runtime" > "$BOOTSTRAP_TEST_RUNTIME_LOG"
printf '%s\n' "$*" > "$BOOTSTRAP_TEST_LOG"
exit "${BOOTSTRAP_TEST_RC:-0}"
CLI
printf '# target setup library\n' > "$SOURCE/scripts/setup_lib.sh"
printf '# target lifecycle helper\n' > "$SOURCE/scripts/backup-lifecycle.sh"
printf '# target backup producer\n' > "$SOURCE/scripts/backup.sh"
chmod +x "$SOURCE/scripts/"*.sh
git -C "$SOURCE" add scripts
git -C "$SOURCE" commit -qm "target runtime"
TARGET_SHA="$(git -C "$SOURCE" rev-parse HEAD)"
git -C "$SOURCE" tag -a v2.0.0 -m "v2.0.0"

git init -q --bare "$ORIGIN"
git -C "$ORIGIN" symbolic-ref HEAD refs/heads/main
git -C "$SOURCE" remote add origin "$ORIGIN"
git -C "$SOURCE" push -q origin main v2.0.0
git clone -q "$ORIGIN" "$INSTALL"
git -C "$INSTALL" checkout -q -B main "$SOURCE_SHA"

run_bootstrap() {
  env JARVIS_RESEARCH_REMOTE="$ORIGIN" \
    BOOTSTRAP_TEST_LOG="$LOG" \
    BOOTSTRAP_TEST_RUNTIME_LOG="$RUNTIME_LOG" \
    BOOTSTRAP_TEST_RC="${BOOTSTRAP_TEST_RC:-0}" \
    TMPDIR="$ROOT" \
    bash "$BOOTSTRAP" --repo "$INSTALL" "$@"
}

rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 --yes 2>&1)"; rc=$?
runtime="$(cat "$RUNTIME_LOG" 2>/dev/null || true)"
if [ "$rc" -eq 0 ] \
   && [ "$(cat "$LOG" 2>/dev/null)" = "--repo $INSTALL update --to v2.0.0 --yes" ] \
   && [ "$(git -C "$INSTALL" rev-parse HEAD)" = "$SOURCE_SHA" ] \
   && [ -n "$runtime" ] && [ ! -e "$runtime" ]; then
  pass "stable target extracts one private runtime, hands off, and cleans it"
else
  check_fail "stable target handoff: rc=$rc runtime=$runtime out=<<<$out>>>"
fi

rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to "$TARGET_SHA" --yes 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] \
   && [ "$(cat "$LOG" 2>/dev/null)" = "--repo $INSTALL update --to $TARGET_SHA --yes" ]; then
  pass "commit-addressed target and noninteractive confirmation are preserved"
else
  check_fail "commit target handoff: rc=$rc out=<<<$out>>>"
fi

rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] \
   && [ "$(cat "$LOG" 2>/dev/null)" = "--repo $INSTALL update --to v2.0.0" ]; then
  pass "interactive confirmation is not bypassed unless requested"
else
  check_fail "interactive handoff: rc=$rc out=<<<$out>>>"
fi

rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0 2>&1)"; rc=$?
if [ "$rc" -eq 2 ] && printf '%s' "$out" | grep -q 'stable vX.Y.Z tag or a lowercase 40-hex commit' \
   && [ ! -e "$LOG" ]; then
  pass "malformed target is a usage error before handoff"
else
  check_fail "malformed target refusal: rc=$rc out=<<<$out>>>"
fi

printf 'dirty\n' > "$INSTALL/untracked"
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 2>&1)"; rc=$?
rm -f "$INSTALL/untracked"
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'uncommitted changes' \
   && [ ! -e "$LOG" ]; then
  pass "dirty checkout is refused before target loading"
else
  check_fail "dirty checkout refusal: rc=$rc out=<<<$out>>>"
fi

# The signed-manifest marker is machine-local product state that the backup
# service rewrites; it must not block an update, and it must not launder
# anything else. Every case below restores the fixture.
MARKER_REL="secrets/manifest-hmac-required"
mkdir -p "$INSTALL/secrets"

# (a) The marker alone must reach target handoff.
: > "$INSTALL/$MARKER_REL"
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 --yes 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] && [ -e "$LOG" ]; then
  pass "a marker-only checkout reaches target handoff"
else
  check_fail "marker-only handoff: rc=$rc out=<<<$out>>>"
fi

# (b) The marker must not launder a second untracked file, and the path must be named.
printf 'dirty\n' > "$INSTALL/ROOT-DIRT.bin"
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 --yes 2>&1)"; rc=$?
rm -f "$INSTALL/ROOT-DIRT.bin"
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'uncommitted changes' \
   && printf '%s' "$out" | grep -q 'ROOT-DIRT.bin' && [ ! -e "$LOG" ]; then
  pass "an unrelated untracked file is still refused, and is named"
else
  check_fail "unrelated untracked refusal: rc=$rc out=<<<$out>>>"
fi

# (c) A staged change is still refused.
printf 'staged\n' > "$INSTALL/staged.bin"
git -C "$INSTALL" add staged.bin
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 --yes 2>&1)"; rc=$?
git -C "$INSTALL" rm -q --cached staged.bin; rm -f "$INSTALL/staged.bin"
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'uncommitted changes' && [ ! -e "$LOG" ]; then
  pass "a staged change is still refused"
else
  check_fail "staged refusal: rc=$rc out=<<<$out>>>"
fi

# (d) A DIRECTORY at the marker path must not launder its contents.
rm -f "$INSTALL/$MARKER_REL"
mkdir -p "$INSTALL/$MARKER_REL/deep"
printf 'payload\n' > "$INSTALL/$MARKER_REL/deep/payload.sh"
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 --yes 2>&1)"; rc=$?
rm -rf "${INSTALL:?}/$MARKER_REL"
if [ "$rc" -eq 1 ] && [ ! -e "$LOG" ]; then
  pass "a directory at the marker path is refused, not exempted"
else
  check_fail "marker-directory refusal: rc=$rc out=<<<$out>>>"
fi

# (e) A SYMLINK at the marker path must not be exempted.
ln -s /etc/hostname "$INSTALL/$MARKER_REL"
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 --yes 2>&1)"; rc=$?
rm -f "$INSTALL/$MARKER_REL"
if [ "$rc" -eq 1 ] && [ ! -e "$LOG" ]; then
  pass "a symlink at the marker path is refused, not exempted"
else
  check_fail "marker-symlink refusal: rc=$rc out=<<<$out>>>"
fi

# (f) A TRACKED marker is refused. Runs in a disposable clone: committing inside
#     $INSTALL would advance HEAD past the target and disable the ancestry-dependent
#     contracts that follow this block.
TRACKED_CLONE="$ROOT/install-tracked-marker"
rm -rf "$TRACKED_CLONE"
git clone -q "$ORIGIN" "$TRACKED_CLONE"
git -C "$TRACKED_CLONE" checkout -q -B main "$SOURCE_SHA"
mkdir -p "$TRACKED_CLONE/secrets"
: > "$TRACKED_CLONE/$MARKER_REL"
git -C "$TRACKED_CLONE" add -f "$MARKER_REL"
git -C "$TRACKED_CLONE" -c user.email="$AUTHOR_EMAIL" -c user.name="$AUTHOR_NAME" commit -qm "track marker"
rm -f "$LOG" "$RUNTIME_LOG"
# Mirror run_bootstrap's environment exactly but point --repo at the disposable
# clone. Omitting JARVIS_RESEARCH_REMOTE would make the origin check the thing
# under test instead of the tracked-marker fence.
out="$(
  env JARVIS_RESEARCH_REMOTE="$ORIGIN" \
    BOOTSTRAP_TEST_LOG="$LOG" \
    BOOTSTRAP_TEST_RUNTIME_LOG="$RUNTIME_LOG" \
    BOOTSTRAP_TEST_RC=0 \
    TMPDIR="$ROOT" \
    bash "$BOOTSTRAP" --repo "$TRACKED_CLONE" --to v2.0.0 --yes 2>&1
)"; rc=$?
rm -rf "$TRACKED_CLONE"
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'tracked' && [ ! -e "$LOG" ]; then
  pass "a tracked marker is refused rather than exempted"
else
  check_fail "tracked marker refusal: rc=$rc out=<<<$out>>>"
fi

# (g) Git inspection failure fails closed. Only the status query is broken here:
#     an unusable GIT_DIR dies earlier, in resolve_repository, and would prove
#     nothing about this guard.
: > "$INSTALL/$MARKER_REL"
rm -f "$LOG" "$RUNTIME_LOG"
out="$(
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=status.showUntrackedFiles GIT_CONFIG_VALUE_0=bogus \
    run_bootstrap --to v2.0.0 --yes 2>&1
)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q 'Could not inspect' && [ ! -e "$LOG" ]; then
  pass "a failing Git inspection fails closed before handoff"
else
  check_fail "git-inspection fail-closed: rc=$rc out=<<<$out>>>"
fi
rm -f "$INSTALL/$MARKER_REL"

# (h) An in-progress Git operation is refused even though the tree is clean. This is
#     the shape a plain porcelain check cannot see: the fast-forward would fail later,
#     after the update transaction is already open.
INSTALL_GIT_DIR="$(git -C "$INSTALL" rev-parse --absolute-git-dir)"
: > "${INSTALL_GIT_DIR}/MERGE_HEAD"
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 --yes 2>&1)"; rc=$?
rm -f "${INSTALL_GIT_DIR}/MERGE_HEAD"
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'already in progress' && [ ! -e "$LOG" ]; then
  pass "an in-progress Git operation is refused before target loading"
else
  check_fail "in-progress refusal: rc=$rc out=<<<$out>>>"
fi

# (i) A tracked file flagged to hide local changes is refused. Porcelain reports
#     nothing for these, so without the index-flag fence the modification is invisible.
git -C "$INSTALL" update-index --skip-worktree docker-compose.yml
printf 'hidden\n' >> "$INSTALL/docker-compose.yml"
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to v2.0.0 --yes 2>&1)"; rc=$?
git -C "$INSTALL" update-index --no-skip-worktree docker-compose.yml
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'hide local changes' \
   && printf '%s' "$out" | grep -q 'docker-compose.yml' && [ ! -e "$LOG" ]; then
  pass "a tracked file flagged to hide local changes is refused, and is named"
else
  check_fail "hide-flag refusal: rc=$rc out=<<<$out>>>"
fi

# (j) An unreadable index fails closed. Case (g) breaks only the status query; an
#     unreadable index breaks the ls-files fences instead, which run earlier and
#     would otherwise report "no hidden flags" for an index Git cannot parse.
printf 'not-an-index\n' > "$ROOT/bogus-index"
rm -f "$LOG" "$RUNTIME_LOG"
out="$(GIT_INDEX_FILE="$ROOT/bogus-index" run_bootstrap --to v2.0.0 --yes 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "index flags" && [ ! -e "$LOG" ]; then
  pass "an unreadable index fails closed before handoff"
else
  check_fail "unreadable-index fail-closed: rc=$rc out=<<<$out>>>"
fi

# Restore the shared fixture to its pinned state for every later case.
git -C "$INSTALL" reset -q --hard "$SOURCE_SHA"
git -C "$INSTALL" clean -qfd

rm -f "$LOG" "$RUNTIME_LOG"
out="$(
  JARVIS_RESEARCH_REMOTE=other/project \
    BOOTSTRAP_TEST_LOG="$LOG" BOOTSTRAP_TEST_RUNTIME_LOG="$RUNTIME_LOG" \
    bash "$BOOTSTRAP" --repo "$INSTALL" --to v2.0.0 2>&1
)"
rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'configured JARVIS repository' \
   && [ ! -e "$LOG" ]; then
  pass "unmanaged origin is refused before fetch and handoff"
else
  check_fail "origin refusal: rc=$rc out=<<<$out>>>"
fi

ln -sf setup_lib.sh "$SOURCE/scripts/backup.sh"
git -C "$SOURCE" add scripts/backup.sh
git -C "$SOURCE" commit -qm "unsafe runtime"
UNSAFE_SHA="$(git -C "$SOURCE" rev-parse HEAD)"
git -C "$SOURCE" push -q origin main
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to "$UNSAFE_SHA" 2>&1)"; rc=$?
if [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q 'unsafe lifecycle file: scripts/backup.sh' \
   && [ ! -e "$LOG" ]; then
  pass "symbolic-link lifecycle entry is never extracted"
else
  check_fail "unsafe tree entry refusal: rc=$rc out=<<<$out>>>"
fi

git -C "$SOURCE" checkout -q v2.0.0 -- scripts/backup.sh
git -C "$SOURCE" commit -qm "restore runtime"
git -C "$SOURCE" rm -q scripts/backup-lifecycle.sh
git -C "$SOURCE" commit -qm "incomplete runtime"
MISSING_SHA="$(git -C "$SOURCE" rev-parse HEAD)"
git -C "$SOURCE" push -q origin main
rm -f "$LOG" "$RUNTIME_LOG"
out="$(run_bootstrap --to "$MISSING_SHA" 2>&1)"; rc=$?
if [ "$rc" -eq 1 ] \
   && printf '%s' "$out" | grep -q 'unsafe lifecycle file: scripts/backup-lifecycle.sh' \
   && [ ! -e "$LOG" ]; then
  pass "incomplete target runtime is refused before handoff"
else
  check_fail "missing target file refusal: rc=$rc out=<<<$out>>>"
fi

rm -f "$LOG" "$RUNTIME_LOG"
BOOTSTRAP_TEST_RC=17
out="$(run_bootstrap --to v2.0.0 2>&1)"; rc=$?
unset BOOTSTRAP_TEST_RC
runtime="$(cat "$RUNTIME_LOG" 2>/dev/null || true)"
if [ "$rc" -eq 17 ] && [ -n "$runtime" ] && [ ! -e "$runtime" ]; then
  pass "target exit status is preserved and its runtime is removed"
else
  check_fail "target status propagation: rc=$rc runtime=$runtime out=<<<$out>>>"
fi

if [ "$fail" -ne 0 ]; then
  printf '\nupdate bootstrap: FAILED (%s checks passed)\n' "$pass_n" >&2
  exit 1
fi
printf '\nupdate bootstrap: all %s checks passed\n' "$pass_n"
