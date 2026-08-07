#!/usr/bin/env bash
# Guard: the shared library must not import a service package.
#
# jarvis_common is a dependency of paper_ingestion, learning_engine and
# telegram_bot. An import in the other direction makes the library unusable
# without the service it reaches into, and it is invisible at review time when
# the import sits inside a function body. This grep is the enforcement: with the
# violation present, `uv run tach check` still reports "All modules validated!",
# so nothing else in `make check` catches it.
#
# The pattern is anchored to the start of a statement on purpose. Unanchored, it
# also matches the words in ordinary comments and would fail on a clean tree.
set -Eeuo pipefail

cd "$(dirname "$0")/.." || { echo "fatal: cannot cd to repo root" >&2; exit 1; }

TARGET="libs/jarvis_common/jarvis_common"
# A missing target would make grep exit non-zero and silently report success.
[ -d "$TARGET" ] || { echo "fatal: $TARGET does not exist" >&2; exit 1; }

if grep -rnE "^[[:space:]]*(from|import)[[:space:]]+(paper_ingestion|learning_engine|telegram_bot)\b" "$TARGET"; then
  echo "ERROR: jarvis_common must not import a service package." >&2
  echo "  Inject the service object as a parameter instead." >&2
  exit 1
fi

echo "check-no-service-imports-in-common: OK"
