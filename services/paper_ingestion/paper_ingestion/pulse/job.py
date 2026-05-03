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
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
from jarvis_common.jobs import JobContext, job_handler
from jarvis_common.task_registry import pulse_train_classifier

from paper_ingestion._state import svc
from paper_ingestion.pulse.citation_signals import compute_citation_signals
from paper_ingestion.pulse.deck import assemble_deck, persist_deck
from paper_ingestion.pulse.discovery import discover_candidates
from paper_ingestion.pulse.profile import load_profile
from paper_ingestion.pulse.scoring import (
    ScoredCandidate,
    stage1_embedding_filter,
    stage2_llm_rerank,
    stage3_combine,
)
from paper_ingestion.pulse.training import FEATURE_NAMES, classifier_scores
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
        # H20/WS-6C: pass user_id=None explicitly — system job, single-tenant mode.
        profile = await load_profile(db_pool, embedder=embedder, user_id=None)
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
            _stage2_batch_size = 5
            _stage2_total = len(stage1_out)
            _stage2_results: list[ScoredCandidate] = []

            async def _stage2_with_progress() -> list[ScoredCandidate]:
                """Score candidates in batches, reporting per-batch progress via ctx."""
                all_results: list[ScoredCandidate] = []
                for _batch_start in range(0, _stage2_total, _stage2_batch_size):
                    batch = stage1_out[_batch_start : _batch_start + _stage2_batch_size]
                    batch_results = await stage2_llm_rerank(
                        batch,
                        profile,
                        http_client,
                        verifier=svc.verifier,
                        openai_client=svc.openai_client,
                    )
                    all_results.extend(batch_results)
                    _scored_so_far = len(all_results)
                    if ctx:
                        _pct = min(0.95, 0.85 + 0.10 * (_scored_so_far / _stage2_total))
                        await ctx.update_progress(
                            _pct,
                            f"Stage 2 LLM scoring ({_scored_so_far}/{_stage2_total})",
                        )
                return all_results

            stage2_out = await asyncio.wait_for(
                _stage2_with_progress(),
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

    # --- 5. stage 3b/4 optional citation + classifier signals ------------
    if ctx:
        await ctx.update_progress(0.88, "Adding citation and classifier signals")
        if await ctx.is_cancelled():
            raise asyncio.CancelledError()
    classifier_meta: dict[str, Any] = {}
    try:
        wants_citation = any(
            profile.weights.get(name, 0.0) > 0
            for name in ("citation_pagerank", "citation_count", "citation_adamic_adar")
        )
        citation_by_external_id = (
            await compute_citation_signals(db_pool, [sc.paper.external_id for sc in stage2_out])
            if wants_citation
            else {}
        )
        enriched: list[ScoredCandidate] = []
        for sc in stage2_out:
            new_signals = dict(sc.signals)
            new_signals.update(citation_by_external_id.get(sc.paper.external_id, {}))
            enriched.append(
                ScoredCandidate(
                    paper=sc.paper,
                    signals=new_signals,
                    llm_relevance=sc.llm_relevance,
                    llm_novelty=sc.llm_novelty,
                    reasoning=sc.reasoning,
                    final_score=sc.final_score,
                    reasoning_verified=sc.reasoning_verified,
                    reasoning_confidence=sc.reasoning_confidence,
                )
            )
        wants_classifier = profile.weights.get("classifier", 0.0) > 0
        if wants_classifier:
            classifier_values, classifier_meta = await classifier_scores(
                db_pool,
                [sc.signals for sc in enriched],
            )
        else:
            classifier_values = [0.0 for _ in enriched]
            classifier_meta = {
                "available": False,
                "feature_names": FEATURE_NAMES,
                "degradation_reason": "classifier weight is disabled",
            }
        stage2_out = [
            ScoredCandidate(
                paper=sc.paper,
                signals={**sc.signals, "classifier": classifier_values[idx]},
                llm_relevance=sc.llm_relevance,
                llm_novelty=sc.llm_novelty,
                reasoning=sc.reasoning,
                final_score=sc.final_score,
                reasoning_verified=sc.reasoning_verified,
                reasoning_confidence=sc.reasoning_confidence,
            )
            for idx, sc in enumerate(enriched)
        ]
    except Exception as exc:  # broad: optional Phase 2 scoring must degrade cleanly
        degraded_reason = degraded_reason or f"optional Pulse Phase 2 signals unavailable: {exc}"
        logger.warning(
            "citation_signals failed; pulse degraded",
            exc_info=exc,
            extra={"stage": "citation_signals"},
        )
    stats["classifier"] = classifier_meta or {
        "available": False,
        "feature_names": FEATURE_NAMES,
    }

    # --- 6. stage 3 (weighted combine) -----------------------------------
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

    # --- 7. assemble deck ------------------------------------------------
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

    # --- 8. persist (upsert papers + persist deck in one transaction) ---
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                successes = 0
                for card in deck:
                    try:
                        # B1.1: nested transaction issues SAVEPOINT/ROLLBACK TO SAVEPOINT
                        # so a single-card failure cannot poison the outer transaction.
                        async with conn.transaction():
                            card.paper.discovery_origin = "pulse"
                            await upsert_paper(conn, card.paper)
                        successes += 1
                    except Exception as exc:  # per-card: roll back savepoint, keep outer txn alive
                        logger.warning(
                            "pulse.upsert_paper failed for %s: %s",
                            card.paper.external_id,
                            exc,
                        )
                        stats["last_error"] = f"upsert_paper: {exc}"
                # B1.2: 0-card deck is observable
                if successes == 0 and len(deck) > 0:
                    logger.warning(
                        "pulse.zero_card_deck: all %d upserts failed; 0-card deck will be "
                        "persisted. last_error=%s",
                        len(deck),
                        stats.get("last_error"),
                    )
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
        try:
            await pulse_train_classifier.defer_async(job_id=str(uuid.uuid4()), user_id=None)
            stats["classifier_training_enqueued"] = True
        except Exception:
            stats["classifier_training_enqueued"] = False
            logger.debug("pulse: classifier training enqueue skipped", exc_info=True)
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
