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
* ``last_error`` — string describing the last recoverable failure, or ``None``
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.pulse.deck import assemble_deck, persist_deck
from app.pulse.discovery import discover_candidates
from app.pulse.profile import load_profile
from app.pulse.scoring import (
    ScoredCandidate,
    stage1_embedding_filter,
    stage2_llm_rerank,
    stage3_combine,
)
from app.services.pdf_workflow import upsert_paper

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
    """
    now = now or datetime.now(UTC)
    start = time.monotonic()

    stats: dict[str, Any] = {
        "candidate_count": 0,
        "stage1_survivors": 0,
        "stage2_scored": 0,
        "llm_calls": 0,
        "duration_s": 0.0,
        "last_error": None,
    }

    # --- 1. profile ------------------------------------------------------
    try:
        profile = await load_profile(db_pool, embedder=embedder)
    except Exception as exc:
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
    try:
        candidates = await discover_candidates(
            db_pool,
            http_client,
            profile,
            since=now - timedelta(days=7),
            source_cache=source_cache,
        )
    except Exception as exc:
        stats["last_error"] = f"discover_candidates: {exc}"
        logger.exception("pulse.discover failed")
        candidates = []
    stats["candidate_count"] = len(candidates)
    logger.info("pulse.stage0", extra={"candidates": len(candidates)})

    # --- 3. stage 1 (embedding filter) -----------------------------------
    try:
        stage1_out = await stage1_embedding_filter(
            candidates, profile, embedder, top_k=profile.stage2_top_k
        )
    except Exception as exc:
        stats["last_error"] = f"stage1: {exc}"
        logger.exception("pulse.stage1 failed")
        stage1_out = []
    stats["stage1_survivors"] = len(stage1_out)
    logger.info(
        "pulse.stage1",
        extra={"candidates": len(candidates), "survivors": len(stage1_out)},
    )

    # --- 4. stage 2 (LLM rerank) with timeout fallback -------------------
    stage2_out: list[ScoredCandidate]
    if not stage1_out:
        stage2_out = []
    else:
        try:
            stage2_out = await asyncio.wait_for(
                stage2_llm_rerank(stage1_out, profile, http_client),
                timeout=_STAGE2_TIMEOUT_SECONDS,
            )
            stats["llm_calls"] = len(stage1_out)
        except TimeoutError:
            stats["last_error"] = f"llm_timeout after {_STAGE2_TIMEOUT_SECONDS}s"
            logger.warning("pulse.stage2 timed out — falling back to stage1")
            stage2_out = _fallback_stage2(stage1_out)
        except Exception as exc:
            stats["last_error"] = f"stage2: {exc}"
            logger.exception("pulse.stage2 failed — falling back to stage1")
            stage2_out = _fallback_stage2(stage1_out)
    stats["stage2_scored"] = len(stage2_out)
    logger.info("pulse.stage2", extra={"scored": len(stage2_out)})

    # --- 5. stage 3 (weighted combine) -----------------------------------
    try:
        stage3_out = await stage3_combine(stage2_out, profile.weights)
    except Exception as exc:
        stats["last_error"] = f"stage3: {exc}"
        logger.exception("pulse.stage3 failed")
        stage3_out = stage2_out
    logger.info("pulse.stage3", extra={"scored": len(stage3_out)})

    # --- 6. assemble deck ------------------------------------------------
    try:
        deck = await assemble_deck(stage3_out, size=profile.deck_size)
    except Exception as exc:
        stats["last_error"] = f"assemble_deck: {exc}"
        logger.exception("pulse.assemble failed")
        deck = []
    logger.info("pulse.assembled", extra={"cards": len(deck)})

    # --- 7. persist (upsert papers + persist deck) ----------------------
    try:
        if deck:
            async with db_pool.acquire() as conn:
                for card in deck:
                    try:
                        await upsert_paper(conn, card.paper)
                    except Exception as exc:
                        logger.warning(
                            "pulse.upsert_paper failed for %s: %s",
                            card.paper.external_id,
                            exc,
                        )
                        stats["last_error"] = f"upsert_paper: {exc}"
        deck_id = await persist_deck(
            db_pool,
            deck_date=now.date(),
            cards=deck,
            stats=stats,
        )
        logger.info("pulse.persisted", extra={"deck_id": deck_id, "cards": len(deck)})
    except Exception as exc:
        stats["last_error"] = f"persist: {exc}"
        logger.exception("pulse.persist failed")

    stats["duration_s"] = round(time.monotonic() - start, 3)
    logger.info("pulse.complete", extra=stats)
    return stats
