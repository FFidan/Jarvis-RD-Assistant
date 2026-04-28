"""Pulse REST endpoints — generate, today, history, rate, explain, stats, debug.

All endpoints require API key auth via ``verify_api_key`` and are rate-limited
by the shared slowapi ``limiter`` from ``paper_ingestion.deps``.

Thin orchestration layer — the heavy lifting lives in ``paper_ingestion.pulse.job``,
``paper_ingestion.pulse.deck``, and the scoring stages.
"""

import logging

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, current_user_id, current_user_id_or_none, log_audit
from jarvis_common import jobs as jobs_lib

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import (
    PulseDebugResponse,
    PulseDebugTopCard,
    PulseDebugTopicEmbedding,
    PulseDeckResponse,
    PulseExplainResponse,
    PulseGenerateResponse,
    PulseRateRequest,
    PulseRateResponse,
    PulseStatsResponse,
)
from paper_ingestion.pulse.deck import load_history, load_today
from paper_ingestion.pulse.training import FEATURE_NAMES

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/pulse",
    tags=["pulse"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
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
    # H16: system job — user_id=None until real auth resolver lands.
    # pulse.generate is a system-level cron job, not user-owned.
    job_id = await jobs_lib.enqueue(db_pool, "pulse.generate", payload={}, user_id=None)
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


@router.post("/rate", response_model=PulseRateResponse)
@limiter.limit("60/minute")
async def rate_card(
    request: Request,
    body: PulseRateRequest = Body(...),
    user_id: int | None = Depends(current_user_id),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PulseRateResponse:
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
                     AND pd.user_id IS NOT DISTINCT FROM $2
                   LIMIT 1""",
                body.paper_id,
                user_id,
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
    return PulseRateResponse(status="ok")


# ---------------------------------------------------------------------------
# GET /api/pulse/explain/{card_id}
# ---------------------------------------------------------------------------
@router.get("/explain/{card_id}", response_model=PulseExplainResponse)
@limiter.limit("30/minute")
async def explain_card(
    request: Request,
    card_id: int,
    user_id: int | None = Depends(current_user_id_or_none),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PulseExplainResponse:
    """Return the reasoning + signal breakdown for a single Pulse card.

    Ownership is enforced via a JOIN to pulse_decks filtered by user_id so that
    sequential-id enumeration (IDOR) is prevented.  ``IS NOT DISTINCT FROM``
    matches NULL-user decks in single-tenant stub mode and real user_id values
    once multi-tenant auth is active.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT pc.id, pc.reasoning, pc.signals, pc.llm_relevance, pc.llm_novelty
            FROM pulse_cards pc
            JOIN pulse_decks pd ON pc.deck_id = pd.id
            WHERE pc.id = $1
              AND pd.user_id IS NOT DISTINCT FROM $2
            """,
            card_id,
            user_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Pulse card not found")
    return PulseExplainResponse(
        card_id=row["id"],
        reasoning=row["reasoning"],
        signals=row["signals"] or {},
        llm_relevance=row["llm_relevance"],
        llm_novelty=row["llm_novelty"],
    )


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


@router.get("/debug", response_model=PulseDebugResponse)
@limiter.limit("30/minute")
async def debug_pulse(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PulseDebugResponse:
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
        model_row = await conn.fetchrow(
            """
            SELECT feature_names, metrics, trained_at
            FROM pulse_models
            WHERE is_active = TRUE
            ORDER BY trained_at DESC
            LIMIT 1
            """
        )

    # Per-source candidate breakdown (from stats JSONB)
    source_counts: dict = deck_stats.get("source_counts", {})
    classifier_stats = deck_stats.get("classifier", {}) or {}
    has_model = bool(model_row) and "feature_names" in model_row
    classifier_metrics = model_row["metrics"] if has_model else {}

    # Topic embedding sanity
    embed_dim_expected = 768
    topic_embeddings: list[PulseDebugTopicEmbedding] = []
    for er in embed_rows:
        val = er["value"]
        if isinstance(val, list):
            dim = len(val)
            topic_embeddings.append(
                PulseDebugTopicEmbedding(
                    key=er["key"],
                    dim=dim,
                    ok=dim == embed_dim_expected,
                    non_null=True,
                )
            )
        else:
            topic_embeddings.append(
                PulseDebugTopicEmbedding(
                    key=er["key"],
                    dim=None,
                    ok=False,
                    non_null=val is not None,
                )
            )

    # Top-N card breakdown
    top_cards = [
        PulseDebugTopCard(
            card_id=r["card_id"],
            paper_id=r["paper_id"],
            title=r["paper_title"],
            signals=r["signals"] or {},
            final_score=float(r["final_score"]),
            llm_relevance=r["llm_relevance"],
            llm_novelty=r["llm_novelty"],
        )
        for r in card_rows
    ]

    deck_date_val = deck_row["deck_date"]
    deck_date_str = (
        deck_date_val.isoformat() if hasattr(deck_date_val, "isoformat") else str(deck_date_val)
    )

    return PulseDebugResponse(
        deck_date=deck_date_str,
        card_count=deck_row["card_count"],
        degraded_reason=deck_row["degraded_reason"],
        source_counts=source_counts,
        topic_embeddings=topic_embeddings,
        top_cards=top_cards,
        classifier_available=has_model or bool(classifier_stats.get("available")),
        classifier_sample_count=classifier_metrics.get("sample_count")
        or classifier_stats.get("sample_count"),
        classifier_feature_names=(model_row["feature_names"] if has_model else None)
        or classifier_stats.get("feature_names")
        or FEATURE_NAMES,
        classifier_auc=classifier_metrics.get("auc") if has_model else None,
        classifier_auc_degradation_reason=(
            classifier_metrics.get("auc_degradation_reason") if has_model else None
        ),
        classifier_degradation_reason=classifier_stats.get("degradation_reason"),
    )
