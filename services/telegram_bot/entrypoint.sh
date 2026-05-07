#!/bin/sh
set -eu
# Don't export DATABASE_URL with embedded password — passwords leak via /proc/<pid>/environ.
# Service code reads /run/secrets/postgres_password lazily at pool construction.
export POSTGRES_USER="${POSTGRES_USER:-jarvis}"
export POSTGRES_DB="${POSTGRES_DB:-jarvis}"
exec "$@"
