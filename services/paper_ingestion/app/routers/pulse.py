"""Pulse REST endpoints — generate, today, history, rate, explain, stats, debug.

All endpoints require API key auth via ``verify_api_key`` and are rate-limited
by the shared slowapi ``limiter`` from ``app.deps``.

Thin orchestration layer — the heavy lifting lives in ``app.pulse.job``,
``app.pulse.deck``, and the scoring stages.
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import current_user_id, log_audit, verify_api_key
from jarvis_common import jobs as jobs_lib

from app.deps import get_db_pool, limiter
from app.models import (
    PulseDeckResponse,
    PulseGenerateResponse,
    PulseRateRequest,
    PulseStatsResponse,
)
from app.pulse.deck import load_history, load_today

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/pulse",
    tags=["pulse"],
    dependencies=[Depends(verify_api_key)],
)


# ---------------------------------------------------------------------------
# POST /api/pulse/generate
# ---------------------------------------------------------------------------


@router.post("/generate", response_model=PulseGenerateResponse)
@limiter.limit("3/hour")
async def generate_pulse(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PulseGenerateResponse:
    """Enqueue an on-demand Pulse deck generation job.

    Returns immediately with a ``job_id`` so the caller can poll
    ``GET /api/jobs/{job_id}`` for progress.  Rate-limited to 3/hour to
    prevent runaway LLM usage from accidental mass clicks.
    """
    logger.info("pulse.generate: enqueueing job")
    job_id = await jobs_lib.enqueue(db_pool, "pulse.generate", payload={})
    await log_audit(
        db_pool,
        action="pulse_generate_enqueued",
        resource="pulse:deck",
        metadata={"job_id": job_id},
    )
    return PulseGenerateResponse(job_id=job_id, status="queued")


# ---------------------------------------------------------------------------
# GET /api/pulse/today
# ---------------------------------------------------------------------------


@router.get("/today", response_model=PulseDeckResponse)
@limiter.limit("30/minute")
async def get_today(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PulseDeckResponse:
    """Fetch today's Pulse deck (404 if not generated yet)."""
    deck = await load_today(db_pool)
    if deck is None:
        raise HTTPException(status_code=404, detail="No Pulse deck for today")
    return deck


# ---------------------------------------------------------------------------
# GET /api/pulse/history
# ---------------------------------------------------------------------------


