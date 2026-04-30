"""Tests for M11 — Pulse weight clamping in load_profile().

Verifies that negative weights are clamped to 0.0 and a WARNING is emitted.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from paper_ingestion.pulse.profile import load_profile


@pytest.mark.asyncio
async def test_load_profile_clamps_negative_weights(caplog):
    """load_profile must clamp negative weights to 0.0 and log exactly one WARNING."""
    # Phase 1 connection: topics, authors, engaged papers
    phase1_conn = AsyncMock()
    phase1_conn.fetch.side_effect = [
        [],  # topics
        [],  # tracked_authors
        [],  # engaged papers for centroid
    ]

    # Phase 3 connection: config (with a negative weight) + ratings + Phase-A extras
    phase3_conn = AsyncMock()
    phase3_conn.fetch.side_effect = [
        # user_config rows: pulse.weights has recency = -0.5
        [
            {
                "key": "pulse.weights",
                "value": {
                    "recency": -0.5,
                    "topic_match": 0.5,
                    "embedding": 0.2,
                    "topic": 0.2,
                    "llm_relevance": 0.3,
                    "llm_novelty": 0.1,
                    "author_bonus": 0.15,
                    "citation_pagerank": 0.0,
                    "citation_count": 0.0,
                    "citation_adamic_adar": 0.0,
                    "classifier": 0.0,
                },
            },
            {"key": "pulse.deck_size", "value": 10},
            {"key": "pulse.stage2_top_k", "value": 50},
        ],
        [],  # positive ratings
        [],  # negative ratings
        [],  # L1 negative topics
        [],  # L1 negative authors
        [],  # L3 dampened topics
        [],  # L2 negative abstracts
    ]

    pool = MagicMock()
    acquire_ctx_1 = MagicMock()
    acquire_ctx_1.__aenter__ = AsyncMock(return_value=phase1_conn)
    acquire_ctx_1.__aexit__ = AsyncMock(return_value=False)
    acquire_ctx_2 = MagicMock()
    acquire_ctx_2.__aenter__ = AsyncMock(return_value=phase3_conn)
    acquire_ctx_2.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.side_effect = [acquire_ctx_1, acquire_ctx_2]

    mock_embedder = AsyncMock()
    mock_embedder.embed_texts.return_value = []

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.profile"):
        profile = await load_profile(pool, embedder=mock_embedder)

    # recency was -0.5 → must be clamped to 0.0
    assert profile.weights["recency"] >= 0.0, (
        f"Expected recency >= 0.0 after clamping, got {profile.weights['recency']}"
    )

    # All weights must be non-negative
    for key, val in profile.weights.items():
        assert val >= 0.0, f"Weight '{key}' is negative ({val}) after clamping"

    # Exactly one WARNING must have been logged
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "clamped" in r.message
    ]
    assert len(warnings) >= 1, (
        "Expected at least one WARNING about negative weights being clamped; "
        f"got caplog records: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_load_profile_no_clamp_warning_when_weights_positive(caplog):
    """No WARNING is logged when all weights are already non-negative."""
    phase1_conn = AsyncMock()
    phase1_conn.fetch.side_effect = [[], [], []]

    phase3_conn = AsyncMock()
    phase3_conn.fetch.side_effect = [
        [
            {
                "key": "pulse.weights",
                "value": {
                    "recency": 0.05,
                    "embedding": 0.2,
                    "topic": 0.2,
                    "llm_relevance": 0.3,
                    "llm_novelty": 0.1,
                    "author_bonus": 0.15,
                    "citation_pagerank": 0.0,
                    "citation_count": 0.0,
                    "citation_adamic_adar": 0.0,
                    "classifier": 0.0,
                },
            },
        ],
        [],  # positive ratings
        [],  # negative ratings
        [],  # L1 negative topics
        [],  # L1 negative authors
        [],  # L3 dampened topics
        [],  # L2 negative abstracts
    ]

    pool = MagicMock()
    ctx1 = MagicMock()
    ctx1.__aenter__ = AsyncMock(return_value=phase1_conn)
    ctx1.__aexit__ = AsyncMock(return_value=False)
    ctx2 = MagicMock()
    ctx2.__aenter__ = AsyncMock(return_value=phase3_conn)
    ctx2.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.side_effect = [ctx1, ctx2]

    mock_embedder = AsyncMock()
    mock_embedder.embed_texts.return_value = []

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.profile"):
        profile = await load_profile(pool, embedder=mock_embedder)

    clamp_warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "clamped" in r.message
    ]
    assert len(clamp_warnings) == 0, "No clamp WARNING should be emitted when all weights are >= 0"
    assert profile.weights["recency"] == 0.05


def _make_pool_with_weights(weights: dict) -> MagicMock:
    """Helper: build a mock db_pool whose phase-3 connection returns the given weights."""
    phase1_conn = AsyncMock()
    phase1_conn.fetch.side_effect = [[], [], []]

    phase3_conn = AsyncMock()
    phase3_conn.fetch.side_effect = [
        [
            {"key": "pulse.weights", "value": weights},
            {"key": "pulse.deck_size", "value": 10},
            {"key": "pulse.stage2_top_k", "value": 50},
        ],
        [],  # positive ratings
        [],  # negative ratings
        [],  # L1 negative topics
        [],  # L1 negative authors
        [],  # L3 dampened topics
        [],  # L2 negative abstracts
    ]

    pool = MagicMock()
    ctx1 = MagicMock()
    ctx1.__aenter__ = AsyncMock(return_value=phase1_conn)
    ctx1.__aexit__ = AsyncMock(return_value=False)
    ctx2 = MagicMock()
    ctx2.__aenter__ = AsyncMock(return_value=phase3_conn)
    ctx2.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.side_effect = [ctx1, ctx2]
    return pool


@pytest.mark.asyncio
async def test_load_profile_clamps_upper_bound(caplog):
    """H3: load_profile must clamp weights > 1.0 down to 1.0."""
    pool = _make_pool_with_weights(
        {
            "embedding": 10.0,  # grossly over-budget
            "topic": 0.2,
            "llm_relevance": 0.3,
            "llm_novelty": 0.1,
            "author_bonus": 0.15,
            "recency": 0.05,
            "citation_pagerank": 0.0,
            "citation_count": 0.0,
            "citation_adamic_adar": 0.0,
            "classifier": 0.0,
        }
    )
    mock_embedder = AsyncMock()
    mock_embedder.embed_texts.return_value = []

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.profile"):
        profile = await load_profile(pool, embedder=mock_embedder)

    assert profile.weights["embedding"] == 1.0, (
        f"Expected embedding clamped to 1.0, got {profile.weights['embedding']}"
    )
    for key, val in profile.weights.items():
        assert val <= 1.0, f"Weight '{key}' exceeds 1.0 ({val}) after clamping"


@pytest.mark.asyncio
async def test_load_profile_warns_on_out_of_range_weights(caplog):
    """H3: exactly one WARNING is emitted (not one per key) when any weight is > 1.0."""
    pool = _make_pool_with_weights(
        {
            "embedding": 10.0,
            "topic": 0.2,
            "llm_relevance": 0.3,
            "llm_novelty": 0.1,
            "author_bonus": 0.15,
            "recency": 0.05,
            "citation_pagerank": 0.0,
            "citation_count": 0.0,
            "citation_adamic_adar": 0.0,
            "classifier": 0.0,
        }
    )
    mock_embedder = AsyncMock()
    mock_embedder.embed_texts.return_value = []

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.profile"):
        await load_profile(pool, embedder=mock_embedder)

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "clamped" in r.message
    ]
    assert len(warnings) == 1, (
        f"Expected exactly 1 WARNING about out-of-range weights, got {len(warnings)}: "
        f"{[r.message for r in warnings]}"
    )
