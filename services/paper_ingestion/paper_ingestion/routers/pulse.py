"""Pulse REST endpoints — generate, today, history, rate, explain, stats, debug.

All endpoints require API key auth via ``verify_api_key`` and are rate-limited
by the shared slowapi ``limiter`` from ``paper_ingestion.deps``.

Thin orchestration layer — the heavy lifting lives in ``paper_ingestion.pulse.job``,
``paper_ingestion.pulse.deck``, and the scoring stages.

Note: ``from __future__ import annotations`` is intentionally absent — see
``docs/plans/2026-04-29-future-import-failure-analysis.md`` for the verified
PydanticUserError trace. ``Body(...)`` annotations on PulseRateRequest are
preserved as the contract-test gate.
"""

import logging
import uuid
from datetime import date

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, current_user_id_strict_with_owner_override, log_audit
from jarvis_common.advisory_lock import _kind_lock_key
from jarvis_common.paper_state import trash_paper as _trash_paper
from jarvis_common.settings import get_core_settings
from jarvis_common.task_registry import KIND_TO_TASK

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION
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
from paper_ingestion.pulse.deck import load_history, load_last_nonempty_deck, load_today
from paper_ingestion.pulse.training import FEATURE_NAMES
from paper_ingestion.routers._paper_helpers import (
    _upsert_recommendation_feedback,
    _upsert_state_and_starred,
)

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


