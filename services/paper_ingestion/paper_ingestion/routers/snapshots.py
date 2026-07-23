"""PDF page snapshot serving endpoint."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from jarvis_common.auth import get_current_user_id
from jarvis_common.paths import secure_path

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.routers.pdfs import assert_paper_pdf_visible

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])

SNAPSHOT_STORAGE_PATH = get_paper_ingestion_settings().snapshot_storage_path


@router.get("/{paper_id}/{page}")
@limiter.limit("120/minute")
async def get_snapshot(
    request: Request,
    paper_id: int,
    page: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> FileResponse:
    """Serve a PDF page snapshot PNG.

    Persisted-public papers are accessible to every authenticated user. Private
    papers require explicit membership in the caller's ``user_library``. An
    absent or out-of-scope paper receives the same opaque 404 response.

    Parameters
    ----------
    paper_id : int
        Paper ID.
    page : int
        Page number (1-indexed).
    """
    # Path traversal protection
    try:
        snapshot_path = secure_path(SNAPSHOT_STORAGE_PATH, str(paper_id), f"page_{page}.png")
    except ValueError:
        raise HTTPException(400, "Invalid path") from None

    async with db_pool.acquire() as conn:
        await assert_paper_pdf_visible(conn, paper_id, user_id)

    if not snapshot_path.exists():
        raise HTTPException(404, f"Snapshot not found: paper {paper_id}, page {page}")

    return FileResponse(snapshot_path, media_type="image/png")
