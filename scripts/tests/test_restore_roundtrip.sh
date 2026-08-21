#!/usr/bin/env bash
# Live backup and destructive restore verification.
#
# The generated stack covers backup, restore, sidecar dispatch, migration,
# encrypted archives, PDFs, data keys, direct LiteLLM routing, and off-host
# credential preservation. Durable identities survive restore while sessions and
# one-time authentication records are invalidated.
#
# Isolation is enforced through a dedicated Compose project with no published
# ports. Cleanup runs only after the project label is revalidated. Release mode
# generates the project name and treats every missing prerequisite as failure.
# Ordinary local use remains opt-in and may skip when prerequisites are absent.
#
# Usage:
#   bash scripts/tests/test_restore_roundtrip.sh --release-gate
#   COMPOSE_PROJECT_NAME=jarvis-rt-<tag> bash scripts/tests/test_restore_roundtrip.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

pass=0; fail=0
ok()  { printf '  PASS: %s\n' "$1"; pass=$((pass + 1)); }
no()  { printf '  FAIL: %s\n' "$1"; fail=$((fail + 1)); }
sec() { printf '\n=== %s ===\n' "$1"; }

# --- Invocation and ownership guards ----------------------------------------
RELEASE_GATE=0
case "${1:-}" in
  --release-gate) RELEASE_GATE=1; shift ;;
  -h|--help)
    sed -n '2,/^set -uo/{ /^set -uo/d; s/^# \{0,1\}//; s/^#$//; p; }' "$0"
    exit 0 ;;
  '') ;;
  *) printf 'FAIL: unknown argument: %s\n' "$1" >&2; exit 2 ;;
esac
if [ "$#" -ne 0 ]; then
  printf 'FAIL: unexpected positional arguments\n' >&2
  exit 2
fi

unavailable() {
  if [ "$RELEASE_GATE" -eq 1 ]; then
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
  fi
  printf 'SKIP: %s\n' "$1" >&2
  exit 0
}

if [ "$RELEASE_GATE" -eq 1 ]; then
  if [ "${COMPOSE_PROJECT_NAME+x}" = x ]; then
    printf 'FAIL: COMPOSE_PROJECT_NAME must be unset in release mode\n' >&2
    exit 2
  fi
  fixture_suffix="$(od -An -N8 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')" \
    || { printf 'FAIL: could not generate the fixture name\n' >&2; exit 1; }
  if ! printf '%s' "$fixture_suffix" | grep -Eq '^[0-9a-f]{16}$'; then
    printf 'FAIL: generated fixture suffix is invalid\n' >&2
    exit 1
  fi
  PROJ="jarvis-rt-${fixture_suffix}"
else
  PROJ="${COMPOSE_PROJECT_NAME:-}"
  if ! printf '%s' "$PROJ" | grep -Eq '^jarvis-rt-[a-z0-9]{4,}$'; then
    unavailable 'set COMPOSE_PROJECT_NAME=jarvis-rt-<tag> to run the local round-trip'
  fi
fi
printf 'fixture project: %s\n' "$PROJ"

if ! command -v docker >/dev/null 2>&1; then
  unavailable 'docker is unavailable; cannot run the live restore round-trip'
fi
if ! command -v openssl >/dev/null 2>&1; then
  unavailable 'openssl is unavailable; cannot build authenticated restore requests'
fi
DOCKER_BIN="$(command -v docker)"
"$DOCKER_BIN" compose version >/dev/null 2>&1 \
  || unavailable 'Docker Compose v2 is unavailable'
"$DOCKER_BIN" info >/dev/null 2>&1 \
  || unavailable 'the Docker daemon is unreachable'

_project_resource_ids() {
  case "$1" in
    container)
      "$DOCKER_BIN" ps -aq --filter "label=com.docker.compose.project=${PROJ}" ;;
    network)
      "$DOCKER_BIN" network ls -q --filter "label=com.docker.compose.project=${PROJ}" ;;
    volume)
      "$DOCKER_BIN" volume ls -q --filter "label=com.docker.compose.project=${PROJ}" ;;
    *) return 2 ;;
  esac
}

_project_resources_empty() {
  local kind resources
  for kind in container network volume; do
    resources="$(_project_resource_ids "$kind")" || return 1
    [ -z "$resources" ] || return 1
  done
}

_project_runtime_resources_empty() {
  local kind resources
  for kind in container network; do
    resources="$(_project_resource_ids "$kind")" || return 1
    [ -z "$resources" ] || return 1
  done
}

_assert_project_absent() {
  if _project_resources_empty; then
    return 0
  fi
  printf 'FAIL: Compose project %s already owns Docker resources\n' "$PROJ" >&2
  return 1
}

_project_resources_owned() {
  local kind resource resources label
  for kind in container network volume; do
    resources="$(_project_resource_ids "$kind")" || return 1
    for resource in $resources; do
      case "$kind" in
        container)
          label="$($DOCKER_BIN inspect --type container --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$resource")" \
            || return 1 ;;
        network|volume)
          label="$($DOCKER_BIN inspect --type "$kind" --format '{{ index .Labels "com.docker.compose.project" }}' "$resource")" \
            || return 1 ;;
      esac
      [ "$label" = "$PROJ" ] || return 1
    done
  done
}

_assert_project_absent || exit 1
PROJECT_OWNERSHIP_CONFIRMED=1
WORK=""
cleanup() {
  local rc="${1:-1}" cleanup_rc=0 retained_volumes
  trap - EXIT
  if [ "$PROJECT_OWNERSHIP_CONFIRMED" -eq 1 ]; then
    if ! _project_resources_owned; then
      printf 'FAIL: refusing cleanup because project ownership could not be revalidated\n' >&2
      cleanup_rc=1
    elif ! _project_runtime_resources_empty; then
      if [ -n "$WORK" ] && [ -f "$WORK/compose.yml" ]; then
        "$DOCKER_BIN" compose -p "$PROJ" -f "$WORK/compose.yml" down --remove-orphans >/dev/null 2>&1 \
          || cleanup_rc=1
      else
        printf 'FAIL: refusing cleanup without the generated Compose file\n' >&2
        cleanup_rc=1
      fi
    fi
    if ! _project_runtime_resources_empty; then
      printf 'FAIL: generated project containers or networks remain after cleanup\n' >&2
      cleanup_rc=1
    fi
    retained_volumes="$(_project_resource_ids volume)" || cleanup_rc=1
    if [ -n "$retained_volumes" ]; then
      printf 'NOTE: retained disposable volumes for project %s: %s\n' "$PROJ" "$retained_volumes" >&2
    fi
  fi
  if [ -n "$WORK" ] && [ -d "$WORK" ]; then
    rm -rf -- "$WORK" || cleanup_rc=1
  fi
  [ "$cleanup_rc" -eq 0 ] || rc=1
  exit "$rc"
}
trap 'cleanup "$?"' EXIT
WORK="$(mktemp -d)" || { printf 'FAIL: could not create fixture directory\n' >&2; exit 1; }

BK_ENC_KEYFILE="/run/secrets/backup_encrypt_key"
ENC_LABEL="encrypted (openssl present)"
SUF=".enc"
export REPO_ROOT BK_ENC_KEYFILE

# --- Throwaway secrets (never the repo ./secrets) ----------------------------
mkdir -p "$WORK/secrets"
SOURCE_PG_PASSWORD="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
TARGET_PG_PASSWORD="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SOURCE_BACKUP_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
TARGET_BACKUP_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SOURCE_CONFIG_KEY='c291cmNlLWNvbmZpZy1rZXktMDEyMzQ1Njc4OWFiY2Q='
TARGET_CONFIG_KEY='dGFyZ2V0LWNvbmZpZy1rZXktMDEyMzQ1Njc4OWFiY2Q='
SOURCE_MODEL_KEY='source-model-hmac-key-0123456789abcdef'
TARGET_MODEL_KEY='target-model-hmac-key-0123456789abcdef'
SOURCE_LITELLM_MASTER="sk-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
TARGET_LITELLM_MASTER="sk-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SOURCE_LITELLM_SALT='source-litellm-salt-0123456789abcdef'
TARGET_LITELLM_SALT='target-litellm-salt-0123456789abcdef'
ROLE_SECRET_FILES='postgres_platform_runtime_password.txt postgres_research_runtime_password.txt postgres_learning_runtime_password.txt postgres_migrator_password.txt postgres_cluster_bootstrap_password.txt postgres_backup_reader_password.txt postgres_restore_operator_password.txt postgres_erasure_executor_password.txt litellm_runtime_password.txt litellm_migrator_password.txt'
SOURCE_BOOTSTRAP_PASSWORD='source-postgres_cluster_bootstrap_password.txt'
TARGET_BOOTSTRAP_PASSWORD='target-postgres_cluster_bootstrap_password.txt'

TARGET_LOCAL_CREDENTIAL_FILES='postgres_password.txt postgres_cluster_bootstrap_password.txt jarvis_api_key.txt jarvis_setup_token.txt qdrant_api_key.txt litellm_master_key.txt backup_encrypt_key.txt smtp_pass.txt telegram_bot_token.txt cloudflare_tunnel_token.txt langfuse_pg_password.txt langfuse_nextauth_secret.txt langfuse_salt.txt langfuse_init_user_password.txt langfuse_init_pk.txt langfuse_init_sk.txt future_credential.txt'
DATA_KEY_FILES='jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt'

write_fixture_secret() { printf '%s' "$2" > "$WORK/secrets/$1"; }
write_fixture_secret postgres_password "$SOURCE_PG_PASSWORD"
write_fixture_secret backup_encrypt_key "$SOURCE_BACKUP_KEY"
write_fixture_secret qdrant_api_key 'source-qdrant-runtime-key'
write_fixture_secret jarvis_config_key.txt "$SOURCE_CONFIG_KEY"
write_fixture_secret jarvis_model_hmac_key.txt "$SOURCE_MODEL_KEY"
write_fixture_secret litellm_salt_key.txt "$SOURCE_LITELLM_SALT"
for _credential in $TARGET_LOCAL_CREDENTIAL_FILES; do
  _credential_value="source-${_credential}"
  [ "$_credential" != litellm_master_key.txt ] \
    || _credential_value="$SOURCE_LITELLM_MASTER"
  write_fixture_secret "$_credential" "$_credential_value"
done
write_fixture_secret postgres_password.txt "$SOURCE_PG_PASSWORD"
write_fixture_secret backup_encrypt_key.txt "$SOURCE_BACKUP_KEY"
write_fixture_secret qdrant_api_key.txt 'source-qdrant-runtime-key'
for _role_secret in $ROLE_SECRET_FILES; do
  write_fixture_secret "$_role_secret" "source-${_role_secret}"
done

mkdir -p "$WORK/host-secrets" "$WORK/provider-state"
cp -a "$WORK/secrets/." "$WORK/host-secrets/"
link_host_secret() {
  local name="$1"
  ln -sfn "${name}.txt" "$WORK/host-secrets/$name" \
    || { printf 'FAIL: could not link the %s fixture secret\n' "$name" >&2; exit 1; }
}
link_host_secret litellm_master_key
link_host_secret litellm_salt_key
link_host_secret litellm_runtime_password
link_host_secret postgres_password
: > "$WORK/provider-state/requests.log"

