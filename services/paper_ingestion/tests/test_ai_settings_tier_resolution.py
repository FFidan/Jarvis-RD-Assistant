"""Unit tests for hardware-tier candidate resolution against the real catalog.

No existing mock-unit test exercises resolve_candidates_for_tier /
catalog_recommendation_for_tier directly — the only coverage lives in the
live-PG settings contract test. These run with the bundled catalog +
config/llm-tier-candidates.yaml so a tier with no selectable model (a
dead-end backend) fails here without a database.
"""

from __future__ import annotations

import pytest

from paper_ingestion.services.ai_settings import (
    catalog_recommendation_for_tier,
    find_candidate_config_path,
    resolve_candidates_for_tier,
)

_TIERS = ("cpu", "lt-8", "8-16", "16-24", "24-48", "ge-48")


@pytest.mark.parametrize("tier", _TIERS)
def test_every_tier_yields_at_least_one_ollama_candidate(tier: str) -> None:
    """Each hardware tier resolves to ≥1 selectable Ollama candidate (no dead-end)."""
    selection = resolve_candidates_for_tier(tier, config_path=find_candidate_config_path())
    ollama = [c for c in selection.candidates if c["backend"] == "ollama"]
    assert ollama, f"{tier} has no selectable Ollama candidate"


@pytest.mark.parametrize("tier", _TIERS)
def test_no_tier_drops_a_candidate_as_missing_from_catalog(tier: str) -> None:
    """The YAML-referenced Ollama models are all present in the curated catalog."""
    selection = resolve_candidates_for_tier(tier, config_path=find_candidate_config_path())
    missing = [i for i in selection.issues if "not in the curated model catalog" in i]
    assert not missing, f"{tier} dropped candidates absent from the catalog: {missing}"


def test_recommended_models_are_catalog_backed() -> None:
    """The four bench-recommended Ollama models resolve to catalog-backed candidates."""
    expected = {
        "cpu": "qwen3:1.7b",
        "lt-8": "qwen3:1.7b",
        "8-16": "qwen2.5:7b-instruct",
    }
    config_path = find_candidate_config_path()
    for tier, model in expected.items():
        selection = resolve_candidates_for_tier(tier, config_path=config_path)
        top = selection.recommended
        assert top["backend"] == "ollama"
        assert top["model"] == model, f"{tier} top candidate should be {model}; got {top['model']}"
        assert top["catalog_id"] == model
        assert top["source"] == "catalog"

    # deepseek-r1:7b is the rank-2 Ollama candidate at the 8-16 tier.
    eight_to_sixteen = resolve_candidates_for_tier("8-16", config_path=config_path)
    assert any(c["model"] == "deepseek-r1:7b" for c in eight_to_sixteen.candidates)


@pytest.mark.parametrize("tier", _TIERS)
def test_catalog_fallback_is_always_available_for_every_tier(tier: str) -> None:
    """catalog_recommendation_for_tier always returns an assignable Ollama smart model."""
    fallback = catalog_recommendation_for_tier(tier)
    assert fallback["backend"] == "ollama"
    assert fallback["catalog_id"]
    assert fallback["source"] == "catalog"
