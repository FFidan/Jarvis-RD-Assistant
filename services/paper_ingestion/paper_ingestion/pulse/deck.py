"""Pulse deck assembly, persistence, and retrieval.

Provides assemble_deck, persist_deck, load_today, load_history.
All DB operations use asyncpg pool patterns consistent with the rest of the service.
"""

import logging
from datetime import date, datetime
from typing import Any

from jarvis_common.paper_visibility import PUBLIC_VISIBILITY_SCOPE

from paper_ingestion.models import PulseCardResponse, PulseDeckResponse
from paper_ingestion.pulse.scoring import ScoredCandidate
from paper_ingestion.queries.predicates import (
    EXCLUDED_STATE_SQL,
    VIEW_PREDICATES,
    paper_visible_sql,
)

logger = logging.getLogger(__name__)

# A pulse card renders its paper's title, authors and url to the deck owner, so
# it may only reference a paper that carries shared scope.  Pulse promotes each
# candidate to public scope in a per-card savepoint before the deck is
# persisted; when that promotion does not complete, whatever row already owns
# the external id stays as it is.  The card INSERT therefore re-reads the
# persisted scope rather than assuming the promotion succeeded.
_SHARED_PAPER_SQL = f"p.visibility_scope = '{PUBLIC_VISIBILITY_SCOPE}'"


def assemble_deck(
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


async def exclude_recently_dismissed(
    db_pool: Any,
    scored: list[ScoredCandidate],
    user_id: int | None = None,
) -> list[ScoredCandidate]:
    """Drop candidates the deck owner rated down within the last 60 days.

    One round trip resolves the whole candidate pool.  A candidate with no
    ``papers`` row yet cannot carry feedback, so it is always kept.

    Parameters
    ----------
    db_pool:
        asyncpg connection pool.
    scored:
        Candidates in rank order; survivors keep that order.
    user_id:
        The deck owner.  ``None`` means single-tenant mode and matches
        ``recommendation_feedback`` rows whose ``user_id`` is NULL.

    Returns
    -------
    list[ScoredCandidate]
        The candidates carrying no recent negative rating.
    """
    if not scored:
        return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT p.external_id
            FROM papers p
            JOIN recommendation_feedback rf ON rf.paper_id = p.id
            WHERE p.external_id = ANY($1::text[])
              AND rf.signal = 'negative'
              AND rf.created_at > NOW() - INTERVAL '60 days'
              AND rf.user_id IS NOT DISTINCT FROM $2
            """,
            [sc.paper.external_id for sc in scored],
            user_id,
        )
    dismissed = {row["external_id"] for row in rows}
    return [sc for sc in scored if sc.paper.external_id not in dismissed]


async def _persist_deck_inner(
    conn: Any,
    deck_date: date,
    cards: list[ScoredCandidate],
    stats: dict,
    degraded_reason: str | None = None,
    user_id: int | None = None,
) -> int:
    """Execute the deck-persistence SQL on an existing connection.

    Must be called inside an active transaction.  Separated from
    ``persist_deck`` so that callers can share a connection (and therefore
    a transaction) with other writes such as ``upsert_paper``.

    Parameters
    ----------
    user_id:
        The user this deck belongs to.  ``None`` means single-tenant mode
        (matches ``paper_user_state`` rows where ``user_id IS NULL``).  In
        multi-tenant deployments pass the concrete user PK so that
        archived/dismissed state for *this* user is correctly applied.

    Returns
    -------
    int
        The number of pulse_cards rows successfully inserted.
    """
    # Upsert pulse_decks row with card_count=0 initially — returns the deck id.
    # ON CONFLICT targets (deck_date, user_id) — the composite UNIQUE NULLS NOT
    # DISTINCT constraint added by migration 043 for multi-tenant support.
    deck_id = await conn.fetchval(
        """
        INSERT INTO pulse_decks
            (deck_date, user_id, card_count, generated_at, stats, degraded_reason)
        VALUES ($1, $4, 0, NOW(), $2::jsonb, $3)
        ON CONFLICT (deck_date, user_id) DO UPDATE
            SET card_count       = 0,
                generated_at     = EXCLUDED.generated_at,
                stats            = EXCLUDED.stats,
                degraded_reason  = EXCLUDED.degraded_reason
        RETURNING id
        """,
        deck_date,
        stats,
        degraded_reason,
        user_id,
    )

    # Delete old cards for this deck (idempotent replace)
    await conn.execute("DELETE FROM pulse_cards WHERE deck_id = $1", deck_id)

    # Insert new cards one by one, counting actual successes.  Candidates the
    # owner rated down are removed while the deck is being selected, so every
    # card arriving here has already cleared that exclusion.
    successes = 0
    for rank, sc in enumerate(cards, start=1):
        reasoning_confidence_str = (
            sc.reasoning_confidence.value if sc.reasoning_confidence is not None else None
        )
        inserted_id = await conn.fetchval(
            f"""
            INSERT INTO pulse_cards
                (deck_id, user_id, paper_id, rank, score, llm_relevance, llm_novelty,
                 reasoning, signals, reasoning_verified, reasoning_confidence)
            SELECT $1, $11, p.id, $3, $4, $5, $6, $7, $8::jsonb, $9, $10
            FROM papers p
            LEFT JOIN paper_user_state pus
                   ON pus.paper_id = p.id
                  AND pus.user_id IS NOT DISTINCT FROM $11
            WHERE p.external_id = $2
              AND {_SHARED_PAPER_SQL}
              AND NOT ({EXCLUDED_STATE_SQL})
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
            sc.reasoning_verified,
            reasoning_confidence_str,
            user_id,
        )
        if inserted_id is not None:
            successes += 1
        else:
            logger.warning(
                "pulse.persist_deck: skipped %r — no shared paper row for this external id",
                sc.paper.external_id,
            )

    # Update deck row with the actual number of successfully inserted cards
    await conn.execute(
        "UPDATE pulse_decks SET card_count = $1 WHERE id = $2",
        successes,
        deck_id,
    )

    return successes


