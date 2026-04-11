"""Pulse deck assembly, persistence, and retrieval.

Provides assemble_deck, persist_deck, load_today, load_history.
All DB operations use asyncpg pool patterns consistent with the rest of the service.
"""

import json
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


async def persist_deck(
    db_pool: Any,
    deck_date: date,
    cards: list[ScoredCandidate],
    stats: dict,
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

    Returns
    -------
    int
        The pulse_decks.id of the upserted deck.
    """
    stats_json = json.dumps(stats)

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Upsert pulse_decks row with card_count=0 initially — returns the deck id
            deck_id = await conn.fetchval(
                """
                INSERT INTO pulse_decks (deck_date, card_count, generated_at, stats)
                VALUES ($1, 0, NOW(), $2::jsonb)
                ON CONFLICT (deck_date) DO UPDATE
                    SET card_count    = 0,
                        generated_at  = EXCLUDED.generated_at,
                        stats         = EXCLUDED.stats
                RETURNING id
                """,
                deck_date,
                stats_json,
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
                    json.dumps(sc.signals),
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
            SELECT id, deck_date, card_count, generated_at, stats
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
            SELECT id, deck_date, card_count, generated_at, stats
            FROM pulse_decks
            WHERE deck_date < CURRENT_DATE
              AND deck_date >= CURRENT_DATE - ($1 || ' days')::INTERVAL
            ORDER BY deck_date DESC
            """,
            str(days),
        )

        result: list[PulseDeckResponse] = []
        for deck_row in deck_rows:
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
            result.append(_build_deck_response(deck_row, card_rows))

    return result
