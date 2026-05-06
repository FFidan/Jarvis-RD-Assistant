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
