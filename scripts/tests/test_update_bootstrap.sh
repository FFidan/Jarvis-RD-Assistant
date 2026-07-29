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
