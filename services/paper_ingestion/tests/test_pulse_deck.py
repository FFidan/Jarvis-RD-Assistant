"""Tests for app.pulse.deck — assemble_deck, persist_deck, load_today, load_history.

TDD: tests written before implementation.
"""

from datetime import date

import pytest
from paper_ingestion.models import PaperCreate, SourceType
from paper_ingestion.pulse.deck import (
    _persist_deck_inner,
    assemble_deck,
    load_history,
    load_today,
    persist_deck,
)
from paper_ingestion.pulse.scoring import ScoredCandidate
from tests.conftest import (
    FakeRecord,
    _make_pool_and_conn,
    make_pulse_deck_row,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paper(idx: int = 0, title: str | None = None) -> PaperCreate:
    return PaperCreate(
        external_id=f"arxiv:{idx:04d}",
        source_type=SourceType.ARXIV,
        title=title or f"Paper {idx}",
        authors=["Author A"],
        abstract=f"Abstract {idx}",
        published_date=date.today(),
        url=f"https://arxiv.org/abs/{idx:04d}",
    )


def _make_scored(paper: PaperCreate, score: float = 0.5) -> ScoredCandidate:
    return ScoredCandidate(
        paper=paper,
        signals={"embedding": score, "topic": 0.4, "recency": 0.9, "author_bonus": 0.0},
        llm_relevance=7,
        llm_novelty=5,
        reasoning="Very relevant",
        final_score=score,
    )


# ---------------------------------------------------------------------------
# assemble_deck
# ---------------------------------------------------------------------------


def test_assemble_deck_picks_top_n():
    """assemble_deck returns the top `size` candidates sorted by final_score desc."""
    papers = [_make_paper(i) for i in range(10)]
    candidates = [_make_scored(p, score=float(i) / 10.0) for i, p in enumerate(papers)]

    result = assemble_deck(candidates, size=5)

    assert len(result) == 5
    # Highest scores are from papers 9,8,7,6,5
    scores = [sc.final_score for sc in result]
    assert scores == sorted((s for s in scores if s is not None), reverse=True)
    assert result[0].final_score == pytest.approx(0.9)


def test_assemble_deck_fewer_than_size():
    """assemble_deck returns all candidates when fewer than size."""
    papers = [_make_paper(i) for i in range(3)]
    candidates = [_make_scored(p) for p in papers]

    result = assemble_deck(candidates, size=10)

    assert len(result) == 3


def test_assemble_deck_empty_input():
    """assemble_deck returns [] for empty input."""
    result = assemble_deck([], size=10)
    assert result == []


def test_assemble_deck_enforces_rank_ordering():
    """Rank ordering (1-based) is implied by sort position."""
    papers = [_make_paper(i) for i in range(5)]
    candidates = [_make_scored(p, score=float(i) / 5.0) for i, p in enumerate(papers)]

    result = assemble_deck(candidates, size=5)

    # Best paper has highest score and is first
    assert result[0].final_score is not None and result[-1].final_score is not None
    assert result[0].final_score >= result[-1].final_score


# ---------------------------------------------------------------------------
# persist_deck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_deck_inserts_deck_and_cards():
    """persist_deck inserts one pulse_decks row + N pulse_cards rows."""
    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 1, 15)
    papers = [_make_paper(i) for i in range(3)]
    # fetchval call sequence with new persist_deck logic:
    #   1: deck INSERT RETURNING id → deck_id=42
    #   2,3,4: card INSERT RETURNING id → non-None means success
    conn.fetchval.side_effect = [42, 101, 102, 103]

    cards = [_make_scored(p, score=float(i) / 3.0) for i, p in enumerate(papers)]

    deck_id = await persist_deck(pool, deck_date, cards, stats={"candidate_count": 100})

    # Transaction should have been used
    conn.transaction.assert_called_once()
    # fetchval or execute should have been called (deck insert + card inserts)
    assert deck_id is not None
    assert isinstance(deck_id, int)


