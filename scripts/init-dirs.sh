#!/usr/bin/env bash
# Create required shared directories for Docker volume mounts.
# Directories are made writable by UID 1000 (appuser inside containers) so
# that non-root container processes can write PDFs and snapshots.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$SCRIPT_DIR/shared/pdf_storage" \
         "$SCRIPT_DIR/shared/snapshots" \
         "$SCRIPT_DIR/shared/local_pdfs"
# Ensure the appuser (UID 1000) inside containers can write to these dirs.
# chmod o+rwx is the safest host-agnostic approach; alternatively use
# `chown 1000:1000` if running as root (e.g. in CI).
chmod -R o+rwx "$SCRIPT_DIR/shared/pdf_storage" \
               "$SCRIPT_DIR/shared/snapshots"
echo "Shared directories created and permissions set."
