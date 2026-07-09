"""Unit tests for hardware-tier candidate resolution against the real catalog.

No existing mock-unit test exercises resolve_candidates_for_tier /
catalog_recommendation_for_tier directly — the only coverage lives in the
live-PG settings contract test. These run with the bundled catalog +
config/llm-tier-candidates.yaml so a tier with no selectable model (a
dead-end backend) fails here without a database.
"""

from __future__ import annotations

import re
from pathlib import Path

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
        "16-24": "qwen2.5:7b-instruct",
        "24-48": "qwen3:14b",
        "ge-48": "qwen3:30b-a3b",
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


def test_24_48_tier_recommends_a_fitting_model_over_the_7b_baseline() -> None:
    """The 24-48 tier recommends qwen3:14b (fits the whole range) over the 7B baseline.

    qwen3:14b mirrors the hardware_fit mid-high advisory and fits a 24 GB card
    alongside the embedder; qwen3:30b-a3b stays the ge-48 headline. The lighter
    7B pick remains selectable.
    """
    selection = resolve_candidates_for_tier("24-48", config_path=find_candidate_config_path())
    assert selection.recommended["backend"] == "ollama"
    assert selection.recommended["model"] == "qwen3:14b"
    assert selection.recommended["source"] == "catalog"
    # The lighter conservative pick stays selectable, just not the headline.
    assert any(c["model"] == "qwen2.5:7b-instruct" for c in selection.candidates)


def test_selection_exposes_generated_at_date_not_the_doc_path() -> None:
    """CandidateSelection surfaces the YAML generated_at date; generated_from stays the doc path."""
    selection = resolve_candidates_for_tier("24-48", config_path=find_candidate_config_path())
    assert selection.generated_at == "2026-05-22"
    assert selection.generated_from == "docs/perf/2026-05-22-llm-tier-bench.md"


def test_pending_model_refresh_candidates_do_not_replace_ranked_defaults() -> None:
    """Refresh candidates stay selectable or visible without promoting defaults."""
    config_path = find_candidate_config_path()

    mid_tier = resolve_candidates_for_tier("16-24", config_path=config_path)
    assert mid_tier.recommended["backend"] == "ollama"
    assert mid_tier.recommended["model"] == "qwen2.5:7b-instruct"
    optional_mid_vllm = next(
        c for c in mid_tier.candidates if c["model"] == "Qwen/Qwen2.5-7B-Instruct-AWQ"
    )
    assert optional_mid_vllm["backend"] == "vllm"
    assert optional_mid_vllm["evidence"] == "sim-bench"
    assert all(c["model"] != "gpt-oss:20b" for c in mid_tier.candidates)

    high_tier = resolve_candidates_for_tier("ge-48", config_path=config_path)
    assert high_tier.recommended["backend"] == "ollama"
    assert high_tier.recommended["model"] == "qwen3:30b-a3b"
    qwen_refresh = next(
        c for c in high_tier.candidates if c["model"] == "Qwen/Qwen3-30B-A3B-Instruct-2507"
    )
    assert qwen_refresh["backend"] == "vllm"
    assert qwen_refresh["catalog_id"] is None
    assert qwen_refresh["source"] == "tier-candidates"
    assert qwen_refresh["evidence"] == "pending-bench"

    assert all(c["model"] != "openai/gpt-oss-20b" for c in high_tier.candidates)


@pytest.mark.parametrize("tier", _TIERS)
def test_catalog_fallback_is_always_available_for_every_tier(tier: str) -> None:
    """catalog_recommendation_for_tier always returns an assignable Ollama smart model."""
    fallback = catalog_recommendation_for_tier(tier)
    assert fallback["backend"] == "ollama"
    assert fallback["catalog_id"]
    assert fallback["source"] == "catalog"


def test_shipped_model_selection_text_has_no_private_process_residue() -> None:
    """Public model-selection text must stay free of implementation diary terms."""
    repo_root = Path(__file__).resolve().parents[3]
    files = [
        find_candidate_config_path(),
        repo_root / "libs/jarvis_common/jarvis_common/data/model_catalog.json",
    ]
    forbidden_terms = (
        "think" + "-strip",
        "streaming" + "-strip",
        "internal " + "Paper",
        "docs/" + "exec",
        "." + "worktrees",
        "work" + "tree",
        "agent" + "-process",
        "Live" + "-deployment validated",
    )

    for path in files:
        text = path.read_text(encoding="utf-8")
        for term in forbidden_terms:
            assert term not in text, f"{path} contains private process term {term!r}"
        assert not re.search(r"commit [0-9a-f]{7,40}", text), f"{path} contains commit diary text"
