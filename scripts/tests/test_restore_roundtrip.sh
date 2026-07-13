#!/usr/bin/env bash
# test_restore_roundtrip.sh — live backup -> break -> older-restore round-trip proof
# for the frictionless disaster-recovery flow.
#
# WHAT THIS PROVES LIVE (against a real postgres:16.8 + the REAL backup.sh /
# restore.sh running in the REAL postgres-backup sidecar dispatch loop, on a
# throwaway postgres-only stack — NO application image is built):
#   * real backup.sh produces real encrypted archives + a real manifest;
#   * the real sidecar sentinel loop consumes .backup_now / .restore_request.json;
#   * SAME-SCHEMA restore reverts mutated data with zero operator step
#     (the .maintenance + .destructive markers are cleared automatically on
#     completion — the assertions check they end cleared, not the mid-restore raise);
#   * OLDER-SCHEMA restore (backup taken at schema 101, live advanced to 102,
#     the 101 backup restored) passes the older-vs-code compat gate and swaps in
#     the older backup — proven by the restored DB dropping back to schema 101
#     with the webauthn tables gone;
#   * the RESTORED older DB forward-migrates to the current schema by running the
#     REAL run_migrations (libs/jarvis_common/jarvis_common/migrations.py — the
#     exact primitive the app_factory watcher runs on maintenance-clear), loaded
#     standalone in a throwaway python:3.12-slim on the fixture network. This is
#     the real function, not a reimplementation.
#   * OFF-HOST (inbox) STEP-8 restore against a fresh-host stand-in: the operator-
#     supplied archive set + one-time key are consumed from the rw restore_inbox, the
#     postgres role is REBOUND to the restored password via ALTER ROLE (single-quote-
#     safe — the restored password contains a quote), the restored ./secrets are
#     materialized into HOST_SECRETS_DIR EXCEPT the local-service keys the target host
#     is authoritative for (qdrant_api_key / langfuse_pg_password / backup_encrypt_key
#     are NOT materialized), the .secrets_rotated marker is written, and both sentinels
#     self-clear. Negative guards prove an unsafe secrets-tar member (absolute/'..'
#     path, or a symlink) fails closed with no materialization.
#   * --recover rejects a SQL-injection payload planted in .restore_swap_state.json's
#     db field (the trigger volume is app-writable): the allowlist makes the run a
#     no-op and the live jarvis DB is untouched (on unfixed code the payload DROPs it).
#
# WHAT IS PROVEN ELSEWHERE (NOT re-proven here — do not overstate):
#   * the full app /health 503->200 transition and background-worker resume are
#     covered on real Postgres by libs/jarvis_common/tests/test_maintenance_middleware.py
#     and services/paper_ingestion/tests/test_main_lifespan.py, which CI runs.
#     This test proves the watcher's core reconcile ACTION (forward-migration), not
#     the FastAPI lifecycle around it.
#   * the app self-restart onto the rotated secrets + rebound role after an inbox
#     restore (no app image is built here) — covered by the app_factory unit tests.
#     PHASE 4 asserts only the SIDECAR-side STEP-8 outcomes (rebind + materialize +
#     rotation marker + sentinel self-clear).
#
# ENCRYPTION: postgres:16.8 ships openssl, so archives are written encrypted and the
# real encrypt->decrypt DR path is exercised. If openssl were absent the fixture
# falls back to plaintext archives (reported in the run banner below).
#
# SAFETY: this is a heavy, on-demand live test (like test_restore_swap_recovery.sh)
# — NOT part of the fast test suite. It refuses to run unless COMPOSE_PROJECT_NAME
# names a dedicated throwaway project (^jarvis-rt-<tag>$, no underscores, so it can
# never equal a real jarvis_* stack). Every container/volume/network lives under that
# project and is torn down with `down -v` on exit. It publishes NO host ports and
# bind-mounts a generated compose file from a mktemp dir — never the repo compose —
# so it is impossible to point at a running stack.
#
# Run: COMPOSE_PROJECT_NAME=jarvis-rt-<tag> bash scripts/tests/test_restore_roundtrip.sh
#      (exit 0 = pass; unset/invalid COMPOSE_PROJECT_NAME or no docker -> SKIP exit 0)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

pass=0; fail=0
ok()  { printf '  PASS: %s\n' "$1"; pass=$((pass + 1)); }
no()  { printf '  FAIL: %s\n' "$1"; fail=$((fail + 1)); }
sec() { printf '\n=== %s ===\n' "$1"; }

# --- Guards: fail-safe by default -------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  printf 'SKIP: docker unavailable; cannot run the live restore round-trip\n' >&2
  exit 0
fi
PROJ="${COMPOSE_PROJECT_NAME:-}"
if ! printf '%s' "$PROJ" | grep -Eq '^jarvis-rt-[a-z0-9]{4,}$'; then
  printf 'SKIP: set COMPOSE_PROJECT_NAME=jarvis-rt-<tag> to run the live round-trip; refusing to touch an unnamed or real project\n' >&2
  exit 0
