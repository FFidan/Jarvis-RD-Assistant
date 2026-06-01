"""Project ↔ Papers linking endpoints."""

import logging
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import assert_paper_ownership, log_audit
from jarvis_common.auth import current_user_id_strict
from jarvis_common.library import add_to_library

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import ProjectPaperItem, ProjectPaperLinkResponse
from learning_engine.routers._guards import assert_project_owner as _assert_project_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["project-papers"])


@router.get("/{project_id}/papers", response_model=list[ProjectPaperItem])
@limiter.limit("60/minute")
async def list_project_papers(
    request: Request,
    project_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[dict]:
    """List papers linked to a project."""
    async with db_pool.acquire() as conn:
        # Scope project lookup by owner — IDOR otherwise.
        await _assert_project_owner(conn, project_id, user_id)

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
    user_id: int = Depends(current_user_id_strict),
) -> dict:
    """Link a paper to a project."""
    should_push_zotero = False
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Scope by owner. Cannot link a paper into another user's project.
            await _assert_project_owner(conn, project_id, user_id)
            await assert_paper_ownership(conn, paper_id, user_id)
            await add_to_library(
                conn,
                user_id=user_id,
                paper_id=paper_id,
                added_via="manual_save",
            )
            result = await conn.execute(
                "INSERT INTO project_papers (project_id, paper_id) "
                "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                project_id,
                paper_id,
            )
            # result is e.g. "INSERT 0 1" (inserted) or "INSERT 0 0" (no-op)
            if result and result == "INSERT 0 0":
                # Paper already linked — return early before enqueuing any job.
                return {"project_id": project_id, "paper_id": paper_id}

            # Trigger Zotero push when a paper is linked to a project if it is starred
            # or was previously pushed to Zotero.  The job handler checks credentials at
            # runtime and returns early if Zotero is not configured for the user.
            # LE-012: capture push intent inside the transaction; defer_async is called
            # after commit (procrastinate uses its own pool and cannot join this txn).
            row = await conn.fetchrow(
                """
                SELECT pus.starred, p.zotero_item_key
                FROM papers p
                LEFT JOIN paper_user_state pus
                  ON pus.paper_id = p.id AND pus.user_id = $2
                WHERE p.id = $1
                """,
                paper_id,
                user_id,
            )
            if row and (row["starred"] or row["zotero_item_key"]):
                should_push_zotero = True

    # Fire zotero.push after transaction commits so the linked row is visible.
    # The handler is idempotent: it reads from DB and returns early if conditions
    # are no longer met, so a stale fire is harmless.
    if should_push_zotero:
        from jarvis_common.task_registry import KIND_TO_TASK

        try:
            await KIND_TO_TASK["zotero.push"].defer_async(
                job_id=str(uuid.uuid4()),
                user_id=user_id,
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
    user_id: int = Depends(current_user_id_strict),
) -> None:
    """Unlink a paper from a project."""
    async with db_pool.acquire() as conn:
        # Prevent IDOR — only delete links whose project belongs to caller.
        result = await conn.execute(
            "DELETE FROM project_papers pp USING projects p "
            "WHERE pp.project_id = $1 AND pp.paper_id = $2 "
            "AND p.id = pp.project_id AND p.user_id = $3",
            project_id,
            paper_id,
            user_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Link not found")
    await log_audit(
        db_pool,
        action="delete",
        resource=f"project_paper:{project_id}:{paper_id}",
        user_id=str(user_id),
    )
