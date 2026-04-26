#!/bin/sh
set -eu
PASSWORD=$(cat /run/secrets/postgres_password)
export DATABASE_URL="postgresql://jarvis:${PASSWORD}@postgres:5432/jarvis"
exec "$@"
