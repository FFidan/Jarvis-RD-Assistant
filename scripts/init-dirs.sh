#!/usr/bin/env bash
# Create required shared directories for Docker volume mounts
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$SCRIPT_DIR/shared/pdf_storage" \
         "$SCRIPT_DIR/shared/snapshots" \
         "$SCRIPT_DIR/shared/local_pdfs"
echo "Shared directories created."
