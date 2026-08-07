#!/usr/bin/env bash
# Verify that uv, committed service requirements, and Docker dependency caps agree.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

cd "$ROOT_DIR"

uv lock --check
OUTPUT_ROOT="$TMP_DIR" bash "$ROOT_DIR/scripts/export-service-requirements.sh"

for file in \
  services/paper_ingestion/requirements.txt \
  services/paper_ingestion/requirements-optional.txt \
  services/paper_ingestion/constraints.txt \
  services/paper_ingestion/constraints-cpu.txt \
  services/paper_ingestion/constraints-optional.txt \
  services/paper_ingestion/constraints-optional-cpu.txt \
  services/learning_engine/requirements.txt \
  services/learning_engine/constraints.txt \
  services/telegram_bot/constraints.txt \
  services/telegram_bot/requirements.txt
do
  diff -u "$ROOT_DIR/$file" "$TMP_DIR/$file"
done

uv run python - <<'PY'
from importlib.metadata import version


def major_minor_patch(raw: str) -> tuple[int, int, int]:
    head = raw.split("+", 1)[0].split("-", 1)[0]
    parts = [int(part) for part in head.split(".")[:3]]
    return tuple((parts + [0, 0, 0])[:3])


fastapi_version = version("fastapi")
if major_minor_patch(fastapi_version) >= (0, 141, 0):
    raise SystemExit(
        f"host FastAPI {fastapi_version} is outside the Docker runtime cap <0.141.0"
    )

print(f"OK host FastAPI {fastapi_version} matches Docker runtime cap <0.141.0")
PY

# The checks above prove the exported files faithfully reproduce the dependency
# groups; they cannot tell that a group is missing something the shared library
# imports. That gap shipped an unstartable image, so it gets its own check.
uv run python scripts/check-shared-import-coverage.py "$ROOT_DIR"