fi
# Belt-and-braces: the regex already excludes underscores, but assert it here too —
# the real dev stacks (jarvis_rd_assistant, jarvis_business) both carry an underscore,
# so an underscore in the project name can never be the fixture and is refused.
case "$PROJ" in
  *_*)
    printf 'SKIP: COMPOSE_PROJECT_NAME=%s contains an underscore and could collide with a real stack; refusing\n' "$PROJ" >&2
    exit 0 ;;
esac

WORK="$(mktemp -d)"
cleanup() {
  if [ -n "${WORK:-}" ] && [ -f "${WORK}/compose.yml" ]; then
    docker compose -p "$PROJ" -f "${WORK}/compose.yml" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  [ -n "${WORK:-}" ] && rm -rf "$WORK"
}
trap cleanup EXIT

# --- Encryption mode: use the real openssl DR path when the image provides it -
if docker run --rm "${POSTGRES_IMAGE:-postgres:16.8}" sh -c 'command -v openssl >/dev/null 2>&1'; then
  BK_ENC_KEYFILE="/run/secrets/backup_encrypt_key"; ENC_LABEL="encrypted (openssl present)"
else
  BK_ENC_KEYFILE=""; ENC_LABEL="plaintext (openssl absent)"
fi
export REPO_ROOT BK_ENC_KEYFILE   # interpolated by docker compose into the fixture

# --- Throwaway secrets (never the repo ./secrets) ----------------------------
mkdir -p "$WORK/secrets"
PG_PASSWORD="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
printf '%s' "$PG_PASSWORD"                                        > "$WORK/secrets/postgres_password"
printf '%s' "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" > "$WORK/secrets/backup_encrypt_key"
printf 'unused'                                                  > "$WORK/secrets/qdrant_api_key"
printf 'dummy-app-secret'                                        > "$WORK/secrets/app_alpha.txt"
printf 'dummy-app-secret'                                        > "$WORK/secrets/app_beta.txt"

# --- Generated fixture compose (postgres-only; no host ports; named volumes) --
# Quoted heredoc: ${REPO_ROOT}/${BK_ENC_KEYFILE}/${POSTGRES_IMAGE} and the sidecar
# entrypoint's $$ escapes are all resolved by docker compose, not by this shell.
# The entrypoint is copied verbatim from docker-compose.yml's postgres-backup so
# the REAL dispatch loop drives the round trip.
cat > "$WORK/compose.yml" <<'YAML'
services:
  postgres:
    image: ${POSTGRES_IMAGE:-postgres:16.8}
    environment:
      POSTGRES_USER: jarvis
      POSTGRES_DB: jarvis
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
    secrets:
      - postgres_password
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U jarvis"]
      interval: 3s
      timeout: 5s
      retries: 20

  postgres-backup:
    image: ${POSTGRES_IMAGE:-postgres:16.8}
    restart: unless-stopped
    environment:
      ENVIRONMENT: development
      PGHOST: postgres
      PGUSER: jarvis
      PGDATABASE: jarvis
      LITELLM_DATABASE: litellm
      BACKUP_ENCRYPT_KEYFILE: ${BK_ENC_KEYFILE}
      QDRANT_URL: http://127.0.0.1:1
      QDRANT_API_KEYFILE: /run/secrets/qdrant_api_key
      SECRETS_DIR: /secrets
      BACKUP_INTERVAL_SECONDS: "3600"
      JARVIS_VERSION: roundtrip-test
    secrets:
      - postgres_password
      - backup_encrypt_key
      - qdrant_api_key
    volumes:
      - ${REPO_ROOT}/scripts/backup.sh:/usr/local/bin/backup.sh:ro
      - ${REPO_ROOT}/scripts/restore.sh:/usr/local/bin/restore.sh:ro
      - ${REPO_ROOT}/scripts/prune.sh:/usr/local/bin/prune.sh:ro
      - ${REPO_ROOT}/db/migrations:/app/db/migrations:ro
      - ${REPO_ROOT}/db/SCHEMA_VERSION:/app/db/SCHEMA_VERSION:ro
      - ./secrets:/secrets:ro
      - host_secrets:/host-secrets:rw
      - postgres_backups:/backups
      - backup_trigger:/backup-trigger
      - restore_inbox:/restore-inbox
      - postgres_data:/postgres-data:ro
    entrypoint: >
      sh -uc "[ -f /backup-trigger/.restore_swap_state.json ] && /usr/local/bin/restore.sh --recover || true;
      while true;
      do /usr/local/bin/restore.sh --inbox-manifest || true;
      if [ -f /backup-trigger/.restore_request.json ]; then /usr/local/bin/restore.sh || echo \"[sidecar] restore failed rc=$$?\" >&2;
      elif [ -f /backup-trigger/.delete_request.json ]; then /usr/local/bin/prune.sh || echo \"[sidecar] prune failed rc=$$?\" >&2;
      else /usr/local/bin/backup.sh || echo \"[sidecar] backup failed rc=$$?\" >&2; fi;
      i=0; while [ \"$$i\" -lt \"$${BACKUP_INTERVAL_SECONDS:-86400}\" ]; do
      if [ -f /backup-trigger/.backup_now ] || [ -f /backup-trigger/.restore_request.json ] || [ -f /backup-trigger/.delete_request.json ]; then break; fi;
      sleep 5; i=$$((i+5)); done; done"
    depends_on:
      postgres:
        condition: service_healthy

secrets:
  postgres_password:
    file: ./secrets/postgres_password
  backup_encrypt_key:
    file: ./secrets/backup_encrypt_key
  qdrant_api_key:
    file: ./secrets/qdrant_api_key

volumes:
  postgres_data:
  postgres_backups:
  backup_trigger:
  restore_inbox:
  host_secrets:
YAML

# --- The REAL run_migrations, loaded standalone (skips the jarvis_common package
#     __init__ which pulls httpx/app_factory; migrations.py itself needs only
#     asyncpg + stdlib, so this runs the exact real primitive torch/httpx-free). -
cat > "$WORK/run_migrations_helper.py" <<'PY'
import asyncio
import importlib.util
import os
from pathlib import Path

import asyncpg

_spec = importlib.util.spec_from_file_location(
    "jarvis_migrations", "/repo/libs/jarvis_common/jarvis_common/migrations.py"
)
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)


