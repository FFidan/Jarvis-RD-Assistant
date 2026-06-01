"""Cross-paper contradiction detection with quote verification.

The scanner is intentionally conservative: it asks the LLM to classify only
pre-filtered pairs of already verified summary findings, then persists a
contradiction only when both returned quotes verify against the source chunks.

Module layout:
- ``contradictions_extract`` — data loading, dataclasses, lexical primitives.
- ``contradictions_persist`` — INSERT/list helpers.
- this file — pair scoring, quote verification, LLM classify, scan orchestration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx
from jarvis_common import get_smart_model
from jarvis_common.llm_client import (
    LLM_TIMEOUT_DEFAULT,
    ChatCompletionOptions,
    call_llm_structured,
    get_litellm_config,
    observe,
)

if TYPE_CHECKING:
    import openai


from jarvis_common.verify import QuoteVerifier

from paper_ingestion.converters import row_to_chunk_response
from paper_ingestion.db_types import ConnLike
from paper_ingestion.models import ChunkResponse
from paper_ingestion.services.contradiction_models import ContradictionClassification
from paper_ingestion.services.contradictions_extract import (
    _NEGATIVE_RE_CUES,
    _POSITIVE_RE,
    ContradictionCandidate,
    VerifiedFinding,
    _build_prompt,
    _cross_reference_ids,
    _jaccard,
    _load_verified_findings,
    _parse_findings,
    _terms,
)
from paper_ingestion.services.contradictions_persist import (
    SCANNER_VERSION,
    _persist_contradiction,
    list_contradictions,
)

logger = logging.getLogger(__name__)

_SYSTEM_CONTRADICTIONS = """\
You are checking whether two quote-backed research findings contradict each other.

Rules:
1. Decide only from the provided findings and quotes.
2. Return is_contradiction=false when the papers merely differ in scope,
   method, dataset, or emphasis.
3. If is_contradiction=true, quote_a and quote_b must be copied exactly from the provided quotes.
4. Do not invent supporting text.