async def persist_deck(
    db_pool: Any,
    deck_date: date,
    cards: list[ScoredCandidate],
    stats: dict,
    conn: Any | None = None,
    degraded_reason: str | None = None,
    user_id: int | None = None,
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
    user_id:
        The user this deck belongs to.  ``None`` means single-tenant mode
        (matches ``paper_user_state`` rows where ``user_id IS NULL``).  In
        multi-tenant deployments pass the concrete user PK so that
        archived/dismissed state for *this* user is correctly applied.

    Returns
    -------
    int
        The number of pulse_cards rows successfully inserted.
    """
    if conn is not None:
        return await _persist_deck_inner(conn, deck_date, cards, stats, degraded_reason, user_id)

    async with db_pool.acquire() as acquired_conn:
        async with acquired_conn.transaction():
            return await _persist_deck_inner(
                acquired_conn, deck_date, cards, stats, degraded_reason, user_id
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
            reasoning_verified=r.get("reasoning_verified"),
            reasoning_confidence=r.get("reasoning_confidence"),
            user_state=r.get("user_state"),
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
        # Read-time visibility and lifecycle predicates can remove cards that
        # existed when the deck row was written. Report what this response
        # actually carries so callers can distinguish a usable deck from an
        # empty one and activate stale fallback honestly.
        card_count=len(cards),
        generated_at=generated_at,
        cards=cards,
        stats=deck_row.get("stats") or {},
        degraded_reason=deck_row.get("degraded_reason"),
    )


async def load_today(
    db_pool: Any,
    user_id: int | None = None,
) -> "PulseDeckResponse | None":
    """Fetch today's pulse deck joined with paper metadata + per-user lifecycle state.

    Returns
    -------
    PulseDeckResponse | None
        Today's deck or None if no deck has been generated yet. Each card's
        ``user_state`` reflects the current ``paper_user_state`` row for the
        caller (NULL ⇒ inbox-default; consumers gate the Save↔Unsave toggle on
        ``user_state == 'to_read'``).

    Notes
    -----
    A card renders its paper's title, authors and url, so all three deck reads
    re-check visibility rather than trusting the scope the paper held when the
    deck was persisted.  Nothing downgrades a paper today, so the predicate is
    inert; it keeps the reads correct if a downgrade path is ever added.
    """
    async with db_pool.acquire() as conn:
        deck_row = await conn.fetchrow(
            """
            SELECT id, deck_date, card_count, generated_at, stats, degraded_reason
            FROM pulse_decks
            WHERE deck_date = CURRENT_DATE
              AND user_id IS NOT DISTINCT FROM $1
            """,
            user_id,
        )
        if deck_row is None:
            return None

        card_rows = await conn.fetch(
            f"""
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
                pc.signals,
                pc.reasoning_verified,
                pc.reasoning_confidence,
                pus.state AS user_state
            FROM pulse_cards pc
            JOIN papers p ON p.id = pc.paper_id
            LEFT JOIN paper_user_state pus
                   ON pus.paper_id = p.id
                  AND pus.user_id IS NOT DISTINCT FROM $2
            WHERE pc.deck_id = $1
              AND {VIEW_PREDICATES["all_non_trash"]}
              AND {paper_visible_sql(2)}
            ORDER BY pc.rank ASC
            """,
            deck_row["id"],
            user_id,
        )

    return _build_deck_response(deck_row, card_rows)


async def load_history(
    db_pool: Any,
    days: int = 30,
    user_id: int | None = None,
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
              AND deck_date >= CURRENT_DATE - $1 * INTERVAL '1 day'
              AND user_id IS NOT DISTINCT FROM $2
            ORDER BY deck_date DESC
            """,
            days,
            user_id,
        )

        if not deck_rows:
            return []

        deck_ids = [row["id"] for row in deck_rows]

        # Batch-fetch all cards for all decks in a single query (avoids N+1)
        all_card_rows = await conn.fetch(
            f"""
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
                pc.signals,
                pc.reasoning_verified,
                pc.reasoning_confidence,
                pus.state AS user_state
            FROM pulse_cards pc
            JOIN papers p ON p.id = pc.paper_id
            LEFT JOIN paper_user_state pus
                   ON pus.paper_id = p.id
                  AND pus.user_id IS NOT DISTINCT FROM $2
            WHERE pc.deck_id = ANY($1::int[])
              AND {paper_visible_sql(2)}
            ORDER BY pc.deck_id, pc.rank ASC
            """,
            deck_ids,
            user_id,
        )

    # Group cards by deck_id in Python
    cards_by_deck: dict[int, list] = {}
    for card_row in all_card_rows:
        cards_by_deck.setdefault(card_row["deck_id"], []).append(card_row)

    return [
        _build_deck_response(deck_row, cards_by_deck.get(deck_row["id"], []))
        for deck_row in deck_rows
    ]


