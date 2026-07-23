#!/usr/bin/env bash
# Real-Qdrant release gate for the persisted public-or-library visibility model.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

readonly OWNER_LABEL="dev.jarvis.release-gate"
readonly OWNER_VALUE="corpus-visibility-qdrant"
readonly QDRANT_IMAGE="qdrant/qdrant:v1.13.2@sha256:81bdf0a9deedbeec68eed207145ade0b9d5db15e2f84069180711aa9698445b1"
readonly JUNIT_REPORT="${JARVIS_QDRANT_JUNIT_REPORT:-corpus-visibility-qdrant.junit.xml}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for command_name in docker curl uv sha256sum; do
  command -v "$command_name" >/dev/null 2>&1 \
    || fail "required command is unavailable: ${command_name}"
done

[[ "${JARVIS_RUN_LIVE_PG:-}" == "1" ]] \
  || fail "JARVIS_RUN_LIVE_PG=1 is required; this release gate never skips"

fixture_seed="${BASHPID}-${RANDOM}-$(date +%s%N)"
fixture_suffix="$(printf '%s' "$fixture_seed" | sha256sum | cut -c1-16)"
readonly FIXTURE_NAME="jarvis-qdrant-${fixture_suffix}"
readonly COLLECTION_NAME="$FIXTURE_NAME"

docker container inspect "$FIXTURE_NAME" >/dev/null 2>&1 \
  && fail "generated fixture already exists: ${FIXTURE_NAME}"

fixture_created=0
cleanup() {
  local rc=$?
  local actual_owner=""
  trap - EXIT INT TERM

  if [[ "$fixture_created" -eq 1 ]] \
    && docker container inspect "$FIXTURE_NAME" >/dev/null 2>&1; then
    actual_owner="$(
      docker container inspect \
        --format '{{ index .Config.Labels "dev.jarvis.release-gate" }}' \
        "$FIXTURE_NAME"
    )"
    if [[ "$actual_owner" != "$OWNER_VALUE" ]]; then
      printf 'ERROR: refusing cleanup of unowned container %s\n' "$FIXTURE_NAME" >&2
      rc=1
    elif docker rm -f "$FIXTURE_NAME" >/dev/null; then
      printf 'cleanup: removed owned fixture %s\n' "$FIXTURE_NAME"
    else
      printf 'ERROR: failed to remove owned fixture %s\n' "$FIXTURE_NAME" >&2
      rc=1
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

printf 'fixture container: %s\n' "$FIXTURE_NAME"
printf 'fixture collection: %s\n' "$COLLECTION_NAME"

docker run --detach \
  --name "$FIXTURE_NAME" \
  --label "${OWNER_LABEL}=${OWNER_VALUE}" \
  --publish 127.0.0.1::6333 \
  "$QDRANT_IMAGE" >/dev/null
fixture_created=1

port_binding="$(docker port "$FIXTURE_NAME" 6333/tcp)"
qdrant_port="${port_binding##*:}"
[[ "$qdrant_port" =~ ^[0-9]+$ ]] \
  || fail "could not resolve the generated Qdrant port: ${port_binding}"
readonly QDRANT_URL="http://127.0.0.1:${qdrant_port}"

ready=0
for _ in $(seq 1 60); do
  if curl -fsS "${QDRANT_URL}/readyz" >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" -eq 1 ]] || fail "Qdrant fixture did not become ready"

set +e
JARVIS_TEST_QDRANT_URL="$QDRANT_URL" \
JARVIS_TEST_QDRANT_COLLECTION="$COLLECTION_NAME" \
uv run pytest \
  -c pyproject.toml \
  --override-ini="addopts=--import-mode=importlib" \
  -m "contract and live_qdrant" \
  services/paper_ingestion/tests/contract/test_visibility_agreement_contract.py::test_live_qdrant_visibility_and_reconciliation_agree \
  --junitxml="$JUNIT_REPORT" \
  -v
pytest_rc=$?

uv run python scripts/check-pytest-junit.py \
  "$JUNIT_REPORT" \
  --label "corpus visibility Qdrant"
validator_rc=$?
set -e

if [[ "$pytest_rc" -ne 0 || "$validator_rc" -ne 0 ]]; then
  fail "live Qdrant gate failed (pytest=${pytest_rc}, validator=${validator_rc})"
fi

printf 'corpus visibility Qdrant gate: PASS\n'