# --- Isolated provider and LiteLLM configuration -----------------------------
cat > "$WORK/faux-provider.py" <<'PY'
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


_REQUEST_LOG = Path("/state/requests.log")


class _FauxProviderHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlsplit(self.path).path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlsplit(self.path).path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        with _REQUEST_LOG.open("a", encoding="utf-8") as request_log:
            request_log.write("chat\n")
        self._send_json(
            200,
            {
                "id": "chatcmpl-restore-fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "faux-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "fixture-provider-ok",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        return


ThreadingHTTPServer(("0.0.0.0", 8080), _FauxProviderHandler).serve_forever()
PY

cat > "$WORK/litellm-config.yaml" <<'YAML'
model_list:
  - model_name: restore-fixture
    litellm_params:
      model: openai/faux-model
      api_base: http://vllm:8080/v1
      api_key: fixture-provider-key

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY

litellm_settings:
  drop_params: true
YAML

# --- Generated fixture compose (no host ports) -------------------------------
# Compose resolves repository and image variables plus the sidecar entrypoint's
# escaped dollar signs when it starts the generated project.
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
      test: ["CMD-SHELL", "psql -U jarvis_cluster_bootstrap -d jarvis -tAc 'SELECT 1' >/dev/null 2>&1 || psql -U jarvis -d jarvis -tAc 'SELECT 1' >/dev/null 2>&1"]
      interval: 3s
      timeout: 5s
      retries: 20

  litellm-db-init:
    image: ${POSTGRES_IMAGE:-postgres:16.8}
    security_opt: ["no-new-privileges:true"]
    secrets:
      - postgres_password
    entrypoint: ["sh", "-c"]
    command:
      - |
        set -e
        export PGPASSWORD="$$(cat /run/secrets/postgres_password)"
        psql -h postgres -U jarvis -d jarvis -v ON_ERROR_STOP=1 \
          -tc "SELECT 1 FROM pg_database WHERE datname = 'litellm'" | grep -q 1 \
          || createdb -h postgres -U jarvis litellm
    restart: "no"
    depends_on:
      postgres:
        condition: service_healthy

  vllm:
    image: ${MIGRATION_PY_IMAGE:-python:3.12-slim}
    security_opt: ["no-new-privileges:true"]
    read_only: true
    environment:
      PYTHONDONTWRITEBYTECODE: "1"
    command: ["python", "/app/faux-provider.py"]
    volumes:
      - ./faux-provider.py:/app/faux-provider.py:ro
      - ./provider-state:/state
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=3)"]
      interval: 2s
      timeout: 5s
      retries: 20

  litellm:
    image: ${LITELLM_IMAGE:-docker.litellm.ai/berriai/litellm@sha256:29252f25ed1b538d44f6b76ec97412c5537a180b39ede744b9f3e86ffdd278f5}
    restart: unless-stopped
    security_opt: ["no-new-privileges:true"]
    environment:
      STORE_MODEL_IN_DB: "true"
      POSTGRES_USER: jarvis_litellm_runtime
      POSTGRES_PASSWORD_FILE: /run/secrets/litellm_runtime_password
    volumes:
      - ./litellm-config.yaml:/app/config.yaml:ro
      - ${REPO_ROOT}/scripts/litellm-entrypoint.sh:/usr/local/bin/litellm-entrypoint.sh:ro
      - ${REPO_ROOT}/litellm/pinned_launcher.py:/app/pinned_launcher.py:ro
      - ${REPO_ROOT}/libs/jarvis_common/jarvis_common/net.py:/app/jarvis_common/net.py:ro
      - ${REPO_ROOT}/libs/jarvis_common/jarvis_common/pinned_transport.py:/app/jarvis_common/pinned_transport.py:ro
      - ./host-secrets:/run/secrets:ro
      - backup_trigger:/backup-trigger:ro
    entrypoint: ["sh", "/usr/local/bin/litellm-entrypoint.sh"]
    command: []
    healthcheck:
      test: ["CMD", "sh", "/usr/local/bin/litellm-entrypoint.sh", "--healthcheck"]
      interval: 3s
      timeout: 5s
      retries: 40
      start_period: 20s
    depends_on:
      postgres:
        condition: service_healthy
      litellm-migrator:
        condition: service_completed_successfully
      vllm:
        condition: service_healthy

  postgres-backup:
    image: ${POSTGRES_IMAGE:-postgres:16.8}
    restart: unless-stopped
    environment:
      ENVIRONMENT: development
      PGHOST: postgres
      PGUSER: jarvis_backup_reader
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_backup_reader_password
      PGDATABASE: jarvis
      LITELLM_DATABASE: litellm
      BACKUP_ENCRYPT_KEYFILE: ${BK_ENC_KEYFILE}
      QDRANT_URL: http://127.0.0.1:1
      QDRANT_API_KEYFILE: /run/secrets/qdrant_api_key
      SECRETS_DIR: /data-keys
      HOST_SECRETS_DIR: /backup-state
      PDF_STORAGE_DIR: /pdf-storage
      BACKUP_INTERVAL_SECONDS: "3600"
      JARVIS_VERSION: roundtrip-test
    secrets:
      - postgres_backup_reader_password
      - backup_encrypt_key
      - qdrant_api_key
    volumes:
      - ${REPO_ROOT}/scripts/backup.sh:/usr/local/bin/backup.sh:ro
      - ${REPO_ROOT}/scripts/backup-lifecycle.sh:/usr/local/bin/backup-lifecycle.sh:ro
      - ${REPO_ROOT}/scripts/prune.sh:/usr/local/bin/prune.sh:ro
      - ./secrets/jarvis_config_key.txt:/data-keys/jarvis_config_key.txt:ro
      - ./secrets/jarvis_model_hmac_key.txt:/data-keys/jarvis_model_hmac_key.txt:ro
      - ./secrets/litellm_salt_key.txt:/data-keys/litellm_salt_key.txt:ro
      - backup_state:/backup-state:rw
      - ./provider-state:/provider-state:ro
      - postgres_backups:/backups
      - backup_trigger:/backup-trigger
      - restore_inbox:/restore-inbox
      - postgres_data:/postgres-data:ro
      - pdf_storage:/pdf-storage
    entrypoint: >
      sh -uc "while true;
      do /usr/local/bin/backup.sh --inbox-manifest || echo \"[sidecar] inbox inventory failed rc=$$?\" >&2;
      if [ -f /backup-trigger/.delete_request.json ]; then /usr/local/bin/prune.sh || echo \"[sidecar] prune failed rc=$$?\" >&2;
      else /usr/local/bin/backup.sh || echo \"[sidecar] backup failed rc=$$?\" >&2; fi;
      i=0; while [ \"$$i\" -lt \"$${BACKUP_INTERVAL_SECONDS:-86400}\" ]; do
      /usr/local/bin/backup.sh --inbox-manifest || echo \"[sidecar] inbox inventory failed rc=$$?\" >&2;
      if [ -f /backup-trigger/.backup_now ] || [ -f /backup-trigger/.delete_request.json ]; then break; fi;
      sleep 5; i=$$((i+5)); done; done"
    depends_on:
      postgres:
        condition: service_healthy
      litellm-migrator:
        condition: service_completed_successfully

  postgres-restore:
    profiles: ["restore"]
    image: ${POSTGRES_IMAGE:-postgres:16.8}
    restart: "no"
    environment:
      ENVIRONMENT: development
      PGHOST: postgres
      PGUSER: jarvis_restore_operator
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_restore_operator_password
      PGDATABASE: jarvis
      BACKUP_ENCRYPT_KEYFILE: ${BK_ENC_KEYFILE}
      QDRANT_URL: http://127.0.0.1:1
      QDRANT_API_KEYFILE: /run/secrets/qdrant_api_key
      SECRETS_DIR: /host-secrets
      PDF_STORAGE_DIR: /pdf-storage
      JARVIS_VERSION: roundtrip-test
    secrets:
      - postgres_restore_operator_password
      - backup_encrypt_key
      - qdrant_api_key
    volumes:
      - ${REPO_ROOT}/scripts/backup.sh:/usr/local/bin/backup.sh:ro
      - ${REPO_ROOT}/scripts/restore.sh:/usr/local/bin/restore.sh:ro
      - ${REPO_ROOT}/db/migrations:/app/db/migrations:ro
      - ${REPO_ROOT}/db/SCHEMA_VERSION:/app/db/SCHEMA_VERSION:ro
      - ./host-secrets:/host-secrets:rw
      - backup_state:/backup-state:rw
      - postgres_backups:/backups
      - backup_trigger:/backup-trigger
      - restore_inbox:/restore-inbox
      - postgres_data:/postgres-data:ro
      - pdf_storage:/pdf-storage
    entrypoint: ["/usr/local/bin/restore.sh"]
    command: ["--run-request"]

  cluster-bootstrap:
    image: ${POSTGRES_IMAGE:-postgres:16.8}
    restart: "no"
    environment:
      PGHOST: postgres
      POSTGRES_DB: jarvis
    secrets:
      - postgres_platform_runtime_password
      - postgres_research_runtime_password
      - postgres_learning_runtime_password
      - postgres_migrator_password
      - postgres_cluster_bootstrap_password
      - postgres_backup_reader_password
      - postgres_restore_operator_password
      - postgres_erasure_executor_password
      - litellm_runtime_password
      - litellm_migrator_password
      - postgres_legacy_source_password
    volumes:
      - ${REPO_ROOT}/scripts/postgres-role-bootstrap.sh:/usr/local/bin/postgres-role-bootstrap.sh:ro
      - ${REPO_ROOT}/db/restore-authority.sql:/app/db/restore-authority.sql:ro
    entrypoint: ["sh", "/usr/local/bin/postgres-role-bootstrap.sh"]

  jarvis-migrator:
    image: ${MIGRATION_PY_IMAGE:-python:3.12-slim}
    restart: "no"
    environment:
      PGHOST: postgres
      PGUSER: jarvis_migrator
      PGDATABASE: jarvis
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_migrator_password
    secrets:
      - postgres_migrator_password
    volumes:
      - ${REPO_ROOT}:/repo:ro
      - .:/work:ro
    entrypoint: ["sh", "-c"]
    command:
      - |
        set -e
        pip install --quiet --disable-pip-version-check asyncpg >/dev/null 2>&1
        export PGPASSWORD="$$(cat /run/secrets/postgres_migrator_password)"
        exec python /work/run_migrations_helper.py

  litellm-migrator:
    image: ${LITELLM_IMAGE:-docker.litellm.ai/berriai/litellm@sha256:29252f25ed1b538d44f6b76ec97412c5537a180b39ede744b9f3e86ffdd278f5}
    restart: "no"
    environment:
      POSTGRES_USER: jarvis_litellm_migrator
      POSTGRES_PASSWORD_FILE: /run/secrets/litellm_migrator_password
    secrets:
      - litellm_migrator_password
    volumes:
      - ./litellm-config.yaml:/app/config.yaml:ro
      - ${REPO_ROOT}/scripts/litellm-entrypoint.sh:/usr/local/bin/litellm-entrypoint.sh:ro
    entrypoint: ["sh", "/usr/local/bin/litellm-entrypoint.sh"]
    command: ["--migrate"]

secrets:
  postgres_password:
    file: ./secrets/postgres_password
  backup_encrypt_key:
    file: ./secrets/backup_encrypt_key
  qdrant_api_key:
    file: ./secrets/qdrant_api_key
  postgres_platform_runtime_password:
    file: ./secrets/postgres_platform_runtime_password.txt
  postgres_research_runtime_password:
    file: ./secrets/postgres_research_runtime_password.txt
  postgres_learning_runtime_password:
    file: ./secrets/postgres_learning_runtime_password.txt
  postgres_migrator_password:
    file: ./secrets/postgres_migrator_password.txt
  postgres_cluster_bootstrap_password:
    file: ./secrets/postgres_cluster_bootstrap_password.txt
  postgres_backup_reader_password:
    file: ./secrets/postgres_backup_reader_password.txt
  postgres_restore_operator_password:
    file: ./secrets/postgres_restore_operator_password.txt
  postgres_erasure_executor_password:
    file: ./secrets/postgres_erasure_executor_password.txt
  litellm_runtime_password:
    file: ./secrets/litellm_runtime_password.txt
  litellm_migrator_password:
    file: ./secrets/litellm_migrator_password.txt
  postgres_legacy_source_password:
    file: ./secrets/postgres_password.txt

volumes:
  postgres_data:
  postgres_backups:
  backup_trigger:
  restore_inbox:
  backup_state:
  pdf_storage:
YAML

# --- Migration runner loaded without package startup side effects -------------
cat > "$WORK/run_migrations_helper.py" <<'PY'
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import asyncpg

_spec = importlib.util.spec_from_file_location(
    "jarvis_migrations", "/repo/libs/jarvis_common/jarvis_common/migrations.py"
)
_mig = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mig
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
        maximum = int(os.environ.get("JARVIS_MIGRATION_MAX_VERSION", "0"))
        if maximum:
            # The historical predecessor is constructed from the retained,
            # integrity-checked files, not from a second checked-in fixture.
            selected = Path("/tmp/fixture-migrations")
            selected.mkdir()
            for migration in Path("/repo/db/migrations").glob("*.sql"):
                if int(migration.name.split("_", maxsplit=1)[0]) <= maximum:
                    (selected / migration.name).write_bytes(migration.read_bytes())
            _mig.required_code_schema = lambda: maximum
            await _mig.run_migrations(pool, selected)
        else:
            await _mig.run_migrations(pool, Path("/repo/db/migrations"))
    finally:
        await pool.close()


asyncio.run(_main())
PY

# --- Fixture helpers ---------------------------------------------------------
dc() { "$DOCKER_BIN" compose -p "$PROJ" -f "$WORK/compose.yml" "$@"; }

if ! dc run --rm --no-deps --entrypoint sh postgres \
  -c 'command -v openssl >/dev/null 2>&1' >/dev/null 2>&1; then
  unavailable "${POSTGRES_IMAGE:-postgres:16.8} is unavailable or does not provide openssl"
fi

# The initial predecessor has the legacy ``jarvis`` bootstrap role. Once the
# root sequence establishes its isolated authority, use that superuser for
# fixture-only assertions and historical reseeding.
fixture_pg_role() {
  if dc exec -T postgres psql -U jarvis_cluster_bootstrap -d postgres -tAc 'SELECT 1' >/dev/null 2>&1; then
    printf '%s' jarvis_cluster_bootstrap
  else
    printf '%s' jarvis
  fi
}
fixture_pg_password() {
  [ "$1" = jarvis_cluster_bootstrap ] && printf '%s' "$SOURCE_BOOTSTRAP_PASSWORD" || printf '%s' "$SOURCE_PG_PASSWORD"
}
# psql inside the postgres container (local socket, trust auth — no password).
q() { dc exec -T postgres psql -U "$(fixture_pg_role)" -d "$1" -tAc "$2" 2>/dev/null | tr -d ' \r\n'; }
# shell inside the running sidecar (owns the /backups + /backup-trigger volumes).
sc() { dc exec -T postgres-backup sh -c "$1"; }
restore_sc() { dc run --rm --no-deps --entrypoint sh postgres-restore -c "$1"; }

provider_hit_count() {
  sc 'wc -l < /provider-state/requests.log 2>/dev/null' | tr -d ' \r\n'
}

litellm_chat_works() {
  local master_key="${1:-$SOURCE_LITELLM_MASTER}"
  dc exec -T -e LITELLM_TEST_KEY="$master_key" postgres-backup \
    perl -MHTTP::Tiny -MJSON::PP -e '
      my $payload = encode_json({
        model => "restore-fixture",
        messages => [{role => "user", content => "restore quarantine check"}],
        stream => JSON::PP::false,
      });
      my $response = HTTP::Tiny->new(timeout => 20)->post(
        "http://litellm:4000/v1/chat/completions",
        {
          headers => {
            "Authorization" => "Bearer $ENV{LITELLM_TEST_KEY}",
            "Content-Type" => "application/json",
          },
          content => $payload,
        },
      );
      exit(1) unless $response->{success};
      my $body = eval { decode_json($response->{content}) } or exit(1);
      my $content = $body->{choices}->[0]->{message}->{content} // "";
      exit($content eq "fixture-provider-ok" ? 0 : 1);
    '
}

litellm_accepts_connections() {
  dc exec -T postgres-backup perl -MIO::Socket::INET -e '
    my $socket = IO::Socket::INET->new(
      PeerHost => "litellm",
      PeerPort => 4000,
      Proto => "tcp",
      Timeout => 2,
    ) or exit(1);
    close $socket;
  '
}

litellm_quarantine_blocks_direct_routing() {
  local hits_before hits_after
  litellm_accepts_connections && return 1
  sleep 1
  litellm_accepts_connections && return 1
  hits_before="$(provider_hit_count)" || return 1
  litellm_chat_works && return 1
  sleep 1
  hits_after="$(provider_hit_count)" || return 1
  [ "$hits_before" = "$hits_after" ]
}

sentinel_present() { sc "[ -f /backup-trigger/$1 ]"; }
private_state_present() { sc "[ -f /backups/.lifecycle/$1 ]"; }
backup_succeeded() { sc 'cat /backups/.last_run.json 2>/dev/null' | grep -q '"succeeded":true'; }
last_run_ts() { sc 'cat /backups/.last_run.json 2>/dev/null' | grep -oE '"timestamp":"[0-9]{8}_[0-9]{6}"' | grep -oE '[0-9]{8}_[0-9]{6}' | head -1; }
# A successful current run must publish both databases, PDFs, the exact data-key
# archive, and an authenticated manifest under one timestamp.
backup_ready() {
  backup_succeeded || return 1
  local ts; ts="$(last_run_ts)"
  [ -n "$ts" ] || return 1
  sc "[ -f /backups/manifest_${ts}.json ] \
      && [ -f /backups/manifest_${ts}.json.hmac ] \
      && [ -f /backups/jarvis_${ts}.sql.gz${SUF} ] \
      && [ -f /backups/litellm_${ts}.sql.gz${SUF} ] \
      && [ -f /backups/pdfs_${ts}.tar.gz${SUF} ] \
      && [ -f /backups/secrets_${ts}.tar.gz${SUF} ] \
      && grep -q '\"filename\":\"pdfs_${ts}.tar.gz${SUF}\"' /backups/manifest_${ts}.json"
}
manifest_schema() { sc "cat /backups/manifest_${1}.json 2>/dev/null" | grep -oE '"schema_version":[0-9]+' | grep -oE '[0-9]+' | head -1; }
no_swap_dbs() { [ "$(q postgres "SELECT count(*) FROM pg_database WHERE datname IN ('jarvis_restore_tmp','jarvis_pre_restore','litellm_restore_tmp','litellm_pre_restore')")" = "0" ]; }
max_version()      { q jarvis "SELECT COALESCE(MAX(version),0) FROM ops.schema_migrations"; }
webauthn_present() { q jarvis "SELECT (to_regclass('platform.webauthn_credentials') IS NOT NULL)"; }
marker_tags()      { q jarvis "SELECT COALESCE(string_agg(tag, ',' ORDER BY tag), '') FROM public.roundtrip_marker"; }
# Governed roles carrying a stored search_path default. Every relation lives in
# an owned schema, so a role without one resolves unqualified names against an
# empty public schema.
roles_with_search_path() {
  q jarvis "SELECT count(*) FROM pg_roles
    WHERE rolname IN (
      'jarvis_platform_owner', 'jarvis_research_owner', 'jarvis_learning_owner',
      'jarvis_ops_owner', 'jarvis_platform_runtime', 'jarvis_research_runtime',
      'jarvis_learning_runtime', 'jarvis_migrator', 'jarvis_legacy_rollback')
      AND EXISTS (
        SELECT 1 FROM unnest(COALESCE(rolconfig, ARRAY[]::text[])) AS stored(setting)
        WHERE stored.setting LIKE 'search_path=%')"
}
manual_step_flagged() { sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"manual_steps_required":true'; }
restore_state_done()  { sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"state":"done"'; }
restore_failed_before_mutation() {
  sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"state":"failed"' || return 1
  sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"drop_started":false' || return 1
  sentinel_present .maintenance && return 1
  sentinel_present .destructive && return 1
  sentinel_present .restore_request.json && return 1
  no_swap_dbs
}

restore_failed_for_outstanding_review() {
  restore_failed_before_mutation || return 1
  sc 'grep -Fq "outbound credential review is acknowledged" /backup-trigger/.restore_status.json'
}

restore_done_clean() {
  restore_state_done || return 1
  sentinel_present .maintenance && return 1
  sentinel_present .destructive && return 1
  sentinel_present .restore_request.json && return 1
  no_swap_dbs || return 1
  return 0
}

restore_authority_pending() {
  local restore_id="$1"
  sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"state":"running"' \
    && sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"phase":"database_authority"' \
    && sc "cat /backup-trigger/.restore_status.json 2>/dev/null" | grep -q "\"restore_id\":\"${restore_id}\"" \
    && sc "cat /backup-trigger/.restore_status.json 2>/dev/null" | grep -q "\"source\":\"${LAST_RESTORE_SOURCE}\""
}

restore_done_for_id() {
  local restore_id="$1"
  restore_done_clean \
    && sc 'cat /backup-trigger/.restore_status.json 2>/dev/null' | grep -q '"phase":"finalize"' \
    && sc "cat /backup-trigger/.restore_status.json 2>/dev/null" | grep -q "\"restore_id\":\"${restore_id}\"" \
    && sc "cat /backup-trigger/.restore_status.json 2>/dev/null" | grep -q "\"source\":\"${LAST_RESTORE_SOURCE}\""
}

dump_diagnostics() {
  printf '  --- diagnostics ---\n' >&2
  printf '  databases: %s\n' "$(q postgres "SELECT string_agg(datname, ',' ORDER BY datname) FROM pg_database WHERE datname NOT IN ('template0','template1')")" >&2
  printf '  trigger dir: %s\n' "$(sc 'ls -a /backup-trigger 2>/dev/null | tr "\n" " "')" >&2
  printf '  backups: %s\n' "$(sc 'ls /backups 2>/dev/null | tr "\n" " "')" >&2
  printf '  last_run: %s\n' "$(sc 'cat /backups/.last_run.json 2>/dev/null')" >&2
  printf '  restore_status: %s\n' "$(sc 'cat /backup-trigger/.restore_status.json 2>/dev/null')" >&2
  dc logs --tail 40 postgres postgres-backup litellm vllm 2>&1 | sed 's/^/  log: /' >&2
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

clear_backups() { sc 'cd /backups && rm -f jarvis_* litellm_* pdfs_* secrets_* qdrant_* manifest_* .last_run.json 2>/dev/null; true'; }

clear_numeric_pdfs() {
  sc "find /pdf-storage -regextype posix-extended -mindepth 1 -maxdepth 1 -regex '.*/[0-9]+\\.pdf' -type f -delete"
}
numeric_pdf_fingerprint() {
  sc 'find /pdf-storage -regextype posix-extended -mindepth 1 -maxdepth 1 -regex '\''.*/[0-9]+\.pdf'\'' -type f -printf '\''%f\n'\'' | sort | while IFS= read -r name; do printf "%s  %s  %s\n" "$(sha256sum "/pdf-storage/$name" | cut -d" " -f1)" "$(stat -c%s "/pdf-storage/$name")" "$name"; done'
}
numeric_pdf_count() {
  sc "find /pdf-storage -regextype posix-extended -mindepth 1 -maxdepth 1 -regex '.*/[0-9]+\\.pdf' -type f -printf x" | wc -c | tr -d ' '
}
seed_nonempty_pdfs() {
  clear_numeric_pdfs || return 1
  sc "printf '%s' 'source-pdf-one' > /pdf-storage/101.pdf; printf '%s' 'source-pdf-two-with-more-bytes' > /pdf-storage/202.pdf"
}

seed_jarvis_101() { # <marker_tag> -> rebuild the complete v101 predecessor
  local tag="$1"
  local role
  role="$(fixture_pg_role)"
  dc exec -T postgres psql -U "$role" -d jarvis -v ON_ERROR_STOP=1 -q <<'SQL' || return 1
DROP SCHEMA IF EXISTS platform CASCADE;
DROP SCHEMA IF EXISTS research CASCADE;
DROP SCHEMA IF EXISTS learning CASCADE;
DROP SCHEMA IF EXISTS ops CASCADE;
DROP SCHEMA public CASCADE;
ALTER DATABASE jarvis OWNER TO jarvis;
CREATE SCHEMA public AUTHORIZATION pg_database_owner;
GRANT USAGE ON SCHEMA public TO PUBLIC;
SQL
  # The fresh-install file retains the complete public-schema predecessor before
  # the ownership boundary. Its two folded-version differences are normalized
  # below so this drill starts from the released schema-101 contract, then runs
  # every retained migration through 113.
  awk '/^-- FRESH-INSTALL OWNERSHIP BOUNDARY$/ { exit } { print }' \
    "$REPO_ROOT/db/init.sql" \
    | dc exec -T postgres psql -U "$role" -d jarvis -v ON_ERROR_STOP=1 -q \
      -c 'SET ROLE jarvis' -f - \
    || return 1
  dc exec -T postgres psql -U "$role" -d jarvis -v ON_ERROR_STOP=1 -q \
    -v marker_tag="$tag" -c 'SET ROLE jarvis' -f - <<'SQL'
ALTER TABLE magic_link_tokens ALTER COLUMN user_id SET NOT NULL;
DELETE FROM schema_migrations WHERE version > 101;
CREATE TABLE roundtrip_marker(tag text);
INSERT INTO users(id, email, role)
VALUES (1, 'roundtrip-owner@example.test', 'admin');
INSERT INTO papers(
  id, external_id, source_type, title, authors, url, discovery_origin
) VALUES (
  1,
  'local:aaaaaaaaaaaaaaaa',
  'local',
  'Round-trip predecessor paper',
  ARRAY['Fixture Author'],
  'local://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  'user_initiated'
);
SELECT setval('users_id_seq', 1, true);
SELECT setval('papers_id_seq', 1, true);
INSERT INTO roundtrip_marker(tag) VALUES (:'marker_tag');
SQL
}

seed_auth_restore_contract() {
  dc exec -T postgres psql -U "$(fixture_pg_role)" -d jarvis -v ON_ERROR_STOP=1 -q <<'SQL'
DELETE FROM platform.sessions;
DELETE FROM platform.magic_link_tokens;
DELETE FROM platform.webauthn_challenges;
DELETE FROM platform.telegram_pairing_tokens;
DELETE FROM platform.webauthn_credentials WHERE user_id = 1;
DELETE FROM platform.telegram_user_pairings WHERE user_id = 1;
INSERT INTO platform.webauthn_credentials(
  id, user_id, credential_id, public_key, sign_count, nickname
) VALUES (
  '11111111-1111-1111-1111-111111111111',
  1,
  decode('726f756e64747269702d63726564656e7469616c', 'hex'),
  decode('726f756e64747269702d7075626c69632d6b6579', 'hex'),
  7,
  'roundtrip passkey'
);
INSERT INTO platform.sessions(user_id, credential_id, expires_at)
VALUES (
  1,
  '11111111-1111-1111-1111-111111111111',
  now() + interval '1 day'
);
INSERT INTO platform.magic_link_tokens(token_hash, user_id, expires_at)
VALUES ('roundtrip-ephemeral-link', 1, now() + interval '15 minutes');
INSERT INTO platform.webauthn_challenges(challenge, user_id, purpose, expires_at)
VALUES (
  decode('726f756e64747269702d6368616c6c656e6765', 'hex'),
  1,
  'authentication',
  now() + interval '15 minutes'
);
INSERT INTO platform.telegram_user_pairings(user_id, chat_id)
VALUES (1, 424242);
INSERT INTO platform.telegram_pairing_tokens(token, user_id, expires_at)
VALUES ('roundtrip-ephemeral-pairing', 1, now() + interval '15 minutes');
SQL
}

durable_auth_state_survived() {
  [ "$(q jarvis "SELECT (
    (SELECT count(*) FROM platform.users WHERE id = 1 AND email = 'roundtrip-owner@example.test') = 1
    AND (SELECT count(*) FROM platform.webauthn_credentials WHERE id = '11111111-1111-1111-1111-111111111111' AND user_id = 1) = 1
    AND (SELECT count(*) FROM platform.telegram_user_pairings WHERE user_id = 1 AND chat_id = 424242) = 1
  )")" = "t" ]
}

ephemeral_auth_state_present() {
  [ "$(q jarvis "SELECT (
    (SELECT count(*) FROM platform.sessions WHERE user_id = 1) = 1
    AND (SELECT count(*) FROM platform.webauthn_challenges WHERE user_id = 1) = 1
    AND (SELECT count(*) FROM platform.magic_link_tokens WHERE user_id = 1) = 1
    AND (SELECT count(*) FROM platform.telegram_pairing_tokens WHERE user_id = 1) = 1
  )")" = "t" ]
}

ephemeral_auth_state_purged() {
  [ "$(q jarvis "SELECT (
    (SELECT count(*) FROM platform.sessions) = 0
    AND (SELECT count(*) FROM platform.webauthn_challenges) = 0
    AND (SELECT count(*) FROM platform.magic_link_tokens) = 0
    AND (SELECT count(*) FROM platform.telegram_pairing_tokens) = 0
  )")" = "t" ]
}

set_marker() { # <tag> — change only the test payload, preserving the migrated schema
  dc exec -T postgres psql -U "$(fixture_pg_role)" -d jarvis -v ON_ERROR_STOP=1 -q <<SQL
DELETE FROM public.roundtrip_marker;
INSERT INTO public.roundtrip_marker(tag) VALUES ('$1');
SQL
}

LAST_RESTORE_ID=""
LAST_RESTORE_SOURCE=""
_write_restore_request() {
  local source="$1" ts="$2" allow="$3" extra="" restore_id requested_at json
  case "$source" in local|inbox) ;; *) return 1 ;; esac
  case "$allow" in
    true|false) extra=",\"allow_missing_pdfs\":${allow}" ;;
    omit) ;;
    *) return 1 ;;
  esac
  restore_id="$(openssl rand -hex 16 2>/dev/null)" || return 1
  printf '%s' "$restore_id" | grep -Eq '^[0-9a-f]{32}$' || return 1
  requested_at="$(date -Iseconds)" || return 1
  json="{\"timestamp\":\"${ts}\",\"confirm\":\"RESTORE\",\"source\":\"${source}\"${extra},\"restore_id\":\"${restore_id}\",\"requested_at\":\"${requested_at}\"}"
  LAST_RESTORE_ID="$restore_id"
  LAST_RESTORE_SOURCE="$source"
  # Clear any prior .restore_status.json first: a "state":"done" left by an earlier
  # phase would let restore_done_clean report clean before restore.sh re-enters
  # "running", firing the follow-up assertions against the not-yet-restored DB.
  # Write the request atomically (tmp+mv) so the sidecar never reads a torn sentinel.
  sc 'rm -f /backup-trigger/.restore_status.json'
  printf '%s' "$json" | dc exec -T postgres-backup sh -c \
    'cat > /backup-trigger/.restore_request.json.tmp && mv -f /backup-trigger/.restore_request.json.tmp /backup-trigger/.restore_request.json'
}

