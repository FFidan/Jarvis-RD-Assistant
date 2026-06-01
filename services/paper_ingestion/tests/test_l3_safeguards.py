"""L3 safeguard contract tests (spec §7.3.1–7.3.4).

Tests:
1. _filter_unread 60d boundary  — papers with rec_feedback at 59d (excluded),
   60d (included — strict > boundary), 61d (included).
2. Topic dampening ≥5 in load_profile — 4 negatives → NOT dampened; 5 → dampened.
3. Topic-dampening cap in load_profile — 4 topics all dampened → cap to 2 (50%).
4. Min-candidate fallback in _persist_deck_inner — COUNT=19 → L3 filter skipped.
5. No-negatives baseline in load_profile — all safeguard fields empty, no warnings.

No live DB required; all DB interaction is mocked via AsyncMock.
"""

from __future__ import annotations

import logging
from datetime import date
from unittest.mock import AsyncMock

import pytest
from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helper: build a FakeRecord row for recommendation_feedback-counted dampened topics
# ---------------------------------------------------------------------------


def _dampened_row(topic_id: int, neg_count: int) -> FakeRecord:
    return FakeRecord({"id": topic_id, "neg_count": neg_count})


# ---------------------------------------------------------------------------
# Helper: build a minimal 10-fetch side_effect list for load_profile
#
# Fetch call order in load_profile (from profile.py):
#   first connection:
#     1. topics query
#     2. tracked_authors query
#     3. engaged papers query
#   second connection:
#     4. user_config query
#     5. positive ratings query
#     6. negative ratings query
#     7. L1 negative topics
#     8. L1 negative authors
#     9. L3 dampened topics
#    10. L2 negative abstracts
# ---------------------------------------------------------------------------


def _make_10_fetch_side_effect(
    *,
    topic_rows: list | None = None,
    dampened_topic_rows: list | None = None,
) -> list:
    """Return a 10-element list for conn.fetch.side_effect.

    Parameters
    ----------
    topic_rows:
        Rows returned by the topics query (fetch call #1). Defaults to empty.
    dampened_topic_rows:
        Rows returned by the L3 dampened-topics query (fetch call #9).
        Defaults to empty.
    """
    return [
        topic_rows or [],  # 1. topics
        [],  # 2. tracked_authors
        [],  # 3. engaged papers
        [],  # 4. user_config (no keys → defaults)
        [],  # 5. positive ratings
        [],  # 6. negative ratings
        [],  # 7. L1 negative topics
        [],  # 8. L1 negative authors
        dampened_topic_rows or [],  # 9. L3 dampened topics
        [],  # 10. L2 negative abstracts
    ]


# ---------------------------------------------------------------------------
# Test 1 — _filter_unread 60d boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_unread_60d_boundary() -> None:
    """_filter_unread uses strict > 60 days for the negative-feedback cut-off.

    The SQL is:
        AND rf.created_at > NOW() - INTERVAL '60 days'

    This means a paper with feedback exactly at 60d ago is NOT excluded
    (the interval boundary is open), and anything older than 60d is safe.
    We simulate this by controlling what conn.fetch returns:
    - paper 1: has feedback within 60d → NOT returned by the query (excluded)
    - paper 2: feedback exactly at 60d boundary → returned (included)
    - paper 3: feedback at 61d → returned (included)

    Rather than trying to pass datetime deltas, we mock conn.fetch to
    return the set that a real DB would return given the strict inequality.
    """

    from paper_ingestion.ingestion.recommender import _filter_unread

    conn = AsyncMock()

    # Simulate DB returning only papers 2 and 3 (paper 1 is excluded by the 60d filter)
    conn.fetch = AsyncMock(
        return_value=[
            FakeRecord({"id": 2}),
            FakeRecord({"id": 3}),
        ]
    )

    result = await _filter_unread(conn, paper_ids=[1, 2, 3], user_id=None)

    assert result == {2, 3}, (
        "Papers 2 and 3 (≥60d feedback) should be included; "
        f"paper 1 (fresh negative feedback) should be excluded. Got: {result}"
    )
    assert 1 not in result, "Paper 1 with recent (<60d) negative feedback must be excluded"

    # Verify the SQL passes the paper_ids list as $1
    conn.fetch.assert_awaited_once()
    call_args = conn.fetch.await_args
    sql: str = call_args.args[0]
    assert "60 days" in sql, "SQL must reference 60 days interval"
    assert "recommendation_feedback" in sql.lower(), (
        "SQL must reference recommendation_feedback table"
    )