def _is_dev_mode() -> bool:
    """Return True when DEV_MODE=true (case-insensitive)."""
    return get_core_settings().dev_mode


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

    We probe the advisory lock (non-blocking) before deferring rather than
    relying on procrastinate's ``queueing_lock`` because procrastinate's lock
    is per-payload, whereas we want a per-kind+user lock that spans the full
    multi-minute pipeline run.
    """
    current_uid = await current_user_id_strict_with_owner_override(
        request, api_key=(getattr(request, "headers", None) or {}).get("X-API-Key")
    )
    key1 = _kind_lock_key("pulse.generate")
    key2 = current_uid or 0

    async with db_pool.acquire() as probe_conn:
        row = await probe_conn.fetchrow(
            "SELECT pg_try_advisory_lock($1, $2) AS got",
            key1,
            key2,
        )
        if row["got"]:
            # Free immediately — we only probed; the actual job holds its own lock
            await probe_conn.execute("SELECT pg_advisory_unlock($1, $2)", key1, key2)
        else:
            # Lock is held — find the in-flight job for the response body (best-effort)
            in_flight = await probe_conn.fetchrow(
                "SELECT id FROM procrastinate_jobs"
                " WHERE task_name LIKE '%pulse.generate%'"
                " AND status IN ('doing', 'todo')"
                " ORDER BY id DESC LIMIT 1"
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "already_running",
                    "in_flight_job_id": in_flight["id"] if in_flight else None,
                },
            )

    logger.info("pulse.generate: enqueueing job")
    jarvis_job_id = str(uuid.uuid4())
    # Phase 2 WS-2D: pass caller user_id so the resulting deck is owned by the
    # user that clicked "generate Pulse". Pre-WS-2A this was a system-wide
    # deck (incorrect once auth resolver returns real IDs).
    await KIND_TO_TASK["pulse.generate"].defer_async(job_id=jarvis_job_id, user_id=current_uid)
    await log_audit(
        db_pool,
        action="pulse_generate_enqueued",
        resource="pulse:deck",
        metadata={"job_id": jarvis_job_id},
    )
    return PulseGenerateResponse(job_id=jarvis_job_id, status="queued")


# ---------------------------------------------------------------------------
# GET /api/pulse/today
# ---------------------------------------------------------------------------


@router.get("/today", response_model=PulseDeckResponse)
@limiter.limit("30/minute")
async def get_today(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PulseDeckResponse:
    """Fetch today's Pulse deck, falling back to the last non-empty deck within 7 days.

    Returns
    -------
    PulseDeckResponse
        - Today's deck when card_count > 0 (``is_stale=False``).
        - A stale fallback deck (``is_stale=True``, ``stale_age_days`` set,
          ``stale_diagnostics`` populated from source_health) when today's deck
          exists but has no cards.
        - Today's empty deck with ``empty_reason="no_data_yet"`` when no
          non-empty deck exists within the last 7 days.
    Raises
    ------
    HTTPException(404)
        When no deck has been generated for today at all.
    """
    user_id = await current_user_id_strict_with_owner_override(
        request, api_key=(getattr(request, "headers", None) or {}).get("X-API-Key")
    )
    deck = await load_today(db_pool, user_id=user_id)
    if deck is None:
        raise HTTPException(status_code=404, detail="No Pulse deck for today")

    # Today's deck has cards — return as-is (new fields stay at defaults)
    if deck.card_count > 0:
        return deck

    # Today's deck is empty: try stale fallback (returns full deck with cards)
    fallback_deck = await load_last_nonempty_deck(db_pool, user_id=user_id, max_age_days=7)
    if fallback_deck is not None:
        stale_age = (date.today() - fallback_deck.deck_date).days
        async with db_pool.acquire() as conn:
            health_rows = await conn.fetch(
                """
                SELECT source_type, last_status, cooldown_until, consecutive_failures
                FROM source_health
                WHERE user_id = $1
                """,
                user_id,
            )
        stale_diagnostics = {
            row["source_type"]: {
                "last_status": row["last_status"],
                "cooldown_until": (
                    row["cooldown_until"].isoformat() if row["cooldown_until"] else None
                ),
                "consecutive_failures": row["consecutive_failures"],
            }
            for row in health_rows
        }
        fallback_deck.is_stale = True
        fallback_deck.stale_age_days = stale_age
        fallback_deck.stale_diagnostics = stale_diagnostics
        return fallback_deck

    # No usable fallback in the last 7 days
    deck.empty_reason = "no_data_yet"
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
    user_id = await current_user_id_strict_with_owner_override(
        request, api_key=(getattr(request, "headers", None) or {}).get("X-API-Key")
    )
    return await load_history(db_pool, days=days, user_id=user_id)


# ---------------------------------------------------------------------------
# POST /api/pulse/rate
# ---------------------------------------------------------------------------


@router.post("/rate", response_model=PulseRateResponse)
@limiter.limit("60/minute")
async def rate_card(
    request: Request,
    body: PulseRateRequest = Body(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PulseRateResponse:
    """Persist a user rating for a Pulse-shown paper (spec §4.4).

    Signal routing:
    - ``open``    — logging-only, no DB writes.
    - ``save``    — upserts lifecycle state to ``to_read`` (no feedback row).
    - ``up``      — positive recommendation_feedback with source ``pulse_thumbs``.
    - ``down``    — negative recommendation_feedback with source ``pulse_thumbs``.
    - ``dismiss`` — trashes paper AND writes negative feedback (``dismiss_combined``),
                    both inside a single transaction so partial failures roll back.

    Guard: paper must be a member of the requesting user's pulse deck (404 if not).
    """
    _ = request  # required by slowapi limiter — pyright suppression idiom (plan §2 constraint 7)
    user_id = await current_user_id_strict_with_owner_override(
        request, api_key=(getattr(request, "headers", None) or {}).get("X-API-Key")
    )
    async with db_pool.acquire() as conn:
        in_deck = await conn.fetchval(
            """SELECT 1 FROM pulse_cards pc
               JOIN pulse_decks pd ON pc.deck_id = pd.id
               WHERE pc.paper_id = $1
                 AND pd.user_id = $2
               LIMIT 1""",
            body.paper_id,
            user_id,
        )
        if not in_deck:
            raise HTTPException(status_code=404, detail="Paper not found in your pulse deck")

        async with conn.transaction():
            if body.rating == "open":
                logger.debug("pulse open: paper_id=%s user_id=%s", body.paper_id, user_id)
            elif body.rating == "save":
                await _upsert_state_and_starred(conn, body.paper_id, user_id, state="to_read")
            elif body.rating == "up":
                await _upsert_recommendation_feedback(
                    conn, body.paper_id, user_id, "positive", "pulse_thumbs"
                )
            elif body.rating == "down":
                await _upsert_recommendation_feedback(
                    conn, body.paper_id, user_id, "negative", "pulse_thumbs"
                )
            elif body.rating == "dismiss":
                await _trash_paper(conn, body.paper_id, user_id)
                await _upsert_recommendation_feedback(
                    conn, body.paper_id, user_id, "negative", "dismiss_combined"
                )
    return PulseRateResponse(status="ok")


# ---------------------------------------------------------------------------
# GET /api/pulse/explain/{card_id}
# ---------------------------------------------------------------------------
@router.get("/explain/{card_id}", response_model=PulseExplainResponse)
@limiter.limit("30/minute")
async def explain_card(
    request: Request,
    card_id: int,
    user_id: int = Depends(current_user_id_strict_with_owner_override),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PulseExplainResponse:
    """Return the reasoning + signal breakdown for a single Pulse card.

    Ownership is enforced via a JOIN to pulse_decks filtered by an exact
    ``user_id`` match so that sequential-id enumeration (IDOR) is prevented.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT pc.id, pc.reasoning, pc.signals, pc.llm_relevance, pc.llm_novelty
            FROM pulse_cards pc
            JOIN pulse_decks pd ON pc.deck_id = pd.id
            WHERE pc.id = $1
              AND pd.user_id = $2
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
    caller_id = await current_user_id_strict_with_owner_override(
        request, api_key=(getattr(request, "headers", None) or {}).get("X-API-Key")
    )
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS decks_generated,
                AVG(NULLIF((stats->>'candidate_count')::float, 0)) AS avg_candidates,
                AVG(NULLIF((stats->>'llm_calls')::float, 0)) AS avg_llm_calls,
                AVG(NULLIF((stats->>'duration_s')::float, 0)) AS avg_duration_s,
                MAX(generated_at) AS last_run_at,
                (
                    SELECT stats->>'last_error'
                    FROM pulse_decks
                    WHERE generated_at >= NOW() - make_interval(days => $1)
                      AND user_id = $2
                    ORDER BY generated_at DESC
                    LIMIT 1
                ) AS last_error,
                (
                    SELECT degraded_reason
                    FROM pulse_decks
                    WHERE generated_at >= NOW() - make_interval(days => $1)
                      AND user_id = $2
                    ORDER BY generated_at DESC
                    LIMIT 1
                ) AS degraded_reason
            FROM pulse_decks
            WHERE generated_at >= NOW() - make_interval(days => $1)
              AND user_id = $2
            """,
            days,
            caller_id,
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

    SEC (Wave-3/W1-5): The diagnostics body exposes classifier feature names,
    AUC, per-card signal weights, and topic-embedding internals — a model-
    inversion vector. The endpoint is gated behind ``DEV_MODE=true``; in
    production it returns 404 to avoid disclosing existence.
    """
    if not _is_dev_mode():
        raise HTTPException(status_code=404)
    caller_id = await current_user_id_strict_with_owner_override(
        request, api_key=(getattr(request, "headers", None) or {}).get("X-API-Key")
    )
    async with db_pool.acquire() as conn:
        # Fetch the most recent deck row for this caller
        deck_row = await conn.fetchrow(
            """
            SELECT id, deck_date, card_count, generated_at, stats, degraded_reason
            FROM pulse_decks
            WHERE user_id = $1
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            caller_id,
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
        # NOTE: user_config pulse.* / topic.* keys are intentionally global
        # single-tenant (one operator per JARVIS instance). Wave-4 multi-tenant:
        # re-key as `pulse.<user_id>.weights`, `topic.<user_id>.<n>.embedding`,
        # etc. and add a `WHERE user_id = $1` filter here.
        embed_rows = await conn.fetch(
            """
            SELECT key, value
            FROM user_config
            WHERE key LIKE 'topic.%.embedding'
            """
        )
        # NOTE: pulse_models classifier is global per deployment (one classifier
        # per JARVIS instance, trained on aggregated feedback). No user_id
        # scoping required today. Wave-4 multi-tenant: add a `user_id` column
        # to pulse_models + filter here if per-user classifiers ship.
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
    source_diagnostics: dict = deck_stats.get("source_diagnostics", {}) or {}
    classifier_stats = deck_stats.get("classifier", {}) or {}
    has_model = bool(model_row) and "feature_names" in model_row
    classifier_metrics = model_row["metrics"] if has_model else {}

    # Topic embedding sanity
    embed_dim_expected = EMBEDDING_DIMENSION
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
        source_diagnostics=source_diagnostics,
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


# ---------------------------------------------------------------------------
# GET /api/pulse/source-health
# ---------------------------------------------------------------------------


@router.get("/source-health")
@limiter.limit("30/minute")
async def get_source_health(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """Return per-source health status (last request, last success, cooldown).

    Returns
    -------
    list[dict]
        List of source health rows, one per source_type, sorted by source_type.
        Fields: source_type, last_request_at, last_success_at, last_status,
                cooldown_until, consecutive_failures.
    """
    user_id = await current_user_id_strict_with_owner_override(
        request, api_key=(getattr(request, "headers", None) or {}).get("X-API-Key")
    )
    rows = await db_pool.fetch(
        """
        SELECT source_type, last_request_at, last_success_at, last_status,
               cooldown_until, consecutive_failures
        FROM source_health
        WHERE user_id = $1
        ORDER BY source_type
        """,
        user_id,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/pulse/source-history
# ---------------------------------------------------------------------------


@router.get("/source-history")
@limiter.limit("30/minute")
async def get_source_history(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, list[dict]]:
    """Return source run history grouped by source_type.

    Returns
    -------
    dict[str, list[dict]]
        Mapping from source_type to list of run records (newest first within
        each source).  Each record includes: started_at, finished_at, status,
        candidate_count, duration_ms.

    Parameters
    ----------
    days : int
        Include runs started in the last *days* days (default 7).
    """
    user_id = await current_user_id_strict_with_owner_override(
        request, api_key=(getattr(request, "headers", None) or {}).get("X-API-Key")
    )
    rows = await db_pool.fetch(
        """
        SELECT source_type, started_at, finished_at, status, candidate_count, duration_ms
        FROM source_run_history
        WHERE user_id = $1
          AND started_at > NOW() - ($2::int || ' days')::interval
        ORDER BY source_type, started_at DESC
        """,
        user_id,
        days,
    )
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["source_type"], []).append(dict(r))
    return grouped
