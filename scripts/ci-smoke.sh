#!/usr/bin/env bash
# Opt-in Docker smoke: boot core services with disposable secrets and probe health.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-jarvis_rd_smoke_$RANDOM}"
OVERRIDE_FILE="$TMP_DIR/docker-compose.smoke-secrets.yml"

# Use a non-default host port for Ollama so the smoke stack can coexist with
# any other Ollama instance (e.g. claude-context-local) bound to 11434.
export OLLAMA_HOST_PORT="${OLLAMA_HOST_PORT:-11440}"

cleanup() {
  COMPOSE_PROJECT_NAME="$PROJECT_NAME" docker compose \
    -f "$ROOT_DIR/docker-compose.yml" \
    --env-file "$ROOT_DIR/versions.env" \
    -f "$OVERRIDE_FILE" \
    down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

printf '%s\n' "smoke-postgres-password" > "$TMP_DIR/postgres_password.txt"
printf '%s\n' "smoke-jarvis-api-key" > "$TMP_DIR/jarvis_api_key.txt"
printf '%s\n' "smoke-telegram-token" > "$TMP_DIR/telegram_bot_token.txt"
printf '%s\n' "smoke-qdrant-api-key" > "$TMP_DIR/qdrant_api_key.txt"
printf '%s\n' "test-litellm-master-key" > "$TMP_DIR/litellm_master_key.txt"
printf '%s\n' "$(openssl rand -base64 32)" > "$TMP_DIR/jarvis_config_key.txt"
# Optional-profile secrets — must be declared so compose does not reject them
# when observability / tunnel / backup profiles are activated in CI.
printf '%s\n' "smoke-langfuse-pg-password" > "$TMP_DIR/langfuse_pg_password.txt"
printf '%s\n' "smoke-langfuse-nextauth-secret" > "$TMP_DIR/langfuse_nextauth_secret.txt"
printf '%s\n' "smoke-langfuse-salt" > "$TMP_DIR/langfuse_salt.txt"
printf '%s\n' "smoke-cloudflare-tunnel-token" > "$TMP_DIR/cloudflare_tunnel_token.txt"
printf '%s\n' "smoke-backup-encrypt-key" > "$TMP_DIR/backup_encrypt_key.txt"
printf '%s\n' "$(openssl rand -hex 32)" > "$TMP_DIR/jarvis_model_hmac_key.txt"
printf '%s\n' "smoke-langfuse-pk" > "$TMP_DIR/langfuse_init_pk.txt"
printf '%s\n' "smoke-langfuse-sk" > "$TMP_DIR/langfuse_init_sk.txt"

cat > "$OVERRIDE_FILE" <<YAML
secrets:
  postgres_password:
    file: $TMP_DIR/postgres_password.txt
  jarvis_api_key:
    file: $TMP_DIR/jarvis_api_key.txt
  telegram_bot_token:
    file: $TMP_DIR/telegram_bot_token.txt
  qdrant_api_key:
    file: $TMP_DIR/qdrant_api_key.txt
  litellm_master_key:
    file: $TMP_DIR/litellm_master_key.txt
  jarvis_config_key:
    file: $TMP_DIR/jarvis_config_key.txt
  langfuse_pg_password:
    file: $TMP_DIR/langfuse_pg_password.txt
  langfuse_nextauth_secret:
    file: $TMP_DIR/langfuse_nextauth_secret.txt
  langfuse_salt:
    file: $TMP_DIR/langfuse_salt.txt
  cloudflare_tunnel_token:
    file: $TMP_DIR/cloudflare_tunnel_token.txt
  backup_encrypt_key:
    file: $TMP_DIR/backup_encrypt_key.txt
  jarvis_model_hmac_key:
    file: $TMP_DIR/jarvis_model_hmac_key.txt
  langfuse_init_pk:
    file: $TMP_DIR/langfuse_init_pk.txt
  langfuse_init_sk:
    file: $TMP_DIR/langfuse_init_sk.txt
YAML

compose() {
  COMPOSE_PROJECT_NAME="$PROJECT_NAME" docker compose \
    -f "$ROOT_DIR/docker-compose.yml" \
    --env-file "$ROOT_DIR/versions.env" \
    -f "$OVERRIDE_FILE" \
    "$@"
}

probe() {
  local name="$1"
  local url="$2"
  local curl_flags=("-fsS" "-o" "/dev/null")
  if [[ "$url" == https://* ]]; then
    curl_flags=("-k" "${curl_flags[@]}")
  fi
  if ! curl "${curl_flags[@]}" "$url"; then
    echo "FAIL $name: $url" >&2
    compose logs --tail=80 "$name" >&2 || true
    exit 1
  fi
  echo "OK $name: $url"
}

assert_route_bodies() {
  local service="$1"
  shift

  compose exec -T "$service" python - "$service" "$@" <<'PY'
import sys

if sys.argv[1] == "paper_ingestion":
    from paper_ingestion.main import app
elif sys.argv[1] == "learning_engine":
    from learning_engine.main import app
else:
    raise SystemExit(f"unknown service {sys.argv[1]!r}")

schema = app.openapi()
failures = []
for operation_spec in sys.argv[2:]:
    method, path = operation_spec.split(" ", 1)
    operation = schema["paths"][path][method.lower()]
    if "requestBody" not in operation:
        failures.append(f"{method} {path}: missing requestBody")
    for parameter in operation.get("parameters", []):
        if parameter.get("in") == "query" and parameter.get("name") == "body":
            failures.append(f"{method} {path}: exposes query parameter named body")

if failures:
    raise SystemExit("\n".join(failures))

print(f"OK {sys.argv[1]} route body contracts")
PY
}

probe_search_preview_body_validation() {
  compose exec -T paper_ingestion python - <<'PY'
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

api_key = Path("/run/secrets/jarvis_api_key").read_text(encoding="utf-8").strip()
payload = json.dumps(
    {
        "query": "route body regression probe",
        "source_types": [],
        "max_results": 5,
    }
).encode("utf-8")
request = Request(
    "http://127.0.0.1:8000/api/search-preview",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "X-API-Key": api_key,
    },
    method="POST",
)

try:
    with urlopen(request, timeout=10) as response:
        body = response.read()
        status = response.status
except HTTPError as exc:
    body = exc.read()
    status = exc.code

try:
    parsed = json.loads(body.decode("utf-8"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"search-preview returned non-JSON status={status}: {body!r}") from exc

def has_query_body_location(value):
    if isinstance(value, dict):
        loc = value.get("loc")
        if loc == ["query", "body"] or loc == ("query", "body"):
            return True
        return any(has_query_body_location(child) for child in value.values())
    if isinstance(value, list):
        return any(has_query_body_location(child) for child in value)
    return False

if has_query_body_location(parsed):
    raise SystemExit(f"search-preview regressed to query body validation: {parsed!r}")
if status >= 500:
    raise SystemExit(f"search-preview probe returned server error {status}: {parsed!r}")

print(f"OK paper_ingestion search-preview body validation status={status}")
PY
}

echo "=== Boot smoke: starting core services ==="
compose up -d --wait --timeout 180 \
  postgres qdrant ollama litellm paper_ingestion learning_engine dashboard

echo "=== Boot smoke: probing health endpoints ==="
probe "paper_ingestion" "http://127.0.0.1:${PAPER_INGESTION_HOST_PORT:-8010}/health"
probe "learning_engine" "http://127.0.0.1:${LEARNING_ENGINE_HOST_PORT:-8011}/health"
probe "dashboard" "http://127.0.0.1:${DASHBOARD_HOST_PORT:-3001}/"

echo "=== Boot smoke: checking Docker route-body contracts ==="
assert_route_bodies paper_ingestion \
  "POST /api/search" \
  "POST /api/search-preview" \
  "POST /api/papers/search-hybrid" \
  "POST /api/discover" \
  "POST /api/analytics/fetch-and-process" \
  "POST /api/contradictions/scan" \
  "POST /api/papers/{paper_id}/contradictions/scan" \
  "POST /api/pulse/rate"
assert_route_bodies learning_engine \
  "POST /api/generate" \
  "POST /api/generate/batch"

echo "=== Boot smoke: probing live route-body validation ==="
probe_search_preview_body_validation

echo "=== Boot smoke: PASS ==="