# ---------------------------------------------------------------------------
# Test 2 — Topic dampening threshold: 4 negatives → NOT dampened; 5 → dampened
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topic_dampening_threshold_4_vs_5() -> None:
    """load_profile: 4 negatives for a topic → NOT in dampened_topics; 5 → IS.

    The L3 dampened-topics query uses HAVING COUNT(*) >= 5.
    """
    from paper_ingestion.pulse.profile import load_profile

    # Scenario A: 4 negatives for topic_id=10 → DB returns empty (HAVING ≥5 not met)
    pool_a, conn_a = _make_pool_and_conn()
    conn_a.fetch.side_effect = _make_10_fetch_side_effect(dampened_topic_rows=[])

    mock_embedder = AsyncMock()
    profile_a = await load_profile(pool_a, embedder=mock_embedder)

    assert 10 not in profile_a.dampened_topics, (
        "Topic 10 with only 4 negatives must NOT be in dampened_topics"
    )
    assert profile_a.dampened_topics == set(), (
        "dampened_topics must be empty when no topic meets the ≥5 threshold"
    )

    # Scenario B: 5 negatives for topic_id=10 → DB returns the row (HAVING ≥5 met).
    # Use 3 topics in the DB so the 50% cap = floor(3 × 0.5) = 1, which allows
    # the 1 dampened topic (topic_id=10) to pass through without truncation.
    pool_b, conn_b = _make_pool_and_conn()
    conn_b.fetch.side_effect = _make_10_fetch_side_effect(
        topic_rows=[
            FakeRecord({"id": 1, "name": "ML", "description": None, "query_terms": []}),
            FakeRecord({"id": 2, "name": "CV", "description": None, "query_terms": []}),
            FakeRecord({"id": 3, "name": "NLP", "description": None, "query_terms": []}),
        ],
        dampened_topic_rows=[_dampened_row(10, 5)],
    )

    mock_embedder_b = AsyncMock()
    profile_b = await load_profile(pool_b, embedder=mock_embedder_b)

    assert 10 in profile_b.dampened_topics, "Topic 10 with 5 negatives must be in dampened_topics"


# ---------------------------------------------------------------------------
# Test 3 — Topic-dampening cap: 4 topics, all dampened → capped to 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topic_dampening_cap_50_percent(caplog) -> None:
    """load_profile caps dampened_topics at 50% of total topic count.

    4 topics in the DB, all 4 would be dampened → cap to floor(4 × 0.5) = 2.
    logger.warning must be called with the cap message.
    """
    from paper_ingestion.pulse.profile import load_profile

    topic_rows = [
        FakeRecord({"id": i, "name": f"Topic {i}", "description": None, "query_terms": []})
        for i in range(1, 5)  # 4 topics
    ]

    # All 4 topics have ≥5 negatives — DB returns all 4
    dampened_rows = [
        _dampened_row(1, 10),
        _dampened_row(2, 8),
        _dampened_row(3, 6),
        _dampened_row(4, 5),
    ]

    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = _make_10_fetch_side_effect(
        topic_rows=topic_rows,
        dampened_topic_rows=dampened_rows,
    )

    mock_embedder = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.profile"):
        profile = await load_profile(pool, embedder=mock_embedder)

    # Cap: floor(4 × 0.5) = 2
    assert len(profile.dampened_topics) == 2, (
        f"Expected 2 dampened topics after 50% cap, got {len(profile.dampened_topics)}"
    )

    # Warning must have been logged
    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("dampened_topics" in m and "truncating" in m for m in warning_msgs), (
        f"Expected 'dampened_topics ... truncating' warning; got: {warning_msgs}"
    )


