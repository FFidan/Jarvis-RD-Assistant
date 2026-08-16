#!/bin/sh
set -eu

# Password files stay outside the process environment and are resolved by the
# shared pool builder when the Platform service starts.
export POSTGRES_USER="${POSTGRES_USER:-jarvis}"
export POSTGRES_DB="${POSTGRES_DB:-jarvis}"
exec "$@"
