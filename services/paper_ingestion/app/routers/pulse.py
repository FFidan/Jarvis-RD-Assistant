"""Pulse REST endpoints — generate, today, history, rate, explain, stats.

All endpoints require API key auth via ``verify_api_key`` and are rate-limited
by the shared slowapi ``limiter`` from ``app.deps``.

Thin orchestration layer — the heavy lifting lives in ``app.pulse.job``,
``app.pulse.deck``, and the scoring stages.
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import verify_api_key

from app.deps import limiter
from app.models import (
    PulseDeckResponse,
    PulseRateRequest,
    PulseStatsResponse,
)
from app.pulse.deck import load_history, load_today
from app.pulse.job import run_pulse

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/pulse",
    tags=["pulse"],
    dependencies=[Depends(verify_api_key)],
)


# ---------------------------------------------------------------------------
# POST /api/pulse/generate
# ---------------------------------------------------------------------------


@router.post("/generate", response_model=PulseDeckResponse)
@limiter.limit("3/hour")
async def generate_pulse(request: Request) -> PulseDeckResponse:
    """Trigger an on-demand Pulse deck generation.

    Returns the freshly-persisted deck.  Rate-limited to 3/hour to prevent
    runaway LLM usage from accidental mass clicks.
    """
    app = request.app
    logger.info("pulse.generate: manual trigger")
    stats = await run_pulse(
        db_pool=app.state.db_pool,
        http_client=app.state.http_client,
        embedder=app.state.embedder,
    )
    if stats.get("last_error"):
        logger.warning("pulse.generate: degraded run, last_error=%s", stats["last_error"])
    deck = await load_today(app.state.db_pool)
    if deck is None:
        raise HTTPException(status_code=500, detail="Pulse ran but no deck was persisted")
    return deck


# ---------------------------------------------------------------------------
# GET /api/pulse/today
# ---------------------------------------------------------------------------


@router.get("/today", response_model=PulseDeckResponse)
@limiter.limit("30/minute")
async def get_today(request: Request) -> PulseDeckResponse:
    """Fetch today's Pulse deck (404 if not generated yet)."""
    deck = await load_today(request.app.state.db_pool)
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
) -> list[PulseDeckResponse]:
    """Return Pulse decks from the last *days* days, newest first."""
    return await load_history(request.app.state.db_pool, days=days)


# ---------------------------------------------------------------------------
# POST /api/pulse/rate
# ---------------------------------------------------------------------------


@router.post("/rate")
@limiter.limit("60/minute")
async def rate_card(request: Request, body: PulseRateRequest) -> dict:
    """Persist a user rating for a Pulse-shown paper.

    Rating is stored in ``pulse_ratings`` with ``source='pulse'`` so that the
    profile centroid refresh can distinguish Pulse clicks from library thumbs.
    """
    try:
        async with request.app.state.db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pulse_ratings (paper_id, rating, source)
                VALUES ($1, $2, 'pulse')
                """,
                body.paper_id,
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
async def explain_card(request: Request, card_id: int) -> dict:
    """Return the reasoning + signal breakdown for a single Pulse card."""
    async with request.app.state.db_pool.acquire() as conn:
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
) -> PulseStatsResponse:
    """Aggregate Pulse run stats over the past *days* days."""
    async with request.app.state.db_pool.acquire() as conn:
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
                ) AS last_error
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
        )
    return PulseStatsResponse(
        window_days=days,
        decks_generated=int(row["decks_generated"] or 0),
        avg_candidates=row["avg_candidates"],
        avg_llm_calls=row["avg_llm_calls"],
        avg_duration_s=row["avg_duration_s"],
        last_run_at=row["last_run_at"],
        last_error=row["last_error"],
    )
