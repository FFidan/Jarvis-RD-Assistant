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


async def _persist_contradiction(
    conn: ConnLike,
    candidate: ContradictionCandidate,
    parsed: ContradictionClassification,
    *,
    page_a: int | None,
    page_b: int | None,
    model: str,
    user_id: int | None = None,
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
    # stance + claim_topic are pair-level (symmetric), so the canonicalize swap
    # below does not touch them. A blank claim_topic persists as NULL.
    stance = parsed.stance
    claim_topic = parsed.claim_topic.strip() or None

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
                scanner_metadata, updated_at, user_id, stance, claim_topic
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'verified',
                $12::jsonb, NOW(), $13, $14, $15
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


async def list_contradictions(
    conn: ConnLike,
    *,
    user_id: int,
    paper_id: int | None = None,
    status: str | None = "verified",
    limit: int = 20,
) -> tuple[list[PaperContradictionResponse], int]:
    """List persisted contradictions scoped to the caller's library."""
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1
    # Scope to papers in the caller's user_library (both sides of contradiction).
    conditions.append(
        f"("
        f"EXISTS (SELECT 1 FROM user_library ul"
        f" WHERE ul.paper_id = pc.paper_a_id AND ul.user_id = ${idx})"
        f" OR EXISTS (SELECT 1 FROM user_library ul"
        f" WHERE ul.paper_id = pc.paper_b_id AND ul.user_id = ${idx})"
        f")"
    )
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


_CLAIM_TOPIC_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _normalize_claim_topic(topic: str) -> str:
    """Lowercase, collapse punctuation/whitespace runs to single spaces, trim.

    So "Effect of X, on Y!" and "effect of x on y" cluster together.
    """
    return _CLAIM_TOPIC_PUNCT_RE.sub(" ", topic.lower()).strip()


async def aggregate_consensus(
    conn: ConnLike,
    *,
    user_id: int,
    limit: int = 50,
    evidence_per_claim: int = 5,
) -> list[ConsensusClaim]:
    """Aggregate persisted supports/opposes stances by normalized claim_topic.

    Scoped to the caller's library via the same OR-predicate as
    ``list_contradictions`` (visible when the user owns either paper). Topics
    are grouped on a normalized form (lowercased, punctuation collapsed) so
    near-duplicate phrasings cluster together; the shortest original topic is
    returned for display. Each cluster carries up to ``evidence_per_claim``
    verified assessments (quotes + pages) so the consensus view can show its
    grounding.
    """
    rows = await conn.fetch(
        """
        SELECT pc.stance, pc.claim_topic, pc.paper_a_id, pc.paper_b_id,
               pc.quote_a, pc.quote_b, pc.page_a, pc.page_b,
               pa.title AS paper_a_title, pb.title AS paper_b_title
        FROM paper_contradictions pc
        JOIN papers pa ON pa.id = pc.paper_a_id
        JOIN papers pb ON pb.id = pc.paper_b_id
        WHERE pc.status = 'verified'
          AND pc.stance IN ('supports', 'opposes')
          AND pc.claim_topic IS NOT NULL
          AND btrim(pc.claim_topic) <> ''
          AND (
              EXISTS (SELECT 1 FROM user_library ul
                      WHERE ul.paper_id = pc.paper_a_id AND ul.user_id = $1)
              OR EXISTS (SELECT 1 FROM user_library ul
                      WHERE ul.paper_id = pc.paper_b_id AND ul.user_id = $1)
          )
        ORDER BY pc.created_at DESC
        LIMIT 1000
        """,
        user_id,
    )

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
    return claims[:limit]