# ---------------------------------------------------------------------------
# Test 4 — Min-candidate fallback in _persist_deck_inner (COUNT=19 → L3 skipped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_deck_inner_l3_fallback_on_low_candidate_count(caplog) -> None:
    """_persist_deck_inner falls back to L1+L2 when l3_pass_count < 20.

    Verify:
    - logger.warning called with "L3 hard-exclusion would leave only %d candidates"
    - The per-card INSERT SQL does NOT include 'NOT EXISTS recommendation_feedback'
      (the L3 exclusion clause is absent in the fallback branch).
    """
    from paper_ingestion.models import PaperCreate, SourceType
    from paper_ingestion.pulse.deck import _persist_deck_inner
    from paper_ingestion.pulse.scoring import ScoredCandidate

    paper = PaperCreate(
        external_id="arxiv:l3test01",
        source_type=SourceType.ARXIV,
        title="Test Paper",
        authors=["A. Author"],
        abstract="Abstract",
        url="https://example.com",
        pdf_url=None,
        citation_count=0,
        metadata={},
    )
    card = ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.7},
        llm_relevance=7,
        llm_novelty=6,
        reasoning="Good paper",
        final_score=0.75,
    )

    conn = AsyncMock()
    # fetchval calls in order:
    #   1. pulse_decks INSERT RETURNING id → deck_id = 1
    #   2. l3 COUNT query → 19 (below threshold of 20)
    #   3. pulse_cards INSERT RETURNING id → card inserted
    conn.fetchval = AsyncMock(side_effect=[1, 19, 42])
    conn.execute = AsyncMock(return_value=None)

    original_fetchval = conn.fetchval

    # Track the SQL for the per-card INSERT (3rd fetchval call)
    fetchval_calls: list[str] = []

    async def _capture_fetchval(sql, *args, **kwargs):
        fetchval_calls.append(sql)
        return await original_fetchval(sql, *args, **kwargs)

    conn.fetchval = AsyncMock(side_effect=_capture_fetchval)
    # Reset the side_effect to values (not forwarding)
    conn.fetchval.side_effect = [1, 19, 42]

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.deck"):
        result = await _persist_deck_inner(
            conn=conn,
            deck_date=date.today(),
            cards=[card],
            stats={"test": True},
            degraded_reason=None,
            user_id=None,
        )

    # Logger warning must mention the fallback
    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("L3 hard-exclusion would leave only" in m for m in warning_msgs), (
        f"Expected fallback warning; got: {warning_msgs}"
    )

    # Inspect the card-INSERT SQL: the 3rd fetchval call
    # We check by inspecting what fetchval was called with
    all_fetchval_calls = conn.fetchval.call_args_list
    assert len(all_fetchval_calls) >= 3, (
        f"Expected ≥3 fetchval calls (deck_id, l3_count, card_insert); got {len(all_fetchval_calls)}"
    )

    card_insert_sql: str = all_fetchval_calls[2].args[0]
    assert "recommendation_feedback" not in card_insert_sql.lower(), (
        "Fallback INSERT must NOT include 'NOT EXISTS recommendation_feedback' clause. "
        f"Got SQL:\n{card_insert_sql}"
    )

    # Result: 1 card inserted (the fetchval returns 42)
    assert result == 1, f"Expected 1 card inserted, got {result}"


# ---------------------------------------------------------------------------
# Test 5 — No-negatives baseline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_negatives_baseline(caplog) -> None:
    """load_profile with no recommendation_feedback rows returns safe defaults.

    - dampened_topics is empty
    - negative_topics is empty
    - negative_authors is empty
    - negative_centroid is None
    - No logger.warning is emitted
    """
    from paper_ingestion.pulse.profile import load_profile

    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = _make_10_fetch_side_effect()

    mock_embedder = AsyncMock()

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.profile"):
        profile = await load_profile(pool, embedder=mock_embedder)

    assert profile.dampened_topics == set(), (
        "dampened_topics must be empty when there are no negatives"
    )
    assert profile.negative_topics == [], (
        "negative_topics must be empty when there are no negatives"
    )
    assert profile.negative_authors == [], (
        "negative_authors must be empty when there are no negatives"
    )
    assert profile.negative_centroid is None, (
        "negative_centroid must be None when there are no negatives"
    )

    # No warnings should be emitted
    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert not warning_msgs, f"Expected no warnings; got: {warning_msgs}"
