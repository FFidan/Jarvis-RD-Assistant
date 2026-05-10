#!/usr/bin/env bash
# verify-edits.sh — post-dispatch edit verifier for /deep-execute subagent sweeps.
#
# Usage:
#   bash scripts/verify-edits.sh <file1> <snippet1> [<file2> <snippet2> ...]
#
# Each pair: (file-path, expected-content-snippet). The script confirms that
# every claimed file edit is still present on disk before the parent agent
# declares the dispatch complete. Exits 1 and prints a report on any missing
# snippet.
#
# Rationale: during multi-agent sweeps, subagents have occasionally claimed
# to edit a file but the change was never written to disk (the Edit tool call
# may have appeared to succeed inside the subagent's context while the parent
# process had already staged a different version). This script is a backstop.
#
# Exit codes:
#   0 — all snippets verified present
#   1 — one or more snippets missing (details printed to stderr)
#   2 — bad arguments (odd count)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
    echo "Usage: $0 <file1> <snippet1> [<file2> <snippet2> ...]" >&2
    echo "" >&2
    echo "  Each pair: absolute-or-repo-relative file path + fixed-string snippet" >&2
    echo "  that must appear somewhere in the file." >&2
    exit 2
}

if [[ $# -eq 0 || $(( $# % 2 )) -ne 0 ]]; then
    usage
fi

FAILED=0
declare -a FAILURES=()
TOTAL_PAIRS=$(( $# / 2 ))

ARGS=("$@")
i=0
while [[ $i -lt ${#ARGS[@]} ]]; do
    FILE="${ARGS[$i]}"
    SNIPPET="${ARGS[$((i+1))]}"
    i=$(( i + 2 ))

    # Resolve relative paths from repo root
    if [[ "$FILE" != /* ]]; then
        FILE="$REPO_ROOT/$FILE"
    fi

    if [[ ! -f "$FILE" ]]; then
        FAILURES+=("MISSING FILE: $FILE")
        FAILED=1
        continue
    fi

    if ! grep -qF "$SNIPPET" "$FILE"; then
        FAILURES+=("SNIPPET NOT FOUND in $FILE -- expected: $SNIPPET")
        FAILED=1
    fi
done

if [[ $FAILED -ne 0 ]]; then
    echo "verify-edits: FAILED -- the following claimed edits are NOT present on disk:" >&2
    for msg in "${FAILURES[@]}"; do
        echo "  - $msg" >&2
    done
    echo "" >&2
    echo "  Root cause: subagent may have reported completion without writing to disk." >&2
    echo "  Action: re-run the failed task(s) or manually verify and re-apply the edits." >&2
    exit 1
fi

echo "verify-edits: OK -- all $TOTAL_PAIRS snippet(s) verified present in working tree."
exit 0