Respond as JSON:
{
  "is_contradiction": true,
  "contradiction_type": "direct|methodological|result|interpretation",
  "explanation": "one concise sentence",
  "quote_a": "exact copied quote from Paper A",
  "quote_b": "exact copied quote from Paper B",
  "confidence": 0.0
}
"""


def _polarity_score(text: str) -> int:
    """Return -1, 0, or 1 from word-boundary cue matching.

    Uses a greedy non-overlapping match selection so that longer, more-specific
    cues (e.g. the positive ``"reduces error"`` or the negative
    ``"not significant"``) take priority over shorter sub-matches (e.g. negative
    ``"reduces"`` or positive ``"significant"``).  This prevents two double-count
    bugs present in the old substring ``in`` implementation:

    * ``"reduces error"`` previously matched positive *and* ``"reduces"`` matched
      negative, cancelling out.
    * ``"not significant"`` matched negative via both ``_NEGATIVE_CUES`` (for the
      full phrase) and a bare negation check (for ``"not"``), inflating the negative count.

    Bare negation words (``"not"``, ``"no"``) are intentionally not matched separately:
    the negative-cue set already covers ``"no benefit"``, ``"no improvement"``, and
    ``"not significant"``; a separate bare-negation pattern would double-count every
    phrase captured by those multi-word cues.
    """
    lowered = text.lower()
    # Collect all candidate matches from both regexes, tagged with polarity.
    all_matches: list[tuple[int, int, int]] = []
    for m in _POSITIVE_RE.finditer(lowered):
        all_matches.append((m.start(), m.end(), 1))
    for m in _NEGATIVE_RE_CUES.finditer(lowered):
        all_matches.append((m.start(), m.end(), -1))
    # Sort longest-first so that multi-word cues consume their span before
    # shorter alternatives can claim the overlapping characters.
    all_matches.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
    # Greedy non-overlapping selection.
    used: list[tuple[int, int]] = []
    positive = 0
    negative = 0
    for start, end, polarity in all_matches:
        if any(s < end and start < e for s, e in used):
            continue  # overlaps an already-accepted match
        used.append((start, end))
        if polarity == 1:
            positive += 1
        else:
            negative += 1
    if positive == negative:
        return 0
    return 1 if positive > negative else -1


def _polarity_adjustment(a: VerifiedFinding, b: VerifiedFinding) -> tuple[float, str]:
    """Return a soft score adjustment and diagnostic reason for polarity cues."""
    polarity_a = _polarity_score(f"{a.finding} {a.quote}")
    polarity_b = _polarity_score(f"{b.finding} {b.quote}")
    if polarity_a == 0 or polarity_b == 0:
        return 0.0, "polarity_neutral"
    if polarity_a != polarity_b:
        return 0.25, "opposite_polarity"
    return -0.1, "same_polarity"


def _score_pair(
    a: VerifiedFinding,
    b: VerifiedFinding,
    *,
    cross_ref: bool,
) -> ContradictionCandidate | None:
    """Score a candidate (a, b) pair and return a ContradictionCandidate or None if filtered."""
    terms_a = _terms(f"{a.title} {a.finding} {a.quote}")
    terms_b = _terms(f"{b.title} {b.finding} {b.quote}")
    overlap = terms_a & terms_b
    semantic_score = _jaccard(terms_a, terms_b)
    if not cross_ref and len(overlap) < 2 and semantic_score < 0.08:
        return None
    polarity_delta, polarity_reason = _polarity_adjustment(a, b)
    lexical_score = min(len(overlap) / 10, 0.7)
    score = lexical_score + min(semantic_score, 0.35) + (0.5 if cross_ref else 0.0)
    score = max(0.01, score + polarity_delta)
    reasons: list[str] = []
    if cross_ref:
        reasons.append("cross_reference")
    if overlap:
        reasons.append("term_overlap")
    if semantic_score:
        reasons.append(f"semantic:{semantic_score:.2f}")
    reasons.append(polarity_reason)
    return ContradictionCandidate(a=a, b=b, score=score, reason="|".join(reasons))


def build_contradiction_candidates(
    findings: list[VerifiedFinding],
    *,
    paper_id: int | None = None,
    limit: int = 25,
) -> list[ContradictionCandidate]:
    """Return a ranked list of likely contradiction candidates.

    For library-wide scans (``paper_id=None``) only cross-referenced pairs are
    evaluated, avoiding O(n²) complexity when the library is large.  For
    single-paper queries the full quadratic scan is kept because exhaustive
    coverage matters more than performance there.
    """
    candidates: list[ContradictionCandidate] = []

    if paper_id is None:
        # --- Library-wide scan: cross-ref pre-filter only (O(n × avg_refs)) ---
        # Build an index from paper_id → findings for that paper.
        by_paper: dict[int, list[VerifiedFinding]] = {}
        for f in findings:
            by_paper.setdefault(f.paper_id, []).append(f)

        # Iterate only pairs where one finding explicitly cross-references the other.
        seen_pairs: set[tuple[int, int]] = set()
        for a in findings:
            for ref_id in a.cross_reference_ids:
                for b in by_paper.get(ref_id, []):
                    if a.paper_id == b.paper_id:
                        continue
                    # Canonicalise pair to avoid scoring (a,b) and (b,a) separately.
                    pair = (min(a.paper_id, b.paper_id), max(a.paper_id, b.paper_id))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    cross_ref = True
                    candidate = _score_pair(a, b, cross_ref=cross_ref)
                    if candidate is not None:
                        candidates.append(candidate)
    else:
        # --- Single-paper query: full O(n²) scan for exhaustive coverage ---
        for idx, a in enumerate(findings):
            for b in findings[idx + 1 :]:
                if a.paper_id == b.paper_id:
                    continue
                if paper_id not in {a.paper_id, b.paper_id}:
                    continue
                cross_ref = (
                    b.paper_id in a.cross_reference_ids or a.paper_id in b.cross_reference_ids
                )
                candidate = _score_pair(a, b, cross_ref=cross_ref)
                if candidate is not None:
                    candidates.append(candidate)

    return sorted(candidates, key=lambda item: item.score, reverse=True)[:limit]


async def _fetch_chunks(conn: ConnLike, paper_id: int) -> list[ChunkResponse]:
    rows = await conn.fetch(
        "SELECT * FROM paper_chunks WHERE paper_id = $1 ORDER BY chunk_index",
        paper_id,
    )
    return [row_to_chunk_response(row) for row in rows]


async def _quotes_verify(
    conn: ConnLike,
    verifier: QuoteVerifier,
    candidate: ContradictionCandidate,
    quote_a: str,
    quote_b: str,
) -> tuple[bool, int | None, int | None]:
    if not quote_a.strip() or not quote_b.strip():
        logger.info(
            "quotes_verify_empty_quote candidate=(%d,%d)",
            candidate.a.paper_id,
            candidate.b.paper_id,
        )
        return False, None, None
    chunks_a = await _fetch_chunks(conn, candidate.a.paper_id)
    chunks_b = await _fetch_chunks(conn, candidate.b.paper_id)
    if not chunks_a or not chunks_b:
        return False, None, None
    full_a = "\n\n".join(chunk.content for chunk in chunks_a)
    full_b = "\n\n".join(chunk.content for chunk in chunks_b)
    result_a = verifier.verify_quote(quote_a, full_a, chunks_a)
    result_b = verifier.verify_quote(quote_b, full_b, chunks_b)
    if not result_a.verified or not result_b.verified:
        return False, None, None
    return True, result_a.page_number, result_b.page_number


@observe()
async def _classify_candidate(
    openai_client: openai.AsyncOpenAI,
    http_client: httpx.AsyncClient,
    candidate: ContradictionCandidate,
    *,
    model: str,
) -> ContradictionClassification | None:
    result = await call_llm_structured(
        openai_client,
        response_model=ContradictionClassification,
        prompt=_build_prompt(candidate),
        options=ChatCompletionOptions(
            model=model,
            max_tokens=500,
            temperature=0.0,
            timeout=LLM_TIMEOUT_DEFAULT,
            system=_SYSTEM_CONTRADICTIONS,
        ),
        config=get_litellm_config(),
    )
    if not result.is_contradiction:
        return None
    return result


@observe()
async def scan_contradictions(
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    verifier: QuoteVerifier,
    *,
    openai_client: openai.AsyncOpenAI,
    paper_id: int | None = None,
    limit: int = 25,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Scan verified findings for cross-paper contradictions.

    Returns counts and persisted IDs. LLM or verification failures for one
    candidate do not abort the whole scan; those candidates are skipped.
    """
    model = get_smart_model()
    async with db_pool.acquire() as conn:
        findings = await _load_verified_findings(conn, paper_id=paper_id, user_id=user_id)
    candidates = build_contradiction_candidates(findings, paper_id=paper_id, limit=limit)

    inserted_ids: list[int] = []
    llm_failures = 0
    verification_failures = 0
    for candidate in candidates:
        try:
            classified = await _classify_candidate(
                openai_client, http_client, candidate, model=model
            )
        except Exception:
            llm_failures += 1
            logger.warning(
                "Contradiction classifier failed for papers %s/%s",
                candidate.a.paper_id,
                candidate.b.paper_id,
                exc_info=True,
            )
            continue
        if classified is None:
            continue
        quote_a = classified.quote_a.strip()
        quote_b = classified.quote_b.strip()
        async with db_pool.acquire() as conn:
            verified, page_a, page_b = await _quotes_verify(
                conn, verifier, candidate, quote_a, quote_b
            )
            if not verified:
                verification_failures += 1
                continue
            contradiction_id = await _persist_contradiction(
                conn,
                candidate,
                classified,
                page_a=page_a,
                page_b=page_b,
                model=model,
                user_id=user_id,
            )
        if contradiction_id is not None and contradiction_id not in inserted_ids:
            inserted_ids.append(contradiction_id)

    return {
        "paper_id": paper_id,
        "candidate_count": len(candidates),
        "contradictions_found": len(inserted_ids),
        "contradiction_ids": inserted_ids,
        "llm_failures": llm_failures,
        "verification_failures": verification_failures,
    }


__all__ = [
    "SCANNER_VERSION",
    "ContradictionCandidate",
    "VerifiedFinding",
    "_SYSTEM_CONTRADICTIONS",
    "_build_prompt",
    "_classify_candidate",
    "_cross_reference_ids",
    "_fetch_chunks",
    "_jaccard",
    "_load_verified_findings",
    "_parse_findings",
    "_persist_contradiction",
    "_polarity_adjustment",
    "_polarity_score",
    "_quotes_verify",
    "_score_pair",
    "_terms",
    "build_contradiction_candidates",
    "list_contradictions",
    "scan_contradictions",
]