write_restore_request() { # <timestamp>
  _write_restore_request local "$1" omit
}

write_inbox_restore_request() { # <timestamp> [true|false|omit]
  _write_restore_request inbox "$1" "${2:-omit}"
}

acknowledge_restore_review() {
  local restore_id="$1"
  printf '%s' "$restore_id" | grep -Eq '^[0-9a-f]{32}$' || return 1
  dc exec -T postgres-backup \
    /usr/local/bin/backup-lifecycle.sh acknowledge-quarantine "$restore_id" \
    >/dev/null 2>&1
}

quarantine_recovery_control_available() {
  local restore_id="${OFF_HOST_RESTORE_ID:-}"
  printf '%s' "$restore_id" | grep -Eq '^[0-9a-f]{32}$' || return 1
  dc exec -T postgres-backup \
    /usr/local/bin/backup-lifecycle.sh inspect-quarantine "$restore_id" \
    >/dev/null 2>&1
}

reset_maint() { sc 'rm -f /backup-trigger/.maintenance /backup-trigger/.destructive /backup-trigger/.restore_status.json /backups/.lifecycle/restore-swap-state.json /backups/.lifecycle/restore-timeout'; }

clear_inbox() {
  sc 'rm -f /restore-inbox/* /restore-inbox/.inbox_manifest.json 2>/dev/null || true'
}
stage_source_operator_key() {
  printf '%s' "$SOURCE_BACKUP_KEY" | dc exec -T postgres-backup sh -c 'cat > /restore-inbox/operator_key'
}

