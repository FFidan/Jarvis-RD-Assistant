"""Persistence and listing for verified cross-paper contradictions."""

from __future__ import annotations

from typing import Any

import asyncpg

from paper_ingestion.models import PaperContradictionResponse
from paper_ingestion.services.contradiction_models import ContradictionClassification
from paper_ingestion.services.contradictions_extract import ContradictionCandidate

ConnLike = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy  # type: ignore[type-arg]

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
                scanner_metadata, updated_at, user_id
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'verified',
                $12::jsonb, NOW(), $13
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
