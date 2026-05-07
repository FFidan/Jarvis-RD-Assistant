"""Pulse reasoning verification (WS-2.3).

Thin wrapper around the existing :class:`QuoteVerifier` that scores a Pulse
card's LLM-generated ``reasoning`` sentence against the candidate paper's
title + abstract. The result is a (verified, RagConfidence) tuple, which the
deck persistence layer stores on ``pulse_cards.reasoning_verified`` +
``pulse_cards.reasoning_confidence`` (migration 034).

Reuses the same exact/fuzzy matching logic as ``rag/verification.py`` —
does not re-implement thresholds.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging

from jarvis_common.verify import QuoteVerifier

from paper_ingestion.models.papers import ChunkResponse
from paper_ingestion.rag.verification import RagConfidence

_PLACEHOLDER_DT = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)

logger = logging.getLogger(__name__)

__all__ = ["verify_pulse_reasoning"]

# Fallback marker used by stage2_llm_rerank when the LLM call failed.
_LLM_FAILED_SENTINEL = "LLM scoring failed"


def _score_to_confidence(score: float | None) -> RagConfidence:
    """Map a fuzz partial_ratio-style score in [0,1] (or None) to a confidence bucket.

    Thresholds: HIGH = near-perfect match (>=97%, matches QuoteVerifier's
    FUZZY_THRESHOLD), MEDIUM = strong partial (>=85%), LOW = weak partial
    (>=70%), UNVERIFIED = no meaningful overlap.
    """
    if score is None:
        return RagConfidence.UNVERIFIED
    pct = score * 100
    if pct >= 97:
        return RagConfidence.HIGH
    if pct >= 85:
        return RagConfidence.MEDIUM
    if pct >= 70:
        return RagConfidence.LOW
    return RagConfidence.UNVERIFIED


async def verify_pulse_reasoning(
    reasoning: str,
    paper_title: str,
    paper_abstract: str,
    verifier: QuoteVerifier,
) -> tuple[bool, RagConfidence]:
    """Verify a Pulse card's reasoning sentence against the paper's title+abstract.

    Returns a ``(verified, confidence)`` tuple. Short-circuits to
    ``(False, UNVERIFIED)`` when the reasoning is empty or matches the
    scoring-failure sentinel emitted by ``stage2_llm_rerank``.

    The underlying ``QuoteVerifier.verify_quote`` is synchronous (pure CPU
    fuzzy match on a ~300-char string) — wrap in ``asyncio.to_thread`` so we
    do not block the event loop when called from async scoring paths.
    """
    if not reasoning or not reasoning.strip():
        return (False, RagConfidence.UNVERIFIED)
    if reasoning.strip() == _LLM_FAILED_SENTINEL:
        return (False, RagConfidence.UNVERIFIED)

    full_text = f"{paper_title}. {paper_abstract or ''}".strip()
    if not full_text:
        return (False, RagConfidence.UNVERIFIED)

    # Wrap the paper text as a single synthetic chunk so verify_quote's fuzzy
    # branch runs (exact-substring-only matching is too strict for LLM-paraphrased
    # reasoning).  The chunk content is identical to full_text so the fuzzy
    # partial_ratio score directly reflects reasoning-vs-paper similarity.
    synthetic_chunk = ChunkResponse(
        id=0,
        paper_id=-1,
        chunk_index=0,
        content=full_text,
        page_number=None,
        created_at=_PLACEHOLDER_DT,
    )

    try:
        result = await asyncio.to_thread(
            verifier.verify_quote, reasoning, full_text, [synthetic_chunk]
        )
    except Exception:
        logger.warning("pulse.verify_reasoning: verifier raised", exc_info=True)
        return (False, RagConfidence.UNVERIFIED)

    confidence = _score_to_confidence(result.match_score)
    verified = confidence != RagConfidence.UNVERIFIED
    return (verified, confidence)
