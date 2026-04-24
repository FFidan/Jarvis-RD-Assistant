"""Pulse overnight job — full 7-step orchestration.

Each stage is wrapped in defensive error handling: any single stage failure
degrades gracefully (Phase 1 principle — Pulse must never crash the service
and must produce a deck even if sources fail or the LLM times out).

Stats dict keys
---------------
* ``candidate_count`` — raw fan-out count from ``discover_candidates``
* ``stage1_survivors`` — count returned by ``stage1_embedding_filter``
* ``stage2_scored`` — count returned by ``stage2_llm_rerank`` (or stage1 on fallback)
* ``llm_calls`` — number of LLM calls issued during stage 2
* ``duration_s`` — wall-clock time of the full pipeline
* ``last_error`` — string describing a *fatal* partial failure, or ``None``
* ``deck_date`` — ISO date string of the deck produced (set before return)
* ``card_count`` — number of cards in the produced deck (set before return)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
from jarvis_common.jobs import JobContext, job_handler

from paper_ingestion._state import svc
from paper_ingestion.pulse.deck import assemble_deck, persist_deck
from paper_ingestion.pulse.discovery import discover_candidates
from paper_ingestion.pulse.profile import load_profile
from paper_ingestion.pulse.scoring import (
    ScoredCandidate,
    stage1_embedding_filter,
    stage2_llm_rerank,
    stage3_combine,
)
from paper_ingestion.services.pdf_workflow import upsert_paper

logger = logging.getLogger(__name__)

_STAGE2_TIMEOUT_SECONDS = 600


def _fallback_stage2(stage1_out: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Clone stage1 survivors as stage2 output with LLM fields cleared."""
    fallback: list[ScoredCandidate] = []
    for sc in stage1_out:
        fallback.append(
            ScoredCandidate(
                paper=sc.paper,
                signals=dict(sc.signals),
                llm_relevance=None,
                llm_novelty=None,
                reasoning=None,
                final_score=sc.final_score,
            )
        )
    return fallback