fingerprint_values() {
  local value
  for value in "$@"; do printf '%s' "$value" | sha256sum | cut -d' ' -f1; done
}
host_files_fingerprint() { # <space-separated basenames>
  local files="$1"
  restore_sc "for f in $files; do [ -f \"/host-secrets/\$f\" ] || exit 1; sha256sum \"/host-secrets/\$f\" | cut -d' ' -f1; done"
}
target_local_fingerprint() { host_files_fingerprint "$TARGET_LOCAL_CREDENTIAL_FILES"; }
host_data_key_fingerprint() { host_files_fingerprint "$DATA_KEY_FILES"; }

seed_source_data_keys() {
  restore_sc 'for f in jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt; do
        [ -f "/host-secrets/$f" ] || exit 1
      done'
}

seed_target_host() {
  local file value
  restore_sc 'mkdir -p /host-secrets' || return 1
  for file in $TARGET_LOCAL_CREDENTIAL_FILES; do
    value="target-${file}"
    case "$file" in
      postgres_password.txt) value="$TARGET_PG_PASSWORD" ;;
      postgres_cluster_bootstrap_password.txt) value="$TARGET_BOOTSTRAP_PASSWORD" ;;
      backup_encrypt_key.txt) value="$TARGET_BACKUP_KEY" ;;
      qdrant_api_key.txt) value='target-qdrant-runtime-key' ;;
      litellm_master_key.txt) value="$TARGET_LITELLM_MASTER" ;;
    esac
    printf '%s' "$value" | dc run --rm --no-deps --entrypoint sh postgres-restore -c "cat > /host-secrets/$file" || return 1
    write_fixture_secret "$file" "$value" || return 1
  done
  printf '%s' "$TARGET_CONFIG_KEY" | dc run --rm --no-deps --entrypoint sh postgres-restore -c 'cat > /host-secrets/jarvis_config_key.txt' || return 1
  printf '%s' "$TARGET_MODEL_KEY" | dc run --rm --no-deps --entrypoint sh postgres-restore -c 'cat > /host-secrets/jarvis_model_hmac_key.txt' || return 1
  printf '%s' "$TARGET_LITELLM_SALT" | dc run --rm --no-deps --entrypoint sh postgres-restore -c 'cat > /host-secrets/litellm_salt_key.txt' || return 1
  write_fixture_secret jarvis_config_key.txt "$TARGET_CONFIG_KEY" || return 1
  write_fixture_secret jarvis_model_hmac_key.txt "$TARGET_MODEL_KEY" || return 1
  write_fixture_secret litellm_salt_key.txt "$TARGET_LITELLM_SALT" || return 1
  write_fixture_secret postgres_password "$TARGET_PG_PASSWORD" || return 1
  write_fixture_secret backup_encrypt_key "$TARGET_BACKUP_KEY" || return 1
  write_fixture_secret qdrant_api_key 'target-qdrant-runtime-key' || return 1
  write_fixture_secret postgres_cluster_bootstrap_password.txt "$TARGET_BOOTSTRAP_PASSWORD" || return 1
  printf '%s' "$TARGET_BOOTSTRAP_PASSWORD" | dc run --rm --no-deps --entrypoint sh postgres-restore -c \
    'cat > /host-secrets/postgres_cluster_bootstrap_password.txt' || return 1
  dc exec -T postgres psql -U "$(fixture_pg_role)" -d postgres -v ON_ERROR_STOP=1 -q \
    -c "ALTER ROLE jarvis_cluster_bootstrap WITH PASSWORD '${TARGET_BOOTSTRAP_PASSWORD}';" >/dev/null || return 1
  [ "$(restore_sc 'cat /host-secrets/postgres_cluster_bootstrap_password.txt')" = "$TARGET_BOOTSTRAP_PASSWORD" ]
}

