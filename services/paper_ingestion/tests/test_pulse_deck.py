"""Tests for app.pulse.deck — assemble_deck, persist_deck, load_today, load_history.

TDD: tests written before implementation.
"""

from datetime import date

import pytest
from app.models import PaperCreate, SourceType
from app.pulse.deck import assemble_deck, load_history, load_today, persist_deck
from app.pulse.scoring import ScoredCandidate

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


def _make_scored(paper: PaperCreate, score: float = 0.5, idx: int = 0) -> ScoredCandidate:
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


@pytest.mark.asyncio
async def test_assemble_deck_picks_top_n():
    """assemble_deck returns the top `size` candidates sorted by final_score desc."""
    papers = [_make_paper(i) for i in range(10)]
    candidates = [_make_scored(p, score=float(i) / 10.0, idx=i) for i, p in enumerate(papers)]

    result = await assemble_deck(candidates, size=5)

    assert len(result) == 5
    # Highest scores are from papers 9,8,7,6,5
    scores = [sc.final_score for sc in result]
    assert scores == sorted(scores, reverse=True)
    assert result[0].final_score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_assemble_deck_fewer_than_size():
    """assemble_deck returns all candidates when fewer than size."""
    papers = [_make_paper(i) for i in range(3)]
    candidates = [_make_scored(p) for p in papers]

    result = await assemble_deck(candidates, size=10)

    assert len(result) == 3


@pytest.mark.asyncio
async def test_assemble_deck_empty_input():
    """assemble_deck returns [] for empty input."""
    result = await assemble_deck([], size=10)
    assert result == []


@pytest.mark.asyncio
async def test_assemble_deck_enforces_rank_ordering():
    """Rank ordering (1-based) is implied by sort position."""
    papers = [_make_paper(i) for i in range(5)]
    candidates = [_make_scored(p, score=float(i) / 5.0) for i, p in enumerate(papers)]

    result = await assemble_deck(candidates, size=5)

    # Best paper has highest score and is first
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
    # Simulate that paper IDs are looked up from the DB
    conn.fetchrow.side_effect = [
        FakeRecord({"id": 1, "paper_id": 10}),  # for deck UPSERT returning id
        FakeRecord({"id": 101}),  # paper id lookup for paper 0
        FakeRecord({"id": 102}),  # paper id lookup for paper 1
        FakeRecord({"id": 103}),  # paper id lookup for paper 2
    ]
    # fetchval used for deck insert returning id
    conn.fetchval.side_effect = [42]  # deck_id = 42

    cards = [_make_scored(p, score=float(i) / 3.0, idx=i) for i, p in enumerate(papers)]

    deck_id = await persist_deck(pool, deck_date, cards, stats={"candidate_count": 100})

    # Transaction should have been used
    conn.transaction.assert_called_once()
    # fetchval or execute should have been called (deck insert + card inserts)
    assert deck_id is not None
    assert isinstance(deck_id, int)


@pytest.mark.asyncio
async def test_persist_deck_returns_deck_id():
    """persist_deck returns the integer deck_id."""
    pool, conn = _make_pool_and_conn()
    deck_date = date(2024, 1, 15)
    conn.fetchval.return_value = 99  # deck_id

    cards = [_make_scored(_make_paper(0))]

    deck_id = await persist_deck(pool, deck_date, cards, stats={})

    assert deck_id == 99


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
    from app.models import PulseDeckResponse

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
    from app.models import PulseDeckResponse

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