async def run_pulse(
    db_pool: Any,
    http_client: httpx.AsyncClient,
    embedder: Any,
    *,
    now: datetime | None = None,
    source_cache: dict | None = None,
    ctx: JobContext | None = None,
) -> dict:
    """Run the full overnight Pulse pipeline.

    Returns a stats dict describing the run.  Never raises — any uncaught
    collaborator error is recorded in ``stats['last_error']`` and the pipeline
    continues from the best-known state.

    Parameters
    ----------
    source_cache:
        Optional dict of pre-initialized source singletons (e.g.
        ``app.state.sources``).  Passed to ``discover_candidates`` so that
        rate-limiter state is preserved across Pulse runs.
    ctx:
        Optional :class:`~jarvis_common.jobs.JobContext` for progress reporting
        and cancellation support when the pipeline runs as a background job.
        When ``None`` (scheduler / direct call) progress is not reported.
    """
    now = now or datetime.now(UTC)
    start = time.monotonic()

    # B4: track degraded vs fatal separately
    degraded_reason: str | None = None

    stats: dict[str, Any] = {
        "candidate_count": 0,
        "stage1_survivors": 0,
        "stage2_scored": 0,
        "llm_calls": 0,
        "duration_s": 0.0,
        "last_error": None,
    }

    # --- 1. profile ------------------------------------------------------
    if ctx:
        await ctx.update_progress(0.05, "Loading profile")
        if await ctx.is_cancelled():
            raise asyncio.CancelledError()
    try:
        profile = await load_profile(db_pool, embedder=embedder)
    except Exception as exc:  # broad: touches DB + embedder; any failure is fatal for this run
        stats["last_error"] = f"load_profile: {exc}"
        logger.exception("pulse.load_profile failed")
        stats["duration_s"] = time.monotonic() - start
        return stats
    logger.info(
        "pulse.profile_loaded",
        extra={
            "topics": len(profile.topics),
            "deck_size": profile.deck_size,
            "stage2_top_k": profile.stage2_top_k,
        },
    )

    # --- 2. discovery ----------------------------------------------------
    if ctx:
        await ctx.update_progress(0.20, "Discovering candidates")
        if await ctx.is_cancelled():
            raise asyncio.CancelledError()
    source_counts: dict[str, int] = {}
    try:
        candidates, source_counts = await discover_candidates(
            db_pool,
            http_client,
            profile,
            since=now - timedelta(days=7),
            source_cache=source_cache,
        )
    except Exception as exc:  # broad: fan-out over heterogeneous source plugins; degrade to []
        stats["last_error"] = f"discover_candidates: {exc}"
        logger.exception("pulse.discover failed")
        candidates = []
    stats["candidate_count"] = len(candidates)
    stats["source_counts"] = source_counts
    logger.info("pulse.stage0", extra={"candidates": len(candidates)})

    # --- 3. stage 1 (embedding filter) -----------------------------------
    if ctx:
        await ctx.update_progress(0.30, "Stage 1 ranking")
        if await ctx.is_cancelled():
            raise asyncio.CancelledError()
    try:
        stage1_out = await stage1_embedding_filter(
            candidates, profile, embedder, top_k=profile.stage2_top_k, now=now.date()
        )
    except Exception as exc:  # broad: stage1 calls embedder; degrade to empty list
        stats["last_error"] = f"stage1: {exc}"
        logger.exception("pulse.stage1 failed")
        stage1_out = []
    stats["stage1_survivors"] = len(stage1_out)
    logger.info(
        "pulse.stage1",
        extra={"candidates": len(candidates), "survivors": len(stage1_out)},
    )

    # --- 4. stage 2 (LLM rerank) with timeout fallback -------------------
    if ctx:
        await ctx.update_progress(0.85, "Stage 2 LLM scoring")
        if await ctx.is_cancelled():
            raise asyncio.CancelledError()
    stage2_out: list[ScoredCandidate]
    if not stage1_out:
        stage2_out = []
    else:
        try:
            stage2_out = await asyncio.wait_for(
                stage2_llm_rerank(stage1_out, profile, http_client),
                timeout=_STAGE2_TIMEOUT_SECONDS,
            )
            # Count actual LLM calls: candidates where llm_relevance was set
            stats["llm_calls"] = sum(1 for sc in stage2_out if sc.llm_relevance is not None)
        except TimeoutError:
            # B4: LLM timeout is degraded (deck still produced), not fatal
            degraded_reason = (
                f"LLM scoring timed out at {_STAGE2_TIMEOUT_SECONDS}s; "
                "deck used embedding-only fallback."
            )
            logger.warning("pulse.stage2 timed out — falling back to stage1")
            stage2_out = _fallback_stage2(stage1_out)
        except Exception as exc:  # broad: stage2 calls LLM over HTTP; fallback keeps deck viable
            # B4: stage2 exception with fallback is degraded (deck still produced)
            degraded_reason = f"stage2 error (embedding-only fallback used): {exc}"
            logger.exception("pulse.stage2 failed — falling back to stage1")
            stage2_out = _fallback_stage2(stage1_out)
    stats["stage2_scored"] = len(stage2_out)
    logger.info("pulse.stage2", extra={"scored": len(stage2_out)})

    # --- 5. stage 3 (weighted combine) -----------------------------------
    if ctx:
        await ctx.update_progress(0.90, "Stage 3 diversification")
        if await ctx.is_cancelled():
            raise asyncio.CancelledError()
    try:
        stage3_out = await stage3_combine(stage2_out, profile.weights)
    except Exception as exc:  # broad: pure computation but must not crash the pipeline
        stats["last_error"] = f"stage3: {exc}"
        logger.exception("pulse.stage3 failed")
        stage3_out = stage2_out
    logger.info("pulse.stage3", extra={"scored": len(stage3_out)})

    # --- 6. assemble deck ------------------------------------------------
    if ctx:
        await ctx.update_progress(0.93, "Assembling deck")
        if await ctx.is_cancelled():
            raise asyncio.CancelledError()
    try:
        deck = assemble_deck(stage3_out, size=profile.deck_size)
    except Exception as exc:  # broad: may call DB/services; must not crash pipeline
        stats["last_error"] = f"assemble_deck: {exc}"
        logger.exception("pulse.assemble failed")
        deck = []
    logger.info("pulse.assembled", extra={"cards": len(deck)})

    # Compute duration before persist so it is available even if persist fails
    stats["duration_s"] = round(time.monotonic() - start, 3)
    stats["deck_date"] = now.date().isoformat()

    # --- 7. persist (upsert papers + persist deck in one transaction) ---
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                for card in deck:
                    try:
                        await upsert_paper(conn, card.paper)
                    except Exception as exc:  # per-card: skip failed card, not whole deck
                        logger.warning(
                            "pulse.upsert_paper failed for %s: %s",
                            card.paper.external_id,
                            exc,
                        )
                        stats["last_error"] = f"upsert_paper: {exc}"
                persisted = await persist_deck(
                    db_pool,
                    deck_date=now.date(),
                    cards=deck,
                    stats=stats,
                    degraded_reason=degraded_reason,
                    conn=conn,
                )
        stats["card_count"] = persisted
        logger.info("pulse.persisted", extra={"persisted": persisted, "cards": len(deck)})
    except Exception as exc:  # broad: outer txn failure (DB unreachable); stats already captured
        stats["last_error"] = f"persist: {exc}"
        stats["card_count"] = 0
        logger.exception("pulse.persist failed")

    if degraded_reason:
        stats["degraded_reason"] = degraded_reason
    if ctx:
        await ctx.update_progress(1.0, "Done")
    logger.info("pulse.complete", extra=stats)
    return stats


@job_handler("pulse.generate")
async def _pulse_generate_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Jobs backbone handler for on-demand Pulse deck generation.

    Registered as handler for kind ``"pulse.generate"``.  Accepts an optional
    ``now`` ISO string in ``payload`` for deterministic testing; all other
    pipeline parameters come from the module-level service state (``svc``).

    Note: ``embedder`` and ``source_cache`` are not serialisable as job payload —
    the handler retrieves them from ``paper_ingestion._state.svc`` which is
    populated during FastAPI lifespan startup.  The worker runs inside the same
    process so ``svc`` is always initialised before any job executes.
    """
    now_str = payload.get("now")
    now = datetime.fromisoformat(now_str) if now_str else None

    stats = await run_pulse(
        db_pool=pool,
        http_client=http_client,
        embedder=svc.embedder,
        now=now,
        source_cache=svc.sources,
        ctx=ctx,
    )
    return {
        "deck_date": stats.get("deck_date"),
        "card_count": stats.get("card_count", 0),
        "stats": stats,
    }
