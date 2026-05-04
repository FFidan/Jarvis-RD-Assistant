"""Cross-paper contradiction detection with quote verification.

The scanner is intentionally conservative: it asks the LLM to classify only
pre-filtered pairs of already verified summary findings, then persists a
contradiction only when both returned quotes verify against the source chunks.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
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
from jarvis_common.prompt_safety import wrap_delimited

if TYPE_CHECKING:
    import openai


from paper_ingestion.converters import row_to_chunk_response
from paper_ingestion.extraction.verify import QuoteVerifier
from paper_ingestion.models import (
    ChunkResponse,
    PaperContradictionResponse,
)
from paper_ingestion.services.contradiction_models import ContradictionClassification

logger = logging.getLogger(__name__)

ConnLike = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy  # type: ignore[type-arg]

SCANNER_VERSION = "paper_contradictions_v1"
_ALLOWED_TYPES = {"direct", "methodological", "result", "interpretation"}
_STOP_WORDS = {
    "about",
    "after",
    "also",
    "among",
    "from",
    "have",
    "into",
    "paper",
    "result",
    "results",
    "show",
    "shows",
    "study",
    "that",
    "their",
    "there",
    "these",
    "this",
    "using",
    "with",
}
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_POSITIVE_CUES = {
    "better",
    "benefit",
    "beneficial",
    "boost",
    "effective",
    "higher",
    "improve",
    "improved",
    "improves",
    "improvement",
    "increase",
    "increased",
    "increases",
    "outperform",
    "outperformed",
    "outperforms",
    "positive",
    "reduces error",
    "significant",
    "supports",
    "works",
}
_NEGATIVE_CUES = {
    "decrease",
    "decreased",
    "decreases",
    "harm",
    "harmful",
    "lower",
    "negative",
    "no benefit",
    "no improvement",
    "not significant",
    "reduce",
    "reduced",
    "reduces",
    "worse",
    "worsen",
    "worsened",
    "worsens",
}
# Build word-boundary regexes with longest-first alternation to prevent shorter cues
# (e.g. "reduces") from shadowing longer multi-word cues (e.g. "reduces error").
_POSITIVE_RE = re.compile(
    r"(?<!\w)("
    + "|".join(re.escape(c) for c in sorted(_POSITIVE_CUES, key=len, reverse=True))
    + r")(?!\w)",
    re.IGNORECASE,
)
_NEGATIVE_RE_CUES = re.compile(
    r"(?<!\w)("
    + "|".join(re.escape(c) for c in sorted(_NEGATIVE_CUES, key=len, reverse=True))
    + r")(?!\w)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class VerifiedFinding:
    """A quote-verified summary finding used as scanner input."""

    paper_id: int
    title: str
    finding: str
    quote: str
    page_number: int | None
    cross_reference_ids: frozenset[int]


@dataclass(frozen=True)
class ContradictionCandidate:
    """A narrowed pair of findings worth sending to the classifier."""

    a: VerifiedFinding
    b: VerifiedFinding
    score: float
    reason: str


def _terms(text: str) -> set[str]:
    return {
        word.lower()
        for word in _WORD_RE.findall(text)
        if len(word) > 3 and word.lower() not in _STOP_WORDS
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    """Return lexical-set similarity as a cheap semantic proxy."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


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


def _cross_reference_ids(raw: Any) -> frozenset[int]:
    if not isinstance(raw, list):
        return frozenset()
    ids: set[int] = set()
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("related_paper_id"), int):
            ids.add(item["related_paper_id"])
    return frozenset(ids)