target_pg_auth_is_preserved() {
  dc run --rm --no-deps --entrypoint psql -e PGPASSWORD="$TARGET_BOOTSTRAP_PASSWORD" postgres-restore \
    -h postgres -U jarvis_cluster_bootstrap -d jarvis -tAc 'SELECT 1' 2>/dev/null | grep -qx '1' || return 1
  ! dc run --rm --no-deps --entrypoint psql -e PGPASSWORD="$SOURCE_BOOTSTRAP_PASSWORD" postgres-restore \
    -h postgres -U jarvis_cluster_bootstrap -d jarvis -tAc 'SELECT 1' >/dev/null 2>&1
}

archive_entry_json() { # <filename>
  local name="$1" meta sha size
  meta="$(sc "sha256sum /restore-inbox/$name | cut -d' ' -f1; stat -c%s /restore-inbox/$name")" || return 1
  sha="$(printf '%s\n' "$meta" | sed -n '1p')"
  size="$(printf '%s\n' "$meta" | sed -n '2p')"
  printf '%s' "$sha" | grep -Eq '^[0-9a-f]{64}$' || return 1
  printf '%s' "$size" | grep -Eq '^[0-9]+$' || return 1
  printf '{"filename":"%s","sha256":"%s","size_bytes":%s}' "$name" "$sha" "$size"
}

write_signed_inbox_manifest() { # <timestamp> <current|legacy> <archive basenames...>
  local ts="$1" mode="$2" first=1 entries="" name entry run_id json
  shift 2
  for name in "$@"; do
    entry="$(archive_entry_json "$name")" || return 1
    [ "$first" = "1" ] || entries="${entries},"
    first=0
    entries="${entries}${entry}"
  done
  if [ "$mode" = "legacy" ]; then
    json="{\"timestamp\":\"${ts}\",\"app_version\":\"1.1.3\",\"schema_version\":102,\"created_at\":\"2026-07-21T00:00:00Z\",\"archives\":[${entries}]}"
  else
    run_id="$(printf '%s' "$ts" | sha256sum | cut -c1-32)"
    json="{\"timestamp\":\"${ts}\",\"run_id\":\"${run_id}\",\"app_version\":\"1.2.0\",\"schema_version\":${CURRENT_SCHEMA},\"created_at\":\"2026-07-21T00:00:00Z\",\"archives\":[${entries}]}"
  fi
  printf '%s' "$json" | dc exec -T postgres-backup sh -c "cat > /restore-inbox/manifest_${ts}.json" \
    || return 1
  sc "derived=\$(openssl dgst -sha256 -hmac jarvis-manifest-v1 -r < /restore-inbox/operator_key | cut -d' ' -f1); \
      openssl dgst -sha256 -mac HMAC -macopt hexkey:\$derived -r < /restore-inbox/manifest_${ts}.json \
      | cut -d' ' -f1 > /restore-inbox/manifest_${ts}.json.hmac"
}

copy_source_archive() { # <source timestamp> <destination timestamp> <role> <extension>
  local source_ts="$1" dest_ts="$2" role="$3" ext="$4"
  sc "cp /backups/${role}_${source_ts}.${ext}${SUF} /restore-inbox/${role}_${dest_ts}.${ext}${SUF}"
}

stage_current_source_set() { # <timestamp>
  local ts="$1"
  clear_inbox || return 1
  stage_source_operator_key || return 1
  sc "cp /backups/jarvis_${ts}.sql.gz${SUF} /backups/litellm_${ts}.sql.gz${SUF} \
      /backups/pdfs_${ts}.tar.gz${SUF} /backups/secrets_${ts}.tar.gz${SUF} \
      /backups/manifest_${ts}.json /backups/manifest_${ts}.json.hmac /restore-inbox/"
}

build_bad_secrets_archive() { # <timestamp>
  local ts="$1"
  sc 'rm -rf /tmp/hostile-secrets; mkdir -p /tmp/hostile-secrets; ln -s /etc/hostname /tmp/hostile-secrets/jarvis_config_key.txt; cp /data-keys/jarvis_model_hmac_key.txt /data-keys/litellm_salt_key.txt /tmp/hostile-secrets/; tar -czf /tmp/hostile-secrets.tgz -C /tmp/hostile-secrets jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt' || return 1
  if [ -n "$SUF" ]; then
    sc "openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -kfile /restore-inbox/operator_key -in /tmp/hostile-secrets.tgz -out /restore-inbox/secrets_${ts}.tar.gz.enc; rm -f /tmp/hostile-secrets.tgz"
  else
    sc "mv /tmp/hostile-secrets.tgz /restore-inbox/secrets_${ts}.tar.gz"
  fi
}

build_bad_pdfs_archive() { # <timestamp>
  local ts="$1"
  sc 'rm -rf /tmp/hostile-pdfs; mkdir -p /tmp/hostile-pdfs; ln -s /etc/hostname /tmp/hostile-pdfs/1.pdf; tar -czf /tmp/hostile-pdfs.tgz -C /tmp/hostile-pdfs 1.pdf' || return 1
  if [ -n "$SUF" ]; then
    sc "openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -kfile /restore-inbox/operator_key -in /tmp/hostile-pdfs.tgz -out /restore-inbox/pdfs_${ts}.tar.gz.enc; rm -f /tmp/hostile-pdfs.tgz"
  else
    sc "mv /tmp/hostile-pdfs.tgz /restore-inbox/pdfs_${ts}.tar.gz"
  fi
}

stage_hostile_current_set() { # <source ts> <dest ts> <secrets|pdfs>
  local source_ts="$1" dest_ts="$2" hostile="$3"
  local j="jarvis_${dest_ts}.sql.gz${SUF}" l="litellm_${dest_ts}.sql.gz${SUF}"
  local p="pdfs_${dest_ts}.tar.gz${SUF}" s="secrets_${dest_ts}.tar.gz${SUF}"
  clear_inbox || return 1
  stage_source_operator_key || return 1
  copy_source_archive "$source_ts" "$dest_ts" jarvis sql.gz || return 1
  copy_source_archive "$source_ts" "$dest_ts" litellm sql.gz || return 1
  if [ "$hostile" = "pdfs" ]; then
    build_bad_pdfs_archive "$dest_ts" || return 1
    copy_source_archive "$source_ts" "$dest_ts" secrets tar.gz || return 1
  else
    copy_source_archive "$source_ts" "$dest_ts" pdfs tar.gz || return 1
    build_bad_secrets_archive "$dest_ts" || return 1
  fi
  write_signed_inbox_manifest "$dest_ts" current "$j" "$l" "$p" "$s"
}

stage_cross_version_set() { # <timestamp>
  # The set an upgrade from a maintained earlier release presents: the current schema,
  # which carries a run_id and has since v1.2.0, signed with the derived-key
  # construction releases before v1.2.6 used. No other leg produces that combination,
  # and it is the one an operator's own restore point actually has.
  local ts="$1"
  stage_current_source_set "$ts" || return 1
  sc "derived=\$(openssl dgst -sha256 -hmac jarvis-manifest-v1 -r < /restore-inbox/operator_key | cut -d' ' -f1); \
      openssl dgst -sha256 -mac HMAC -macopt hexkey:\$derived -r < /restore-inbox/manifest_${ts}.json \
      | cut -d' ' -f1 > /restore-inbox/manifest_${ts}.json.hmac"
}

stage_legacy_no_pdf_set() { # <source ts> <dest ts>
  local source_ts="$1" dest_ts="$2"
  local j="jarvis_${dest_ts}.sql.gz${SUF}" l="litellm_${dest_ts}.sql.gz${SUF}"
  local s="secrets_${dest_ts}.tar.gz${SUF}"
  clear_inbox || return 1
  stage_source_operator_key || return 1
  copy_source_archive "$source_ts" "$dest_ts" jarvis sql.gz || return 1
  copy_source_archive "$source_ts" "$dest_ts" litellm sql.gz || return 1
  copy_source_archive "$source_ts" "$dest_ts" secrets tar.gz || return 1
  write_signed_inbox_manifest "$dest_ts" legacy "$j" "$l" "$s"
}

