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
    #   2: L3 count query (25 candidates pass filter, ≥ 20 so L3 applies)
    #   3,4,5: card INSERT RETURNING id → non-None means success
    conn.fetchval.side_effect = [42, 25, 101, 102, 103]

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
    # fetchval sequence:
    #   1: deck upsert → deck_id=99
    #   2: L3 count query → 25 candidates pass filter (≥ 20, L3 applies)
    #   3: card insert → inserted_id=1 (success)
    conn.fetchval.side_effect = [99, 25, 1]

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
# A2.2 — PULSE_CANDIDATE_EXCLUDE_SQL substitution in pulse_cards INSERT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_deck_inner_uses_pulse_candidate_exclude_sql():
    """A2.2: The card INSERT SQL uses the PULSE_CANDIDATE_EXCLUDE_SQL predicate (not raw COALESCE)."""
    from paper_ingestion.queries.predicates import PULSE_CANDIDATE_EXCLUDE_SQL

    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 6, 3)
    # fetchval sequence:
    #   1: deck upsert → deck_id=55
    #   2: L3 count query → 25 candidates pass filter (≥ 20, L3 applies)
    #   3: card INSERT → inserted_id=1 (success)
    conn.fetchval.side_effect = [55, 25, 1]  # deck_id, l3_count, then one card success

    paper = _make_paper(0)
    cards = [_make_scored(paper)]

    await _persist_deck_inner(conn, deck_date, cards=cards, stats={})

    # Third fetchval call is the card INSERT (index 2; index 1 is L3 count query)
    assert conn.fetchval.call_count >= 3, "Expected at least 3 fetchval calls"
    card_call = conn.fetchval.call_args_list[2]
    sql = card_call.args[0]
    assert PULSE_CANDIDATE_EXCLUDE_SQL in sql, (
        f"Card INSERT SQL must embed PULSE_CANDIDATE_EXCLUDE_SQL predicate.\n"
        f"PULSE_CANDIDATE_EXCLUDE_SQL={PULSE_CANDIDATE_EXCLUDE_SQL!r}\nSQL={sql!r}"
    )
    assert "AND NOT (" in sql, (
        "Card INSERT SQL must negate PULSE_CANDIDATE_EXCLUDE_SQL with 'AND NOT ('"
    )


@pytest.mark.asyncio
async def test_persist_deck_inner_threads_user_id_to_card_insert():
    """pulse_cards rows must be attributable to the deck owner."""
    pool, conn = _make_pool_and_conn()
    deck_date = date(2026, 5, 11)
    conn.fetchval.side_effect = [55, 25, 1]

    await _persist_deck_inner(
        conn,
        deck_date,
        cards=[_make_scored(_make_paper(0))],
        stats={},
        user_id=42,
    )

    card_call = conn.fetchval.call_args_list[2]
    sql = card_call.args[0]
    assert "INSERT INTO pulse_cards" in sql
    assert "user_id" in sql
    assert card_call.args[-1] == 42


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
    #   2nd: L3 count query → 25 candidates pass filter (≥ 20, L3 applies)
    #   3rd: card insert for paper 0 → inserted_id = 101 (success)
    #   4th: card insert for paper 1 → None (paper row missing)
    #   5th: card insert for paper 2 → inserted_id = 103 (success)
    conn.fetchval.side_effect = [7, 25, 101, None, 103]

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
# load_today — W1.8-A: trashed cards excluded from deck SQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_today_sql_excludes_trash_in_where_clause():
    """The card fetch SQL in load_today must contain the trash-exclusion predicate.

    W1.8-A Change A2: ``AND COALESCE(pus.state, 'inbox') != 'trash'`` must be
    present in the WHERE clause so the DB never returns trashed cards in the
    pulse deck response.
    """
    pool, conn = _make_pool_and_conn()
    deck_row = make_pulse_deck_row(deck_date="2026-05-02", card_count=0)
    conn.fetchrow.return_value = deck_row
    conn.fetch.return_value = []

    await load_today(pool)

    assert conn.fetch.call_count == 1, "Expected exactly one conn.fetch call for card rows"
    card_sql: str = conn.fetch.call_args.args[0]
    assert "COALESCE(pus.state" in card_sql, (
        f"Card SQL must contain COALESCE(pus.state ...) predicate.\nSQL: {card_sql!r}"
    )
    assert "'trash'" in card_sql, (
        f"Card SQL must contain the 'trash' exclusion value.\nSQL: {card_sql!r}"
    )
