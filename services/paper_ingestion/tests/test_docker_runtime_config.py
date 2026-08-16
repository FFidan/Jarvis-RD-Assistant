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

    assert shared_env["PULSE_STAGE2_MODEL"] == "${PULSE_STAGE2_MODEL:-smart}"
    assert shared_env["PULSE_STAGE2_MAX_RETRIES"] == "${PULSE_STAGE2_MAX_RETRIES:-1}"


def test_litellm_db_backed_model_management_wiring() -> None:
    """litellm must enable DB-backed /model/* endpoints without leaking secrets.

    STORE_MODEL_IN_DB is a plain toggle and may live in compose env;
    DATABASE_URL must NOT (the shim builds it from the runtime password
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
    assert depends_on["litellm-migrator"]["condition"] == "service_completed_successfully"

    assert compose["secrets"]["litellm_salt_key"]["file"] == "./secrets/litellm_salt_key.txt"


def test_litellm_healthcheck_preserves_recovery_startup_and_liveliness() -> None:
    """LiteLLM health must allow restore review and ignore database readiness.

    With a configured DATABASE_URL, /health/readiness returns 503 whenever
    PostgreSQL is unreachable. During restore review, the intentionally paused
    provider listener must still allow recovery-dependent services to start.
    """
    compose = _load_compose()
    healthcheck = compose["services"]["litellm"]["healthcheck"]["test"]
    entrypoint = (REPO_ROOT / "scripts" / "litellm-entrypoint.sh").read_text(encoding="utf-8")
    healthcheck_body = entrypoint.split("litellm_healthcheck() {", 1)[1].split("\n}", 1)[0]

    assert healthcheck == [
        "CMD",
        "sh",
        "/usr/local/bin/litellm-entrypoint.sh",
        "--healthcheck",
    ]
    assert healthcheck_body.index("litellm_restore_hold_active") < healthcheck_body.index(
        "/health/liveliness"
    )
    assert "/health/readiness" not in healthcheck_body


def test_litellm_migrator_uses_dedicated_database_authority() -> None:
    """The one-shot LiteLLM migrator uses its own database credential.

    Cluster bootstrap creates the database and login before this job runs. The
    runtime service waits for this job and never receives its password.
    """
    compose = _load_compose()
    migrator = compose["services"]["litellm-migrator"]

    assert migrator["restart"] == "no"
    assert migrator["depends_on"]["cluster-bootstrap"]["condition"] == (
        "service_completed_successfully"
    )
    assert migrator["environment"] == {
        "POSTGRES_USER": "jarvis_litellm_migrator",
        "POSTGRES_PASSWORD_FILE": "/run/secrets/litellm_migrator_password",
    }
    assert migrator["secrets"] == ["litellm_migrator_password"]
    assert migrator["command"] == ["--migrate"]
    assert "litellm_migrator_password" not in compose["services"]["litellm"]["secrets"]


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