async def load_last_nonempty_deck(
    db_pool: Any,
    user_id: int | None,
    max_age_days: int = 7,
) -> "PulseDeckResponse | None":
    """Return the most recent non-empty deck within ``max_age_days`` for this user.

    Loads full deck + cards (mirroring ``load_today``) so the fallback response
    actually contains content for the frontend to render.
    """
    async with db_pool.acquire() as conn:
        deck_row = await conn.fetchrow(
            f"""
            SELECT pd.id, pd.deck_date, pd.card_count, pd.generated_at,
                   pd.stats, pd.degraded_reason
            FROM pulse_decks pd
            WHERE pd.user_id IS NOT DISTINCT FROM $1
              AND pd.card_count > 0
              AND pd.deck_date >= (CURRENT_DATE - $2::int)
              AND EXISTS (
                  SELECT 1
                  FROM pulse_cards eligible_pc
                  JOIN papers p ON p.id = eligible_pc.paper_id
                  LEFT JOIN paper_user_state pus
                         ON pus.paper_id = p.id
                        AND pus.user_id IS NOT DISTINCT FROM $1
                  WHERE eligible_pc.deck_id = pd.id
                    AND {VIEW_PREDICATES["all_non_trash"]}
                    AND {paper_visible_sql(1)}
                    AND NOT EXISTS (
                        SELECT 1
                        FROM recommendation_feedback rf
                        WHERE rf.paper_id = eligible_pc.paper_id
                          AND rf.user_id IS NOT DISTINCT FROM $1
                          AND rf.signal = 'negative'
                          AND rf.created_at > NOW() - INTERVAL '60 days'
                    )
              )
            ORDER BY pd.deck_date DESC
            LIMIT 1
            """,
            user_id,
            max_age_days,
        )
        if deck_row is None:
            return None

        card_rows = await conn.fetch(
            f"""
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
                pc.signals,
                pc.reasoning_verified,
                pc.reasoning_confidence,
                pus.state AS user_state
            FROM pulse_cards pc
            JOIN papers p ON p.id = pc.paper_id
            LEFT JOIN paper_user_state pus
                   ON pus.paper_id = p.id
                  AND pus.user_id IS NOT DISTINCT FROM $2
            WHERE pc.deck_id = $1
              AND {VIEW_PREDICATES["all_non_trash"]}
              AND {paper_visible_sql(2)}
              AND NOT EXISTS (
                  SELECT 1
                  FROM recommendation_feedback rf
                  WHERE rf.paper_id = pc.paper_id
                    AND rf.user_id IS NOT DISTINCT FROM $2
                    AND rf.signal = 'negative'
                    AND rf.created_at > NOW() - INTERVAL '60 days'
              )
            ORDER BY pc.rank ASC
            """,
            deck_row["id"],
            user_id,
        )

    return _build_deck_response(deck_row, card_rows)