def _parse_findings(row: asyncpg.Record) -> list[VerifiedFinding]:
    raw_findings = row["key_findings"] or []
    if not isinstance(raw_findings, list):
        return []
    cross_reference_ids = _cross_reference_ids(row["cross_references"] or [])
    parsed: list[VerifiedFinding] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        if item.get("verified") is False:
            continue
        finding = str(item.get("finding") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not finding or not quote:
            continue
        parsed.append(
            VerifiedFinding(
                paper_id=row["paper_id"],
                title=row["title"],
                finding=finding,
                quote=quote,
                page_number=item.get("page_number")
                if isinstance(item.get("page_number"), int)
                else None,
                cross_reference_ids=cross_reference_ids,
            )
        )
    return parsed


async def _load_verified_findings(
    conn: ConnLike,
    *,
    paper_id: int | None = None,
) -> list[VerifiedFinding]:
    rows = await conn.fetch(
        """
        SELECT p.id AS paper_id, p.title, ps.key_findings, ps.cross_references
        FROM paper_summaries ps
        JOIN papers p ON p.id = ps.paper_id
        WHERE jsonb_typeof(ps.key_findings) = 'array'
          AND jsonb_array_length(ps.key_findings) > 0
          AND ($1::integer IS NULL OR p.id = $1 OR EXISTS (
              SELECT 1
              FROM jsonb_array_elements(COALESCE(ps.cross_references, '[]'::jsonb)) AS ref
              WHERE ref->>'related_paper_id' ~ '^[0-9]+$'
                AND (ref->>'related_paper_id')::integer = $1
          ))
        ORDER BY ps.created_at DESC
        LIMIT 250
        """,
        paper_id,
    )
    findings: list[VerifiedFinding] = []
    for row in rows:
        findings.extend(_parse_findings(row))
    return findings


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


def _build_prompt(candidate: ContradictionCandidate) -> str:
    return f"""\
You are checking whether two quote-backed research findings contradict each other.

Rules:
1. Decide only from the provided findings and quotes.
2. Return is_contradiction=false when the papers merely differ in scope,
   method, dataset, or emphasis.
3. If is_contradiction=true, quote_a and quote_b must be copied exactly from the provided quotes.
4. Do not invent supporting text.

Paper A:
{wrap_delimited("title_a", candidate.a.title)}
{wrap_delimited("finding_a", candidate.a.finding)}
{wrap_delimited("quote_a", candidate.a.quote)}

Paper B:
{wrap_delimited("title_b", candidate.b.title)}
{wrap_delimited("finding_b", candidate.b.finding)}
{wrap_delimited("quote_b", candidate.b.quote)}

Respond as JSON:
{{
  "is_contradiction": true,
  "contradiction_type": "direct|methodological|result|interpretation",
  "explanation": "one concise sentence",
  "quote_a": "exact copied quote from Paper A",
  "quote_b": "exact copied quote from Paper B",
  "confidence": 0.0
}}
"""


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


async def _persist_contradiction(
    conn: ConnLike,
    candidate: ContradictionCandidate,
    parsed: ContradictionClassification,
    *,
    page_a: int | None,
    page_b: int | None,
    model: str,
) -> int | None:
    paper_a = candidate.a
    paper_b = candidate.b
    quote_a = parsed.quote_a.strip()
    quote_b = parsed.quote_b.strip()
    contradiction_type = parsed.contradiction_type
    confidence = parsed.confidence
    explanation = parsed.explanation.strip()
    if not explanation:
        explanation = "The verified findings make conflicting claims."

    # Canonicalize paper ordering for stable uniqueness.
    if paper_a.paper_id > paper_b.paper_id:
        paper_a, paper_b = paper_b, paper_a
        quote_a, quote_b = quote_b, quote_a
        page_a, page_b = page_b, page_a

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO paper_contradictions (
                paper_a_id, paper_b_id, finding_a, finding_b, quote_a, quote_b,
                page_a, page_b, contradiction_type, explanation, confidence, status,
                scanner_metadata, updated_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'verified',
                $12::jsonb, NOW()
            )
            RETURNING id
            """,
            paper_a.paper_id,
            paper_b.paper_id,
            paper_a.finding,
            paper_b.finding,
            quote_a,
            quote_b,
            page_a,
            page_b,
            contradiction_type,
            explanation,
            confidence,
            {
                "scanner_version": SCANNER_VERSION,
                "candidate_score": candidate.score,
                "candidate_reason": candidate.reason,
                "model": model,
            },
        )
        if row is not None:
            return row["id"]
    except asyncpg.UniqueViolationError:
        pass

    row = await conn.fetchrow(
        """
        SELECT id FROM paper_contradictions
        WHERE LEAST(paper_a_id, paper_b_id) = LEAST($1::integer, $2::integer)
          AND GREATEST(paper_a_id, paper_b_id) = GREATEST($1::integer, $2::integer)
          AND quote_a = $3
          AND quote_b = $4
        LIMIT 1
        """,
        paper_a.paper_id,
        paper_b.paper_id,
        quote_a,
        quote_b,
    )
    return row["id"] if row else None


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
) -> dict[str, Any]:
    """Scan verified findings for cross-paper contradictions.

    Returns counts and persisted IDs. LLM or verification failures for one
    candidate do not abort the whole scan; those candidates are skipped.
    """
    model = get_smart_model()
    async with db_pool.acquire() as conn:
        findings = await _load_verified_findings(conn, paper_id=paper_id)
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


async def list_contradictions(
    conn: ConnLike,
    *,
    paper_id: int | None = None,
    status: str | None = "verified",
    limit: int = 20,
) -> tuple[list[PaperContradictionResponse], int]:
    """List persisted contradictions with paper titles."""
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1
    if paper_id is not None:
        conditions.append(f"(pc.paper_a_id = ${idx} OR pc.paper_b_id = ${idx})")
        params.append(paper_id)
        idx += 1
    if status is not None:
        conditions.append(f"pc.status = ${idx}")
        params.append(status)
        idx += 1
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    rows = await conn.fetch(
        f"""
        SELECT pc.*, pa.title AS paper_a_title, pb.title AS paper_b_title,
               COUNT(*) OVER() AS total_count
        FROM paper_contradictions pc
        JOIN papers pa ON pa.id = pc.paper_a_id
        JOIN papers pb ON pb.id = pc.paper_b_id
        {where}
        ORDER BY pc.created_at DESC
        LIMIT ${idx}
        """,
        *params,
    )
    total = rows[0]["total_count"] if rows else 0
    return (
        [
            PaperContradictionResponse(
                id=row["id"],
                paper_a_id=row["paper_a_id"],
                paper_b_id=row["paper_b_id"],
                paper_a_title=row["paper_a_title"],
                paper_b_title=row["paper_b_title"],
                finding_a=row["finding_a"],
                finding_b=row["finding_b"],
                quote_a=row["quote_a"],
                quote_b=row["quote_b"],
                page_a=row["page_a"],
                page_b=row["page_b"],
                contradiction_type=row["contradiction_type"],
                explanation=row["explanation"],
                confidence=row["confidence"],
                status=row["status"],
                created_at=row["created_at"],
            )
            for row in rows
        ],
        total,
    )
