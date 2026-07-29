"""Negative-feedback and topic-dampening safeguard tests.

Tests:
1. _filter_unread 60d boundary  — papers with rec_feedback at 59d (excluded),
   60d (included — strict > boundary), 61d (included).
2. Topic dampening >=5 in load_profile — 4 negatives -> NOT dampened; 5 -> dampened.
3. Topic-dampening cap in load_profile — 4 topics all dampened -> cap to 2 (50%).
4. Deck selection drops recently dismissed candidates before it truncates,
   so a dismissal frees its slot instead of shortening the deck.
5. No-negatives baseline in load_profile — all safeguard fields empty, no warnings.
6. Live PG — a dismissed paper reaches no pulse_cards row while the deck still fills.
7. Live PG — the deck reads carry the paper-visibility predicate.
8. Live PG — stale fallback removes cards with current negative feedback.

Tests 1-5 need no DB; 6-8 are DB-backed and skip without JARVIS_RUN_LIVE_PG=1.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from tests.conftest import FakeRecord, _make_pool_and_conn

from paper_ingestion.models import PaperCreate, SourceType
from paper_ingestion.pulse.scoring import ScoredCandidate

# ---------------------------------------------------------------------------
# Helper: build a FakeRecord row for recommendation_feedback-counted dampened topics
# ---------------------------------------------------------------------------


def _dampened_row(topic_id: int, neg_count: int) -> FakeRecord:
    return FakeRecord({"id": topic_id, "neg_count": neg_count})


def _candidate(external_id: str, final_score: float) -> ScoredCandidate:
    """Build a stage-3 candidate whose external id identifies it in assertions."""
    return ScoredCandidate(
        paper=PaperCreate(
            external_id=external_id,
            source_type=SourceType.ARXIV,
            title=f"Candidate {external_id}",
            authors=["Selection Author"],
            abstract="Abstract",
            url=f"https://arxiv.test/{external_id}",
        ),
        signals={"embedding": final_score},
        llm_relevance=7,
        llm_novelty=6,
        reasoning="relevant",
        final_score=final_score,
    )


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
# Test 4 — deck selection excludes dismissed candidates before it truncates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deck_selection_drops_dismissed_candidates_before_truncating() -> None:
    """A dismissed candidate frees its deck slot for the next eligible one.

    Twelve ranked candidates, a deck size of ten, and the owner rated two of
    the top ten down.  Removing them while selecting leaves ten eligible
    candidates, so the deck still fills; removing them after the deck was cut
    to ten would leave eight.  Nothing here hands the pipeline a candidate
    count — the deck is measured, and the database only reports which papers
    carry recent negative feedback.
    """
    from paper_ingestion.pulse.job import _select_deck_cards

    candidates = [_candidate(f"arxiv:cand{n:02d}", 1.0 - n / 100) for n in range(12)]
    dismissed_ids = {"arxiv:cand00", "arxiv:cand04"}
    pool, _conn = _make_pool_and_conn(
        fetch_return=[FakeRecord({"external_id": eid}) for eid in sorted(dismissed_ids)]
    )

    deck, dismissed_count = await _select_deck_cards(pool, candidates, size=10, user_id=7)

    assert len(deck) == 10, (
        "the deck must still fill from the remaining eligible candidates; "
        f"got {len(deck)} cards, which is what filtering an already-truncated deck yields"
    )
    assert {sc.paper.external_id for sc in deck}.isdisjoint(dismissed_ids), (
        "a paper rated down within the last 60 days must not reach the deck; got "
        f"{[sc.paper.external_id for sc in deck]}"
    )
    assert dismissed_count == 2, (
        f"two candidates carried recent negative feedback; got {dismissed_count}"
    )


@pytest.mark.asyncio
async def test_deck_selection_keeps_a_healthy_deck_at_full_size() -> None:
    """With no negative feedback the deck is still the plain top-N by score."""
    from paper_ingestion.pulse.job import _select_deck_cards

    candidates = [_candidate(f"arxiv:fill{n:02d}", 1.0 - n / 100) for n in range(12)]
    pool, _conn = _make_pool_and_conn(fetch_return=[])

    deck, dismissed_count = await _select_deck_cards(pool, candidates, size=10, user_id=7)

    assert [sc.paper.external_id for sc in deck] == [
        sc.paper.external_id for sc in candidates[:10]
    ], "the exclusion must not reorder or shrink a deck with nothing dismissed"
    assert dismissed_count == 0


_WIRED_SHORT_DECK_REASON = "dismissed-short-deck reason reached the diagnostics"


def test_short_deck_reason_speaks_only_when_dismissals_cost_cards() -> None:
    """The short-deck reason is raised only when the exclusion actually cost cards."""
    from paper_ingestion.pulse.job import _dismissed_short_deck_reason

    assert _dismissed_short_deck_reason(cards=10, size=10, dismissed=3) is None, (
        "a deck that filled needs no explanation"
    )
    assert _dismissed_short_deck_reason(cards=4, size=10, dismissed=0) is None, (
        "a deck shortened by thin discovery must not be blamed on the owner's ratings"
    )
    reason = _dismissed_short_deck_reason(cards=4, size=10, dismissed=6)
    assert reason is not None and "60 days" in reason, (
        f"a deck cut short by dismissals must say so; got {reason!r}"
    )
    assert "so the deck" not in reason
    assert "The deck filled 4 of 10 card slots" in reason, (
        "the message must report exclusions and deck fill as separate facts; "
        f"thin discovery may also have left slots empty: {reason!r}"
    )


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


# ---------------------------------------------------------------------------
# Test 6 — a dismissed paper reaches no card, and the deck still fills
# Verified: services/paper_ingestion/paper_ingestion/pulse/job.py:277
#           (job.py:85 _select_deck_cards, called at job.py:304, before the cut)
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_dismissed_paper_reaches_no_card_while_the_deck_still_fills(
    contract_conn,
    contract_two_users,
) -> None:
    """A thumbs-down keeps its paper out of the deck without costing a card.

    Five real candidates and a real negative rating on the highest-ranked one,
    against a deck size of three.  The rated-down paper must own no
    ``pulse_cards`` row, and the deck must still carry three cards drawn from
    the four that survived — the promise ``docs/manual/pulse.md`` makes.
    """
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock, patch

    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.pulse.job import run_pulse

    pool = SharedConnPool(contract_conn)
    user_id = contract_two_users.user_a_id
    deck_size = 3
    external_ids = [f"dismissal-deck-{n:02d}" for n in range(5)]
    dismissed_id = external_ids[0]

    for external_id in external_ids:
        await contract_conn.execute(
            """INSERT INTO papers (external_id, source_type, title, authors, url,
                                   discovered_by, visibility_scope)
               VALUES ($1, 'arxiv', $2, ARRAY['Selection Author'], $3, $4, 'public')""",
            external_id,
            f"Candidate {external_id}",
            f"https://arxiv.test/{external_id}",
            user_id,
        )
    await contract_conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           SELECT id, $2, 'negative', 'pulse_thumbs' FROM papers WHERE external_id = $1""",
        dismissed_id,
        user_id,
    )

    candidates = [_candidate(eid, 1.0 - n / 100) for n, eid in enumerate(external_ids)]
    now = datetime(2098, 11, 3, 4, 0, tzinfo=UTC)

    with (
        patch(
            "paper_ingestion.pulse.job.load_profile",
            AsyncMock(
                return_value=MagicMock(
                    topics=[],
                    tracked_author_names=set(),
                    tracked_author_s2_ids=set(),
                    library_centroid=None,
                    weights={"embedding": 1.0},
                    deck_size=deck_size,
                    stage2_top_k=10,
                    liked_paper_ids=[],
                    recent_positive_titles=[],
                    recent_negative_titles=[],
                    lookback_days=7,
                )
            ),
        ),
        patch(
            "paper_ingestion.pulse.job.discover_candidates",
            AsyncMock(return_value=(list(candidates), {}, {})),
        ),
        patch(
            "paper_ingestion.pulse.job.stage1_embedding_filter",
            AsyncMock(return_value=list(candidates)),
        ),
        patch(
            "paper_ingestion.pulse.job.stage2_llm_rerank",
            AsyncMock(side_effect=lambda batch, *a, **kw: batch),
        ),
        patch("paper_ingestion.pulse.job.effective_num_ctx", AsyncMock(return_value=4096)),
        patch(
            "paper_ingestion.pulse.job.stage3_combine",
            AsyncMock(side_effect=lambda scored, weights: scored),
        ),
        patch(
            "paper_ingestion.pulse.job.upsert_verified_public_paper",
            AsyncMock(return_value=None),
        ),
        # The condition itself is a pure function covered above. What is not
        # otherwise covered is that its answer reaches the run's diagnostics at
        # all, so this pins the wiring by making the answer unmistakable.
        patch(
            "paper_ingestion.pulse.job._dismissed_short_deck_reason",
            return_value=_WIRED_SHORT_DECK_REASON,
        ),
    ):
        stats = await run_pulse(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
            now=now,
            user_id=user_id,
        )

    carded = [
        row["external_id"]
        for row in await contract_conn.fetch(
            """SELECT p.external_id
                 FROM pulse_cards pc
                 JOIN pulse_decks pd ON pd.id = pc.deck_id
                 JOIN papers p ON p.id = pc.paper_id
                WHERE pd.deck_date = $1 AND pd.user_id = $2
                ORDER BY pc.rank""",
            now.date(),
            user_id,
        )
    ]

    assert dismissed_id not in carded, (
        f"{dismissed_id} was rated down and must own no card; deck was {carded}"
    )
    assert len(carded) == deck_size, (
        f"the deck must still fill to {deck_size} from the four surviving candidates; got {carded}"
    )
    assert stats["card_count"] == deck_size, (
        f"card_count must match the persisted cards; got {stats['card_count']!r}"
    )
    assert stats["degraded_reason"] == _WIRED_SHORT_DECK_REASON, (
        "a short-deck reason must reach the run's diagnostics, or the user is "
        f"never told why their deck came up short; got {stats['degraded_reason']!r}"
    )