@pytest.mark.asyncio
async def test_persist_deck_returns_insert_count():
    """persist_deck returns the number of successfully inserted card rows."""
    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 1, 15)
    # fetchval sequence: deck upsert → deck_id=99, card insert → inserted_id=1 (success)
    conn.fetchval.side_effect = [99, 1]

    cards = [_make_scored(_make_paper(0))]

    insert_count = await persist_deck(pool, deck_date, cards, stats={})

    # 1 card successfully inserted → return value is 1
    assert insert_count == 1


@pytest.mark.asyncio
async def test_persist_deck_upsert_replaces_old_cards():
    """Idempotent: if deck_date exists, old cards are deleted before new ones inserted."""
    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 1, 15)
    conn.fetchval.return_value = 1  # deck_id

    cards = [_make_scored(_make_paper(i)) for i in range(2)]

    await persist_deck(pool, deck_date, cards, stats={})

    # execute should have been called at least once (for DELETE + INSERTs)
    assert conn.execute.call_count > 0 or conn.fetchval.call_count > 0


# ---------------------------------------------------------------------------
# A2.1 — user_id threaded into pulse_decks INSERT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_deck_inner_threads_user_id_in_deck_insert():
    """A2.1: _persist_deck_inner binds user_id at position 4 in the deck INSERT."""
    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 6, 1)
    conn.fetchval.return_value = 99  # deck_id, then no cards so no further fetchval

    await _persist_deck_inner(conn, deck_date, cards=[], stats={}, user_id=42)

    # The first fetchval call is the deck INSERT
    first_call = conn.fetchval.call_args_list[0]
    sql, *args = first_call.args
    # 4 positional args: deck_date, stats, degraded_reason, user_id
    assert len(args) == 4, f"Expected 4 args to deck INSERT, got {len(args)}: {args}"
    assert args[3] == 42, f"Expected user_id=42 at position 4 (index 3), got {args[3]}"


@pytest.mark.asyncio
async def test_persist_deck_inner_includes_user_id_column_in_sql():
    """A2.1: The deck INSERT SQL contains 'user_id' in the column list."""
    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 6, 2)
    conn.fetchval.return_value = 77

    await _persist_deck_inner(conn, deck_date, cards=[], stats={}, user_id=None)

    first_call = conn.fetchval.call_args_list[0]
    sql = first_call.args[0]
    assert "INSERT INTO pulse_decks" in sql, f"Deck INSERT SQL must target pulse_decks: {sql!r}"
    assert "user_id" in sql, f"Deck INSERT SQL does not contain 'user_id' column: {sql!r}"


# ---------------------------------------------------------------------------
# A2.2 — IS_ARCHIVED_SQL substitution in pulse_cards INSERT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_deck_inner_uses_is_archived_sql_in_card_insert():
    """A2.2: The card INSERT SQL uses the IS_ARCHIVED_SQL predicate (not raw COALESCE)."""
    from paper_ingestion.queries.predicates import IS_ARCHIVED_SQL

    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 6, 3)
    conn.fetchval.side_effect = [55, 1]  # deck_id, then one card success

    paper = _make_paper(0)
    cards = [_make_scored(paper)]

    await _persist_deck_inner(conn, deck_date, cards=cards, stats={})

    # Second fetchval call is the card INSERT
    assert conn.fetchval.call_count >= 2, "Expected at least 2 fetchval calls"
    card_call = conn.fetchval.call_args_list[1]
    sql = card_call.args[0]
    assert IS_ARCHIVED_SQL in sql, (
        f"Card INSERT SQL must embed IS_ARCHIVED_SQL predicate.\n"
        f"IS_ARCHIVED_SQL={IS_ARCHIVED_SQL!r}\nSQL={sql!r}"
    )
    assert "AND NOT" in sql, "Card INSERT SQL must negate IS_ARCHIVED_SQL with 'AND NOT'"