run_hostile_case() { # <source ts> <dest ts> <secrets|pdfs>
  local source_ts="$1" dest_ts="$2" hostile="$3"
  local marker_before creds_before keys_before pdfs_before clean=1
  marker_before="$(marker_tags)"
  creds_before="$(target_local_fingerprint)"
  keys_before="$(host_data_key_fingerprint)"
  pdfs_before="$(numeric_pdf_fingerprint)"
  if ! stage_hostile_current_set "$source_ts" "$dest_ts" "$hostile"; then
    no "could not stage the hostile ${hostile} fixture"
    return 1
  fi
  write_inbox_restore_request "$dest_ts" || { no "could not request the hostile ${hostile} restore"; return 1; }
  if run_restore_request_expect_failure \
      && wait_for 30 "${hostile} archive rejection before database mutation" restore_failed_before_mutation; then
    [ "$(marker_tags)" = "$marker_before" ] || { no "hostile ${hostile}: database marker changed"; clean=0; }
    [ "$(target_local_fingerprint)" = "$creds_before" ] || { no "hostile ${hostile}: target-local credentials changed"; clean=0; }
    [ "$(host_data_key_fingerprint)" = "$keys_before" ] || { no "hostile ${hostile}: data keys changed"; clean=0; }
    [ "$(numeric_pdf_fingerprint)" = "$pdfs_before" ] || { no "hostile ${hostile}: live PDFs changed"; clean=0; }
    target_pg_auth_is_preserved || { no "hostile ${hostile}: target PostgreSQL password changed"; clean=0; }
    [ "$clean" = "1" ] && ok "authenticated hostile ${hostile} archive failed before DB, key, credential, or PDF mutation"
  else
    no "hostile ${hostile} archive did not fail before mutation"
  fi
  reset_maint
}

seed_jarvis_113() { # Construct the supported v1.2.5 predecessor from retained migrations.
  local role
  seed_jarvis_101 "${1:-schema-113-seed}" || return 1
  run_fixture_migrations 113 || return 1
  role="$(fixture_pg_role)"
  [ "$role" = jarvis ] && return 0
  # This snapshot models the predecessor's reader contract. The post-restore
  # sequence deliberately reconstructs all ownership and ACLs from scratch.
  dc exec -T postgres psql -U "$role" -d jarvis -v ON_ERROR_STOP=1 -q -c \
    'GRANT USAGE ON SCHEMA public TO jarvis_backup_reader; GRANT SELECT ON ALL TABLES IN SCHEMA public TO jarvis_backup_reader; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO jarvis_backup_reader;'
}

run_fixture_migrations() { # <maximum schema version; 0 means packaged current>
  local maximum="${1:-0}" role password
  role="$(fixture_pg_role)"
  password="$(fixture_pg_password "$role")"
  local pg_cid
  pg_cid="$(dc ps -q postgres)"
  [ -n "$pg_cid" ] || { echo "  no postgres container for migration run" >&2; return 1; }
  docker run --rm \
    --network "container:${pg_cid}" \
    -v "$REPO_ROOT:/repo:ro" -v "$WORK:/work:ro" \
    -e PGHOST=127.0.0.1 -e PGPORT=5432 -e PGUSER="$role" \
    -e PGPASSWORD="$password" -e PGDATABASE=jarvis \
    -e JARVIS_MIGRATION_MAX_VERSION="$maximum" \
    "${MIGRATION_PY_IMAGE:-python:3.12-slim}" \
    sh -c 'pip install --quiet --disable-pip-version-check asyncpg >/dev/null 2>&1 && python /work/run_migrations_helper.py'
}

grant_fixture_backup_read() {
  dc exec -T postgres psql -U "$(fixture_pg_role)" -d jarvis -v ON_ERROR_STOP=1 -q \
    -c 'GRANT SELECT ON public.roundtrip_marker TO jarvis_backup_reader'
}

advance_fixture_authority() { # Move the supported predecessor through root-owned jobs.
  dc run --rm --no-deps cluster-bootstrap prepare \
    && dc run --rm --no-deps jarvis-migrator \
    && dc run --rm --no-deps litellm-migrator \
    && dc run --rm --no-deps cluster-bootstrap finalize \
    && grant_fixture_backup_read
}

run_restore_authority() { # Complete one accepted request through the production recovery sequence.
  local restore_id="$1"
  dc run --rm --no-deps postgres-restore --recover \
    && dc run --rm --no-deps postgres-restore --run-request \
    && wait_for 30 "restore ${restore_id} to reach database authority" restore_authority_pending "$restore_id" \
    && dc run --rm --no-deps cluster-bootstrap restore-prepare \
    && dc run --rm --no-deps jarvis-migrator \
    && dc run --rm --no-deps litellm-migrator \
    && dc run --rm --no-deps cluster-bootstrap restore-finalize \
    && dc run --rm --no-deps postgres-restore --complete-authority \
    && grant_fixture_backup_read \
    && wait_for 30 "restore ${restore_id} finalization" restore_done_for_id "$restore_id"
}

run_restore_request_expect_failure() {
  dc run --rm --no-deps postgres-restore --recover \
    && ! dc run --rm --no-deps postgres-restore --run-request
}

CURRENT_SCHEMA="$(find "$REPO_ROOT/db/migrations" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9]_*.sql' -printf '%f\n' \
  | sed -E 's/^0*([0-9]+)_.*/\1/' | sort -n | tail -1)"
if ! printf '%s' "$CURRENT_SCHEMA" | grep -Eq '^[0-9]+$'; then
  printf 'FAIL: could not resolve the current migration version\n' >&2
  exit 1
fi

# --- Start the isolated fixture ----------------------------------------------
printf 'archive mode: %s\n' "$ENC_LABEL"
# Seed the supported predecessor before starting any authority or application
# job. Starting the whole graph here races cluster bootstrap against network
# creation and would ask current runtimes to connect before their roles exist.
if ! dc up -d postgres vllm >/dev/null 2>&1; then
  no "fixture stack failed to start"; dc logs --tail 40 2>&1 | sed 's/^/  /' >&2
  printf '\nROUND-TRIP: PASS=%s FAIL=%s\n' "$pass" "$fail"; exit 1
fi
ready=0
stable_checks=0
for _ in $(seq 1 40); do
  if dc exec -T postgres psql -U jarvis -d jarvis -tAc 'SELECT 1' 2>/dev/null \
      | grep -qx '1'; then
    stable_checks=$((stable_checks + 1))
    if [ "$stable_checks" -ge 3 ]; then ready=1; break; fi
  else
    stable_checks=0
  fi
  sleep 2
done
[ "$ready" = "1" ] || { no "postgres did not reach stable readiness"; dump_diagnostics; printf '\nROUND-TRIP: PASS=%s FAIL=%s\n' "$pass" "$fail"; exit 1; }

seed_source_data_keys \
  || { no "could not seed the same-host data-key destination"; exit 1; }

