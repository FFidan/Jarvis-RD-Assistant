"""PDF page snapshot serving endpoint."""

from pathlib import Path

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from jarvis_common.auth import get_current_user_id

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import SourceType

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])

SNAPSHOT_STORAGE_PATH = get_paper_ingestion_settings().snapshot_storage_path

# Public-corpus source types whose page snapshots are shared with any
# authenticated user (D4). All other source types (LOCAL uploads, ZOTERO
# imports) are private-origin and scoped to their discoverer / library.
_PUBLIC_SOURCE_TYPES: frozenset[str] = frozenset(
    {
        SourceType.ARXIV.value,
        SourceType.SEMANTIC_SCHOLAR.value,
        SourceType.OPENALEX.value,
        SourceType.PUBMED.value,
    }
)


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

    Public-source papers (arXiv, S2, OpenAlex, PubMed) are accessible to any
    authenticated user (D4 shared corpus decision).  Private-origin papers
    (LOCAL uploads, ZOTERO imports) are scoped to the caller who discovered them
    (``discovered_by``), to anyone with the paper in their ``user_library``, or
    when the row is unattributed (``discovered_by IS NULL``); a non-owner gets
    an opaque 404 that does not reveal whether the paper exists.

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

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.source_type,
                   p.discovered_by,
                   EXISTS (
                       SELECT 1 FROM user_library ul
                       WHERE ul.paper_id = p.id AND ul.user_id = $2
                   ) AS in_library
            FROM papers p
            WHERE p.id = $1
            """,
            paper_id,
            user_id,
        )

    # Unknown paper_id → opaque 404 (same as missing snapshot below)
    if row is None or (
        row["source_type"] not in _PUBLIC_SOURCE_TYPES
        and row["discovered_by"] is not None
        and row["discovered_by"] != user_id
        and not row["in_library"]
    ):
        raise HTTPException(404, f"Snapshot not found: paper {paper_id}, page {page}")

    if not snapshot_path.exists():
        raise HTTPException(404, f"Snapshot not found: paper {paper_id}, page {page}")

    return FileResponse(snapshot_path, media_type="image/png")
