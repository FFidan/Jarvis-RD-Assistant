"""PDF page snapshot serving endpoint."""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.deps import limiter

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])

SNAPSHOT_STORAGE_PATH = os.environ.get("SNAPSHOT_STORAGE_PATH", "/data/snapshots")


@router.get("/{paper_id}/{page}")
@limiter.limit("120/minute")
async def get_snapshot(request: Request, paper_id: int, page: int) -> FileResponse:
    """Serve a PDF page snapshot PNG.

    Parameters
    ----------
    paper_id : int
        Paper ID.
    page : int
        Page number (1-indexed).
    """
    base = Path(SNAPSHOT_STORAGE_PATH).resolve()
    snapshot_path = (base / str(paper_id) / f"page_{page}.png").resolve()

    # Path traversal protection
    if not snapshot_path.is_relative_to(base):
        raise HTTPException(400, "Invalid path")

    if not snapshot_path.exists():
        raise HTTPException(404, f"Snapshot not found: paper {paper_id}, page {page}")

    return FileResponse(snapshot_path, media_type="image/png")
