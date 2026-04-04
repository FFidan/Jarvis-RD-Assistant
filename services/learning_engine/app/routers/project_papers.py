"""Project ↔ Papers linking endpoints."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.deps import get_db_pool, limiter
from app.models import ProjectPaperItem, ProjectPaperLinkResponse

router = APIRouter(tags=["project-papers"])


@router.get("/api/projects/{project_id}/papers", response_model=list[ProjectPaperItem])
@limiter.limit("60/minute")
async def list_project_papers(
    request: Request,
    project_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """List papers linked to a project."""
    async with db_pool.acquire() as conn:
        # Verify project exists (same connection as data query to avoid TOCTOU)
        project = await conn.fetchval(
            "SELECT id FROM projects WHERE id = $1", project_id
        )
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        rows = await conn.fetch(
            """
            SELECT p.id, p.title, p.authors, p.source_type, p.published_date,
                   pp.notes, pp.added_at
            FROM project_papers pp
            JOIN papers p ON p.id = pp.paper_id
            WHERE pp.project_id = $1
            ORDER BY pp.added_at DESC
            """,
            project_id,
        )
    return [dict(r) for r in rows]


@router.post("/api/projects/{project_id}/papers/{paper_id}", status_code=201, response_model=ProjectPaperLinkResponse)
@limiter.limit("30/minute")
async def link_paper(
    request: Request,
    project_id: int,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict:
    """Link a paper to a project."""
    async with db_pool.acquire() as conn:
        project = await conn.fetchrow(
            "SELECT id FROM projects WHERE id = $1", project_id
        )
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        paper = await conn.fetchrow(
            "SELECT id FROM papers WHERE id = $1", paper_id
        )
        if not paper:
            raise HTTPException(status_code=404, detail="Paper not found")
        result = await conn.execute(
            "INSERT INTO project_papers (project_id, paper_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            project_id,
            paper_id,
        )
    # result is e.g. "INSERT 0 1" (inserted) or "INSERT 0 0" (no-op)
    if result and result == "INSERT 0 0":
        return JSONResponse(
            content={"project_id": project_id, "paper_id": paper_id, "message": "Paper already linked"},
            status_code=200,
        )
    return {"project_id": project_id, "paper_id": paper_id}


@router.delete("/api/projects/{project_id}/papers/{paper_id}", status_code=204)
@limiter.limit("30/minute")
async def unlink_paper(
    request: Request,
    project_id: int,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Unlink a paper from a project."""
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM project_papers WHERE project_id = $1 AND paper_id = $2",
            project_id,
            paper_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Link not found")
