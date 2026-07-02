"""Raw-PDF serving endpoint for the in-PDF annotation reader."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from jarvis_common.auth import get_current_user_id
from jarvis_common.paths import secure_path

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import SourceType

router = APIRouter(prefix="/api/pdfs", tags=["pdfs"])

PDF_STORAGE_PATH = get_paper_ingestion_settings().pdf_storage_path

# Public-corpus source types whose PDFs are shared with any authenticated user
# (D4 shared-corpus decision). All other source types (LOCAL uploads, ZOTERO
# library imports) are private-origin and scoped to their discoverer / library.
_PUBLIC_SOURCE_TYPES: frozenset[str] = frozenset(
    {
        SourceType.ARXIV.value,
        SourceType.SEMANTIC_SCHOLAR.value,
        SourceType.OPENALEX.value,
        SourceType.PUBMED.value,
    }
)


async def assert_paper_pdf_visible(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    paper_id: int,
    user_id: int,
) -> str:
    """Return the paper's ``source_type`` if the caller may view its PDF, else 404.

    Public-source papers (arXiv, S2, OpenAlex, PubMed) are visible to any
    authenticated user (shared-corpus decision). Private-origin papers (LOCAL
    uploads, ZOTERO imports) are visible only to the caller who discovered them
    (``discovered_by``), to anyone with the paper in their ``user_library``, or
    when the row is unattributed (``discovered_by IS NULL``, legacy/shared
    corpus — matches ``paper_visible_sql``). An unknown or out-of-scope paper
    raises an opaque 404 — a 403 would leak whether the paper exists.

    Shared by the PDF endpoint and the highlights CRUD router so that anyone who
    can *view* a PDF can *annotate* it.
    """
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
    if row is None or (
        row["source_type"] not in _PUBLIC_SOURCE_TYPES
        and row["discovered_by"] is not None
        and row["discovered_by"] != user_id
        and not row["in_library"]
    ):
        raise HTTPException(404, f"Paper not found: {paper_id}")
    return row["source_type"]


@router.get("/{paper_id}")
@limiter.limit("60/minute")
async def get_pdf(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> FileResponse:
    """Serve a paper's raw PDF for the in-browser annotation reader.

    Visibility mirrors the snapshot endpoint: public-source papers are served to
    any authenticated user; uploaded/local papers are scoped to the caller's
    library, with an opaque 404 for everything out of scope.

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
