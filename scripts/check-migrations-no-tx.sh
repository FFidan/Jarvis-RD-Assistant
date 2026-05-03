#!/usr/bin/env bash
# Lint: forbid explicit BEGIN/COMMIT in NEW migrations (044+).
#
# The migration runner wraps each migration in a transaction. Older
# migrations (pre-044) ship explicit BEGIN/COMMIT for historical reasons;
# the runner safely strips outer transaction control via
# `_strip_outer_transaction_control()`. From migration 044 onward the
# convention is to omit BEGIN/COMMIT, and this script enforces it.
set -euo pipefail

# Anchor to repo root so `db/migrations/*.sql` works regardless of CWD.
cd "$(dirname "$0")/.." || { echo "fatal: cannot cd to repo root" >&2; exit 1; }

VIOLATIONS=()
for f in db/migrations/*.sql; do
  base=$(basename "$f")
  num="${base%%_*}"
  case "$num" in (''|*[!0-9]*) continue ;; esac  # skip non-numeric-prefixed files
  if [ "$num" -lt 44 ]; then
    continue
  fi
  if grep -qnE '^\s*(BEGIN|COMMIT)\s*;' "$f"; then
    VIOLATIONS+=("$f")
  fi
done

if [ "${#VIOLATIONS[@]}" -gt 0 ]; then
  echo "Migrations 044+ must not contain BEGIN/COMMIT (the runner wraps in a transaction):"
  for f in "${VIOLATIONS[@]}"; do
    grep -nE '^\s*(BEGIN|COMMIT)\s*;' "$f" | sed "s|^|$f:|"
  done
  exit 1
fi
exit 0
