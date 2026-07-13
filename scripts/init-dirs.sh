#!/usr/bin/env bash
# Create required shared directories for Docker volume mounts.
# The container appuser runs as UID 1000; where the host allows it we hand the
# dirs to that UID so non-root container processes can write PDFs and snapshots.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$SCRIPT_DIR/shared/pdf_storage" \
         "$SCRIPT_DIR/shared/snapshots" \
         "$SCRIPT_DIR/shared/local_pdfs"
# Handing ownership/permissions to the container UID is best-effort: a non-root
# invoker without the privilege to chown cannot, and that must not fail the
# bootstrap. The documented setup.sh path likewise creates these dirs without
# chowning them, so continuing here matches that supported install.
if ! chown -R 1000:1000 "$SCRIPT_DIR/shared/pdf_storage" \
                        "$SCRIPT_DIR/shared/snapshots" \
                        "$SCRIPT_DIR/shared/local_pdfs" 2>/dev/null; then
  echo "Note: could not set shared/ ownership to UID 1000 (non-root host); continuing." >&2
fi
chmod -R 750 "$SCRIPT_DIR/shared/pdf_storage" \
             "$SCRIPT_DIR/shared/snapshots" \
             "$SCRIPT_DIR/shared/local_pdfs" 2>/dev/null \
  || echo "Note: could not tighten shared/ permissions; continuing." >&2
echo "Shared directories ready."
