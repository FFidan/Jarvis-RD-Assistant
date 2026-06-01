"""RAG answer verification — sentence-level confidence scoring.

Splits the LLM answer into sentences, verifies each against the source
chunk content using the existing QuoteVerifier (exact + fuzzy matching),
and returns a structured confidence report.

This module intentionally does NOT modify verification.py — it wraps
QuoteVerifier from the outside.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import asyncpg
from jarvis_common.verify import FUZZY_THRESHOLD, QuoteVerifier

from paper_ingestion.models.papers import ChunkResponse

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

__all__ = [
    "RagConfidence",
    "VerifiedSentence",
    "RagVerificationReport",
    "verify_answer_sentences",
]

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class RagConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNVERIFIED = "UNVERIFIED"


@dataclass
class VerifiedSentence:
    text: str
    verified: bool


@dataclass
class RagVerificationReport:
    total: int
    verified_count: int
    pass_rate: float  # in [0.0, 1.0]; 0.0 when total == 0
    confidence: RagConfidence
    per_sentence: list[VerifiedSentence] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PLACEHOLDER_DT = datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)

_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z\d"\'"])')
_ALPHANUM_RE = re.compile(r"[a-zA-Z0-9]")


def _split_sentences(answer: str) -> list[str]:
    """Split *answer* into sentences, filtering empty/non-alphanumeric segments."""
    raw = _SENTENCE_RE.split(answer.strip())
    return [s for s in raw if s and _ALPHANUM_RE.search(s)]


def _build_confidence(pass_rate: float, total: int) -> RagConfidence:
    if total == 0:
        return RagConfidence.UNVERIFIED
    if pass_rate == 1.0:
        return RagConfidence.HIGH
    if pass_rate >= 0.5:
        return RagConfidence.MEDIUM
    if pass_rate > 0.0:
        return RagConfidence.LOW
    return RagConfidence.UNVERIFIED


def _make_chunk_responses(
    chunks: list[dict], *, skip_paper_id: int | None = None
) -> list[ChunkResponse]:
    """Build ChunkResponse objects from source dicts.

    Parameters
    ----------
    chunks:
        Source dicts, each with at least a ``"content"`` key.
    skip_paper_id:
        When *None* (default), no paper-id filtering is applied and
        the synthetic ``paper_id=-1`` is used (single-paper / no-pid path).
        When set, only chunks whose ``paper_id`` matches this value are
        included; the ``paper_id`` field in the returned objects is set to
        this value (cross-paper path).

    Uses a synthetic ``id`` equal to the enumeration index because the
    verifier only reads ``content`` and ``page_number`` from the chunk
    list for fuzzy matching.
    """
    assigned_paper_id = skip_paper_id if skip_paper_id is not None else -1
    out: list[ChunkResponse] = []
    for i, src in enumerate(chunks):
        if "content" not in src:
            continue
        if skip_paper_id is not None:
            if src.get("paper_id") is not None and src["paper_id"] != skip_paper_id:
                continue
        out.append(
            ChunkResponse(
                id=i,
                paper_id=assigned_paper_id,
                chunk_index=src.get("chunk_index", i),
                content=src["content"],
                page_number=src.get("page_number"),
                created_at=_PLACEHOLDER_DT,
            )
        )
    return out


async def _fetch_fulltext(conn: asyncpg.pool.PoolConnectionProxy, paper_id: int) -> str:
    """Fetch and concatenate all chunks for *paper_id* from the DB."""
    rows = await conn.fetch(
        "SELECT content FROM paper_chunks WHERE paper_id = $1 ORDER BY chunk_index",
        paper_id,
    )
    return "\n\n".join(row["content"] for row in rows)


def _verify_sentence(
    sentence: str,
    full_texts: dict[int, str],
    chunks_by_paper: dict[int, list[ChunkResponse]],
    verifier: QuoteVerifier,
) -> bool:
    """Return True if *sentence* verifies against ANY of the provided papers."""
    for paper_id, full_text in full_texts.items():
        chunks = chunks_by_paper.get(paper_id, [])
        result = verifier.verify_quote(sentence, full_text, chunks)
        if result.verified:
            # Double-check: exact_match is always ok; fuzzy needs score >= threshold
            if result.match_type == "exact":
                return True
            if result.match_score is not None and result.match_score * 100 >= FUZZY_THRESHOLD:
                return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def verify_answer_sentences(
    answer: str,
    sources: list[dict],
    verifier: QuoteVerifier,
    db_pool: asyncpg.Pool,
) -> RagVerificationReport:
    """Verify each sentence of *answer* against *sources*.

    Parameters
    ----------
    answer:
        The full LLM-generated answer text.
    sources:
        List of source dicts.  Cross-paper sources have ``paper_id``,
        ``chunk_index``, and ``content``; single-paper sources have only
        ``content`` (and optionally ``page_number``).  When ``paper_id`` is
        absent from all sources we fall back to content-only verification
        (no DB fetch needed).
    verifier:
        Injected QuoteVerifier instance.
    db_pool:
        asyncpg connection pool used to fetch full paper text.
    """
    sentences = _split_sentences(answer)
    if not sentences:
        return RagVerificationReport(
            total=0,
            verified_count=0,
            pass_rate=0.0,
            confidence=RagConfidence.UNVERIFIED,
            per_sentence=[],
        )

    # Collect unique paper_ids from sources (may be absent for single-paper RAG)
    paper_ids: list[int] = list(
        {src["paper_id"] for src in sources if "paper_id" in src and src["paper_id"] is not None}
    )

    # Fetch full texts (memoized per call via local dict — no module-level cache)
    full_texts: dict[int, str] = {}
    if paper_ids:
        async with db_pool.acquire() as conn:
            for pid in paper_ids:
                full_texts[pid] = await _fetch_fulltext(conn, pid)
    else:
        # Single-paper path: no paper_id in sources — build synthetic full text
        # from the concatenated source content (already in memory, no DB needed)
        synthetic_text = "\n\n".join(src["content"] for src in sources if "content" in src)
        # Use sentinel paper_id = -1 so the loop below works uniformly
        full_texts[-1] = synthetic_text
        paper_ids = [-1]

    # Build ChunkResponse objects grouped by paper_id
    chunks_by_paper: dict[int, list[ChunkResponse]] = {}
    for pid in paper_ids:
        if pid == -1:
            # Synthetic path: one ChunkResponse per source entry
            chunks_by_paper[-1] = _make_chunk_responses(sources)
        else:
            chunks_by_paper[pid] = _make_chunk_responses(sources, skip_paper_id=pid)

    # Per-sentence verification
    _batch_size = 10

    async def _verify_batch(batch: list[str]) -> list[VerifiedSentence]:
        results: list[VerifiedSentence] = []
        for sent in batch:
            verified = await asyncio.to_thread(
                _verify_sentence, sent, full_texts, chunks_by_paper, verifier
            )
            results.append(VerifiedSentence(text=sent, verified=verified))
        return results

    per_sentence: list[VerifiedSentence] = []
    if len(sentences) > 50:
        batches = [sentences[i : i + _batch_size] for i in range(0, len(sentences), _batch_size)]
        batch_results = await asyncio.gather(*(_verify_batch(b) for b in batches))
        for br in batch_results:
            per_sentence.extend(br)
    else:
        per_sentence = await _verify_batch(sentences)

    total = len(per_sentence)
    verified_count = sum(1 for s in per_sentence if s.verified)
    pass_rate = verified_count / total if total > 0 else 0.0

    return RagVerificationReport(
        total=total,
        verified_count=verified_count,
        pass_rate=pass_rate,
        confidence=_build_confidence(pass_rate, total),
        per_sentence=per_sentence,
    )
