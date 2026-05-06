#!/usr/bin/env bash
# Lint: forbid explicit BEGIN/COMMIT/ROLLBACK in NEW migrations (044+).
#
# The migration runner wraps each migration in a transaction. Older
# migrations (pre-044) ship explicit BEGIN/COMMIT for historical reasons;
# the runner safely strips outer transaction control via
# `_strip_outer_transaction_control()`. From migration 044 onward the
# convention is to omit BEGIN/COMMIT/ROLLBACK, and this script enforces it.
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
  if grep -qnE '^\s*(BEGIN|COMMIT|ROLLBACK)\s*;' "$f"; then
    VIOLATIONS+=("$f")
  fi
done

if [ "${#VIOLATIONS[@]}" -gt 0 ]; then
  echo "Migrations 044+ must not contain BEGIN/COMMIT/ROLLBACK (the runner wraps in a transaction):"
  for f in "${VIOLATIONS[@]}"; do
    grep -nE '^\s*(BEGIN|COMMIT|ROLLBACK)\s*;' "$f" | sed "s|^|$f:|"
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
# Check 3: init.sql must never blanket-mark migrations with generate_series.
# Fresh installs run init.sql before the service migration runner. If init.sql
# seeds versions it does not actually embody, the runner skips real migrations
# and leaves silent schema gaps.
# ---------------------------------------------------------------------------
INIT_SQL_NO_COMMENTS=$(grep -vE '^[[:space:]]*--' db/init.sql)

if printf '%s\n' "$INIT_SQL_NO_COMMENTS" | grep -q "generate_series"; then
  echo "db/init.sql must not use generate_series to seed schema_migrations." >&2
  echo "  Seed only explicitly embodied migration versions." >&2
  exit 1
fi

BOOTSTRAP_SQL=$(printf '%s\n' "$INIT_SQL_NO_COMMENTS" | sed -n '/CREATE TABLE IF NOT EXISTS schema_migrations/,$p')
RUNTIME_REPLAY_VERSIONS=(33 49 50 51 52 53 54 55 56 57 58)

for version in "${RUNTIME_REPLAY_VERSIONS[@]}"; do
  if printf '%s\n' "$BOOTSTRAP_SQL" | grep -qE "\\($version\\)"; then
    echo "db/init.sql must not pre-seed migration $version; it is replayed by the runtime runner." >&2
    exit 1
  fi
done

exit 0
