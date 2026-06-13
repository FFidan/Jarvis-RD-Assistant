"""Docker runtime configuration contracts for paper ingestion dependencies."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_paper_ingestion_persists_huggingface_cache() -> None:
    """The HF cache must survive paper_ingestion container recreation."""
    compose = _load_compose()

    volumes = compose["services"]["paper_ingestion"]["volumes"]

    assert "hf_cache:/tmp/hf_cache" in volumes
    assert "hf_cache" in compose["volumes"]


def test_ollama_max_loaded_models_is_env_configurable() -> None:
    """docker-compose.yml must honor the documented OLLAMA_MAX_LOADED_MODELS knob."""
    compose = _load_compose()

    environment = compose["services"]["ollama"]["environment"]

    assert "OLLAMA_MAX_LOADED_MODELS=${OLLAMA_MAX_LOADED_MODELS:-3}" in environment


def test_paper_ingestion_receives_pulse_stage2_runtime_knobs() -> None:
    """Compose must pass documented Pulse speed/quality knobs to paper_ingestion."""
    compose = _load_compose()

    shared_env = compose["x-shared-env"]

    assert shared_env["PULSE_STAGE2_MODEL"] == "${PULSE_STAGE2_MODEL:-fast}"
    assert shared_env["PULSE_STAGE2_MAX_RETRIES"] == "${PULSE_STAGE2_MAX_RETRIES:-1}"


def test_litellm_db_backed_model_management_wiring() -> None:
    """litellm must enable DB-backed /model/* endpoints without leaking secrets.

    STORE_MODEL_IN_DB is a plain toggle and may live in compose env;
    DATABASE_URL must NOT (the shim builds it from the postgres_password
    secret). The :ro config mount stays — the YAML remains the bootstrap seed
    for the embed model and router defaults.
    """
    compose = _load_compose()
    litellm = compose["services"]["litellm"]

    assert litellm["environment"]["STORE_MODEL_IN_DB"] == "true"
    assert "DATABASE_URL" not in litellm["environment"]
    assert "./litellm/config.yaml:/app/config.yaml:ro" in litellm["volumes"]

    depends_on = litellm["depends_on"]
    assert depends_on["ollama"]["condition"] == "service_healthy"
    assert depends_on["postgres"]["condition"] == "service_healthy"
    assert depends_on["litellm-db-init"]["condition"] == "service_completed_successfully"

    assert compose["secrets"]["litellm_salt_key"]["file"] == "./secrets/litellm_salt_key.txt"


def test_litellm_healthcheck_probes_liveliness_not_readiness() -> None:
    """litellm's healthcheck must ignore DB state.

    With a configured DATABASE_URL, /health/readiness returns 503 whenever
    postgres is unreachable — a postgres blip would mark litellm unhealthy and
    deadlock every service gating on ``litellm: service_healthy``.
    """
    compose = _load_compose()
    probe = compose["services"]["litellm"]["healthcheck"]["test"][1]

    assert "/health/liveliness" in probe
    assert "/health/readiness" not in probe


def test_litellm_db_init_creates_database_idempotently() -> None:
    """One-shot litellm-db-init covers EXISTING installs (initdb never re-runs).

    It must use the postgres image (the litellm image ships no psql/createdb),
    read the password from the Docker Secret, and exit successfully when the
    database already exists.
    """
    compose = _load_compose()
    db_init = compose["services"]["litellm-db-init"]

    assert db_init["image"] == "${POSTGRES_IMAGE:-postgres:16.8}"
    assert db_init["restart"] == "no"
    assert db_init["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "postgres_password" in db_init["secrets"]

    command = db_init["command"][0]
    assert "$(cat /run/secrets/postgres_password)" in command
    assert "pg_database WHERE datname = 'litellm'" in command
    assert "createdb" in command


def test_llm_alias_consumers_wait_for_paper_ingestion() -> None:
    """learning_engine and telegram_bot must wait for paper_ingestion health.

    The ``smart``/``fast`` aliases exist only after paper_ingestion's model
    bootstrap has seeded litellm's DB; starting earlier would 404 queued LLM
    jobs. paper_ingestion itself must not depend back on either consumer
    (dependency cycle).
    """
    compose = _load_compose()
    services = compose["services"]

    for consumer in ("learning_engine", "telegram_bot"):
        assert (
            services[consumer]["depends_on"]["paper_ingestion"]["condition"] == "service_healthy"
        ), f"{consumer} must gate on paper_ingestion health"

    paper_ingestion_deps = services["paper_ingestion"]["depends_on"]
    assert "learning_engine" not in paper_ingestion_deps
    assert "telegram_bot" not in paper_ingestion_deps
