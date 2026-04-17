"""Pulse deck assembly, persistence, and retrieval.

Provides assemble_deck, persist_deck, load_today, load_history.
All DB operations use asyncpg pool patterns consistent with the rest of the service.
"""

import logging
from datetime import date, datetime
from typing import Any

from app.models import PulseCardResponse, PulseDeckResponse
from app.pulse.scoring import ScoredCandidate

logger = logging.getLogger(__name__)


async def assemble_deck(
    scored: list[ScoredCandidate],
    size: int = 10,
) -> list[ScoredCandidate]:
    """Pick the top `size` candidates by final_score, enforcing rank ordering.

    Parameters
    ----------
    scored:
        Output of stage3_combine (already sorted descending by final_score).
    size:
        Maximum number of cards to include in the deck.

    Returns
    -------
    list[ScoredCandidate]
        Top-size candidates sorted by final_score descending.
    """
    if not scored:
        return []
    # Ensure sorted (stage3 already sorts, but be defensive)
    top = sorted(scored, key=lambda sc: sc.final_score or 0.0, reverse=True)
    return top[:size]


async def _persist_deck_inner(
    conn: Any,
    deck_date: date,
    cards: list[ScoredCandidate],
    stats: dict,
    degraded_reason: str | None = None,
) -> int:
    """Execute the deck-persistence SQL on an existing connection.

    Must be called inside an active transaction.  Separated from
    ``persist_deck`` so that callers can share a connection (and therefore
    a transaction) with other writes such as ``upsert_paper``.
    """
    # Upsert pulse_decks row with card_count=0 initially — returns the deck id
    deck_id = await conn.fetchval(
        """
        INSERT INTO pulse_decks (deck_date, card_count, generated_at, stats, degraded_reason)
        VALUES ($1, 0, NOW(), $2::jsonb, $3)
        ON CONFLICT (deck_date) DO UPDATE
            SET card_count       = 0,
                generated_at     = EXCLUDED.generated_at,
                stats            = EXCLUDED.stats,
                degraded_reason  = EXCLUDED.degraded_reason
        RETURNING id
        """,
        deck_date,
        stats,
        degraded_reason,
    )

    # Delete old cards for this deck (idempotent replace)
    await conn.execute("DELETE FROM pulse_cards WHERE deck_id = $1", deck_id)

    # Insert new cards one by one, counting actual successes
    successes = 0
    for rank, sc in enumerate(cards, start=1):
        inserted_id = await conn.fetchval(
            """
            INSERT INTO pulse_cards
                (deck_id, paper_id, rank, score, llm_relevance, llm_novelty,
                 reasoning, signals)
            SELECT $1, p.id, $3, $4, $5, $6, $7, $8::jsonb
            FROM papers p
            WHERE p.external_id = $2
            ON CONFLICT (deck_id, paper_id) DO NOTHING
            RETURNING id
            """,
            deck_id,
            sc.paper.external_id,
            rank,
            sc.final_score or 0.0,
            sc.llm_relevance,
            sc.llm_novelty,
            sc.reasoning,
            sc.signals,
        )
        if inserted_id is not None:
            successes += 1
        else:
            logger.warning(
                "pulse.persist_deck: skipped %r — paper row missing",
                sc.paper.external_id,
            )

    # Update deck row with the actual number of successfully inserted cards
    await conn.execute(
        "UPDATE pulse_decks SET card_count = $1 WHERE id = $2",
        successes,
        deck_id,
    )

    return deck_id


async def persist_deck(
    db_pool: Any,
    deck_date: date,
    cards: list[ScoredCandidate],
    stats: dict,
    conn: Any | None = None,
    degraded_reason: str | None = None,
) -> int:
    """Persist a pulse deck to the database in a single transaction.

    Idempotent: if a deck already exists for deck_date, the existing row is
    updated (card_count, generated_at, stats refreshed) and old cards are
    deleted before new ones are inserted.

    Parameters
    ----------
    db_pool:
        asyncpg connection pool.
    deck_date:
        The date this deck covers (unique per day).
    cards:
        Scored candidates to persist as pulse_cards rows.
    stats:
        Arbitrary pipeline statistics stored in the pulse_decks.stats JSONB.
    conn:
        Optional existing asyncpg connection.  When provided the function
        executes on that connection without acquiring a new one or opening a
        new transaction (the caller is responsible for the surrounding
        transaction).  When ``None`` (default) a new connection is acquired
        from ``db_pool`` and wrapped in its own transaction.
    degraded_reason:
        Optional human-readable string explaining why the deck was produced
        with reduced quality (e.g. LLM timeout or stage2 fallback).  Stored
        in the ``pulse_decks.degraded_reason`` column added by migration 023.

    Returns
    -------
    int
        The pulse_decks.id of the upserted deck.
    """
    if conn is not None:
        return await _persist_deck_inner(conn, deck_date, cards, stats, degraded_reason)

    async with db_pool.acquire() as acquired_conn:
        async with acquired_conn.transaction():
            return await _persist_deck_inner(
                acquired_conn, deck_date, cards, stats, degraded_reason
            )


