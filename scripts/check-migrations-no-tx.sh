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
  [ -f "$f" ] || continue
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
  [ -f "$f" ] || continue
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

# WAVE 1 squash (2026-05-19): the RUNTIME_REPLAY_VERSIONS absence loop was
# replaced by a positive contiguity check. Post-squash init.sql pre-marks ALL
# 88 versions (1..88 contiguous, no gaps) so the runtime runner is a no-op on
# fresh install. The generate_series ban above is still load-bearing — the
# explicit contiguous list is the audit trail that init.sql truly embodies each
# version.
BOOTSTRAP_SQL=$(printf '%s\n' "$INIT_SQL_NO_COMMENTS" | sed -n '/CREATE TABLE IF NOT EXISTS schema_migrations/,$p')

# Extract the seeded version numbers from the INSERT VALUES block.
SEEDED_VERSIONS=$(printf '%s\n' "$BOOTSTRAP_SQL" \
  | grep -oE '\([0-9]+\)' \
  | tr -d '()' \
  | sort -n)

SEEDED_COUNT=$(printf '%s\n' "$SEEDED_VERSIONS" | grep -c '[0-9]')
SEEDED_MIN=$(printf '%s\n' "$SEEDED_VERSIONS" | head -1)
SEEDED_MAX=$(printf '%s\n' "$SEEDED_VERSIONS" | tail -1)

# Derive expected count from db/init.sql rather than hardcoding 88.
# Extracts the highest version number from the INSERT INTO schema_migrations
# VALUES block — robust to future squash additions.
EXPECTED=$(awk '/INSERT INTO schema_migrations/,/ON CONFLICT \(version\)/' db/init.sql \
  | grep -oE '\([0-9]+\)' | tr -d '()' | sort -n | tail -1)

if [ "$SEEDED_COUNT" -ne "$EXPECTED" ] || [ "$SEEDED_MIN" -ne 1 ] || [ "$SEEDED_MAX" -ne "$EXPECTED" ]; then
  echo "db/init.sql schema_migrations bootstrap must seed exactly versions 1..${EXPECTED} contiguous." >&2
  echo "  Found: count=$SEEDED_COUNT, min=$SEEDED_MIN, max=$SEEDED_MAX (expected ${EXPECTED}, 1, ${EXPECTED})." >&2
  exit 1
fi

# Verify no gaps: seeded list must equal seq 1 $EXPECTED.
EXPECTED_SEQ=$(seq 1 "$EXPECTED" | tr '\n' ' ')
ACTUAL=$(printf '%s\n' "$SEEDED_VERSIONS" | tr '\n' ' ')
if [ "$ACTUAL" != "$EXPECTED_SEQ" ]; then
  echo "db/init.sql schema_migrations bootstrap has gaps or duplicates." >&2
  echo "  Expected: $EXPECTED_SEQ" >&2
  echo "  Actual:   $ACTUAL" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Check 4: ADD CONSTRAINT (migrations 044+) must be inside a DO $$ ... EXCEPTION
# ... END $$ block. Bare ADD CONSTRAINT crashes on re-apply when init.sql has
# pre-seeded the constraint. Pre-044 migrations are exempt (historical reasons).
# ---------------------------------------------------------------------------
for f in db/migrations/*.sql; do
    [ -f "$f" ] || continue
    base=$(basename "$f")
    num="${base%%_*}"
    case "$num" in (''|*[!0-9]*) continue ;; esac
    if [ "$num" -lt 51 ]; then continue; fi  # 001-050 pre-date the DO $$...EXCEPTION convention
    if grep -qE 'ADD CONSTRAINT' "$f" && ! grep -q 'EXCEPTION' "$f"; then
        echo "FAIL: $f: ADD CONSTRAINT without DO \$\$...EXCEPTION guard. See migrations/051 for the canonical pattern." >&2
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# Check 5: No tagged dollar-quotes ($tag$...$tag$) in migrations.
# The migration scanner's _strip_outer_transaction_control only handles $$ literals.
# ---------------------------------------------------------------------------
for f in db/migrations/*.sql; do
    [ -f "$f" ] || continue
    if grep -qE '\$[A-Za-z_][A-Za-z0-9_]*\$' "$f"; then
        echo "FAIL: $f: tagged dollar-quote (\$tag\$) detected; only \$\$ is supported by the migration scanner." >&2
        exit 1
    fi
done

exit 0
