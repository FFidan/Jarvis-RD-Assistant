"""Pulse overnight job — full 7-step orchestration.

Each stage is wrapped in defensive error handling: any single stage failure
degrades gracefully (Pulse must never crash the service
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
from jarvis_common.advisory_lock import AdvisoryLock, _kind_lock_key
from jarvis_common.event_log import log_event
from jarvis_common.jobs import ProgressContext
from jarvis_common.llm_client import observe
from jarvis_common.task_registry import KIND_TO_TASK

from paper_ingestion._state import get_services
from paper_ingestion.config import get_paper_ingestion_settings as _get_cfg
from paper_ingestion.pulse.citation_signals import compute_citation_signals
from paper_ingestion.pulse.deck import assemble_deck, persist_deck
from paper_ingestion.pulse.discovery import discover_candidates
from paper_ingestion.pulse.profile import load_profile
from paper_ingestion.pulse.scoring import (
    ScoredCandidate,
    Stage2ClientUnavailableError,
    stage1_embedding_filter,
    stage2_llm_rerank,
    stage3_combine,
)
from paper_ingestion.pulse.training import FEATURE_NAMES, classifier_scores
from paper_ingestion.services.pdf_workflow import upsert_paper

logger = logging.getLogger(__name__)


def _stage2_timeout() -> int:
    """Return the Stage-2 LLM rerank wall-clock timeout from settings.

    Reads PULSE_STAGE2_TIMEOUT_SECONDS at call time so monkeypatch.setenv
    takes effect without requiring an importlib.reload().
    """
    return _get_cfg().pulse_stage2_timeout_seconds


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


def _zero_candidate_degraded_reason(source_diagnostics: dict[str, dict[str, Any]]) -> str:
    if not source_diagnostics:
        return "No Pulse candidates returned; no enabled discovery source produced candidates."
    parts: list[str] = []
    for source_name, diagnostic in source_diagnostics.items():
        status = diagnostic.get("status")
        if status == "ok":
            continue
        message = str(diagnostic.get("message") or "").rstrip(".")
        if message:
            parts.append(message)
        else:
            parts.append(f"{source_name}: {status}")
    if not parts:
        return "No Pulse candidates returned; enabled discovery sources returned empty results."
    return "No Pulse candidates returned; " + "; ".join(parts[:4]) + "."


@observe()
async def run_pulse(
    db_pool: Any,
    http_client: httpx.AsyncClient,
    embedder: Any,
    *,
    now: datetime | None = None,
    source_cache: dict | None = None,
    ctx: ProgressContext | None = None,
    user_id: int | None = None,
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
        Optional :class:`~jarvis_common.jobs.ProgressContext` for progress reporting
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
        "degraded_reason": None,
        "source_diagnostics": {},
        "deck_date": None,
        "card_count": 0,
        "source_counts": {},
        "classifier": None,
        "classifier_training_enqueued": False,
    }

    # --- 1. profile ------------------------------------------------------
    if ctx:
        await ctx.update_progress(0.05, "Loading profile")
        if await ctx.is_cancelled():
            raise asyncio.CancelledError()
    try:
        profile = await load_profile(db_pool, embedder=embedder, user_id=user_id)
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
    source_diagnostics: dict[str, dict[str, Any]] = {}
    try:
        discovery_result = await discover_candidates(
            db_pool,
            http_client,
            profile,
            since=now - timedelta(days=profile.lookback_days),
            source_cache=source_cache,
        )
        candidates, source_counts, source_diagnostics = discovery_result
    except Exception as exc:  # broad: fan-out over heterogeneous source plugins; degrade to []
        stats["last_error"] = f"discover_candidates: {exc}"
        logger.exception("pulse.discover failed")
        candidates = []
    stats["candidate_count"] = len(candidates)
    stats["source_counts"] = source_counts
    stats["source_diagnostics"] = source_diagnostics
    if not candidates and stats["last_error"] is None:
        degraded_reason = _zero_candidate_degraded_reason(source_diagnostics)
        stats["degraded_reason"] = degraded_reason
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
    stage2_out, degraded_reason, llm_calls = await _run_stage2(stage1_out, profile, ctx)
    stats["stage2_scored"] = len(stage2_out)
    stats["llm_calls"] = llm_calls
    if degraded_reason:
        stats["degraded_reason"] = degraded_reason
    logger.info("pulse.stage2", extra={"scored": len(stage2_out)})

    # --- 5. stage 3b/4 optional citation + classifier signals ------------
    if ctx:
        await ctx.update_progress(0.88, "Adding citation and classifier signals")
        if await ctx.is_cancelled():
            raise asyncio.CancelledError()
    stage2_out, classifier_meta, opt_degraded = await _run_optional_signals(
        db_pool, stage2_out, profile, user_id
    )
    if opt_degraded:
        degraded_reason = degraded_reason or opt_degraded
        stats["degraded_reason"] = degraded_reason
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
    # Preserve the first degraded_reason set (e.g. zero-candidates); only overwrite if a later
    # stage produced a reason and no earlier reason was already recorded.
    if degraded_reason is not None:
        stats["degraded_reason"] = degraded_reason

    # --- 8. persist (upsert papers + persist deck in one transaction) ---
    # Use stats["degraded_reason"] — the local `degraded_reason` variable may have been
    # clobbered to None by _run_stage2's return (stage1 empty → stage2 returns None reason),
    # while stats["degraded_reason"] correctly holds the earlier zero-candidates reason.
    card_count = await _persist_pipeline(
        db_pool, deck, now, stats, stats["degraded_reason"], user_id
    )
    stats["card_count"] = card_count

    # --- 9. emit classifier training enqueue + verification telemetry ---
    await _emit_post_run_telemetry(db_pool, ctx, stage2_out, stats, user_id)

    logger.info("pulse.complete", extra=stats)
    return stats


async def _run_stage2(
    stage1_out: list[ScoredCandidate],
    profile: Any,
    ctx: Any,
) -> tuple[list[ScoredCandidate], str | None, int]:
    """Stage 4 — LLM rerank with timeout + fallback.

    Returns (stage2_out, degraded_reason, llm_calls).
    """
    if not stage1_out:
        return [], None, 0

    degraded_reason: str | None = None
    stage2_out: list[ScoredCandidate]
    llm_calls = 0

    try:
        _stage2_total = len(stage1_out)

        async def _stage2_with_progress() -> list[ScoredCandidate]:
            """Score all candidates in one call; inner stage2 handles concurrency."""
            services = get_services()
            results = await stage2_llm_rerank(
                stage1_out,
                profile,
                verifier=services.verifier,
                openai_client=services.openai_client,
            )
            if ctx:
                await ctx.update_progress(
                    0.95,
                    f"Stage 2 LLM scoring ({_stage2_total}/{_stage2_total})",
                )
            return results

        stage2_out = await asyncio.wait_for(
            _stage2_with_progress(),
            timeout=_stage2_timeout(),
        )
        # Count actual LLM calls: candidates where llm_relevance was set
        llm_calls = sum(1 for sc in stage2_out if sc.llm_relevance is not None)
    except Stage2ClientUnavailableError:
        # explicit sentinel — openai_client was None at stage2 entry
        degraded_reason = "stage2 skipped: openai_client unavailable"
        logger.warning(
            "pulse.stage2 skipped — openai_client is None; deck degraded to stage1 results"
        )
        stage2_out = _fallback_stage2(stage1_out)
    except TimeoutError:
        # B4: LLM timeout is degraded (deck still produced), not fatal
        _timeout_s = _stage2_timeout()
        degraded_reason = (
            f"LLM scoring timed out at {_timeout_s}s; deck used embedding-only fallback."
        )
        logger.warning("pulse.stage2 timed out — falling back to stage1")
        stage2_out = _fallback_stage2(stage1_out)
    except Exception as exc:  # broad: stage2 calls LLM over HTTP; fallback keeps deck viable
        # B4: stage2 exception with fallback is degraded (deck still produced)
        degraded_reason = f"stage2 error (embedding-only fallback used): {exc}"
        logger.exception("pulse.stage2 failed — falling back to stage1")
        stage2_out = _fallback_stage2(stage1_out)

    return stage2_out, degraded_reason, llm_calls


def _augment_signals(
    candidates: list[ScoredCandidate],
    extra_by_id: dict[str, dict[str, Any]],
) -> list[ScoredCandidate]:
    """Merge per-paper extra signals into candidate signal dicts. Returns a new list."""
    return [
        ScoredCandidate(
            paper=sc.paper,
            signals={**sc.signals, **extra_by_id.get(sc.paper.external_id, {})},
            llm_relevance=sc.llm_relevance,
            llm_novelty=sc.llm_novelty,
            reasoning=sc.reasoning,
            final_score=sc.final_score,
            reasoning_verified=sc.reasoning_verified,
            reasoning_confidence=sc.reasoning_confidence,
        )
        for sc in candidates
    ]


async def _run_optional_signals(
    db_pool: Any,
    stage2_out: list[ScoredCandidate],
    profile: Any,
    user_id: int | None,
) -> tuple[list[ScoredCandidate], dict, str | None]:
    """Stage 3b/4 — citation signals + classifier scores.

    Returns (updated_candidates, classifier_meta, degraded_reason).
    """
    classifier_meta: dict[str, Any] = {}
    degraded_reason: str | None = None

    try:
        wants_citation = any(
            profile.weights.get(name, 0.0) > 0
            for name in ("citation_pagerank", "citation_count", "citation_adamic_adar")
        )
        citation_by_external_id = (
            await compute_citation_signals(
                db_pool,
                [sc.paper.external_id for sc in stage2_out],
                user_id=user_id,
            )
            if wants_citation
            else {}
        )
        enriched = _augment_signals(stage2_out, citation_by_external_id)

        wants_classifier = profile.weights.get("classifier", 0.0) > 0
        if wants_classifier:
            classifier_values, classifier_meta = await classifier_scores(
                db_pool,
                [sc.signals for sc in enriched],
                user_id=user_id,
            )
        else:
            classifier_values = [0.0 for _ in enriched]
            classifier_meta = {
                "available": False,
                "feature_names": FEATURE_NAMES,
                "degradation_reason": "classifier weight is disabled",
            }
        stage2_out = _augment_signals(
            enriched,
            {
                sc.paper.external_id: {"classifier": classifier_values[idx]}
                for idx, sc in enumerate(enriched)
            },
        )
    except Exception as exc:  # broad: optional citation scoring must degrade cleanly
        degraded_reason = f"optional Pulse citation signals unavailable: {exc}"
        logger.warning(
            "citation_signals failed; pulse degraded",
            exc_info=exc,
            extra={"stage": "citation_signals"},
        )

    return stage2_out, classifier_meta, degraded_reason


async def _persist_pipeline(
    db_pool: Any,
    deck: list,
    now: datetime,
    stats: dict[str, Any],
    degraded_reason: str | None,
    user_id: int | None,
) -> int:
    """Stage 8 — upsert papers + persist deck inside a single outer transaction.

    Returns card_count.
    """
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
                            # Pulse inserts canonical-only; library membership
                            # for the deck owner is recorded when the user
                            # accepts the card (rate=save) via the
                            # /api/pulse/rate endpoint.
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
                    user_id=user_id,
                )
        logger.info("pulse.persisted", extra={"persisted": persisted, "cards": len(deck)})
        return persisted
    except Exception as exc:  # broad: outer txn failure (DB unreachable); stats already captured
        stats["last_error"] = f"persist: {exc}"
        logger.exception("pulse.persist failed")
        return 0


async def _emit_post_run_telemetry(
    db_pool: Any,
    ctx: Any,
    stage2_out: list[ScoredCandidate],
    stats: dict[str, Any],
    user_id: int | None,
) -> None:
    """Stage 9 — enqueue classifier training + emit verification telemetry (best-effort)."""
    try:
        classifier_job_id = str(uuid.uuid4())
        # Thread the deck owner's user_id so the follow-up
        # classifier-training job is attributable.
        await KIND_TO_TASK["pulse.train_classifier"].defer_async(
            job_id=classifier_job_id,
            user_id=user_id,
        )
        stats["classifier_training_enqueued"] = True
        stats["classifier_training_job_id"] = classifier_job_id
    except Exception:
        stats["classifier_training_enqueued"] = False
        logger.debug("pulse: classifier training enqueue skipped", exc_info=True)
    if ctx:
        await ctx.update_progress(1.0, "Done")

    # --- 9. emit verification telemetry (best-effort, after all other work) ---
    _total_verified = sum(1 for sc in stage2_out if sc.reasoning_verified is not None)
    _passed = sum(1 for sc in stage2_out if sc.reasoning_verified is True)
    _failed = _total_verified - _passed
    _pass_rate = round(_passed / _total_verified, 4) if _total_verified > 0 else 0.0
    stats["verification_stats"] = {
        "pass_rate": _pass_rate,
        "total": _total_verified,
        "passed": _passed,
        "failed": _failed,
    }
    try:
        await log_event(
            pool=db_pool,
            level="info",
            category="job",
            source="pulse",
            message="pulse.verification_stats",
            context={
                "pass_rate": _pass_rate,
                "total": _total_verified,
                "passed": _passed,
                "failed": _failed,
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never crash the pipeline
        logger.warning("pulse: failed to emit verification_stats event", exc_info=True)


async def _pulse_generate_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
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

    user_id_raw = payload.get("user_id")
    user_id = int(user_id_raw) if user_id_raw is not None else None
    user_id_or_zero = user_id or 0
    async with AdvisoryLock(
        pool, key1=_kind_lock_key("pulse.generate"), key2=user_id_or_zero
    ) as locked:
        if not locked:
            return {"status": "blocked", "reason": "Pulse already running"}
        services = get_services()
        stats = await run_pulse(
            db_pool=pool,
            http_client=http_client,
            embedder=services.embedder,
            now=now,
            source_cache=services.sources,
            ctx=ctx,
            user_id=user_id,
        )
    return {
        "deck_date": stats.get("deck_date"),
        "card_count": stats.get("card_count", 0),
        "stats": stats,
    }