@router.get("/history", response_model=list[PulseDeckResponse])
@limiter.limit("30/minute")
async def get_history(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[PulseDeckResponse]:
    """Return Pulse decks from the last *days* days, newest first."""
    return await load_history(db_pool, days=days)


# ---------------------------------------------------------------------------
# POST /api/pulse/rate
# ---------------------------------------------------------------------------


@router.post("/rate")
@limiter.limit("60/minute")
async def rate_card(
    request: Request,
    body: PulseRateRequest,
    user_id: int | None = Depends(current_user_id),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict:
    """Persist a user rating for a Pulse-shown paper.

    Rating is stored in ``pulse_ratings`` with ``source='pulse'`` so that the
    profile centroid refresh can distinguish Pulse clicks from library thumbs.

    Guard: the paper must belong to a Pulse deck before it can be rated.
    Duplicate ratings (double-click) are handled by ON CONFLICT DO UPDATE.
    """
    try:
        async with db_pool.acquire() as conn:
            # Guard: paper must exist in a pulse deck
            member = await conn.fetchval(
                """SELECT 1 FROM pulse_cards pc
                   JOIN pulse_decks pd ON pc.deck_id = pd.id
                   WHERE pc.paper_id = $1
                   LIMIT 1""",
                body.paper_id,
            )
            if not member:
                raise HTTPException(status_code=404, detail="Paper not found in your pulse deck")

            await conn.execute(
                """INSERT INTO pulse_ratings (paper_id, user_id, rating, source)
                   VALUES ($1, $2, $3, 'pulse')
                   ON CONFLICT (paper_id, user_id) DO UPDATE
                     SET rating    = EXCLUDED.rating,
                         created_at = NOW()""",
                body.paper_id,
                user_id,
                body.rating,
            )
    except asyncpg.ForeignKeyViolationError as exc:
        raise HTTPException(status_code=404, detail="Paper not found") from exc
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# GET /api/pulse/explain/{card_id}
# ---------------------------------------------------------------------------


@router.get("/explain/{card_id}")
@limiter.limit("30/minute")
async def explain_card(
    request: Request,
    card_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict:
    """Return the reasoning + signal breakdown for a single Pulse card."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, reasoning, signals, llm_relevance, llm_novelty
            FROM pulse_cards
            WHERE id = $1
            """,
            card_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Pulse card not found")
    return {
        "card_id": row["id"],
        "reasoning": row["reasoning"],
        "signals": row["signals"] or {},
        "llm_relevance": row["llm_relevance"],
        "llm_novelty": row["llm_novelty"],
    }


# ---------------------------------------------------------------------------
# GET /api/pulse/stats
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=PulseStatsResponse)
@limiter.limit("30/minute")
async def get_stats(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PulseStatsResponse:
    """Aggregate Pulse run stats over the past *days* days."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS decks_generated,
                AVG(NULLIF((stats->>'candidate_count')::float, 0)) AS avg_candidates,
                AVG((stats->>'llm_calls')::float) AS avg_llm_calls,
                AVG((stats->>'duration_s')::float) AS avg_duration_s,
                MAX(generated_at) AS last_run_at,
                (
                    SELECT stats->>'last_error'
                    FROM pulse_decks
                    WHERE stats->>'last_error' IS NOT NULL
                      AND generated_at >= NOW() - make_interval(days => $1)
                    ORDER BY generated_at DESC
                    LIMIT 1
                ) AS last_error,
                (
                    SELECT degraded_reason
                    FROM pulse_decks
                    WHERE degraded_reason IS NOT NULL
                      AND generated_at >= NOW() - make_interval(days => $1)
                    ORDER BY generated_at DESC
                    LIMIT 1
                ) AS degraded_reason
            FROM pulse_decks
            WHERE generated_at >= NOW() - make_interval(days => $1)
            """,
            days,
        )
    if row is None:
        return PulseStatsResponse(
            window_days=days,
            decks_generated=0,
            avg_candidates=None,
            avg_llm_calls=None,
            avg_duration_s=None,
            last_run_at=None,
            last_error=None,
            degraded_reason=None,
        )
    return PulseStatsResponse(
        window_days=days,
        decks_generated=int(row["decks_generated"] or 0),
        avg_candidates=row["avg_candidates"],
        avg_llm_calls=row["avg_llm_calls"],
        avg_duration_s=row["avg_duration_s"],
        last_run_at=row["last_run_at"],
        last_error=row["last_error"],
        degraded_reason=row["degraded_reason"],
    )


# ---------------------------------------------------------------------------
# GET /api/pulse/debug
# ---------------------------------------------------------------------------


@router.get("/debug")
@limiter.limit("30/minute")
async def debug_pulse(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict:
    """Return diagnostics for the latest Pulse deck.

    Includes:
    * Per-source candidate counts from the latest deck's ``stats`` JSONB.
    * Topic-embedding sanity check (non-null, correct dimension).
    * Top-10 card signal breakdown (paper_id, title, signals, final_score).
    """
    async with db_pool.acquire() as conn:
        # Fetch the most recent deck row
        deck_row = await conn.fetchrow(
            """
            SELECT id, deck_date, card_count, generated_at, stats, degraded_reason
            FROM pulse_decks
            ORDER BY generated_at DESC
            LIMIT 1
            """
        )
        if deck_row is None:
            raise HTTPException(status_code=404, detail="No Pulse deck found")

        deck_stats: dict = deck_row["stats"] or {}

        # Fetch top 10 cards with paper metadata
        card_rows = await conn.fetch(
            """
            SELECT
                pc.id        AS card_id,
                pc.paper_id,
                p.title      AS paper_title,
                pc.rank,
                pc.score     AS final_score,
                pc.llm_relevance,
                pc.llm_novelty,
                pc.signals
            FROM pulse_cards pc
            JOIN papers p ON p.id = pc.paper_id
            WHERE pc.deck_id = $1
            ORDER BY pc.rank ASC
            LIMIT 10
            """,
            deck_row["id"],
        )

        # Topic-embedding sanity check
        embed_rows = await conn.fetch(
            """
            SELECT key, value
            FROM user_config
            WHERE key LIKE 'topic.%.embedding'
            """
        )

    # Per-source candidate breakdown (from stats JSONB)
    source_counts: dict = deck_stats.get("source_counts", {})

    # Topic embedding sanity
    embed_dim_expected = 768
    topic_embeddings: list[dict] = []
    for er in embed_rows:
        val = er["value"]
        if isinstance(val, list):
            dim = len(val)
            topic_embeddings.append(
                {
                    "key": er["key"],
                    "dim": dim,
                    "ok": dim == embed_dim_expected,
                    "non_null": True,
                }
            )
        else:
            topic_embeddings.append(
                {
                    "key": er["key"],
                    "dim": None,
                    "ok": False,
                    "non_null": val is not None,
                }
            )

    # Top-N card breakdown
    top_cards = [
        {
            "card_id": r["card_id"],
            "paper_id": r["paper_id"],
            "title": r["paper_title"],
            "signals": r["signals"] or {},
            "final_score": float(r["final_score"]),
            "llm_relevance": r["llm_relevance"],
            "llm_novelty": r["llm_novelty"],
        }
        for r in card_rows
    ]

    return {
        "deck_date": deck_row["deck_date"].isoformat()
        if hasattr(deck_row["deck_date"], "isoformat")
        else deck_row["deck_date"],
        "card_count": deck_row["card_count"],
        "degraded_reason": deck_row["degraded_reason"],
        "source_counts": source_counts,
        "topic_embeddings": topic_embeddings,
        "top_cards": top_cards,
    }
