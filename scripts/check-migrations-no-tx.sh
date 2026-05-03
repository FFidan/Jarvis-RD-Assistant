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

# ---------------------------------------------------------------------------
# Check 2: Wrong exception class for ADD CONSTRAINT guard.
# ADD CONSTRAINT (and other non-table DDL) raises duplicate_object, NOT
# duplicate_table. A handler that catches duplicate_table silently drops
# nothing, leaving ADD CONSTRAINT errors unhandled. Flag any migration that
# has both ADD CONSTRAINT and WHEN duplicate_table in the same DO block.
# ---------------------------------------------------------------------------
DUP_VIOLATIONS=()
for f in db/migrations/*.sql; do
  # Quick two-grep pre-filter: file must contain both patterns to be a candidate.
  if grep -q "ADD CONSTRAINT" "$f" && grep -qiE "WHEN duplicate_table" "$f"; then
    DUP_VIOLATIONS+=("$f")
  fi
done

if [ "${#DUP_VIOLATIONS[@]}" -gt 0 ]; then
  echo "Migrations with ADD CONSTRAINT guarded by 'WHEN duplicate_table' (should be 'WHEN duplicate_object'):"
  for f in "${DUP_VIOLATIONS[@]}"; do
    grep -n "duplicate_table" "$f" | sed "s|^|$f:|"
  done
  exit 1
fi

# ---------------------------------------------------------------------------
# Check 3: init.sql generate_series upper bound must match the highest
# migration number on disk.  Fresh installs call init.sql; if the bound is
# lower than the latest migration, those migrations will be re-applied on
# every startup by the migration runner (the schema_migrations rows are
# missing, so the runner treats them as unapplied).
# ---------------------------------------------------------------------------
LATEST_MIG=$(ls db/migrations/*.sql 2>/dev/null \
  | grep -oE '/[0-9]+_' | grep -oE '[0-9]+' | sort -n | tail -1)
INIT_BOUND=$(grep -oE 'generate_series\(1, ([0-9]+)\)' db/init.sql \
  | grep -oE '[0-9]+' | tail -1)

if [ -z "$LATEST_MIG" ] || [ -z "$INIT_BOUND" ]; then
  echo "check-migrations-no-tx: could not determine LATEST_MIG ($LATEST_MIG) or INIT_BOUND ($INIT_BOUND)" >&2
  exit 1
fi

if [ "$INIT_BOUND" -lt "$LATEST_MIG" ]; then
  echo "init.sql generate_series upper bound ($INIT_BOUND) is less than latest migration ($LATEST_MIG)."
  echo "  Bump 'generate_series(1, $INIT_BOUND)' → 'generate_series(1, $LATEST_MIG)' in db/init.sql"
  exit 1
fi

exit 0