def _build_deck_response(
    deck_row: Any,
    card_rows: list[Any],
) -> PulseDeckResponse:
    """Convert DB rows to a PulseDeckResponse."""
    cards = [
        PulseCardResponse(
            card_id=r["id"],
            paper_id=r["paper_id"],
            paper_title=r["paper_title"],
            paper_authors=r.get("paper_authors") or [],
            paper_url=r.get("paper_url"),
            rank=r["rank"],
            score=float(r["score"]),
            llm_relevance=r.get("llm_relevance"),
            llm_novelty=r.get("llm_novelty"),
            reasoning=r.get("reasoning"),
            signals=r.get("signals") or {},
        )
        for r in card_rows
    ]
    # Parse generated_at — may be string or datetime
    generated_at = deck_row["generated_at"]
    if isinstance(generated_at, str):
        generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))

    deck_date = deck_row["deck_date"]
    if isinstance(deck_date, str):
        deck_date = date.fromisoformat(deck_date)

    return PulseDeckResponse(
        deck_id=deck_row["id"],
        deck_date=deck_date,
        card_count=deck_row["card_count"],
        generated_at=generated_at,
        cards=cards,
        stats=deck_row.get("stats") or {},
        degraded_reason=deck_row.get("degraded_reason"),
    )


async def load_today(db_pool: Any) -> "PulseDeckResponse | None":
    """Fetch today's pulse deck joined with paper metadata.

    Returns
    -------
    PulseDeckResponse | None
        Today's deck or None if no deck has been generated yet.
    """
    async with db_pool.acquire() as conn:
        deck_row = await conn.fetchrow(
            """
            SELECT id, deck_date, card_count, generated_at, stats, degraded_reason
            FROM pulse_decks
            WHERE deck_date = CURRENT_DATE
            """
        )
        if deck_row is None:
            return None

        card_rows = await conn.fetch(
            """
            SELECT
                pc.id,
                pc.deck_id,
                pc.paper_id,
                p.title   AS paper_title,
                p.authors AS paper_authors,
                p.url     AS paper_url,
                pc.rank,
                pc.score,
                pc.llm_relevance,
                pc.llm_novelty,
                pc.reasoning,
                pc.signals
            FROM pulse_cards pc
            JOIN papers p ON p.id = pc.paper_id
            WHERE pc.deck_id = $1
            ORDER BY pc.rank ASC
            """,
            deck_row["id"],
        )

    return _build_deck_response(deck_row, card_rows)


async def load_history(
    db_pool: Any,
    days: int = 30,
) -> list["PulseDeckResponse"]:
    """Fetch pulse decks from the last `days` days, newest first.

    Parameters
    ----------
    db_pool:
        asyncpg connection pool.
    days:
        How many calendar days back to query (default 30).

    Returns
    -------
    list[PulseDeckResponse]
        Historical decks sorted newest-first. Does not include today.
    """
    async with db_pool.acquire() as conn:
        deck_rows = await conn.fetch(
            """
            SELECT id, deck_date, card_count, generated_at, stats, degraded_reason
            FROM pulse_decks
            WHERE deck_date < CURRENT_DATE
              AND deck_date >= CURRENT_DATE - ($1 || ' days')::INTERVAL
            ORDER BY deck_date DESC
            """,
            str(days),
        )

        if not deck_rows:
            return []

        deck_ids = [row["id"] for row in deck_rows]

        # Batch-fetch all cards for all decks in a single query (avoids N+1)
        all_card_rows = await conn.fetch(
            """
            SELECT
                pc.id,
                pc.deck_id,
                pc.paper_id,
                p.title   AS paper_title,
                p.authors AS paper_authors,
                p.url     AS paper_url,
                pc.rank,
                pc.score,
                pc.llm_relevance,
                pc.llm_novelty,
                pc.reasoning,
                pc.signals
            FROM pulse_cards pc
            JOIN papers p ON p.id = pc.paper_id
            WHERE pc.deck_id = ANY($1::int[])
            ORDER BY pc.deck_id, pc.rank ASC
            """,
            deck_ids,
        )

    # Group cards by deck_id in Python
    cards_by_deck: dict[int, list] = {}
    for card_row in all_card_rows:
        cards_by_deck.setdefault(card_row["deck_id"], []).append(card_row)

    return [
        _build_deck_response(deck_row, cards_by_deck.get(deck_row["id"], []))
        for deck_row in deck_rows
    ]
