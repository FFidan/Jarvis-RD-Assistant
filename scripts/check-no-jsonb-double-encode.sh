#!/usr/bin/env bash
# Fail if json.dumps( appears within 20 lines of ::jsonb in any .py file.
# asyncpg auto-encodes JSONB via init_pg_connection's set_type_codec; calling
# json.dumps() too is a double-encode bug (see audit H1-H4).
# Window widened from ±5 to ±20 to catch executemany-style multi-line patterns.
set -euo pipefail

matches=$(grep -rn -B 20 -A 20 '::jsonb' services/ libs/ 2>/dev/null \
  | grep -E 'json\.dumps\(' \
  | grep -v '# nolint:jsonb-double-encode' \
  || true)

if [ -n "$matches" ]; then
  echo "ERROR: json.dumps() found near ::jsonb cast — likely double-encode:"
  echo "$matches"
  echo ""
  echo "asyncpg's JSONB codec auto-encodes via init_pg_connection."
  echo "Pass native dict/list/value directly to \$N::jsonb."
  echo "If intentional (e.g. passing JSON text to a non-jsonb column),"
  echo "add: # nolint:jsonb-double-encode  on the json.dumps line."
  exit 1
fi
echo "OK: no jsonb double-encode patterns found."
