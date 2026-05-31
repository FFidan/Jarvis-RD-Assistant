#!/usr/bin/env bash
# Greps the repo for personal-data patterns; fails on hits not in the allowlist.
# Used by pre-commit + W15 R-PII sweep.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
ALLOWLIST="$ROOT/scripts/pii-allowlist.txt"

if [[ ! -f "$ALLOWLIST" ]]; then
    echo "ERROR: $ALLOWLIST missing" >&2
    exit 2
fi

# Patterns to search for (separately to keep output readable):
declare -a patterns=(
    "/home/ferhat"
    "@(googlemail|gmail|tuhh)\.de"
    "FF-PC"
    "/mnt/backup"
    "claude-ollama"
)

# Files to skip (gitignored dirs are not searched by git grep anyway; these
# explicit excludes cover tracked-but-internal audit/handoff content):
declare -a exclude_paths=(
    ":!.git"
    ":!docs/audit"
    ":!docs/archive"
    ":!handoff"
)

declare -A allowed
# Read allowlist: lines of "file<TAB>pattern" (or just "file" for blanket).
while IFS=$'\t' read -r file pattern; do
    [[ -z "$file" || "$file" == \#* ]] && continue
    allowed["$file|${pattern:-.*}"]=1
done < "$ALLOWLIST"

violation_count=0
for pat in "${patterns[@]}"; do
    # IFS=: with 3-var read: bash assigns the remainder (including colons)
    # to the last var, so content fields containing colons (URLs, emails)
    # parse correctly. File paths with colons would be a problem, but they
    # don't occur in this repo.
    while IFS=: read -r file line content; do
        [[ -z "$file" ]] && continue
        rel="${file#$ROOT/}"
        # Check allowlist: any "$rel|<pattern>" where pattern matches content
        matched=0
        for key in "${!allowed[@]}"; do
            allowed_file="${key%%|*}"
            allowed_pat="${key##*|}"
            if [[ "$rel" == "$allowed_file" ]]; then
                if [[ "$allowed_pat" == ".*" ]] || echo "$content" | grep -qE "$allowed_pat"; then
                    matched=1
                    break
                fi
            fi
        done
        if [[ $matched -eq 0 ]]; then
            echo "PII violation: $rel:$line: $content"
            violation_count=$((violation_count + 1))
        fi
    done < <(git -C "$ROOT" grep -nE "$pat" -- "${exclude_paths[@]}" 2>/dev/null || true)
done

if [[ $violation_count -gt 0 ]]; then
    echo ""
    echo "FAILED: $violation_count un-allowlisted PII hit(s) found." >&2
    echo "If a hit is legitimate, add it to $ALLOWLIST (format: file<TAB>regex_pattern)." >&2
    exit 1
fi

echo "PII allowlist check: PASS (0 un-allowlisted hits)"
exit 0
