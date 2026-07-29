"""Persistence and listing for verified cross-paper contradictions."""

from __future__ import annotations

import re
from typing import Any

import asyncpg

from paper_ingestion.db_types import ConnLike
from paper_ingestion.models import ConsensusAssessment, ConsensusClaim, PaperContradictionResponse
from paper_ingestion.services.contradiction_models import ContradictionClassification
from paper_ingestion.services.contradictions_extract import ContradictionCandidate

SCANNER_VERSION = "paper_contradictions_v1"

_QUOTE_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_quote_whitespace(quote: str) -> str:
    """Collapse internal whitespace runs to a single space and trim ends.

    Two verbatim quotes that differ only by incidental whitespace (a double
    space, a tab, a line-wrap) must store and hash identically, or they create
    separate ``paper_contradictions`` rows that inflate the caller's own
    supports/opposes tallies instead of deduping against the unique index.
    """
    return _QUOTE_WHITESPACE_RE.sub(" ", quote).strip()


async def _find_existing_contradiction_id(
    conn: ConnLike,
    *,
    paper_ids: tuple[int, int],
    quotes: tuple[str, str],
    content_generations: tuple[int, int],
    user_id: int,
) -> int | None:
    """Find the canonical row, including quotes stored before normalization."""
    paper_a_id, paper_b_id = paper_ids
    quote_a, quote_b = quotes
    paper_a_generation, paper_b_generation = content_generations
    row = await conn.fetchrow(
        """
        SELECT id FROM paper_contradictions
        WHERE LEAST(paper_a_id, paper_b_id) = LEAST($1::integer, $2::integer)
          AND GREATEST(paper_a_id, paper_b_id) = GREATEST($1::integer, $2::integer)
          AND regexp_replace(btrim(quote_a), '[[:space:]]+', ' ', 'g') = $3
          AND regexp_replace(btrim(quote_b), '[[:space:]]+', ' ', 'g') = $4
          AND user_id = $5
          AND paper_a_content_generation = $6
          AND paper_b_content_generation = $7
        ORDER BY id
        LIMIT 1
        """,
        paper_a_id,
        paper_b_id,
        quote_a,
        quote_b,
        user_id,
        paper_a_generation,
        paper_b_generation,
    )
    return row["id"] if row else None


async def _persist_contradiction(
    conn: ConnLike,
    candidate: ContradictionCandidate,
    parsed: ContradictionClassification,
    *,
    page_a: int | None,
    page_b: int | None,
    model: str,
    user_id: int,
) -> int | None:
    paper_a = candidate.a
    paper_b = candidate.b
    quote_a = _normalize_quote_whitespace(parsed.quote_a)
    quote_b = _normalize_quote_whitespace(parsed.quote_b)
    contradiction_type = parsed.contradiction_type
    confidence = parsed.confidence
    explanation = parsed.explanation.strip()
    if not explanation:
        explanation = "The verified findings make conflicting claims."
    # stance + claim_topic are pair-level (symmetric), so the canonicalize swap
    # below does not touch them. A blank claim_topic persists as NULL.
    stance = parsed.stance
    claim_topic = parsed.claim_topic.strip() or None

    # Canonicalize paper ordering for stable uniqueness.
    if paper_a.paper_id > paper_b.paper_id:
        paper_a, paper_b = paper_b, paper_a
        quote_a, quote_b = quote_b, quote_a
        page_a, page_b = page_b, page_a

    existing_id = await _find_existing_contradiction_id(
        conn,
        paper_ids=(paper_a.paper_id, paper_b.paper_id),
        quotes=(quote_a, quote_b),
        content_generations=(paper_a.content_generation, paper_b.content_generation),
        user_id=user_id,
    )
    if existing_id is not None:
        return existing_id

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO paper_contradictions (
                paper_a_id, paper_b_id, finding_a, finding_b, quote_a, quote_b,
                page_a, page_b, contradiction_type, explanation, confidence, status,
                scanner_metadata, updated_at, user_id, stance, claim_topic,
                paper_a_content_generation, paper_b_content_generation
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'verified',
                $12::jsonb, NOW(), $13, $14, $15, $16, $17
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
            user_id,
            stance,
            claim_topic,
            paper_a.content_generation,
            paper_b.content_generation,
        )
        if row is not None:
            return row["id"]
    except asyncpg.UniqueViolationError:
        pass

    return await _find_existing_contradiction_id(
        conn,
        paper_ids=(paper_a.paper_id, paper_b.paper_id),
        quotes=(quote_a, quote_b),
        content_generations=(paper_a.content_generation, paper_b.content_generation),
        user_id=user_id,
    )


