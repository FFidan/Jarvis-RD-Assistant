#!/usr/bin/env bash
# verify-edits.sh — post-dispatch edit verifier for /deep-execute subagent sweeps.
#
# Usage:
#   bash scripts/verify-edits.sh <file1> <snippet1> [<file2> <snippet2> ...]
#
# Each pair: (file-path, expected-content-snippet). The script confirms that
# every claimed file edit is still present on disk AND in the git staged blob
# before the parent agent declares the dispatch complete. Exits 1 and prints a
# report on any missing snippet.
#
# Rationale (Bucket E3 RCA — 2026-05-10):
#   During multi-agent sweeps, subagents have twice claimed edits that were
#   silently reverted between Edit and commit. The confirmed mechanism is:
#
#   1. ruff-format pre-commit hook (no --check): rewrites .py files on disk
#      but marks the hook FAILED, aborting the commit. The git index blob is
#      NOT updated. Working tree is now ahead of index (formatted vs unformatted).
#      pre-commit additionally stashes unstaged changes before running hooks and
#      restores them after — so multiple pending edits can be hidden from hooks.
#
#   2. The PostToolUse auto-format.sh hook (global ~/.claude/settings.json)
#      runs ruff-format on every .py file Claude edits. This largely prevents
#      the above divergence for Python files IF staging happens after the hook.
#      But the hook is silent and its effect is invisible to the agent.
#
#   3. git add -u re-picks NEWER on-disk content. If a pre-commit hook rewrites
#      a file after staging, the agent can recover with git add -u. But if another
#      agent or process modifies the file between the original Edit and git add,
#      the agent's intended change is silently lost.
#
#   4. For non-.py files (Makefile, docker-compose.yml): auto-format.sh does
#      nothing (Makefile) or calls prettier which may not be installed (.yml).
#      ruff-format ignores them. Silent reverts for these files are caused by
#      mechanism #3 (race between agents editing shared files) or by a failed
#      commit that is not detected and re-staged.
#
# This version adds three checks beyond the original working-tree grep:
#   A. Staged-blob check: if the file is tracked/staged, verify the snippet is
#      also in the git staged blob (git show :PATH). A working-tree match with
#      no staged match means the edit was written but never staged.
#   B. Committed check: if --committed flag is passed, verify the snippet is in
#      the last commit blob (git show HEAD:PATH).
#   C. Index/working-tree divergence warning: report when working tree and
#      staged blob differ (indicates a pending git add is needed).
#
# Exit codes:
#   0 — all snippets verified present
#   1 — one or more snippets missing (details printed to stderr)
#   2 — bad arguments (odd count)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Optional flag: --committed → also check HEAD blob, not just index/working-tree
CHECK_COMMITTED=0
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == "--committed" ]]; then
        CHECK_COMMITTED=1
    else
        ARGS+=("$arg")
    fi
done

usage() {
    echo "Usage: $0 [--committed] <file1> <snippet1> [<file2> <snippet2> ...]" >&2
    echo "" >&2
    echo "  --committed   Also verify snippet appears in the HEAD commit blob." >&2
    echo "  Each pair: absolute-or-repo-relative file path + fixed-string snippet" >&2
    echo "  that must appear somewhere in the file." >&2
    exit 2
}

