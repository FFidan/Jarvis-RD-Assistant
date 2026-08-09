#!/usr/bin/env bash
# Deprecated compatibility entry point. The primary installer owns all setup
# behavior; this path preserves only the historical local development command.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

setup_args=(--non-interactive --profile=dev)
while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-disk-check)
      setup_args+=(--skip-disk-check)
      shift
      ;;
    *)
      printf 'ERROR: unsupported compatibility option: %s\n' "$1" >&2
      printf 'Use ./setup.sh directly for current installation options.\n' >&2
      exit 2
      ;;
  esac
done

if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then
  setup_args+=(--compose-project-name "$COMPOSE_PROJECT_NAME")
fi

printf 'WARN: scripts/jarvis-setup.sh is deprecated; delegating to ./setup.sh.\n' >&2
exec "${REPO_ROOT}/setup.sh" "${setup_args[@]}"
