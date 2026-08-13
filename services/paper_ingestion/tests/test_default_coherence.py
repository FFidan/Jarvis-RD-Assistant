"""Model-role default coherence + structured-output enforcement-wiring guards.

A single static/import unit test (no network, no DB) that fails the instant a
model-role default ships incoherently across its sources of truth, or the M1
structured-output enforcement is silently reverted. This is the unit-level guard
for the original ``PULSE_STAGE2_MODEL=fast`` class of incoherence.

Two source layouts coexist intentionally:

* ``smart`` / ``fast`` — the compose defaults are deliberately EMPTY
  (``${VAR:-}``); the boot reconciler applies ``main.py``'s static fallback when
  the env is absent. So coherence for these is ``.env.example value ==
  main.py static fallback``, NOT a compose-default comparison.
* ``pulse_stage2`` / ``embed`` — all three layers (config field default,
  ``.env.example``, compose default) agree on a literal value.
"""

from __future__ import annotations

import re
from pathlib import Path

import instructor
import yaml

from jarvis_common.app_factory import STRUCTURED_DECODING_MODE
from paper_ingestion.config import PaperIngestionSettings
from paper_ingestion.litellm_reconciler import _LITELLM_ROLE_FALLBACKS

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
LITELLM_CONFIG = REPO_ROOT / "litellm" / "config.yaml"
COMPOSE = REPO_ROOT / "docker-compose.yml"

_COMPOSE_DEFAULT = re.compile(r"^\$\{[A-Z0-9_]+:-(?P<default>.*)\}$")


def _env_example() -> dict[str, str]:
    """Parse ``.env.example`` into a flat key->value map (first ``=`` splits)."""
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _compose_shared_env() -> dict[str, str]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["x-shared-env"]


def _compose_default(value: str) -> str:
    """Extract the ``default`` from a ``${VAR:-default}`` compose interpolation."""
    match = _COMPOSE_DEFAULT.match(value)
    assert match is not None, f"not a ${{VAR:-default}} interpolation: {value!r}"
    return match.group("default")


# ---------------------------------------------------------------------------
# Coherence — the four role triples
# ---------------------------------------------------------------------------


def test_smart_role_default_coherent() -> None:
    """``.env.example`` smart value == main.py static fallback == ``qwen3:8b``."""
    static_default = _LITELLM_ROLE_FALLBACKS["llm.smart_model"][1]
    assert static_default == "qwen3:8b"
    assert _env_example()["JARVIS_SMART_MODEL"] == static_default


def test_fast_role_default_coherent() -> None:
    """``.env.example`` fast value == main.py static fallback == ``qwen3:4b``."""
    static_default = _LITELLM_ROLE_FALLBACKS["llm.fast_model"][1]
    assert static_default == "qwen3:4b"
    assert _env_example()["JARVIS_FAST_MODEL"] == static_default


def test_pulse_stage2_default_coherent() -> None:
    """config field default == ``.env.example`` == compose default == ``smart``."""
    field_default = PaperIngestionSettings.model_fields["pulse_stage2_model"].default
    compose_default = _compose_default(_compose_shared_env()["PULSE_STAGE2_MODEL"])
    assert field_default == "smart"
    assert _env_example()["PULSE_STAGE2_MODEL"] == field_default
    assert compose_default == field_default


def test_embed_default_coherent() -> None:
    """config field default == ``.env.example`` == compose default == ``embed``."""
    field_default = PaperIngestionSettings.model_fields["embedding_model"].default
    compose_default = _compose_default(_compose_shared_env()["EMBEDDING_MODEL"])
    assert field_default == "embed"
    assert _env_example()["EMBEDDING_MODEL"] == field_default
    assert compose_default == field_default


# ---------------------------------------------------------------------------
# Enforcement wiring — structured-output guards (M1)
# ---------------------------------------------------------------------------


def test_e1_structured_decoding_mode_is_json_schema() -> None:
    assert STRUCTURED_DECODING_MODE == "JSON_SCHEMA"


def test_e2_mode_resolves_to_instructor_json_schema() -> None:
    assert instructor.Mode[STRUCTURED_DECODING_MODE] is instructor.Mode.JSON_SCHEMA


def test_e3_litellm_drops_unsupported_params() -> None:
    config = yaml.safe_load(LITELLM_CONFIG.read_text(encoding="utf-8"))
    assert config["litellm_settings"]["drop_params"] is True


def test_e4_no_per_model_response_format_override() -> None:
    """A per-model ``response_format`` would let LiteLLM strip it from requests."""
    config = yaml.safe_load(LITELLM_CONFIG.read_text(encoding="utf-8"))
    for entry in config["model_list"]:
        params = entry["litellm_params"]
        assert "response_format" not in params, (
            f"model {entry['model_name']!r} sets response_format; "
            "this lets LiteLLM strip structured-output enforcement"
        )