if [[ ${#ARGS[@]} -eq 0 || $(( ${#ARGS[@]} % 2 )) -ne 0 ]]; then
    usage
fi

FAILED=0
declare -a FAILURES=()
declare -a WARNINGS=()
TOTAL_PAIRS=$(( ${#ARGS[@]} / 2 ))

i=0
while [[ $i -lt ${#ARGS[@]} ]]; do
    FILE="${ARGS[$i]}"
    SNIPPET="${ARGS[$((i+1))]}"
    i=$(( i + 2 ))

    # Resolve relative paths from repo root
    if [[ "$FILE" != /* ]]; then
        FILE="$REPO_ROOT/$FILE"
    fi

    # Compute repo-relative path for git commands
    REL_PATH="${FILE#"$REPO_ROOT/"}"

    # ------------------------------------------------------------------ #
    # A. Working-tree check (original behaviour)
    # ------------------------------------------------------------------ #
    if [[ ! -f "$FILE" ]]; then
        FAILURES+=("MISSING FILE: $FILE")
        FAILED=1
        continue
    fi

    if ! grep -qF "$SNIPPET" "$FILE"; then
        FAILURES+=("SNIPPET NOT FOUND in working tree: $FILE -- expected: $SNIPPET")
        FAILED=1
        # Still continue to check staged/committed blobs for the full picture
    fi

    # ------------------------------------------------------------------ #
    # B. Staged-blob check (new in E3)
    # ------------------------------------------------------------------ #
    STAGED_BLOB=$(git -C "$REPO_ROOT" show ":${REL_PATH}" 2>/dev/null) || STAGED_BLOB=""
    if [[ -n "$STAGED_BLOB" ]]; then
        if ! echo "$STAGED_BLOB" | grep -qF "$SNIPPET"; then
            FAILURES+=("SNIPPET NOT IN STAGED BLOB: $FILE -- expected: $SNIPPET")
            FAILURES+=("  (working-tree has it but git index does not — 'git add $REL_PATH' needed)")
            FAILED=1
        fi

        # Cross-check: warn if working tree and staged blob differ at all
        WT_CONTENT=$(cat "$FILE")
        if [[ "$WT_CONTENT" != "$STAGED_BLOB" ]]; then
            WARNINGS+=("INDEX/WORKTREE DIVERGE: $FILE (run 'git diff $REL_PATH' to inspect)")
        fi
    else
        # File is not tracked/staged — only working-tree check applies
        WARNINGS+=("NOT STAGED: $FILE -- edit is only on disk, not in git index")
    fi

    # ------------------------------------------------------------------ #
    # C. Committed-blob check (optional, --committed flag)
    # ------------------------------------------------------------------ #
    if [[ $CHECK_COMMITTED -eq 1 ]]; then
        COMMITTED_BLOB=$(git -C "$REPO_ROOT" show "HEAD:${REL_PATH}" 2>/dev/null) || COMMITTED_BLOB=""
        if [[ -n "$COMMITTED_BLOB" ]]; then
            if ! echo "$COMMITTED_BLOB" | grep -qF "$SNIPPET"; then
                FAILURES+=("SNIPPET NOT IN COMMITTED BLOB (HEAD): $FILE -- expected: $SNIPPET")
                FAILURES+=("  (file staged/on-disk but not yet committed)")
                FAILED=1
            fi
        else
            WARNINGS+=("NOT IN HEAD COMMIT: $FILE (new file not yet committed)")
        fi
    fi
done

# ------------------------------------------------------------------ #
# Report
# ------------------------------------------------------------------ #
if [[ ${#WARNINGS[@]} -gt 0 ]]; then
    echo "verify-edits: WARNINGS:" >&2
    for msg in "${WARNINGS[@]}"; do
        echo "  ! $msg" >&2
    done
    echo "" >&2
fi

if [[ $FAILED -ne 0 ]]; then
    echo "verify-edits: FAILED -- the following claimed edits are NOT present:" >&2
    for msg in "${FAILURES[@]}"; do
        echo "  - $msg" >&2
    done
    echo "" >&2
    echo "  Root causes (E3 RCA):" >&2
    echo "    1. ruff-format pre-commit hook rewrites staged .py files but aborts commit;" >&2
    echo "       staged blob stays unformatted while working tree is formatted." >&2
    echo "       Fix: re-add the file(s) ('git add <file>') then re-commit." >&2
    echo "    2. Edit tool wrote content but auto-format.sh (PostToolUse) rewrote it;" >&2
    echo "       snippet may never have been correct on disk at staging time." >&2
    echo "    3. Race between parallel agents on a shared file; last writer wins." >&2
    echo "    4. git add -u picked up an unwanted formatter-rewrite instead of agent intent." >&2
    exit 1
fi

echo "verify-edits: OK -- all $TOTAL_PAIRS snippet(s) verified present in working tree and staged blob."
exit 0
