"""Paper-ingestion analytics endpoints."""

from typing import Literal

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from jarvis_common import jobs as jobs_lib
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, limiter

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class MissingFoundationalPaper(BaseModel):
    """Citation-stub paper that appears foundational but is not in the library."""

    paper_id: int
    title: str
    authors: list[str]
    year: int | None = None
    citation_count: int
    cited_by_library_count: int
    url: str | None = None
    pdf_available: bool


class FetchAndProcessRequest(BaseModel):
    paper_id: int


class FetchAndProcessResponse(BaseModel):
    paper_id: int
    status: Literal["queued", "no_pdf"]
    job_id: str | None = None
    message: str | None = None


@router.get("/missing-foundational", response_model=list[MissingFoundationalPaper])
@limiter.limit("30/minute")
async def get_missing_foundational(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[MissingFoundationalPaper]:
    """Return high-citation citation stubs already discovered from local papers."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                p.id,
                p.title,
                p.authors,
                EXTRACT(YEAR FROM p.published_date)::int AS year,
                p.citation_count,
                p.url,
                p.pdf_url,
                p.pdf_downloaded,
                COUNT(DISTINCT pc.source_paper_id) AS cited_by_library_count
            FROM papers p
            JOIN paper_citations pc ON pc.cited_paper_id = p.id
            WHERE p.metadata->>'stub' = 'true'
              AND COALESCE(p.pdf_downloaded, FALSE) = FALSE
            GROUP BY p.id
            ORDER BY cited_by_library_count DESC, p.citation_count DESC NULLS LAST, p.title
            LIMIT 10
            """
        )
    return [
        MissingFoundationalPaper(
            paper_id=row["id"],
            title=row["title"],
            authors=row["authors"] or [],
            year=row["year"],
            citation_count=row["citation_count"] or 0,
            cited_by_library_count=row["cited_by_library_count"] or 0,
            url=row["url"],
            pdf_available=bool(row["pdf_downloaded"] or row["pdf_url"]),
        )
        for row in rows
    ]


@router.post("/fetch-and-process", response_model=FetchAndProcessResponse)
@limiter.limit("10/minute")
async def fetch_and_process_foundational(
    request: Request,
    body: FetchAndProcessRequest = Body(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> FetchAndProcessResponse:
    """Promote a citation stub and enqueue processing only when a PDF exists."""
    async with db_pool.acquire() as conn:
        paper = await conn.fetchrow(
            """
            SELECT id, pdf_url, pdf_downloaded, pdf_local_path, metadata
            FROM papers
            WHERE id = $1 AND metadata->>'stub' = 'true'
            """,
            body.paper_id,
        )
        if not paper:
            raise HTTPException(status_code=404, detail="Citation stub paper not found")
        await conn.execute(
            """
            UPDATE papers
            SET metadata = COALESCE(metadata, '{}'::jsonb) || '{"stub": "false"}'::jsonb
            WHERE id = $1
            """,
            body.paper_id,
        )

    if paper["pdf_downloaded"] and paper["pdf_local_path"]:
        job_id = await jobs_lib.enqueue(db_pool, "paper.process", {"paper_id": body.paper_id})
        return FetchAndProcessResponse(paper_id=body.paper_id, status="queued", job_id=job_id)
    if paper["pdf_url"]:
        job_id = await jobs_lib.enqueue(db_pool, "paper.analyze", {"paper_id": body.paper_id})
        return FetchAndProcessResponse(paper_id=body.paper_id, status="queued", job_id=job_id)
    return FetchAndProcessResponse(
        paper_id=body.paper_id,
        status="no_pdf",
        message="No PDF URL is available for this citation stub.",
    )
