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
    *,
    lock_for_update: bool = False,
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
    lock_for_update : bool
        Hold a row lock through the caller's transaction. Highlight creation
        uses this to keep its generation stamp ordered with source replacement.

    Returns
    -------
    str
        The paper's descriptive source type.

    Raises
    ------
    fastapi.HTTPException
        With an opaque 404 when the paper is absent, records no stored PDF, or
        is outside the caller's visibility scope.

    Notes
    -----
    Authorization uses the central persisted-scope-or-library predicate;
    provenance and discoverer fields never grant access. Six call sites share
    this guard, so its predicate is the read boundary for all of them at once:
    the PDF route below, the page-image route in
    :mod:`paper_ingestion.routers.snapshots`, both highlight routes in
    :mod:`paper_ingestion.routers.highlights`, the Zotero highlight-export
    route in :mod:`paper_ingestion.routers.zotero`, and that export's job
    handler in :mod:`paper_ingestion.integrations._zotero_jobs`, which
    re-checks the guard when the job runs.

    A row whose ``pdf_local_path`` is unset is treated as absent. The stored
    record, not the storage directory, decides what a paper currently has:
    every writer publishes that pointer in the same transaction that promotes
    ``{paper_id}.pdf``, and a superseded promotion clears it without removing
    the file (see :mod:`paper_ingestion.services.pdf_workflow`). Requiring the
    pointer keeps a file that no live record claims out of every shared route.
    """
    visibility_sql = paper_visibility_sql(2, alias="p")
    lock_sql = " FOR UPDATE OF p" if lock_for_update else ""
    row = await conn.fetchrow(
        "SELECT p.source_type FROM papers p "
        f"WHERE p.id = $1 AND p.pdf_local_path IS NOT NULL AND {visibility_sql}"
        f"{lock_sql}",
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