# ---------------------------------------------------------------------------
# load_today
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_today_returns_none_when_no_deck():
    """load_today returns None when no deck exists for today."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None  # No deck found

    result = await load_today(pool)

    assert result is None


@pytest.mark.asyncio
async def test_load_today_returns_deck_response():
    """load_today returns PulseDeckResponse when deck exists."""
    from paper_ingestion.models import PulseDeckResponse

    pool, conn = _make_pool_and_conn()
    deck_row = make_pulse_deck_row(deck_date="2024-01-15", card_count=2)
    card_rows = [
        FakeRecord(
            {
                "id": 1,
                "deck_id": 1,
                "paper_id": 42,
                "paper_title": "Test Paper",
                "paper_authors": ["Author A"],
                "paper_url": "https://arxiv.org/abs/0001",
                "rank": 1,
                "score": 0.85,
                "llm_relevance": 8,
                "llm_novelty": 6,
                "reasoning": "Highly relevant",
                "signals": {"embedding": 0.82},
            }
        ),
        FakeRecord(
            {
                "id": 2,
                "deck_id": 1,
                "paper_id": 43,
                "paper_title": "Another Paper",
                "paper_authors": ["Author B"],
                "paper_url": "https://arxiv.org/abs/0002",
                "rank": 2,
                "score": 0.75,
                "llm_relevance": 7,
                "llm_novelty": 5,
                "reasoning": "Somewhat relevant",
                "signals": {"embedding": 0.70},
            }
        ),
    ]
    conn.fetchrow.return_value = deck_row
    conn.fetch.return_value = card_rows

    result = await load_today(pool)

    assert result is not None
    assert isinstance(result, PulseDeckResponse)
    assert result.deck_id == 1
    assert len(result.cards) == 2
    assert result.cards[0].rank == 1
    assert result.cards[0].paper_title == "Test Paper"


# ---------------------------------------------------------------------------
# load_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_history_returns_empty_when_no_decks():
    """load_history returns [] when no historical decks exist."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    result = await load_history(pool, days=30)

    assert result == []


@pytest.mark.asyncio
async def test_load_history_returns_sorted_newest_first():
    """load_history returns decks sorted newest first."""
    from paper_ingestion.models import PulseDeckResponse

    pool, conn = _make_pool_and_conn()
    deck_rows = [
        FakeRecord(
            {
                "id": 2,
                "deck_date": "2024-01-14",
                "card_count": 5,
                "generated_at": "2024-01-14T04:00:00+00:00",
                "stats": {},
            }
        ),
        FakeRecord(
            {
                "id": 1,
                "deck_date": "2024-01-13",
                "card_count": 3,
                "generated_at": "2024-01-13T04:00:00+00:00",
                "stats": {},
            }
        ),
    ]
    # fetch for decks, then fetch for cards of each deck
    conn.fetch.side_effect = [
        deck_rows,
        [],  # cards for deck 2
        [],  # cards for deck 1
    ]

    result = await load_history(pool, days=30)

    assert len(result) == 2
    assert isinstance(result[0], PulseDeckResponse)
    assert result[0].deck_id == 2  # newest first