_CURRENT_CONTRADICTIONS_CTE = """
        WITH ranked_current_contradictions AS (
            SELECT pc.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY
                           LEAST(pc.paper_a_id, pc.paper_b_id),
                           GREATEST(pc.paper_a_id, pc.paper_b_id),
                           CASE WHEN pc.paper_a_id <= pc.paper_b_id
                               THEN regexp_replace(
                                   btrim(pc.quote_a), '[[:space:]]+', ' ', 'g')
                               ELSE regexp_replace(
                                   btrim(pc.quote_b), '[[:space:]]+', ' ', 'g')
                           END,
                           CASE WHEN pc.paper_a_id <= pc.paper_b_id
                               THEN regexp_replace(
                                   btrim(pc.quote_b), '[[:space:]]+', ' ', 'g')
                               ELSE regexp_replace(
                                   btrim(pc.quote_a), '[[:space:]]+', ' ', 'g')
                           END,
                           pc.user_id,
                           CASE WHEN pc.paper_a_id <= pc.paper_b_id
                               THEN pc.paper_a_content_generation
                               ELSE pc.paper_b_content_generation
                           END,
                           CASE WHEN pc.paper_a_id <= pc.paper_b_id
                               THEN pc.paper_b_content_generation
                               ELSE pc.paper_a_content_generation
                           END
                       ORDER BY pc.id
                   ) AS evidence_rank
            FROM paper_contradictions pc
            JOIN papers current_a ON current_a.id = pc.paper_a_id
            JOIN papers current_b ON current_b.id = pc.paper_b_id
            WHERE pc.paper_a_content_generation = current_a.content_generation
              AND pc.paper_b_content_generation = current_b.content_generation
        ),
        current_contradictions AS (
            SELECT *
            FROM ranked_current_contradictions
            WHERE evidence_rank = 1
        )
"""


async def list_contradictions(
    conn: ConnLike,
    *,
    user_id: int,
    paper_id: int | None = None,
    status: str | None = "verified",
    limit: int = 20,
) -> tuple[list[PaperContradictionResponse], int]:
    """List persisted contradictions whose full evidence pair is in the caller's library."""
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1
    conditions.append(
        f"("
        f"EXISTS (SELECT 1 FROM user_library ul"
        f" WHERE ul.paper_id = pc.paper_a_id AND ul.user_id = ${idx})"
        f" AND EXISTS (SELECT 1 FROM user_library ul"
        f" WHERE ul.paper_id = pc.paper_b_id AND ul.user_id = ${idx})"
        f")"
    )
    # Holding both papers grants access to the papers, not to another user's
    # assessment of them. The findings and explanation stored here are the
    # scanning user's own derived work.
    conditions.append(f"pc.user_id IS NOT DISTINCT FROM ${idx}")
    params.append(user_id)
    idx += 1
    # 'supports' rows belong to the consensus view, not the contradiction list;
    # legacy rows (NULL stance) predate the stance column and remain contradictions.
    conditions.append("(pc.stance IS NULL OR pc.stance = 'opposes')")
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
        {_CURRENT_CONTRADICTIONS_CTE}
        SELECT pc.*, pa.title AS paper_a_title, pb.title AS paper_b_title,
               COUNT(*) OVER() AS total_count
        FROM current_contradictions pc
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


_CLAIM_TOPIC_PUNCT_RE = re.compile(r"[_\W]+")


