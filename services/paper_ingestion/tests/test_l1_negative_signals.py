"""Unit tests for L1 negative_topics and negative_authors signals in load_profile().

Covers:
- SQL correctness: negative signal filter, 90-day window, LIMIT 10
- NULL-safe user_id scoping (IS NOT DISTINCT FROM)
- Signal exclusivity: 'negative' not 'positive'
- Empty result path: returns empty lists
"""

from unittest.mock import AsyncMock

import pytest
from paper_ingestion.pulse.profile import load_profile
from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Shared side_effect builders
# ---------------------------------------------------------------------------

_CONFIG_ROWS = [
    FakeRecord(
        {
            "key": "pulse.weights",
            "value": {
                "embedding": 0.2,
                "topic": 0.2,
                "llm_relevance": 0.3,
                "llm_novelty": 0.1,
                "author_bonus": 0.15,
                "recency": 0.05,
            },
        }
    ),
    FakeRecord({"key": "pulse.deck_size", "value": 10}),
    FakeRecord({"key": "pulse.stage2_top_k", "value": 50}),
]

# conn.fetch call indices (0-based) for the 10 sequential fetches inside load_profile():
#   0: topics
#   1: tracked_authors
#   2: engaged papers
#   3: user_config
#   4: positive ratings
#   5: negative ratings
#   6: L1 negative topics   ← under test
#   7: L1 negative authors  ← under test
#   8: L3 dampened topics
#   9: L2 negative abstracts
_IDX_NEG_TOPICS = 6
_IDX_NEG_AUTHORS = 7


def _base_side_effect(
    neg_topic_rows: list = [],  # noqa: B006
    neg_author_rows: list = [],  # noqa: B006
) -> list:
    """Return a full 10-element side_effect list with the given negative rows."""
    return [
        [],  # 0 topics
        [],  # 1 tracked_authors
        [],  # 2 engaged papers
        _CONFIG_ROWS,  # 3 user_config
        [],  # 4 positive ratings
        [],  # 5 negative ratings
        list(neg_topic_rows),  # 6 L1 negative topics
        list(neg_author_rows),  # 7 L1 negative authors
        [],  # 8 L3 dampened topics
        [],  # 9 L2 negative abstracts
    ]


# ---------------------------------------------------------------------------
# Test 1: negative topics SQL — recommendation_feedback, signal='negative',
#         LIMIT 10, 90-day window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_topics_query_returns_top_10_by_neg_count_in_90d():
    """SQL for L1 negative topics references recommendation_feedback, filters on
    signal = 'negative', enforces a 90-day window, and caps results at LIMIT 10.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = _base_side_effect(
        neg_topic_rows=[FakeRecord({"name": "Reinforcement Learning", "neg_count": 5})],
    )
    mock_embedder = AsyncMock()

    await load_profile(pool, embedder=mock_embedder)

    # Retrieve the SQL sent to the 7th fetch call (index 6)
    sql: str = conn.fetch.await_args_list[_IDX_NEG_TOPICS].args[0]

    assert "recommendation_feedback" in sql, (
        f"Expected 'recommendation_feedback' in SQL; got:\n{sql}"
    )
    assert "signal = 'negative'" in sql, f"Expected \"signal = 'negative'\" in SQL; got:\n{sql}"
    assert "LIMIT 10" in sql, f"Expected 'LIMIT 10' in SQL; got:\n{sql}"
    assert "90 days" in sql, f"Expected '90 days' window predicate in SQL; got:\n{sql}"


# ---------------------------------------------------------------------------
# Test 2: negative authors SQL — same structural requirements as topics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_authors_query_returns_top_10_by_neg_count_in_90d():
    """SQL for L1 negative authors references recommendation_feedback, filters on
    signal = 'negative', enforces a 90-day window, and caps results at LIMIT 10.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = _base_side_effect(
        neg_author_rows=[FakeRecord({"author": "Tedious Researcher", "neg_count": 3})],
    )
    mock_embedder = AsyncMock()

    await load_profile(pool, embedder=mock_embedder)

    sql: str = conn.fetch.await_args_list[_IDX_NEG_AUTHORS].args[0]

    assert "recommendation_feedback" in sql, (
        f"Expected 'recommendation_feedback' in SQL; got:\n{sql}"
    )
    assert "signal = 'negative'" in sql, f"Expected \"signal = 'negative'\" in SQL; got:\n{sql}"
    assert "LIMIT 10" in sql, f"Expected 'LIMIT 10' in SQL; got:\n{sql}"
    assert "90 days" in sql, f"Expected '90 days' window predicate in SQL; got:\n{sql}"


# ---------------------------------------------------------------------------
# Test 3: negative topics SQL must NOT filter on signal = 'positive'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_topics_excludes_positive_signal():
    """The L1 negative-topics query must not contain the 'positive' signal literal.

    A query that also matches 'positive' feedback would incorrectly inflate the
    penalty list with topics the user actually enjoys.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = _base_side_effect()
    mock_embedder = AsyncMock()

    await load_profile(pool, embedder=mock_embedder)

    sql: str = conn.fetch.await_args_list[_IDX_NEG_TOPICS].args[0]

    assert "signal = 'positive'" not in sql, (
        f"Negative-topics SQL must NOT filter on 'positive'; got:\n{sql}"
    )
    # Confirm the correct filter is still present
    assert "signal = 'negative'" in sql, f"Expected \"signal = 'negative'\" in SQL; got:\n{sql}"


# ---------------------------------------------------------------------------
# Test 4: both negative queries use IS NOT DISTINCT FROM for NULL-safe user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_topics_user_id_scoping_is_not_distinct_from():
    """Both L1 negative-signal queries must scope by user_id using IS NOT DISTINCT FROM.

    This NULL-safe comparison ensures that when user_id is None (system mode)
    all ratings are returned, while a non-None user_id limits results to that user.
    A plain '= $1' would exclude NULL user_id rows, breaking single-tenant mode.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = _base_side_effect()
    mock_embedder = AsyncMock()

    await load_profile(pool, embedder=mock_embedder, user_id=42)

    topics_sql: str = conn.fetch.await_args_list[_IDX_NEG_TOPICS].args[0]
    authors_sql: str = conn.fetch.await_args_list[_IDX_NEG_AUTHORS].args[0]

    assert "IS NOT DISTINCT FROM" in topics_sql, (
        f"Expected 'IS NOT DISTINCT FROM' in negative-topics SQL; got:\n{topics_sql}"
    )
    assert "IS NOT DISTINCT FROM" in authors_sql, (
        f"Expected 'IS NOT DISTINCT FROM' in negative-authors SQL; got:\n{authors_sql}"
    )


# ---------------------------------------------------------------------------
# Test 5: empty DB rows → empty negative_topics and negative_authors lists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_signals_empty_returns_empty_lists():
    """When no negative-feedback rows exist, negative_topics and negative_authors
    are both empty lists — not None, not omitted.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = _base_side_effect(
        neg_topic_rows=[],  # explicit empty for clarity
        neg_author_rows=[],
    )
    mock_embedder = AsyncMock()

    profile = await load_profile(pool, embedder=mock_embedder)

    assert profile.negative_topics == [], (
        f"Expected negative_topics == [] when DB returns empty; got {profile.negative_topics!r}"
    )
    assert profile.negative_authors == [], (
        f"Expected negative_authors == [] when DB returns empty; got {profile.negative_authors!r}"
    )
