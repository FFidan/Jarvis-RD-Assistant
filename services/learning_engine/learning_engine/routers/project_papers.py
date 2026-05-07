"""Project ↔ Papers linking endpoints."""

import logging
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from jarvis_common import log_audit
from jarvis_common.auth import current_user_id_or_none

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import ProjectPaperItem, ProjectPaperLinkResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["project-papers"])


@router.get("/{project_id}/papers", response_model=list[ProjectPaperItem])
@limiter.limit("60/minute")
async def list_project_papers(
    request: Request,
    project_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """List papers linked to a project."""
    async with db_pool.acquire() as conn:
        # Verify project exists (same connection as data query to avoid TOCTOU)
        project = await conn.fetchval("SELECT id FROM projects WHERE id = $1", project_id)
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


@router.post(
    "/{project_id}/papers/{paper_id}",
    status_code=201,
    response_model=ProjectPaperLinkResponse,
)
@limiter.limit("30/minute")
async def link_paper(
    request: Request,
    project_id: int,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict | JSONResponse:
    """Link a paper to a project."""
    should_push_zotero = False
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            project = await conn.fetchrow("SELECT id FROM projects WHERE id = $1", project_id)
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")
            paper = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
            if not paper:
                raise HTTPException(status_code=404, detail="Paper not found")
            result = await conn.execute(
                "INSERT INTO project_papers (project_id, paper_id) "
                "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                project_id,
                paper_id,
            )
            # result is e.g. "INSERT 0 1" (inserted) or "INSERT 0 0" (no-op)
            if result and result == "INSERT 0 0":
                # Paper already linked — return early before enqueuing any job.
                return JSONResponse(
                    content={
                        "project_id": project_id,
                        "paper_id": paper_id,
                        "message": "Paper already linked",
                    },
                    status_code=200,
                )

            # Trigger Zotero push when a paper is linked to a project if it is starred
            # or was previously pushed to Zotero.  The job handler checks config at runtime
            # and returns early if Zotero is disabled.
            # LE-012: capture push intent inside the transaction; defer_async is called
            # after commit (procrastinate uses its own pool and cannot join this txn).
            row = await conn.fetchrow(
                """
                SELECT pus.starred, p.zotero_item_key
                FROM papers p
                LEFT JOIN paper_user_state pus ON pus.paper_id = p.id
                WHERE p.id = $1
                """,
                paper_id,
            )
            if row and (row["starred"] or row["zotero_item_key"]):
                should_push_zotero = True

    # Fire zotero.push after transaction commits so the linked row is visible.
    # The handler is idempotent: it reads from DB and returns early if conditions
    # are no longer met, so a stale fire is harmless.
    if should_push_zotero:
        from jarvis_common.task_registry import zotero_push

        try:
            await zotero_push.defer_async(
                job_id=str(uuid.uuid4()),
                user_id=None,
                paper_id=paper_id,
            )
            logger.debug(
                "Enqueued zotero.push for paper %d linked to project %d",
                paper_id,
                project_id,
            )
        except Exception:
            logger.warning(
                "Failed to enqueue zotero.push after project link (paper=%d project=%d)",
                paper_id,
                project_id,
                exc_info=True,
            )

    return {"project_id": project_id, "paper_id": paper_id}


@router.delete("/{project_id}/papers/{paper_id}", status_code=204)
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
    user_id = await current_user_id_or_none(request)
    await log_audit(
        db_pool,
        action="delete",
        resource=f"project_paper:{project_id}:{paper_id}",
        user_id=str(user_id) if user_id is not None else None,
    )