def _normalize_claim_topic(topic: str) -> str:
    """Casefold, collapse punctuation/whitespace runs to single spaces, trim.

    Unicode-aware: ``\\w`` matches letters of any script, so "Effect of X, on Y!"
    and "effect of x on y" cluster together, and so do two phrasings of the same
    non-Latin topic, e.g. "Эффект X, на Y!" and "эффект x на y" both normalize to
    "эффект x на y" -- while an unrelated non-Latin topic keeps its own distinct
    key instead of every script outside a-z0-9 collapsing to the empty string.
    """
    return _CLAIM_TOPIC_PUNCT_RE.sub(" ", topic.casefold()).strip()


_CONSENSUS_ROW_CAP = 1000


async def aggregate_consensus(
    conn: ConnLike,
    *,
    user_id: int,
    limit: int = 50,
    evidence_per_claim: int = 5,
) -> tuple[list[ConsensusClaim], bool]:
    """Aggregate supports/opposes whose full evidence pair is in the caller's library.

    Topics are grouped on a normalized form (casefolded, punctuation collapsed)
    so near-duplicate phrasings cluster together. Each cluster carries up to
    ``evidence_per_claim`` verified assessments for evidence drill-down.

    Returns
    -------
    tuple[list[ConsensusClaim], bool]
        The claim clusters (already capped to ``limit``) and a ``truncated``
        flag that is ``True`` when the underlying set of verified stance rows
        exceeds ``_CONSENSUS_ROW_CAP`` -- i.e. some evidence was excluded
        before clustering even began, independent of the ``limit`` param.
    """
    rows = await conn.fetch(
        f"""
        {_CURRENT_CONTRADICTIONS_CTE}
        SELECT pc.stance, pc.claim_topic, pc.paper_a_id, pc.paper_b_id,
               pc.quote_a, pc.quote_b, pc.page_a, pc.page_b,
               pa.title AS paper_a_title, pb.title AS paper_b_title
        FROM current_contradictions pc
        JOIN papers pa ON pa.id = pc.paper_a_id
        JOIN papers pb ON pb.id = pc.paper_b_id
        WHERE pc.status = 'verified'
          AND pc.stance IN ('supports', 'opposes')
          AND pc.claim_topic IS NOT NULL
          AND btrim(pc.claim_topic) <> ''
          AND EXISTS (SELECT 1 FROM user_library ul
                      WHERE ul.paper_id = pc.paper_a_id AND ul.user_id = $1)
          AND EXISTS (SELECT 1 FROM user_library ul
                      WHERE ul.paper_id = pc.paper_b_id AND ul.user_id = $1)
          AND pc.user_id IS NOT DISTINCT FROM $1
        ORDER BY pc.created_at DESC
        LIMIT $2
        """,
        user_id,
        _CONSENSUS_ROW_CAP + 1,
    )
    truncated = len(rows) > _CONSENSUS_ROW_CAP
    rows = rows[:_CONSENSUS_ROW_CAP]

    clusters: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _normalize_claim_topic(row["claim_topic"])
        cluster = clusters.setdefault(
            key,
            {
                "claim_topic": row["claim_topic"],
                "supports": 0,
                "opposes": 0,
                "paper_ids": set(),
                "assessments": [],
            },
        )
        if len(row["claim_topic"]) < len(cluster["claim_topic"]):
            cluster["claim_topic"] = row["claim_topic"]
        if row["stance"] == "supports":
            cluster["supports"] += 1
        else:
            cluster["opposes"] += 1
        cluster["paper_ids"].add(row["paper_a_id"])
        cluster["paper_ids"].add(row["paper_b_id"])
        if len(cluster["assessments"]) < evidence_per_claim:
            cluster["assessments"].append(
                ConsensusAssessment(
                    stance=row["stance"],
                    paper_a_title=row["paper_a_title"],
                    paper_b_title=row["paper_b_title"],
                    quote_a=row["quote_a"],
                    quote_b=row["quote_b"],
                    page_a=row["page_a"],
                    page_b=row["page_b"],
                )
            )

    claims = [
        ConsensusClaim(
            claim_topic=cluster["claim_topic"],
            supports=cluster["supports"],
            opposes=cluster["opposes"],
            paper_ids=sorted(cluster["paper_ids"]),
            assessments=cluster["assessments"],
        )
        for cluster in clusters.values()
    ]
    claims.sort(key=lambda claim: claim.supports + claim.opposes, reverse=True)
    return claims[:limit], truncated
