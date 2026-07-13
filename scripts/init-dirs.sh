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
if chown -R 1000:1000 "$SCRIPT_DIR/shared/pdf_storage" \
                      "$SCRIPT_DIR/shared/snapshots" \
                      "$SCRIPT_DIR/shared/local_pdfs" 2>/dev/null; then
  # Owned by the container UID (1000): restrict to owner + group.
  chmod -R 750 "$SCRIPT_DIR/shared/pdf_storage" \
               "$SCRIPT_DIR/shared/snapshots" \
               "$SCRIPT_DIR/shared/local_pdfs" 2>/dev/null || true
else
  echo "Note: could not set shared/ ownership to UID 1000 (non-root host); continuing." >&2
  # Not owned by UID 1000 -> keep world read/traverse (755) so the container, which
  # runs as 1000 and is in the 'other' class here, can still read the mounts incl.
  # the read-only local_pdfs ingestion dir. 750 would make them inaccessible; 755
  # matches setup.sh's path, which creates these dirs without tightening them.
  chmod -R 755 "$SCRIPT_DIR/shared/pdf_storage" \
               "$SCRIPT_DIR/shared/snapshots" \
               "$SCRIPT_DIR/shared/local_pdfs" 2>/dev/null || true
fi
echo "Shared directories ready."
