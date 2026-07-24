"""Raw-PDF serving endpoint for the in-PDF annotation reader."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from jarvis_common.auth import get_current_user_id
from jarvis_common.paper_visibility import paper_visibility_sql
from jarvis_common.paths import secure_path

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import get_db_pool, limiter

router = APIRouter(prefix="/api/pdfs", tags=["pdfs"])

PDF_STORAGE_PATH = get_paper_ingestion_settings().pdf_storage_path


async def assert_paper_pdf_visible(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    paper_id: int,
    user_id: int,
) -> str:
    """Return ``source_type`` when the caller may view the paper's PDF.

    Parameters
    ----------
    conn : asyncpg.Connection | asyncpg.pool.PoolConnectionProxy
        Open database connection used for the authorization lookup.
    paper_id : int
        Paper whose stored PDF is being requested.
    user_id : int
        Authenticated caller.

    Returns
    -------
    str
        The paper's descriptive source type.

    Raises
    ------
    fastapi.HTTPException
        With an opaque 404 when the paper is absent or outside the caller's
        visibility scope.

    Notes
    -----
    Authorization uses the central persisted-scope-or-library predicate;
    provenance and discoverer fields never grant access. The PDF, snapshot,
    and highlights routes share this guard so their read boundary is symmetric.
    """
    visibility_sql = paper_visibility_sql(2, alias="p")
    row = await conn.fetchrow(
        f"SELECT p.source_type FROM papers p WHERE p.id = $1 AND {visibility_sql}",
        paper_id,
        user_id,
    )
    if row is None:
        raise HTTPException(404, f"Paper not found: {paper_id}")
    return str(row["source_type"])


@router.get("/{paper_id}")
@limiter.limit("60/minute")
async def get_pdf(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> FileResponse:
    """Serve a paper's raw PDF for the in-browser annotation reader.

    Visibility mirrors the snapshot endpoint: persisted-public papers are
    served to every authenticated user, while private papers require explicit
    membership in the caller's library. Out-of-scope papers return an opaque
    404.

    Parameters
    ----------
    paper_id : int
        Paper ID.
    """
    # Path-traversal guard, mirroring the snapshots endpoint. ``paper_id`` is
    # int-typed so this is unreachable in practice, but kept as defence-in-depth.
    try:
        pdf_path = secure_path(PDF_STORAGE_PATH, f"{paper_id}.pdf")
    except ValueError:
        raise HTTPException(400, "Invalid path") from None

    async with db_pool.acquire() as conn:
        await assert_paper_pdf_visible(conn, paper_id, user_id)

    if not pdf_path.exists():
        raise HTTPException(404, f"PDF not found: paper {paper_id}")

    return FileResponse(pdf_path, media_type="application/pdf")
