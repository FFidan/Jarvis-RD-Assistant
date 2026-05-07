#!/bin/sh
set -eu
PASSWORD=$(cat /run/secrets/postgres_password)
export DATABASE_URL="postgresql://${POSTGRES_USER:-jarvis}:${PASSWORD}@postgres:5432/${POSTGRES_DB:-jarvis}"
exec "$@"
