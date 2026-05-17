#!/usr/bin/env bash
# scripts/check-no-tracked-secrets.sh — Recurrence guard: fail if any
# secrets/*.txt file is tracked by git.
#
# Allowlist: secrets/.gitkeep and secrets/README.md are intentionally tracked
# (they document the directory and serve as git-history placeholders).
# Any other file under secrets/ with a .txt extension must NOT be committed —
# those are credential files that should remain git-ignored.
#
# Usage:
#   bash scripts/check-no-tracked-secrets.sh   # exits 1 if tracked secrets found
#
# Wired into: make check (via no-tracked-secrets target), CI lint-test job.
set -euo pipefail

cd "$(dirname "$0")/.."

# git ls-files lists paths relative to the repo root that are currently tracked.
tracked="$(git ls-files 'secrets/*.txt')"

if [ -n "$tracked" ]; then
  echo "ERROR: The following secrets/*.txt files are tracked by git:" >&2
  echo "$tracked" | sed 's/^/  /' >&2
  echo "" >&2
  echo "These files contain credentials and must NEVER be committed." >&2
  echo "Fix: git rm --cached <file> (then verify with git check-ignore <file>)." >&2
  echo "See docs/SECURITY.md for the leaked-secret remediation procedure." >&2
  exit 1
fi

echo "check-no-tracked-secrets: OK (no secrets/*.txt files are git-tracked)"