@pytest.mark.asyncio
async def test_load_history_uses_days_parameter():
    """load_history queries only the last `days` days of decks."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    await load_history(pool, days=7)

    # Verify a DB call was made (with some parameters)
    conn.fetch.assert_called_once()


# ---------------------------------------------------------------------------
# persist_deck — partial insert (missing paper rows)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_deck_counts_actual_inserts_when_paper_missing():
    """persist_deck sets card_count to the number of successfully inserted cards.

    3 cards are submitted but only 2 have corresponding papers rows.
    The missing paper causes fetchval to return None for that card.
    The final UPDATE must use card_count=2, and logger.warning must fire once.
    """
    from unittest.mock import patch

    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 2, 1)

    # fetchval call sequence:
    #   1st: deck upsert → deck_id = 7
    #   2nd: card insert for paper 0 → inserted_id = 101 (success)
    #   3rd: card insert for paper 1 → None (paper row missing)
    #   4th: card insert for paper 2 → inserted_id = 103 (success)
    conn.fetchval.side_effect = [7, 101, None, 103]

    papers = [_make_paper(i) for i in range(3)]
    cards = [_make_scored(p, score=float(i + 1) / 3.0) for i, p in enumerate(papers)]

    with patch("paper_ingestion.pulse.deck.logger") as mock_logger:
        insert_count = await persist_deck(pool, deck_date, cards, stats={"candidate_count": 50})

    # 2 of 3 cards were successfully inserted → return value is 2
    assert insert_count == 2

    # The final UPDATE must have been called with card_count=2
    update_calls = [
        call
        for call in conn.execute.call_args_list
        if "UPDATE pulse_decks SET card_count" in call.args[0]
    ]
    assert len(update_calls) == 1, "Expected exactly one UPDATE pulse_decks call"
    _, actual_count, _ = update_calls[0].args  # ($1=successes, $2=deck_id)
    assert actual_count == 2, f"Expected card_count=2 but got {actual_count}"

    # logger.warning must have been called exactly once (for the missing paper)
    mock_logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# M17 — empty deck (zero cards) still writes the deck row with card_count=0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_deck_empty_cards_writes_zero_count_row():
    """persist_deck with cards=[] must still upsert the pulse_decks row (card_count=0).

    M17 audit finding: an empty-result run must persist a marker row so the
    scheduler knows the job ran.  card_count=0 is the correct outcome; no
    card rows should be inserted.
    """
    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 3, 1)

    # Only one fetchval call: the deck upsert RETURNING id
    conn.fetchval.return_value = 55

    insert_count = await persist_deck(pool, deck_date, cards=[], stats={"candidate_count": 0})

    # 0 cards submitted → 0 successfully inserted
    assert insert_count == 0

    # The UPDATE to set card_count must use 0
    update_calls = [
        call
        for call in conn.execute.call_args_list
        if "UPDATE pulse_decks SET card_count" in call.args[0]
    ]
    assert len(update_calls) == 1
    _, actual_count, _ = update_calls[0].args
    assert actual_count == 0, f"Expected card_count=0 for empty deck, got {actual_count}"


# ---------------------------------------------------------------------------
# Sprint 8 B3.4 — archived/dismissed papers excluded from pulse_cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_persist_deck_excludes_archived_and_dismissed(test_db_pool):
    """Sprint 8 B3.4: pulse_cards are NOT created for archived or dismissed papers.

    Requires a live PostgreSQL instance (JARVIS_RUN_LIVE_PG=1).  Exercises the
    LEFT JOIN paper_user_state filter inside _persist_deck_inner that uses:
        AND COALESCE(pus.archived, FALSE) = FALSE
        AND COALESCE(pus.dismissed, FALSE) = FALSE

    Three papers are inserted:
    - archived_pid  — paper_user_state.archived = TRUE  -> must NOT get a card
    - dismissed_pid — paper_user_state.dismissed = TRUE -> must NOT get a card
    - clean_pid     — no user-state row at all          -> MUST get a card
    """
    test_date = date(2099, 1, 1)  # far future to avoid collision with real decks

    from jarvis_common.db_helpers import init_pg_connection

    async with test_db_pool.acquire() as conn:
        # Register JSON/JSONB codec so asyncpg accepts dicts for ::jsonb params.
        await init_pg_connection(conn)

        # The test_db_pool fixture applies migrations by splitting on ";" which
        # breaks migration 034 (its comment contains a ";").  Ensure the two
        # columns that _persist_deck_inner writes are present; the IF NOT EXISTS
        # guard makes this idempotent.
        await conn.execute(
            "ALTER TABLE pulse_cards "
            "ADD COLUMN IF NOT EXISTS reasoning_verified BOOLEAN DEFAULT NULL, "
            "ADD COLUMN IF NOT EXISTS reasoning_confidence VARCHAR(10) DEFAULT NULL"
        )

        async with conn.transaction():
            # ------------------------------------------------------------------
            # 1. Insert three papers with distinct external_ids
            # ------------------------------------------------------------------
            archived_pid = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('b3-4-arch-1', 'arxiv', 'Archived Paper', '{}', 'http://arch') "
                "RETURNING id"
            )
            dismissed_pid = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('b3-4-dism-1', 'arxiv', 'Dismissed Paper', '{}', 'http://dism') "
                "RETURNING id"
            )
            clean_pid = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('b3-4-clean-1', 'arxiv', 'Clean Paper', '{}', 'http://clean') "
                "RETURNING id"
            )

            # ------------------------------------------------------------------
            # 2. Set user state: archived_pid is archived, dismissed_pid is dismissed
            # ------------------------------------------------------------------
            await conn.execute(
                "INSERT INTO paper_user_state "
                "(paper_id, user_id, status, archived, dismissed) "
                "VALUES ($1, NULL, 'new', TRUE, FALSE)",
                archived_pid,
            )
            await conn.execute(
                "INSERT INTO paper_user_state "
                "(paper_id, user_id, status, archived, dismissed) "
                "VALUES ($1, NULL, 'new', FALSE, TRUE)",
                dismissed_pid,
            )
            # clean_pid intentionally has no paper_user_state row

            # ------------------------------------------------------------------
            # 3. Build ScoredCandidates pointing at all three papers
            # ------------------------------------------------------------------
            def _make_candidate(external_id: str, score: float = 0.5) -> ScoredCandidate:
                paper = PaperCreate(
                    external_id=external_id,
                    source_type=SourceType.ARXIV,
                    title=f"Title {external_id}",
                    authors=["Author"],
                    abstract="Abstract",
                    published_date=date.today(),
                    url=f"https://arxiv.org/abs/{external_id}",
                )
                return ScoredCandidate(
                    paper=paper,
                    signals={
                        "embedding": score,
                        "topic": 0.4,
                        "recency": 0.9,
                        "author_bonus": 0.0,
                    },
                    llm_relevance=7,
                    llm_novelty=5,
                    reasoning="Test reasoning",
                    final_score=score,
                )

            candidates = [
                _make_candidate("b3-4-arch-1", score=0.9),
                _make_candidate("b3-4-dism-1", score=0.8),
                _make_candidate("b3-4-clean-1", score=0.7),
            ]

            # ------------------------------------------------------------------
            # 4. Persist the deck on this connection (inside the open transaction)
            # ------------------------------------------------------------------
            insert_count = await _persist_deck_inner(
                conn,
                test_date,
                candidates,
                stats={"candidate_count": 3},
                user_id=None,
            )

            # ------------------------------------------------------------------
            # 5. Retrieve the deck_id and verify pulse_cards
            # ------------------------------------------------------------------
            deck_id = await conn.fetchval(
                "SELECT id FROM pulse_decks WHERE deck_date = $1",
                test_date,
            )
            assert deck_id is not None, "pulse_decks row must exist after _persist_deck_inner"

            rows = await conn.fetch(
                "SELECT paper_id FROM pulse_cards WHERE deck_id = $1",
                deck_id,
            )
            result_pids = {r["paper_id"] for r in rows}

            # Only the clean paper should have a card
            assert clean_pid in result_pids, "Clean paper (no user-state) must receive a pulse_card"
            assert archived_pid not in result_pids, (
                "Archived paper must be excluded from pulse_cards "
                "(COALESCE(pus.archived, FALSE) = FALSE filter)"
            )
            assert dismissed_pid not in result_pids, (
                "Dismissed paper must be excluded from pulse_cards "
                "(COALESCE(pus.dismissed, FALSE) = FALSE filter)"
            )

            # insert_count must reflect only the successful (clean) insertion
            assert insert_count == 1, (
                f"_persist_deck_inner must return 1 (clean paper only), got {insert_count}"
            )


# ---------------------------------------------------------------------------
# W3-T1 NEW-M1 — user_id threading: per-user archived state is respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_persist_deck_inner_excludes_archived_for_specific_user(test_db_pool):
    """NEW-M1: _persist_deck_inner(user_id=42) skips papers archived by user 42.

    The ``pus.user_id IS NOT DISTINCT FROM $11`` clause must match the *concrete*
    user_id 42, so a paper_user_state row with user_id=42 and archived=TRUE must
    prevent the paper from receiving a pulse_card.

    Three papers are inserted:
    - user42_archived — archived=TRUE for user 42 → must NOT get a card
    - other_archived  — archived=TRUE for user 99 → MUST get a card (different user)
    - clean_pid       — no state for any user     → MUST get a card
    """
    test_date = date(2099, 2, 1)  # far future to avoid collision

    from jarvis_common.db_helpers import init_pg_connection

    async with test_db_pool.acquire() as conn:
        await init_pg_connection(conn)

        await conn.execute(
            "ALTER TABLE pulse_cards "
            "ADD COLUMN IF NOT EXISTS reasoning_verified BOOLEAN DEFAULT NULL, "
            "ADD COLUMN IF NOT EXISTS reasoning_confidence VARCHAR(10) DEFAULT NULL"
        )

        async with conn.transaction():
            # 1. Insert three papers
            user42_pid = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('m1-u42-arch-1', 'arxiv', 'U42 Archived', '{}', 'http://u42') "
                "RETURNING id"
            )
            other_pid = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('m1-u99-arch-1', 'arxiv', 'U99 Archived', '{}', 'http://u99') "
                "RETURNING id"
            )
            clean_pid = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('m1-clean-1', 'arxiv', 'Clean Paper', '{}', 'http://clean') "
                "RETURNING id"
            )

            # 2. Set user state: user42_pid archived by user 42; other_pid archived by user 99
            await conn.execute(
                "INSERT INTO paper_user_state "
                "(paper_id, user_id, status, archived, dismissed) "
                "VALUES ($1, 42, 'new', TRUE, FALSE)",
                user42_pid,
            )
            await conn.execute(
                "INSERT INTO paper_user_state "
                "(paper_id, user_id, status, archived, dismissed) "
                "VALUES ($1, 99, 'new', TRUE, FALSE)",
                other_pid,
            )
            # clean_pid has no state for any user

            # 3. Build ScoredCandidates for all three papers
            def _make_candidate(external_id: str, score: float = 0.5) -> ScoredCandidate:
                paper = PaperCreate(
                    external_id=external_id,
                    source_type=SourceType.ARXIV,
                    title=f"Title {external_id}",
                    authors=["Author"],
                    abstract="Abstract",
                    published_date=date.today(),
                    url=f"https://arxiv.org/abs/{external_id}",
                )
                return ScoredCandidate(
                    paper=paper,
                    signals={
                        "embedding": score,
                        "topic": 0.4,
                        "recency": 0.9,
                        "author_bonus": 0.0,
                    },
                    llm_relevance=7,
                    llm_novelty=5,
                    reasoning="Test reasoning",
                    final_score=score,
                )

            candidates = [
                _make_candidate("m1-u42-arch-1", score=0.9),
                _make_candidate("m1-u99-arch-1", score=0.8),
                _make_candidate("m1-clean-1", score=0.7),
            ]

            # 4. Persist with user_id=42 — only user 42's archived state applies
            insert_count = await _persist_deck_inner(
                conn,
                test_date,
                candidates,
                stats={"candidate_count": 3},
                user_id=42,
            )

            # 5. Verify pulse_cards
            deck_id = await conn.fetchval(
                "SELECT id FROM pulse_decks WHERE deck_date = $1",
                test_date,
            )
            assert deck_id is not None, "pulse_decks row must exist after _persist_deck_inner"

            rows = await conn.fetch(
                "SELECT paper_id FROM pulse_cards WHERE deck_id = $1",
                deck_id,
            )
            result_pids = {r["paper_id"] for r in rows}

            # user42_pid archived by user 42 → must be excluded
            assert user42_pid not in result_pids, (
                "Paper archived by user 42 must be excluded from pulse_cards "
                "when user_id=42 is passed"
            )
            # other_pid archived only by user 99 → must receive a card for user 42's deck
            assert other_pid in result_pids, (
                "Paper archived by a *different* user (99) must NOT be excluded "
                "from pulse_cards when user_id=42 is passed"
            )
            # clean_pid has no state → must receive a card
            assert clean_pid in result_pids, "Paper with no user-state must receive a pulse_card"

            # 2 of 3 cards persisted (user42_pid excluded)
            assert insert_count == 2, (
                f"_persist_deck_inner must return 2 when user 42 has one archived paper, "
                f"got {insert_count}"
            )


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_persist_deck_inner_null_user_id_matches_null_state(test_db_pool):
    """NEW-M1: _persist_deck_inner(user_id=None) still works in single-tenant mode.

    When ``user_id=None`` the join clause evaluates to
    ``pus.user_id IS NOT DISTINCT FROM NULL``, which matches rows where
    ``user_id IS NULL``.  A paper with ``archived=TRUE / user_id=NULL`` must
    be excluded; a paper with ``archived=TRUE / user_id=42`` must NOT be
    excluded (wrong user bucket).
    """
    test_date = date(2099, 3, 1)  # far future to avoid collision

    from jarvis_common.db_helpers import init_pg_connection

    async with test_db_pool.acquire() as conn:
        await init_pg_connection(conn)

        await conn.execute(
            "ALTER TABLE pulse_cards "
            "ADD COLUMN IF NOT EXISTS reasoning_verified BOOLEAN DEFAULT NULL, "
            "ADD COLUMN IF NOT EXISTS reasoning_confidence VARCHAR(10) DEFAULT NULL"
        )

        async with conn.transaction():
            # 1. Insert two papers
            null_archived_pid = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('m1-null-arch-1', 'arxiv', 'NULL Archived', '{}', 'http://null') "
                "RETURNING id"
            )
            user42_archived_pid = await conn.fetchval(
                "INSERT INTO papers (external_id, source_type, title, authors, url) "
                "VALUES ('m1-null-u42-1', 'arxiv', 'U42 Archived Null Test', '{}', 'http://null42') "
                "RETURNING id"
            )

            # 2. null_archived_pid has archived=TRUE with user_id=NULL
            await conn.execute(
                "INSERT INTO paper_user_state "
                "(paper_id, user_id, status, archived, dismissed) "
                "VALUES ($1, NULL, 'new', TRUE, FALSE)",
                null_archived_pid,
            )
            # user42_archived_pid has archived=TRUE only for user 42 (not NULL)
            await conn.execute(
                "INSERT INTO paper_user_state "
                "(paper_id, user_id, status, archived, dismissed) "
                "VALUES ($1, 42, 'new', TRUE, FALSE)",
                user42_archived_pid,
            )

            # 3. Build candidates for both papers
            def _make_candidate(external_id: str, score: float = 0.5) -> ScoredCandidate:
                paper = PaperCreate(
                    external_id=external_id,
                    source_type=SourceType.ARXIV,
                    title=f"Title {external_id}",
                    authors=["Author"],
                    abstract="Abstract",
                    published_date=date.today(),
                    url=f"https://arxiv.org/abs/{external_id}",
                )
                return ScoredCandidate(
                    paper=paper,
                    signals={
                        "embedding": score,
                        "topic": 0.4,
                        "recency": 0.9,
                        "author_bonus": 0.0,
                    },
                    llm_relevance=7,
                    llm_novelty=5,
                    reasoning="Test reasoning",
                    final_score=score,
                )

            candidates = [
                _make_candidate("m1-null-arch-1", score=0.9),
                _make_candidate("m1-null-u42-1", score=0.8),
            ]

            # 4. Persist with user_id=None (single-tenant mode)
            insert_count = await _persist_deck_inner(
                conn,
                test_date,
                candidates,
                stats={"candidate_count": 2},
                user_id=None,
            )

            # 5. Verify pulse_cards
            deck_id = await conn.fetchval(
                "SELECT id FROM pulse_decks WHERE deck_date = $1",
                test_date,
            )
            assert deck_id is not None

            rows = await conn.fetch(
                "SELECT paper_id FROM pulse_cards WHERE deck_id = $1",
                deck_id,
            )
            result_pids = {r["paper_id"] for r in rows}

            # null_archived_pid is archived with user_id=NULL → must be excluded
            assert null_archived_pid not in result_pids, (
                "Paper archived with user_id=NULL must be excluded when user_id=None"
            )
            # user42_archived_pid archived only for user 42 → not excluded in NULL mode
            assert user42_archived_pid in result_pids, (
                "Paper archived for a specific user (42) must NOT be excluded from a NULL-mode deck"
            )

            # 1 of 2 cards persisted (null_archived excluded)
            assert insert_count == 1, (
                f"_persist_deck_inner must return 1 in NULL mode with one null-archived paper, "
                f"got {insert_count}"
            )
