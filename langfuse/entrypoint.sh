#!/bin/sh
# langfuse/entrypoint.sh — read three Langfuse secrets from /run/secrets,
# export them into the environment, then drop to the unprivileged `nextjs`
# user and exec the upstream entrypoint. See Dockerfile.langfuse for why.
#
# Runs as root. Refuses to start if any secret file is missing or empty so a
# misconfigured deploy fails fast rather than silently running with empty
# auth/encryption material (matches the litellm shim's fail-fast contract at
# docker-compose.yml:251-254).
set -eu

SEC_DIR=/run/secrets
PG_PASS_FILE=${SEC_DIR}/langfuse_pg_password
NEXTAUTH_FILE=${SEC_DIR}/langfuse_nextauth_secret
SALT_FILE=${SEC_DIR}/langfuse_salt

for f in "${PG_PASS_FILE}" "${NEXTAUTH_FILE}" "${SALT_FILE}"; do
  if [ ! -s "${f}" ]; then
    echo "FATAL: ${f} is empty or missing — refusing to start Langfuse." >&2
    echo "       Re-create the file under ./secrets/ and re-run 'make observability-up'." >&2
    exit 1
  fi
done

# DATABASE_URL is assembled from the password file + the fixed dsn shape that
# the langfuse-postgres sibling service initialises with (POSTGRES_USER=langfuse,
# POSTGRES_DB=langfuse, hostname=langfuse-postgres). Keeping the user/db/host
# wired here avoids re-introducing a separate plaintext env var just to carry
# them.
#
# URL-encoding constraint: the password is interpolated into the DSN without
# URL-encoding. The generator at scripts/init-secrets.sh uses `openssl rand
# -hex 24` which only produces URL-safe `[0-9a-f]`. If the generator is ever
# changed to base64 / random ASCII, add URL-encoding here (e.g. via Python's
# urllib.parse.quote in a sibling shim) or the DSN will silently break.
LANGFUSE_PG_PASSWORD="$(cat "${PG_PASS_FILE}")"
export DATABASE_URL="postgresql://langfuse:${LANGFUSE_PG_PASSWORD}@langfuse-postgres:5432/langfuse"
export NEXTAUTH_SECRET="$(cat "${NEXTAUTH_FILE}")"
export SALT="$(cat "${SALT_FILE}")"
unset LANGFUSE_PG_PASSWORD

# Drop privileges to the upstream image's `nextjs` user (uid 1001, gid 65533 /
# nogroup — verified via `docker run --rm langfuse/langfuse:2 id nextjs`).
# `su-exec` is installed by the wrapper Dockerfile because the BusyBox
# `setpriv` applet in the upstream Alpine base lacks --reuid/--regid (only
# util-linux's setpriv supports those). su-exec is the canonical Alpine
# replacement for `gosu` and execs the target without forking, so PID 1
# remains the upstream process tree and signals propagate cleanly.
# Defense-in-depth: if a future Alpine bump renames/removes su-exec, fail with
# a debuggable message rather than the opaque exec "not found" propagation.
command -v su-exec >/dev/null 2>&1 || {
  echo "FATAL: su-exec not on PATH — wrapper image build broke." >&2
  echo "       Re-build with: docker compose --profile observability build langfuse" >&2
  exit 1
}

exec su-exec nextjs:nogroup \
  dumb-init -- ./web/entrypoint.sh "$@"