async def _main() -> None:
    pool = await asyncpg.create_pool(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        database=os.environ["PGDATABASE"],
        min_size=1,
        max_size=2,
    )
    try:
        await _mig.run_migrations(pool, Path("/repo/db/migrations"))
    finally:
        await pool.close()


asyncio.run(_main())
PY

# --- Fixture helpers ---------------------------------------------------------
dc() { docker compose -p "$PROJ" -f "$WORK/compose.yml" "$@"; }
# psql inside the postgres container (local socket, trust auth — no password).
q() { dc exec -T postgres psql -U jarvis -d "$1" -tAc "$2" 2>/dev/null | tr -d ' \r\n'; }
# shell inside the running sidecar (owns the /backups + /backup-trigger volumes).
sc() { dc exec -T postgres-backup sh -c "$1"; }

sentinel_present() { sc "[ -f /backup-trigger/$1 ]"; }
backup_succeeded() { sc 'cat /backups/.last_run.json 2>/dev/null' | grep -q '"succeeded":true'; }
last_run_ts() { sc 'cat /backups/.last_run.json 2>/dev/null' | grep -oE '"timestamp":"[0-9]{8}_[0-9]{6}"' | grep -oE '[0-9]{8}_[0-9]{6}' | head -1; }
# A successful run whose manifest + jarvis archive are BOTH present for the same
# timestamp — guards against catching a `succeeded` .last_run whose artifacts a
# concurrent clear removed (the sidecar loop backs up on its own between requests).
backup_ready() {
  backup_succeeded || return 1
  local ts; ts="$(last_run_ts)"
  [ -n "$ts" ] || return 1
  sc "[ -f /backups/manifest_${ts}.json ] && ls /backups/jarvis_${ts}.sql.gz* >/dev/null 2>&1"
}
manifest_schema() { sc "cat /backups/manifest_${1}.json 2>/dev/null" | grep -oE '"schema_version":[0-9]+' | grep -oE '[0-9]+' | head -1; }
no_swap_dbs() { [ "$(q postgres "SELECT count(*) FROM pg_database WHERE datname IN ('jarvis_restore_tmp','jarvis_pre_restore','litellm_restore_tmp','litellm_pre_restore')")" = "0" ]; }
max_version()      { q jarvis "SELECT COALESCE(MAX(version),0) FROM schema_migrations"; }
webauthn_present() { q jarvis "SELECT (to_regclass('public.webauthn_credentials') IS NOT NULL)"; }
marker_tags()      { q jarvis "SELECT COALESCE(string_agg(tag, ',' ORDER BY tag), '') FROM roundtrip_marker"; }
manual_step_flagged() { sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"manual_steps_required":true'; }
restore_state_done()  { sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"state":"done"'; }

restore_done_clean() {
  restore_state_done || return 1
  sentinel_present .maintenance && return 1
  sentinel_present .destructive && return 1
  sentinel_present .restore_request.json && return 1
  no_swap_dbs || return 1
  return 0
}

dump_diagnostics() {
  printf '  --- diagnostics ---\n' >&2
  printf '  databases: %s\n' "$(q postgres "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database WHERE datname NOT IN ('template0','template1')")" >&2
  printf '  trigger dir: %s\n' "$(sc 'ls -a /backup-trigger 2>/dev/null | tr "\n" " "')" >&2
  printf '  backups: %s\n' "$(sc 'ls /backups 2>/dev/null | tr "\n" " "')" >&2
  printf '  last_run: %s\n' "$(sc 'cat /backups/.last_run.json 2>/dev/null')" >&2
  printf '  restore_status: %s\n' "$(sc 'cat /backup-trigger/.restore_status.json 2>/dev/null')" >&2
  dc logs --tail 30 postgres-backup 2>&1 | sed 's/^/  log: /' >&2
}

wait_for() { # <timeout_s> <description> <predicate-fn...>
  local timeout="$1" desc="$2"; shift 2
  local elapsed=0
  while [ "$elapsed" -lt "$timeout" ]; do
    if "$@"; then return 0; fi
    sleep 3; elapsed=$((elapsed + 3))
  done
  printf '  TIMEOUT after %ss waiting for: %s\n' "$timeout" "$desc" >&2
  dump_diagnostics
  return 1
}

clear_backups() { sc 'cd /backups && rm -f jarvis_* litellm_* secrets_* qdrant_* manifest_* .last_run.json 2>/dev/null; true'; }

seed_jarvis() { # <max_version 101|102> <marker_tag>  -> rebuild the jarvis schema in place
  local maxv="$1" tag="$2" extra=""
  [ "$maxv" = "102" ] && extra=",(102)"
  dc exec -T postgres psql -U jarvis -d jarvis -v ON_ERROR_STOP=1 -q <<SQL
SET client_min_messages = warning;
DROP TABLE IF EXISTS sessions CASCADE;
DROP TABLE IF EXISTS webauthn_challenges CASCADE;
DROP TABLE IF EXISTS webauthn_credentials CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS roundtrip_marker CASCADE;
DROP TABLE IF EXISTS schema_migrations CASCADE;
CREATE TABLE schema_migrations(version int PRIMARY KEY, applied_at timestamptz DEFAULT now());
CREATE TABLE users(id bigint PRIMARY KEY);
CREATE TABLE sessions(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id bigint);
CREATE TABLE roundtrip_marker(tag text);
INSERT INTO users(id) VALUES (1);
INSERT INTO sessions(id, user_id) VALUES (gen_random_uuid(), 1);
INSERT INTO roundtrip_marker(tag) VALUES ('${tag}');
INSERT INTO schema_migrations(version) VALUES (101)${extra};
SQL
}

write_restore_request() { # <timestamp> — mirror the sentinel backups.py:request_restore writes
  local ts="$1" json
  json="{\"timestamp\":\"${ts}\",\"confirm\":\"RESTORE\",\"source\":\"local\",\"requested_at\":\"$(date -Iseconds)\"}"
  # Clear any prior .restore_status.json first: a "state":"done" left by an earlier
  # phase would let restore_done_clean report clean before restore.sh re-enters
  # "running", firing the follow-up assertions against the not-yet-restored DB.
  # Write the request atomically (tmp+mv) so the sidecar never reads a torn sentinel.
  sc 'rm -f /backup-trigger/.restore_status.json'
  printf '%s' "$json" | dc exec -T postgres-backup sh -c \
    'cat > /backup-trigger/.restore_request.json.tmp && mv -f /backup-trigger/.restore_request.json.tmp /backup-trigger/.restore_request.json'
}

write_inbox_restore_request() { # <timestamp> — request an off-host (inbox) restore
  local ts="$1" json
  json="{\"timestamp\":\"${ts}\",\"confirm\":\"RESTORE\",\"source\":\"inbox\",\"requested_at\":\"$(date -Iseconds)\"}"
  sc 'rm -f /backup-trigger/.restore_status.json'
  printf '%s' "$json" | dc exec -T postgres-backup sh -c \
    'cat > /backup-trigger/.restore_request.json.tmp && mv -f /backup-trigger/.restore_request.json.tmp /backup-trigger/.restore_request.json'
}
host_secrets_has() { sc "[ -f /host-secrets/$1 ]"; }
# No STEP-8 materialization happened iff neither the rebindable postgres secret nor a
# normal restored secret was written into host-secrets.
host_secrets_unmaterialized() { ! host_secrets_has postgres_password.txt && ! host_secrets_has app_alpha.txt; }
# A recorded terminal failure that HELD the destructive maintenance sentinel — the
# fail-closed outcome for a STEP-8 rejection AFTER the DBs were already restored.
restore_failed_held() {
  sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"state":"failed"' || return 1
  sentinel_present .destructive
}
reset_maint() { sc 'rm -f /backup-trigger/.maintenance /backup-trigger/.destructive /backup-trigger/.restore_swap_state.json /backup-trigger/.restore_status.json'; }

# Copy the real DB archives + a fresh one-time operator key into a cleaned inbox
# (each restore shreds the key on exit, so it is re-staged per case). TS4/SUF from env.
stage_inbox_dbset() {
  sc "rm -f /restore-inbox/*_${TS4}.* /restore-inbox/operator_key /restore-inbox/.inbox_manifest.json 2>/dev/null || true; \
      cp /backups/jarvis_${TS4}.sql.gz${SUF} /backups/litellm_${TS4}.sql.gz${SUF} /restore-inbox/ \
      && cp /secrets/backup_encrypt_key /restore-inbox/operator_key"
}

# Write /restore-inbox/secrets_<ts>.tar.gz[.enc] with a single UNSAFE member so the
# STEP-8 guard must reject it: <abs> = an absolute-path member, <sym> = a symlink.
build_bad_secrets() {
  local kind="$1" mk
  if [ "$kind" = "abs" ]; then
    mk='tar -P -czf /tmp/bad.tgz /etc/hostname'
  else
    mk='rm -rf /tmp/sd; mkdir -p /tmp/sd; ln -sf /host-secrets /tmp/sd/evil; tar -czf /tmp/bad.tgz -C /tmp/sd evil'
  fi
  if [ -n "$SUF" ]; then
    sc "$mk && openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -kfile /restore-inbox/operator_key \
        -in /tmp/bad.tgz -out /restore-inbox/secrets_${TS4}.tar.gz.enc && rm -f /tmp/bad.tgz"
  else
    sc "$mk && mv /tmp/bad.tgz /restore-inbox/secrets_${TS4}.tar.gz"
  fi
}

# A manifest listing ONLY the two DB archives (their real sha256s) so STEP-2 integrity
# passes without pinning the bad secrets tar (which is not listed, so not sha-checked).
write_trimmed_manifest() {
  local real je le
  real="$(sc "cat /backups/manifest_${TS4}.json")"
  je="$(printf '%s' "$real" | grep -oE "\{\"filename\":\"jarvis_${TS4}[^}]*\}")"
  le="$(printf '%s' "$real" | grep -oE "\{\"filename\":\"litellm_${TS4}[^}]*\}")"
  printf '{"timestamp":"%s","schema_version":102,"archives":[%s,%s]}' "$TS4" "$je" "$le" \
    | dc exec -T postgres-backup sh -c "cat > /restore-inbox/manifest_${TS4}.json"
}

# Drive a full inbox restore whose secrets tar has an unsafe member; assert it fails
# closed (DBs restored, then STEP-8 rejects the member -> maintenance HELD, nothing
# materialized), then clear the held sentinels so the next case starts clean.
run_bad_secrets_case() { # <abs|sym> <label>
  local kind="$1" label="$2" clean=1
  stage_inbox_dbset
  build_bad_secrets "$kind"
  write_trimmed_manifest
  write_inbox_restore_request "$TS4"
  if wait_for 180 "inbox restore to fail closed on the ${label} member" restore_failed_held; then
    host_secrets_unmaterialized || { no "${label}: STEP-8 materialized host-secrets despite a rejected member"; clean=0; }
    no_swap_dbs                 || { no "${label}: left a _restore_tmp/_pre_restore database"; clean=0; }
    [ "$clean" = "1" ] && ok "inbox restore rejects a ${label} secrets-tar member, holds maintenance, writes no host-secrets"
  else
    no "inbox restore did not fail-closed on the ${label} member"
  fi
  reset_maint
}

run_real_migrations() { # run the REAL run_migrations against the live fixture jarvis DB
  local pg_cid
  pg_cid="$(dc ps -q postgres)"
  [ -n "$pg_cid" ] || { echo "  no postgres container for migration run" >&2; return 1; }
  docker run --rm \
    --network "container:${pg_cid}" \
    -v "$REPO_ROOT:/repo:ro" -v "$WORK:/work:ro" \
    -e PGHOST=127.0.0.1 -e PGPORT=5432 -e PGUSER=jarvis \
    -e PGPASSWORD="$PG_PASSWORD" -e PGDATABASE=jarvis \
    "${MIGRATION_PY_IMAGE:-python:3.12-slim}" \
    sh -c 'pip install --quiet --disable-pip-version-check asyncpg >/dev/null 2>&1 && python /work/run_migrations_helper.py'
}

# --- Bring the fixture up + provision litellm --------------------------------
printf 'fixture project: %s   archives: %s\n' "$PROJ" "$ENC_LABEL"
if ! dc up -d >/dev/null 2>&1; then
  no "fixture stack failed to start"; dc logs --tail 40 2>&1 | sed 's/^/  /' >&2
  printf '\nROUND-TRIP: PASS=%s FAIL=%s\n' "$pass" "$fail"; exit 1
fi
ready=0
for _ in $(seq 1 40); do dc exec -T postgres pg_isready -U jarvis >/dev/null 2>&1 && { ready=1; break; }; sleep 2; done
[ "$ready" = "1" ] || { no "postgres never became ready"; dump_diagnostics; printf '\nROUND-TRIP: PASS=%s FAIL=%s\n' "$pass" "$fail"; exit 1; }

# Let the sidecar's startup auto-backup settle (it fails — litellm does not exist
# yet — writing a .last_run) so no backup is in-flight when the phases clear/trigger.
wait_for 60 "sidecar startup pass to settle" sc '[ -f /backups/.last_run.json ]' || true

# litellm is created by litellm-db-init in production; the fixture omits that
# service and creates the DB directly (backup.sh does a fatal pg_dump of it).
dc exec -T postgres psql -U jarvis -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE litellm;" >/dev/null 2>&1 || true
dc exec -T postgres psql -U jarvis -d litellm -v ON_ERROR_STOP=1 -q <<'SQL' >/dev/null
CREATE TABLE IF NOT EXISTS "LiteLLM_Config"(param_name text);
INSERT INTO "LiteLLM_Config"(param_name) VALUES ('seeded');
SQL

# =============================================================================
# PHASE 1 — same-schema round trip (schema 102): data reverts, healthy, 0 steps
# =============================================================================
sec "PHASE 1: same-schema restore reverts mutated data (schema 102)"
seed_jarvis 102 "restore-point"
clear_backups
sc 'touch /backup-trigger/.backup_now'
if wait_for 120 "consistent backup at schema 102" backup_ready; then
  TS1="$(last_run_ts)"
  if [ "$(manifest_schema "$TS1")" = "102" ]; then
    ok "backup produced jarvis_${TS1}.sql.gz + manifest at schema 102"
  else
    no "backup schema wrong (ts=$TS1 schema=$(manifest_schema "$TS1"))"
  fi
else
  no "backup never reported success"; TS1=""
fi

if [ -n "${TS1:-}" ]; then
  # Mutate live data, then restore the pre-mutation backup.
  dc exec -T postgres psql -U jarvis -d jarvis -v ON_ERROR_STOP=1 -q <<'SQL' >/dev/null
DELETE FROM roundtrip_marker;
INSERT INTO roundtrip_marker(tag) VALUES ('MUTATED-should-not-survive');
SQL
  write_restore_request "$TS1"
  if wait_for 180 "same-schema restore to complete cleanly" restore_done_clean; then
    fails_before=$fail
    [ "$(marker_tags)" = "restore-point" ] || no "data did not revert (marker='$(marker_tags)')"
    no_swap_dbs || no "restore left a _restore_tmp/_pre_restore database"
    ! sentinel_present .maintenance || no ".maintenance not cleared after a clean restore"
    ! sentinel_present .destructive || no ".destructive not cleared after a clean restore"
    ! manual_step_flagged || no "restore signalled a manual operator step (should be zero-touch)"
    [ "$fail" = "$fails_before" ] && ok "same-schema restore: data reverted, healthy, zero operator step"
  else
    no "same-schema restore did not complete cleanly"
  fi
fi

# =============================================================================
# PHASE 2 — older-schema round trip: back up at 101, advance live to 102,
#           restore the 101 backup, then forward-migrate it back to 102.
# =============================================================================
sec "PHASE 2: older-schema restore + real forward-migration"
seed_jarvis 101 "older-restore-point"
clear_backups
sc 'touch /backup-trigger/.backup_now'
if wait_for 120 "consistent backup at schema 101" backup_ready; then
  TS2="$(last_run_ts)"
  if [ -n "$TS2" ] && [ "$(manifest_schema "$TS2")" = "101" ]; then
    ok "older backup captured at schema 101 (ts=$TS2)"
  else
    no "older backup schema wrong (ts=$TS2 schema=$(manifest_schema "$TS2"))"
  fi
else
  no "older backup never reported success"; TS2=""
fi

# Advance the LIVE DB 101 -> 102 with the real run_migrations (applies 0102).
if run_real_migrations; then
  if [ "$(max_version)" = "102" ] && [ "$(webauthn_present)" = "t" ]; then
    ok "live DB advanced 101 -> 102 by the real run_migrations (webauthn tables created)"
  else
    no "live advance wrong (max=$(max_version) webauthn=$(webauthn_present))"
  fi
else
  no "real run_migrations failed to advance the live DB to 102"
fi

if [ -n "${TS2:-}" ]; then
  write_restore_request "$TS2"
  if wait_for 180 "older-schema restore to complete cleanly" restore_done_clean; then
    fails_before=$fail
    [ "$(max_version)" = "101" ] || no "older restore did not drop schema to 101 (max=$(max_version))"
    [ "$(webauthn_present)" = "f" ] || no "webauthn tables survived the older restore (advanced DB restored, not the 101 backup)"
    [ "$(marker_tags)" = "older-restore-point" ] || no "older restore point data missing (marker='$(marker_tags)')"
    no_swap_dbs || no "older restore left a _restore_tmp/_pre_restore database"
    ! manual_step_flagged || no "older restore signalled a manual operator step (should be zero-touch)"
    [ "$fail" = "$fails_before" ] && ok "older-schema restore: compat gate allowed 101<102, swapped in the older backup, zero-touch"

    # Forward-migrate the restored older DB with the SAME real primitive the
    # app_factory watcher runs on maintenance-clear.
    if run_real_migrations; then
      if [ "$(max_version)" = "102" ] && [ "$(webauthn_present)" = "t" ]; then
        ok "restored older DB self-healed 101 -> 102 via the real run_migrations (floor satisfied, no raise)"
      else
        no "forward-migration wrong (max=$(max_version) webauthn=$(webauthn_present))"
      fi
    else
      no "real run_migrations raised/failed forward-migrating the restored DB"
    fi
  else
    no "older-schema restore did not complete cleanly"
  fi
fi

# =============================================================================
# PHASE 3 — structural tie-in: the live watchers read the very sentinels this
#           round trip drove (grep the REAL repo compose, do not stand it up).
# =============================================================================
sec "PHASE 3: live app services mount the sentinel volume this round trip drove"
svc_bt_line() { # print the backup_trigger mount line(s) for service $1; rc1 if none
  awk -v svc="$1" '
    /^  [a-zA-Z0-9_-]+:$/ { cur=$1; sub(":","",cur) }
    cur==svc && /backup_trigger:\/backup-trigger/ { print; f=1 }
    END { exit (f ? 0 : 1) }
  ' "$REPO_ROOT/docker-compose.yml"
}
# paper_ingestion mounts it read-WRITE (it also writes the .backup_now /
# .restore_request.json sentinels this test drove, and its app_factory watcher
# reads them back); learning_engine mounts it read-only (watcher only).
if svc_bt_line paper_ingestion >/dev/null; then
  ok "paper_ingestion mounts the backup_trigger sentinel volume the fixture drove"
else
  no "paper_ingestion does not mount backup_trigger in the repo compose"
fi
if svc_bt_line learning_engine | grep -q ':ro'; then
  ok "learning_engine mounts backup_trigger read-only (its watcher reads the same sentinels)"
else
  no "learning_engine does not mount backup_trigger :ro in the repo compose"
fi

# =============================================================================
# PHASE 4 — off-host (inbox) STEP-8 restore: role rebind + host-key exclusions + self-clear
#           (the app self-restart onto the rotated secrets is covered by the
#           app_factory unit tests; here only the SIDECAR-side outcomes are asserted).
# =============================================================================
sec "PHASE 4: off-host (inbox) STEP-8 restore — role rebind, host-key exclusions, self-clear"
# The real backup tars the whole /secrets dir, so drop the .txt-named secrets STEP 8
# consumes into it (the fixture's Docker-secret files are un-suffixed). postgres_password
# carries a single quote to exercise the ALTER ROLE single-quote doubling on a REAL role.
NEWPW="p'w"
printf '%s' "$NEWPW"    > "$WORK/secrets/postgres_password.txt"
printf 'benign-qdrant'  > "$WORK/secrets/qdrant_api_key.txt"
printf 'benign-lfpg'    > "$WORK/secrets/langfuse_pg_password.txt"
if [ -n "$BK_ENC_KEYFILE" ]; then SUF=".enc"; else SUF=""; fi

seed_jarvis 102 "inbox-restore-point"
clear_backups
sc 'touch /backup-trigger/.backup_now'
if wait_for 120 "consistent backup for the inbox round trip" backup_ready; then
  TS4="$(last_run_ts)"
  ok "backup for the inbox round trip produced archives at ${TS4}"
else
  no "inbox round-trip backup never reported success"; TS4=""
fi

if [ -n "${TS4:-}" ]; then
  # Negative guards first (the role is still at PG_PASSWORD; these fail at the STEP-8
  # member check, before the ALTER ROLE rebind, so they never poison the live role).
  run_bad_secrets_case abs "absolute/'..' path"
  run_bad_secrets_case sym "symlink/hardlink"

  # Happy path: real secrets archive + real manifest; mutate live data first so the
  # revert is observable.
  stage_inbox_dbset
  sc "cp /backups/secrets_${TS4}.tar.gz${SUF} /backups/manifest_${TS4}.json /restore-inbox/"
  dc exec -T postgres psql -U jarvis -d jarvis -v ON_ERROR_STOP=1 -q <<'SQL' >/dev/null
DELETE FROM roundtrip_marker;
INSERT INTO roundtrip_marker(tag) VALUES ('MUTATED-should-not-survive');
SQL
  write_inbox_restore_request "$TS4"
  if wait_for 180 "off-host inbox restore to complete cleanly" restore_done_clean; then
    fails_before=$fail
    [ "$(marker_tags)" = "inbox-restore-point" ] || no "inbox restore did not revert data (marker='$(marker_tags)')"
    # Role rebound to the restored p'w password (single quote handled): the NEW
    # password authenticates over TCP and the OLD one no longer does.
    rebind_ok=1
    new_auth="$(dc exec -T -e PGPASSWORD="$NEWPW" postgres-backup \
      psql -h postgres -U jarvis -d jarvis -tAc 'SELECT 1' 2>/dev/null | tr -d ' \r\n')"
    [ "$new_auth" = "1" ] || { no "the rebound (p'w) password does not authenticate"; rebind_ok=0; }
    if dc exec -T -e PGPASSWORD="$PG_PASSWORD" postgres-backup \
         psql -h postgres -U jarvis -d jarvis -tAc 'SELECT 1' >/dev/null 2>&1; then
      no "the OLD postgres password still authenticates (ALTER ROLE did not rebind)"; rebind_ok=0
    fi
    [ "$rebind_ok" = "1" ] && ok "ALTER ROLE rebound the role to the restored password (single quote handled; old rejected)"
    # The target host's own local-service keys are NOT overwritten; a normal
    # restored secret IS materialized; the backup key is never materialized.
    host_secrets_has postgres_password.txt || no "postgres_password.txt was not materialized into host-secrets"
    host_secrets_has app_alpha.txt         || no "a normal restored secret (app_alpha.txt) was not materialized"
    ! host_secrets_has qdrant_api_key.txt      || no "qdrant_api_key.txt was materialized (excluded — qdrant is not restarted)"
    ! host_secrets_has langfuse_pg_password.txt || no "langfuse_pg_password.txt was materialized (excluded — langfuse-postgres is not restarted)"
    ! host_secrets_has backup_encrypt_key.txt   || no "backup_encrypt_key.txt was materialized (the target keeps its own backup key)"
    [ "$(sc 'cat /host-secrets/postgres_password.txt 2>/dev/null' | tr -d '\r\n')" = "$NEWPW" ] \
      || no "materialized postgres_password.txt content is wrong (single quote mangled)"
    sc 'cat /backup-trigger/.secrets_rotated 2>/dev/null' | grep -qE '^[0-9]+$' \
      || no ".secrets_rotated is missing or not an integer epoch"
    [ "$fail" = "$fails_before" ] \
      && ok "inbox STEP-8: DBs reverted, role rebound, host keys excluded, secrets rotated, sentinels self-cleared"
  else
    no "off-host inbox restore did not complete cleanly"
  fi
  # Un-poison the live role (rebound to p'w above) so PHASE 5's password-auth path works
  # and the sidecar loop can resume — via the trust socket, no password needed.
  dc exec -T postgres psql -U jarvis -d postgres -v ON_ERROR_STOP=1 -q \
    -c "ALTER ROLE jarvis WITH PASSWORD '${PG_PASSWORD}';" >/dev/null 2>&1 || true
fi

# =============================================================================
# PHASE 5 — --recover rejects a SQL-injection payload in the swap-state file.
# =============================================================================
sec "PHASE 5: --recover rejects an injected swap-state db name (no DROP)"
if [ -n "${TS4:-}" ]; then
  # Plant a hostile .restore_swap_state.json (the trigger volume is app-writable) whose
  # db field is a SQL-injection payload targeting the live jarvis DB. read_swap_db's
  # allowlist must reject it so --recover runs NO SQL; on unfixed code the injected
  # '; DROP DATABASE jarvis; --' would execute against the maintenance DB and drop it.
  inj='{"db":"x'"'"'; DROP DATABASE jarvis; --","phase":"swapping_out"}'
  printf '%s' "$inj" | dc exec -T postgres-backup sh -c 'cat > /backup-trigger/.restore_swap_state.json'
  before_marker="$(marker_tags)"
  dc exec -T postgres-backup /usr/local/bin/restore.sh --recover >/dev/null 2>&1 || true
  jarvis_ok=1
  q jarvis "SELECT 1" >/dev/null 2>&1 || { no "jarvis DB no longer exists after --recover (injection dropped it)"; jarvis_ok=0; }
  [ "$(marker_tags)" = "$before_marker" ] || { no "jarvis data changed after --recover (marker='$(marker_tags)')"; jarvis_ok=0; }
  [ "$jarvis_ok" = "1" ] && ok "--recover allowlist rejected the injected db name — jarvis intact, no DROP, clean no-op"
  ! sentinel_present .restore_swap_state.json || no "--recover left the hostile swap-state file in place (could wedge future recovers)"
else
  no "PHASE 5 skipped: no backup timestamp from PHASE 4"
fi

# =============================================================================
printf '\n================================================================\n'
printf 'RESTORE ROUND-TRIP: PASS=%s  FAIL=%s\n' "$pass" "$fail"
printf '================================================================\n'
[ "$fail" -eq 0 ] || exit 1