# ---------------------------------------------------------------------------
# Test 7 — the deck reads carry the paper-visibility predicate
# Verified: services/paper_ingestion/paper_ingestion/pulse/deck.py:340
#           (deck.py:366, :439, :509 -- the three deck card queries)
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_deck_reads_apply_the_paper_visibility_predicate(
    contract_conn,
    contract_two_users,
) -> None:
    """The three deck reads filter on paper visibility, and today that is inert.

    Pulse only ever persists a card for a paper that already carries public
    scope, and no product path downgrades a paper afterwards, so the predicate
    is expected to change nothing: the first half asserts every read still
    returns the card.  The second half downgrades the row directly — the only
    way to reach that state today — to show the predicate is wired in rather
    than merely harmless. Without it, a later scope change could leave the
    paper readable through an already-persisted deck.
    """
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.pulse.deck import (
        load_history,
        load_last_nonempty_deck,
        load_today,
        persist_deck,
    )

    pool = SharedConnPool(contract_conn)
    user_id = contract_two_users.user_a_id
    external_id = "visibility-deck-01"

    await contract_conn.execute(
        """INSERT INTO papers (external_id, source_type, title, authors, url,
                               discovered_by, visibility_scope)
           VALUES ($1, 'arxiv', 'Visible Candidate', ARRAY['Author'],
                   'https://arxiv.test/visibility-deck-01', $2, 'public')""",
        external_id,
        user_id,
    )
    today = await contract_conn.fetchval("SELECT CURRENT_DATE")
    yesterday = await contract_conn.fetchval("SELECT CURRENT_DATE - 1")
    card = _candidate(external_id, 0.9)
    for deck_date in (today, yesterday):
        await persist_deck(pool, deck_date, cards=[card], stats={}, user_id=user_id)

    async def _card_counts() -> tuple[int, int, int | None]:
        """Return the card counts of today's, yesterday's and fallback decks."""
        today_deck = await load_today(pool, user_id=user_id)
        history = await load_history(pool, days=30, user_id=user_id)
        last_nonempty = await load_last_nonempty_deck(pool, user_id=user_id)
        assert today_deck is not None, "today's deck row must exist"
        yesterdays = [deck for deck in history if deck.deck_date == yesterday]
        assert yesterdays, "yesterday's deck must appear in the history window"
        fallback_count = len(last_nonempty.cards) if last_nonempty is not None else None
        return len(today_deck.cards), len(yesterdays[0].cards), fallback_count

    assert await _card_counts() == (1, 1, 1), (
        "a public paper must stay readable through every deck path — the predicate "
        "is defence in depth and must cost nothing today"
    )

    await contract_conn.execute(
        "UPDATE papers SET visibility_scope = 'private' WHERE external_id = $1",
        external_id,
    )

    assert await _card_counts() == (0, 0, None), (
        "a paper that lost public scope must drop out of every deck read; a read "
        "path without the predicate would keep serving it"
    )


