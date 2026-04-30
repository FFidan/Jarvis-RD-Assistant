#!/usr/bin/env bash
# Lint: forbid raw `pus.archived` references in paper_ingestion outside
# predicates.py. All paper_user_state.archived checks in paper_ingestion must
# go through the canonical IS_ARCHIVED_SQL / IS_NOT_ARCHIVED_SQL constants in
# services/paper_ingestion/paper_ingestion/queries/predicates.py.
#
# Note: telegram_bot deploys as a separate service and is intentionally OUT
# OF SCOPE for this guard — it duplicates the predicate (with a comment) to
# avoid a cross-service Python import.
set -euo pipefail
cd "$(dirname "$0")/.." || { echo "fatal: cannot cd to repo root" >&2; exit 1; }

# Allow-list: predicates.py itself, and tests (they may reference for assertions).
VIOLATIONS=$(grep -rn 'pus\.archived' \
    --include='*.py' \
    services/paper_ingestion/ libs/ \
  | grep -v 'predicates\.py' \
  | grep -v '/tests/' \
  | grep -v '_test\.py' \
  || true)

if [ -n "$VIOLATIONS" ]; then
    echo "Raw 'pus.archived' references must use IS_ARCHIVED_SQL from queries/predicates.py:"
    echo "$VIOLATIONS"
    exit 1
fi
exit 0