# =============================================================================
# Supported-predecessor restore
# =============================================================================
sec "Supported predecessor 113 advances through root authority"
if seed_jarvis_113 "restore-point" \
   && advance_fixture_authority \
   && [ "$(max_version)" = "$CURRENT_SCHEMA" ] \
   && [ "$(webauthn_present)" = "t" ] \
   && [ "$(q jarvis 'SELECT count(*) FROM platform.telegram_user_pairings WHERE chat_id < 0')" = "0" ] \
   && [ "$(q jarvis "SELECT value #>> '{}' FROM platform.user_config WHERE user_id IS NULL AND key = 'owner.user_id'")" = "1" ] \
   && [ "$(q jarvis "SELECT count(*) FROM platform.audit_log WHERE action = 'owner.backfilled'")" = "1" ]; then
  ok "supported predecessor 113 advanced to schema ${CURRENT_SCHEMA} through bootstrap, migrators, and finalization"
else
  no "supported predecessor 113 did not advance through the integrated authority sequence"
  dump_diagnostics
  printf '\nROUND-TRIP: PASS=%s FAIL=%s\n' "$pass" "$fail"
  exit 1
fi

# An upgraded deployment must resolve unqualified names exactly as a fresh
# install does. Only the bootstrap superuser may store another role's default,
# so the upgrade sequence itself has to issue them.
governed_search_paths="$(roles_with_search_path)"
if [ "$governed_search_paths" = "9" ]; then
  ok "the upgrade sequence stored a search_path default for every governed role"
else
  no "the upgrade sequence left governed roles without a search_path default (${governed_search_paths} of 9)"
fi

# Reconstructed authority must match the fresh-install privilege boundary in
# db/init.sql, not a broader one. These three objects drifted between the two
# and are the boundary a restore most easily weakens: the job facade is
# Platform-only, the append-only audit tables take writes solely through the
# validating capability, and the scheduled-nudge update is the Research-only
# capability a restore previously dropped.
restore_boundary_holds() {
  local reader lister canceller ins upd del subj nudge
  reader=$(q jarvis "SELECT has_function_privilege('jarvis_research_runtime', 'ops.jarvis_job_read_v1(text)', 'EXECUTE')")
  lister=$(q jarvis "SELECT has_function_privilege('jarvis_learning_runtime', 'ops.jarvis_job_list_v1(text,text,text,integer)', 'EXECUTE')")
  canceller=$(q jarvis "SELECT has_function_privilege('jarvis_learning_runtime', 'ops.jarvis_job_cancel_v1(text,text)', 'EXECUTE')")
  ins=$(q jarvis "SELECT has_table_privilege('jarvis_platform_runtime', 'platform.audit_log', 'INSERT')")
  upd=$(q jarvis "SELECT has_table_privilege('jarvis_platform_runtime', 'platform.audit_log', 'UPDATE')")
  del=$(q jarvis "SELECT has_table_privilege('jarvis_platform_runtime', 'platform.audit_subjects', 'DELETE')")
  subj=$(q jarvis "SELECT has_table_privilege('jarvis_platform_runtime', 'platform.audit_subjects', 'UPDATE')")
  nudge=$(q jarvis "SELECT has_function_privilege('jarvis_research_runtime', 'learning.update_scheduled_nudge_v1(integer,boolean,text,boolean,boolean,boolean,jsonb)', 'EXECUTE')")
  local req_ins req_upd req_del ack_ins usr_del usr_upd usr_email cap
  req_ins=$(q jarvis "SELECT has_table_privilege('jarvis_platform_runtime', 'platform.erasure_requests', 'INSERT')")
  req_upd=$(q jarvis "SELECT has_table_privilege('jarvis_platform_runtime', 'platform.erasure_requests', 'UPDATE')")
  req_del=$(q jarvis "SELECT has_table_privilege('jarvis_platform_runtime', 'platform.erasure_requests', 'DELETE')")
  ack_ins=$(q jarvis "SELECT has_table_privilege('jarvis_platform_runtime', 'platform.erasure_acknowledgements', 'INSERT')")
  usr_del=$(q jarvis "SELECT has_table_privilege('jarvis_platform_runtime', 'platform.users', 'DELETE')")
  usr_upd=$(q jarvis "SELECT has_column_privilege('jarvis_platform_runtime', 'platform.users', 'deleted_at', 'UPDATE')")
  usr_email=$(q jarvis "SELECT has_column_privilege('jarvis_platform_runtime', 'platform.users', 'email', 'UPDATE')")
  cap=$(q jarvis "SELECT has_function_privilege('jarvis_platform_runtime', 'platform.resume_erasure_v1(uuid)', 'EXECUTE')")
  [ "$reader" = f ] && [ "$lister" = f ] && [ "$canceller" = f ] \
    && [ "$ins" = f ] && [ "$upd" = f ] && [ "$del" = f ] && [ "$subj" = f ] \
    && [ "$nudge" = t ] \
    && [ "$req_ins" = f ] && [ "$req_upd" = f ] && [ "$req_del" = f ] && [ "$ack_ins" = f ] \
    && [ "$usr_del" = f ] && [ "$usr_upd" = f ] && [ "$usr_email" = t ] && [ "$cap" = t ]
}
if restore_boundary_holds; then
  ok "reconstructed authority matches the fresh-install privilege boundary (job facade, audit tables, erasure tables and the deletion clock are capability-only)"
else
  no "reconstructed authority is broader than a fresh install: a restored deployment would be weaker than db/init.sql builds"
fi
if ! dc up -d --no-deps litellm postgres-backup >/dev/null 2>&1; then
  no "runtime fixture services failed to start after authority reconstruction"
  dump_diagnostics
  printf '\nROUND-TRIP: PASS=%s FAIL=%s\n' "$pass" "$fail"
  exit 1
fi
seed_nonempty_pdfs
if wait_for 120 "direct LiteLLM route to the isolated provider" litellm_chat_works; then
  ok "direct LiteLLM chat reached the isolated provider before restore"
else
  no "direct LiteLLM chat did not reach the isolated provider before restore"
fi

# The scheduled backup starts only after the root sequence grants its reader.
wait_for 60 "sidecar startup backup" sc '[ -f /backups/.last_run.json ]' || true
if seed_auth_restore_contract && durable_auth_state_survived && ephemeral_auth_state_present; then
  ok "backup fixture contains durable identities and all ephemeral authentication records"
else
  no "could not seed the authentication restore contract"
fi
PDF_ROOT_ID="$(sc "stat -c '%d:%i' /pdf-storage")"
PDFS_NONEMPTY_EXPECTED="$(numeric_pdf_fingerprint)"
clear_backups
sc 'touch /backup-trigger/.backup_now'
if wait_for 120 "consistent backup at schema ${CURRENT_SCHEMA}" backup_ready; then
  TS1="$(last_run_ts)"
  if [ "$(manifest_schema "$TS1")" = "$CURRENT_SCHEMA" ]; then
    ok "backup produced both databases, data keys, PDFs, and an authenticated manifest at schema ${CURRENT_SCHEMA}"
  else
    no "backup schema wrong (ts=$TS1 schema=$(manifest_schema "$TS1"))"
  fi
else
  no "backup never reported success"; TS1=""
fi

if [ -n "${TS1:-}" ]; then
  # Mutate live data, then restore the pre-mutation backup.
  dc exec -T postgres psql -U "$(fixture_pg_role)" -d jarvis -v ON_ERROR_STOP=1 -q <<'SQL' >/dev/null
DELETE FROM public.roundtrip_marker;
INSERT INTO public.roundtrip_marker(tag) VALUES ('MUTATED-should-not-survive');
SQL
  clear_numeric_pdfs
  sc "printf '%s' 'target-only-pdf' > /pdf-storage/999.pdf"
  write_restore_request "$TS1"
  if run_restore_authority "$LAST_RESTORE_ID"; then
    fails_before=$fail
    [ "$(marker_tags)" = "restore-point" ] || no "data did not revert (marker='$(marker_tags)')"
    no_swap_dbs || no "restore left a _restore_tmp/_pre_restore database"
    ! sentinel_present .maintenance || no ".maintenance not cleared after a clean restore"
    ! sentinel_present .destructive || no ".destructive not cleared after a clean restore"
    ! manual_step_flagged || no "restore required an unexpected manual operator step"
    durable_auth_state_survived \
      || no "same-schema restore did not preserve the user, passkey, and Telegram pairing"
    ephemeral_auth_state_purged \
      || no "same-schema restore did not invalidate all ephemeral authentication records"
    [ "$(numeric_pdf_fingerprint)" = "$PDFS_NONEMPTY_EXPECTED" ] || no "non-empty PDF set did not restore byte-for-byte"
    [ "$(sc "stat -c '%d:%i' /pdf-storage")" = "$PDF_ROOT_ID" ] || no "restore replaced the stable PDF storage root"
    [ "$fail" = "$fails_before" ] && ok "same-schema restore: DB and non-empty PDFs reverted byte-for-byte, stable root preserved, zero operator step"
  else
    no "same-schema restore did not complete cleanly"
  fi
fi

# =============================================================================
# Empty current PDF archive
# =============================================================================
sec "Empty numeric PDF set restores exactly"
set_marker "empty-pdf-restore-point"
clear_numeric_pdfs
clear_backups
sc 'touch /backup-trigger/.backup_now'
if wait_for 120 "current backup with an empty PDF set" backup_ready; then
  TS_EMPTY="$(last_run_ts)"
  sc "printf '%s' 'must-be-removed' > /pdf-storage/303.pdf"
  write_restore_request "$TS_EMPTY"
  if run_restore_authority "$LAST_RESTORE_ID"; then
    fails_before=$fail
    [ "$(numeric_pdf_count)" = "0" ] || no "empty PDF restore left numeric PDF objects"
    [ "$(marker_tags)" = "empty-pdf-restore-point" ] || no "empty-PDF restore did not revert the database"
    [ "$(sc "stat -c '%d:%i' /pdf-storage")" = "$PDF_ROOT_ID" ] || no "empty-PDF restore replaced the stable storage root"
    [ "$fail" = "$fails_before" ] && ok "empty numeric PDF set restored exactly and kept the stable root"
  else
    no "empty-PDF restore did not complete cleanly"
  fi
else
  no "backup with an empty PDF set never reported success"
fi

# =============================================================================
# Older-schema restore: construct the supported 113 predecessor, advance to the
# packaged schema, restore the 113 backup, and re-run the root recovery sequence.
# =============================================================================
sec "Schema 113 restore and integrated forward migration"
seed_jarvis_113 "older-restore-point"
clear_backups
sc 'touch /backup-trigger/.backup_now'
if wait_for 120 "consistent backup at schema 113" backup_ready; then
  TS2="$(last_run_ts)"
  if [ -n "$TS2" ] && [ "$(manifest_schema "$TS2")" = "113" ]; then
    ok "supported predecessor backup captured at schema 113 (ts=$TS2)"
  else
    no "supported predecessor backup schema wrong (ts=$TS2 schema=$(manifest_schema "$TS2"))"
  fi
else
  no "older backup never reported success"; TS2=""
fi

# Advance the fixture database through the production authority sequence.
if advance_fixture_authority; then
  if [ "$(max_version)" = "$CURRENT_SCHEMA" ] && [ "$(webauthn_present)" = "t" ]; then
    ok "fixture database advanced 113 -> ${CURRENT_SCHEMA} through root jobs"
  else
    no "live advance wrong (max=$(max_version) webauthn=$(webauthn_present))"
  fi
else
  no "root authority sequence failed to advance the fixture database to ${CURRENT_SCHEMA}"
fi

if [ -n "${TS2:-}" ]; then
  write_restore_request "$TS2"
  if run_restore_authority "$LAST_RESTORE_ID"; then
    fails_before=$fail
    [ "$(max_version)" = "$CURRENT_SCHEMA" ] || no "integrated forward migration did not reach ${CURRENT_SCHEMA} (max=$(max_version))"
    [ "$(webauthn_present)" = "t" ] || no "integrated forward migration did not restore WebAuthn tables"
    [ "$(marker_tags)" = "older-restore-point" ] || no "older restore point data missing (marker='$(marker_tags)')"
    no_swap_dbs || no "older restore left a _restore_tmp/_pre_restore database"
    ! manual_step_flagged || no "older restore required an unexpected manual operator step"
    [ "$fail" = "$fails_before" ] && ok "schema 113 restore advanced to ${CURRENT_SCHEMA} through the integrated authority sequence"
  else
    no "older-schema restore did not complete cleanly"
  fi
fi

# =============================================================================
# Compose service mounts
# =============================================================================
sec "Compose services mount the backup-trigger volume"
svc_bt_line() { # print the backup_trigger mount line(s) for service $1; rc1 if none
  awk -v svc="$1" '
    /^  [a-zA-Z0-9_-]+:$/ { cur=$1; sub(":","",cur) }
    cur==svc && /backup_trigger:\/backup-trigger/ { print; f=1 }
    END { exit (f ? 0 : 1) }
  ' "$REPO_ROOT/docker-compose.yml"
}
# paper_ingestion writes backup and restore requests. learning_engine only reads
# maintenance and restore state, so its mount is read-only.
if svc_bt_line paper_ingestion >/dev/null; then
  ok "paper_ingestion mounts the backup_trigger volume"
else
  no "paper_ingestion does not mount backup_trigger in the repo compose"
fi
if svc_bt_line learning_engine | grep -q ':ro'; then
  ok "learning_engine mounts backup_trigger read-only (its watcher reads the same sentinels)"
else
  no "learning_engine does not mount backup_trigger :ro in the repo compose"
fi

# =============================================================================
# Off-host restore preserves target credentials
# =============================================================================
sec "Off-host restore preserves target credentials"
seed_nonempty_pdfs
set_marker "inbox-restore-point"
if seed_auth_restore_contract && durable_auth_state_survived && ephemeral_auth_state_present; then
  ok "off-host source contains durable identities and ephemeral authentication state"
else
  no "could not seed off-host authentication state"
fi
SOURCE_PDFS_EXPECTED="$(numeric_pdf_fingerprint)"
SOURCE_DATA_KEYS_EXPECTED="$(fingerprint_values "$SOURCE_CONFIG_KEY" "$SOURCE_MODEL_KEY" "$SOURCE_LITELLM_SALT")"
clear_backups
sc 'touch /backup-trigger/.backup_now'
if wait_for 120 "consistent backup for the inbox round trip" backup_ready; then
  TS4="$(last_run_ts)"
  secret_members="$(sc "openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -kfile /run/secrets/backup_encrypt_key -in /backups/secrets_${TS4}.tar.gz.enc | tar -tzf - | sort")"
  expected_members="$(printf '%s\n' jarvis_config_key.txt jarvis_model_hmac_key.txt litellm_salt_key.txt | sort)"
  if [ "$secret_members" = "$expected_members" ]; then
    ok "current backup includes PDFs and exactly the three database-coupled data keys"
  else
    no "current data-key archive members are wrong: ${secret_members}"
  fi
else
  no "inbox round-trip backup never reported success"; TS4=""
fi

if [ -n "${TS4:-}" ]; then
  if seed_target_host && target_pg_auth_is_preserved; then
    ok "source and target PostgreSQL credentials are distinct and the target password authenticates"
  else
    no "could not establish the distinct target PostgreSQL credential"
  fi
  TARGET_CREDENTIALS_EXPECTED="$(target_local_fingerprint)"
  TARGET_DATA_KEYS_BEFORE="$(host_data_key_fingerprint)"
  [ "$TARGET_DATA_KEYS_BEFORE" != "$SOURCE_DATA_KEYS_EXPECTED" ] \
    || no "target data-key fixture accidentally matches the source"
  dc exec -T postgres psql -U "$(fixture_pg_role)" -d jarvis -v ON_ERROR_STOP=1 -q <<'SQL' >/dev/null
DELETE FROM public.roundtrip_marker;
INSERT INTO public.roundtrip_marker(tag) VALUES ('TARGET-before-inbox');
SQL
  clear_numeric_pdfs
  sc "printf '%s' 'target-live-pdf' > /pdf-storage/909.pdf"

  run_hostile_case "$TS4" 20000101_000001 secrets
  run_hostile_case "$TS4" 20000101_000002 pdfs

  stage_current_source_set "$TS4"
  write_inbox_restore_request "$TS4"
  OFF_HOST_RESTORE_ID="$LAST_RESTORE_ID"
  if run_restore_authority "$LAST_RESTORE_ID"; then
    fails_before=$fail
    [ "$(marker_tags)" = "inbox-restore-point" ] || no "inbox restore did not revert data (marker='$(marker_tags)')"
    target_pg_auth_is_preserved || no "off-host restore changed the target PostgreSQL password"
    [ "$(target_local_fingerprint)" = "$TARGET_CREDENTIALS_EXPECTED" ] \
      || no "off-host restore changed a target-local service credential"
    [ "$(host_data_key_fingerprint)" = "$SOURCE_DATA_KEYS_EXPECTED" ] \
      || no "off-host restore did not install exactly the source data keys"
    durable_auth_state_survived \
      || no "off-host restore did not preserve the user, passkey, and Telegram pairing"
    ephemeral_auth_state_purged \
      || no "off-host restore did not invalidate all ephemeral authentication records"
    [ "$(numeric_pdf_fingerprint)" = "$SOURCE_PDFS_EXPECTED" ] \
      || no "off-host restore did not install the source PDFs byte-for-byte"
    [ "$(sc "stat -c '%d:%i' /pdf-storage")" = "$PDF_ROOT_ID" ] \
      || no "off-host restore replaced the stable PDF root"
    sc 'cat /backup-trigger/.secrets_rotated 2>/dev/null' | grep -qE '^[0-9]+$' \
      || no ".secrets_rotated is missing or not an integer epoch"
    [ "$fail" = "$fails_before" ] \
      && ok "off-host restore crossed DB/PDF/data keys while preserving target PostgreSQL and target-local credentials"
  else
    no "off-host inbox restore did not complete cleanly"
  fi
  if sentinel_present .outbound-quarantine.json \
      && wait_for 60 "direct LiteLLM route to stop during restore review" litellm_quarantine_blocks_direct_routing; then
    ok "outbound review stopped direct LiteLLM routing without reaching the provider"
  else
    no "direct LiteLLM routing remained available during outbound review"
  fi
  write_inbox_restore_request "$TS4"
  if run_restore_request_expect_failure \
      && wait_for 30 "outstanding restore review to refuse a second request" \
      restore_failed_for_outstanding_review; then
    ok "outstanding restore review refused a second restore before mutation"
  else
    no "outstanding restore review did not refuse a second restore"
  fi
  hits_before_recreate="$(provider_hit_count)"
  recreate_ok=1
  dc up -d --force-recreate litellm postgres-backup >/dev/null 2>&1 \
    || { printf '  recreation diagnostic: Compose startup failed\n' >&2; recreate_ok=0; }
  wait_for 120 "restore review control after service recreation" \
    quarantine_recovery_control_available \
    || { printf '  recreation diagnostic: review control unavailable\n' >&2; recreate_ok=0; }
  litellm_quarantine_blocks_direct_routing \
    || { printf '  recreation diagnostic: provider route accepted traffic\n' >&2; recreate_ok=0; }
  [ "$(provider_hit_count)" = "$hits_before_recreate" ] \
    || { printf '  recreation diagnostic: provider received a request\n' >&2; recreate_ok=0; }
  if [ "$recreate_ok" = "1" ]; then
    ok "service recreation preserved recovery control without reaching the provider"
  else
    no "service recreation blocked recovery control or reached the provider"
  fi
  if acknowledge_restore_review "$OFF_HOST_RESTORE_ID"; then
    ok "exact restore review was acknowledged before the next off-host restore"
  else
    no "could not acknowledge the completed off-host restore review"
  fi
  if ! sentinel_present .outbound-quarantine.json \
      && wait_for 120 "direct LiteLLM route after exact review acknowledgement" \
        litellm_chat_works "$TARGET_LITELLM_MASTER"; then
    ok "direct LiteLLM routing resumed after the exact review acknowledgement"
  else
    no "direct LiteLLM routing did not resume after the exact review acknowledgement"
  fi
fi

# =============================================================================
# A backup set signed by an earlier release still authenticates and restores
# =============================================================================
sec "Cross-version restore accepts a set signed before v1.2.6"
if [ -n "${TS4:-}" ]; then
  # The compatibility signature is computed with Digest::SHA. If a future image drops
  # it the module load fails, stderr is discarded, and every earlier-release manifest
  # silently stops authenticating — so assert it in the image rather than on the host.
  if sc 'perl -MDigest::SHA=hmac_sha256_hex -e1' >/dev/null 2>&1; then
    ok "the backup image provides the module the compatibility signature needs"
  else
    no "the backup image has no Digest::SHA; earlier-release manifests cannot authenticate"
  fi
  set_marker "cross-version-target-before"
  stage_cross_version_set "$TS4"
  write_inbox_restore_request "$TS4"
  CROSS_RESTORE_ID="$LAST_RESTORE_ID"
  if run_restore_authority "$LAST_RESTORE_ID"; then
    fails_before=$fail
    [ "$(marker_tags)" = "inbox-restore-point" ] \
      || no "earlier-release restore did not revert data (marker='$(marker_tags)')"
    [ "$(numeric_pdf_fingerprint)" = "$SOURCE_PDFS_EXPECTED" ] \
      || no "earlier-release restore did not install the source PDFs byte-for-byte"
    target_pg_auth_is_preserved \
      || no "earlier-release restore changed the target PostgreSQL password"
    [ "$fail" = "$fails_before" ] \
      && ok "a manifest signed before v1.2.6 authenticated and restored its set"
  else
    no "a manifest signed before v1.2.6 did not restore"
  fi
  if acknowledge_restore_review "$CROSS_RESTORE_ID"; then
    ok "cross-version restore review was acknowledged by exact restore ID"
  else
    no "could not acknowledge the cross-version restore review"
  fi
fi

# =============================================================================
# A signed pre-v1.2 backup without PDFs needs explicit acknowledgement
# =============================================================================
sec "Legacy no-PDF restore is explicit and produces an empty numeric set"
if [ -n "${TS4:-}" ]; then
  LEGACY_TS=20000101_000003
  set_marker "legacy-live-before"
  clear_numeric_pdfs
  sc "printf '%s' 'legacy-live-pdf-must-survive-refusal' > /pdf-storage/707.pdf"
  legacy_pdfs_before="$(numeric_pdf_fingerprint)"
  stage_legacy_no_pdf_set "$TS4" "$LEGACY_TS"
  write_inbox_restore_request "$LEGACY_TS"
  if run_restore_request_expect_failure \
      && wait_for 30 "legacy no-PDF restore refusal without acknowledgement" restore_failed_before_mutation; then
    fails_before=$fail
    [ "$(marker_tags)" = "legacy-live-before" ] || no "unacknowledged legacy restore changed the database"
    [ "$(numeric_pdf_fingerprint)" = "$legacy_pdfs_before" ] || no "unacknowledged legacy restore changed PDFs"
    target_pg_auth_is_preserved || no "unacknowledged legacy restore changed PostgreSQL authentication"
    [ "$(target_local_fingerprint)" = "$TARGET_CREDENTIALS_EXPECTED" ] || no "unacknowledged legacy restore changed target credentials"
    [ "$fail" = "$fails_before" ] && ok "signed pre-v1.2 backup without PDFs was refused before mutation by default"
  else
    no "signed pre-v1.2 backup without PDFs was not refused before mutation"
  fi
  reset_maint

  stage_legacy_no_pdf_set "$TS4" "$LEGACY_TS"
  write_inbox_restore_request "$LEGACY_TS" true
  LEGACY_RESTORE_ID="$LAST_RESTORE_ID"
  if run_restore_authority "$LAST_RESTORE_ID"; then
    fails_before=$fail
    [ "$(marker_tags)" = "inbox-restore-point" ] || no "acknowledged legacy restore did not restore the source database"
    [ "$(numeric_pdf_count)" = "0" ] || no "acknowledged legacy no-PDF restore did not produce an empty numeric set"
    [ "$(sc "stat -c '%d:%i' /pdf-storage")" = "$PDF_ROOT_ID" ] || no "legacy no-PDF restore replaced the stable PDF root"
    target_pg_auth_is_preserved || no "legacy restore changed the target PostgreSQL password"
    [ "$(target_local_fingerprint)" = "$TARGET_CREDENTIALS_EXPECTED" ] || no "legacy restore changed target-local credentials"
    [ "$(host_data_key_fingerprint)" = "$SOURCE_DATA_KEYS_EXPECTED" ] || no "legacy restore did not retain the restored source data keys"
    [ "$fail" = "$fails_before" ] && ok "allow_missing_pdfs=true restored the signed legacy set and emptied numeric PDFs"
  else
    no "acknowledged legacy no-PDF restore did not complete cleanly"
  fi
  if acknowledge_restore_review "$LEGACY_RESTORE_ID"; then
    ok "legacy restore review was acknowledged by exact restore ID"
  else
    no "could not acknowledge the legacy restore review"
  fi
else
  no "legacy no-PDF check requires an off-host source backup"
fi

# =============================================================================
# --recover rejects an injected database name and preserves evidence
# =============================================================================
sec "--recover rejects an injected swap-state database name"
if [ -n "${TS4:-}" ]; then
  # Place a SQL-injection payload in the private swap-state database field. The
  # database-name allowlist must reject it before recovery issues SQL and must
  # preserve the malformed state as evidence.
  inj='{"db":"x'"'"'; DROP DATABASE jarvis; --","phase":"swapping_out"}'
  printf '%s' "$inj" | dc exec -T postgres-backup sh -c 'mkdir -p /backups/.lifecycle; cat > /backups/.lifecycle/restore-swap-state.json'
  before_marker="$(marker_tags)"
  dc run --rm --no-deps postgres-restore --recover >/dev/null 2>&1 || true
  jarvis_ok=1
  q jarvis "SELECT 1" >/dev/null 2>&1 || { no "jarvis DB no longer exists after --recover (injection dropped it)"; jarvis_ok=0; }
  [ "$(marker_tags)" = "$before_marker" ] || { no "jarvis data changed after --recover (marker='$(marker_tags)')"; jarvis_ok=0; }
  private_state_present restore-swap-state.json || { no "--recover discarded malformed recovery evidence"; jarvis_ok=0; }
  [ "$jarvis_ok" = "1" ] && ok "--recover rejected the injected db name, preserved evidence, and left jarvis intact"
  sc 'rm -f /backups/.lifecycle/restore-swap-state.json'
else
  no "recovery injection check requires an off-host source backup"
fi

# =============================================================================
printf '\n================================================================\n'
printf 'RESTORE ROUND-TRIP: PASS=%s  FAIL=%s\n' "$pass" "$fail"
printf '================================================================\n'
[ "$fail" -eq 0 ] || exit 1