# ---------------------------------------------------------------------------
# Test 8 — stale fallback applies current negative feedback
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_stale_fallback_excludes_a_recently_rated_down_card(
    contract_conn,
    contract_two_users,
) -> None:
    """A persisted fallback deck returns only cards still eligible today."""
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.pulse.deck import load_last_nonempty_deck, persist_deck

    pool = SharedConnPool(contract_conn)
    user_id = contract_two_users.user_a_id
    dropped_external_id = "stale-feedback-deck-dropped"
    kept_external_id = "stale-feedback-deck-kept"

    for external_id in (dropped_external_id, kept_external_id):
        await contract_conn.execute(
            """INSERT INTO papers (external_id, source_type, title, authors, url,
                                   discovered_by, visibility_scope)
               VALUES ($1, 'arxiv', $2, ARRAY['Fallback Author'], $3, $4, 'public')""",
            external_id,
            f"Candidate {external_id}",
            f"https://arxiv.test/{external_id}",
            user_id,
        )

    await contract_conn.execute("DELETE FROM pulse_decks WHERE user_id = $1", user_id)
    yesterday = await contract_conn.fetchval("SELECT CURRENT_DATE - 1")
    persisted = await persist_deck(
        pool,
        yesterday,
        cards=[
            _candidate(dropped_external_id, 0.9),
            _candidate(kept_external_id, 0.8),
        ],
        stats={},
        user_id=user_id,
    )
    assert persisted == 2

    paper_ids = {
        row["external_id"]: row["id"]
        for row in await contract_conn.fetch(
            "SELECT id, external_id FROM papers WHERE external_id = ANY($1::text[])",
            [dropped_external_id, kept_external_id],
        )
    }
    await contract_conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           VALUES ($1, $2, 'negative', 'pulse_thumbs')""",
        paper_ids[dropped_external_id],
        user_id,
    )

    deck = await load_last_nonempty_deck(pool, user_id=user_id)

    assert deck is not None
    assert deck.card_count == len(deck.cards) == 1
    assert [card.paper_id for card in deck.cards] == [paper_ids[kept_external_id]]
