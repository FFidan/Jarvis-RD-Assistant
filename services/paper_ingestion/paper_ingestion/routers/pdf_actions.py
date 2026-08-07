"""PDF download, processing, upload, and scan endpoints."""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from jarvis_common import JobCreateResponse, assert_paper_ownership, current_user_id_strict
from jarvis_common.auth import require_admin
from jarvis_common.library import DbLike, add_to_library, is_in_library
from jarvis_common.settings import get_core_settings

from paper_ingestion.converters import row_to_paper_response
from paper_ingestion.deps import get_db_pool, get_embedder, get_pdf_processor, limiter
from paper_ingestion.models import (
    BatchProcessResponse,
    PaperResponse,
)
from paper_ingestion.pdf_processor import (
    MAX_UPLOAD_PDF_SIZE,
    PDF_STORAGE_PATH,
    PDFProcessor,
    PDFPublishBlockedError,
    check_pdf_path_safe,
    pdf_publish_operation,
)
from paper_ingestion.services.job_enqueue import enqueue_job
from paper_ingestion.services.pdf_workflow import (
    PDFRebuildNotPermittedError,
    PDFRecordMissingError,
    ProcessPdfResult,
    download_and_store_pdf,
    run_process_pdf,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["pdf"])

REBUILD_REQUIRES_LIBRARY_DETAIL = "Add this paper to your library before rebuilding its content."

# The synchronous process-pdf branch holds one pooled connection for the whole
# run — extraction, page rendering and embedding included. This bound caps how
# many of those runs may be in flight per worker process so the rest of the
# service keeps most of the pool (jarvis_common.app_factory sets max_size 10).
SYNC_PROCESS_CONCURRENCY = 3
SYNC_PROCESS_SLOTS = asyncio.Semaphore(SYNC_PROCESS_CONCURRENCY)


@asynccontextmanager
async def _bounded_sync_run() -> AsyncIterator[None]:
    """Hold one synchronous-processing slot, refusing rather than queueing.

    A queued request would occupy a worker for the whole run ahead of it while
    holding nothing the caller can use, so a saturated bound is refused at once.

    Raises
    ------
    fastapi.HTTPException
        With status 429 when the bound is already saturated.
    """
    if SYNC_PROCESS_SLOTS.locked():
        raise HTTPException(
            status_code=429,
            detail="Too many synchronous PDF processing requests. "
            "Retry shortly, or omit sync=true to queue the work.",
        )
    async with SYNC_PROCESS_SLOTS:
        yield


async def _require_library_membership(
    conn: DbLike,
    paper_id: int,
    user_id: int,
) -> None:
    """Require the caller to hold ``paper_id`` before its content may be rebuilt.

    A rebuild discards the paper's existing derived content, so it needs library
    membership rather than the read visibility :func:`assert_paper_ownership`
    grants, which every authenticated caller satisfies for a public paper.

    Parameters
    ----------
    conn : DbLike
        Open connection owned by the caller; reused rather than acquiring another.
    paper_id : int
        The paper primary key to check.
    user_id : int
        Authenticated caller ID.

    Raises
    ------
    fastapi.HTTPException
        With status 403 when the paper is absent from the caller's library.
    """
    if not await is_in_library(conn, user_id=user_id, paper_id=paper_id):
        raise HTTPException(status_code=403, detail=REBUILD_REQUIRES_LIBRARY_DETAIL)


def _process_failure_error(exc: RuntimeError, request: Request, paper_id: int) -> HTTPException:
    """Return the 502 for a failed synchronous run, sanitized outside dev mode.

    Parameters
    ----------
    exc : RuntimeError
        Service-level failure raised by the workflow.
    request : fastapi.Request
        Request whose ``request_id`` correlates the client reply with the logs.
    paper_id : int
        Fallback correlation id when the request carries none.
    """
    request_id = getattr(request.state, "request_id", None) or str(paper_id)
    if get_core_settings().dev_mode:
        detail: dict = {
            "detail": str(exc),
            "error_type": type(exc).__name__,
            "error_detail": str(exc)[:200],
            "request_id": request_id,
        }
    else:
        logger.exception("PDF process 502", extra={"request_id": request_id, "exc": str(exc)})
        detail = {"detail": "PDF processing failed", "request_id": request_id}
    return HTTPException(status_code=502, detail=detail)


# ---------------------------------------------------------------------------
# POST /api/download-pdf/{paper_id}
# ---------------------------------------------------------------------------


@router.post("/download-pdf/{paper_id}", response_model=PaperResponse)
@limiter.limit("30/minute")
async def download_pdf(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    pdf_processor: PDFProcessor = Depends(get_pdf_processor),
    user_id: int = Depends(current_user_id_strict),
) -> PaperResponse:
    """Download the PDF for a paper.

    Parameters
    ----------
    paper_id : int
        Database paper ID.

    Returns
    -------
    PaperResponse
        Updated paper with ``pdf_local_path`` and ``pdf_downloaded=True``.
    """
    import httpx

    # Load and validate (short transaction — no lock held during I/O)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await assert_paper_ownership(conn, paper_id, user_id)
    if not row["pdf_url"]:
        raise HTTPException(status_code=400, detail="Paper has no PDF URL")
    if row["pdf_downloaded"]:
        return row_to_paper_response(row)

    # Download (no DB connection held across slow HTTP)
    try:
        updated = await download_and_store_pdf(
            db_pool,
            pdf_processor,
            row["pdf_url"],
            paper_id,
        )
    except PDFPublishBlockedError as exc:
        raise HTTPException(
            status_code=503,
            detail="A restore is in progress. Try downloading the PDF again shortly.",
        ) from exc
    except ValueError as e:
        request_id = getattr(request.state, "request_id", None) or str(paper_id)
        logger.exception("PDF download validation failed", extra={"request_id": request_id})
        raise HTTPException(
            status_code=400,
            detail=f"PDF download failed (request_id={request_id}). Please check the paper URL.",
        ) from e
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="PDF download failed")
    except PDFRecordMissingError as exc:
        raise HTTPException(
            status_code=404,
            detail="The paper changed while its PDF was downloading.",
        ) from exc
    return row_to_paper_response(updated)


# ---------------------------------------------------------------------------
# POST /api/process-pdf/{paper_id}
# ---------------------------------------------------------------------------


# response_model is intentionally omitted: the async branch returns {job_id, status}
# while the sync branch returns {paper_id, chunk_count, status} — two different shapes.
# FastAPI serialises whichever dict the handler returns without validation errors.
@router.post("/process-pdf/{paper_id}", response_model=None)
@limiter.limit("5/minute")
async def process_pdf(
    request: Request,
    paper_id: int,
    force: bool = Query(default=False),
    sync: bool = Query(
        default=False,
        description=(
            "If false (default), enqueue a paper.process job and return "
            "{job_id, status}. If true, run synchronously (backward compat)."
        ),
    ),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    pdf_processor: PDFProcessor = Depends(get_pdf_processor),
    embedder=Depends(get_embedder),
    user_id: int = Depends(current_user_id_strict),
) -> dict[str, object] | ProcessPdfResult:
    """Extract text, chunk, embed, and generate snapshots for a paper's PDF.

    Parameters
    ----------
    paper_id : int
        Database paper ID. PDF must already be downloaded.
    sync : bool
        When ``False`` (default), enqueues an async ``paper.process`` job and
        returns ``{"job_id": "...", "status": "queued"}``.
        When ``True``, runs the processing synchronously and returns the result
        dict immediately (backward-compatible behaviour for scripts/tests).

    Returns
    -------
    dict
        Async mode: ``{"job_id": "...", "status": "queued"}``.
        Sync mode: ``{"paper_id": ..., "chunk_count": ..., "status": ...}``.

    Raises
    ------
    fastapi.HTTPException
        403 when ``force`` is set and the paper is not in the caller's library,
        on either branch: discarding and rebuilding a paper's derived content
        requires holding it, not merely being able to see it. 429 when the
        synchronous concurrency bound is already saturated.
    """
    if not sync:
        import uuid  # noqa: PLC0415

        from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

        async with db_pool.acquire() as conn:
            await assert_paper_ownership(conn, paper_id, user_id)
            if force:
                await _require_library_membership(conn, paper_id, user_id)
        jarvis_job_id = str(uuid.uuid4())
        await KIND_TO_TASK["paper.process"].defer_async(
            job_id=jarvis_job_id, user_id=user_id, paper_id=paper_id, force=force
        )
        return {"job_id": jarvis_job_id, "status": "queued"}

    # Synchronous path (sync=True) — original blocking behaviour
    async with _bounded_sync_run():
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
            await assert_paper_ownership(conn, paper_id, user_id)
            if force:
                await _require_library_membership(conn, paper_id, user_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        if not row["pdf_downloaded"] or not row["pdf_local_path"]:
            raise HTTPException(status_code=400, detail="PDF not yet downloaded")

        pdf_path = Path(row["pdf_local_path"])

        # Path traversal protection (S-12)
        if not check_pdf_path_safe(pdf_path, PDF_STORAGE_PATH):
            raise HTTPException(status_code=400, detail="Invalid PDF path")

        if not pdf_path.exists():
            raise HTTPException(status_code=404, detail="PDF file missing from disk")

        try:
            return await run_process_pdf(
                paper_id,
                pdf_path,
                db_pool,
                pdf_processor,
                embedder,
                force=force,
                requester_id=user_id,
            )
        except PDFRebuildNotPermittedError as exc:
            raise HTTPException(status_code=403, detail=REBUILD_REQUIRES_LIBRARY_DETAIL) from exc
        except RuntimeError as exc:
            raise _process_failure_error(exc, request, paper_id) from exc


# ---------------------------------------------------------------------------
# POST /api/upload-pdf
# ---------------------------------------------------------------------------


@router.post("/upload-pdf", response_model=PaperResponse)
@limiter.limit("10/minute")
async def upload_pdf(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(..., max_length=500),
    authors: str = Form(default="", max_length=2000),
    abstract: str = Form(default="", max_length=10000),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> PaperResponse:
    """Upload a local PDF file and register it as a paper.

    Parameters
    ----------
    file : UploadFile
        The PDF file to upload.
    title : str
        Paper title.
    authors : str
        Comma-separated author names (optional).
    abstract : str
        Paper abstract (optional).

    Returns
    -------
    PaperResponse
        The newly created paper record.
    """
    # Validate filename
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must be a PDF (.pdf)")
    if file.content_type is not None and file.content_type not in (
        "application/pdf",
        "application/octet-stream",
    ):
        raise HTTPException(status_code=400, detail="File must be a PDF (.pdf)")

    # Stream file to temp path, validating as we go
    from uuid import uuid4 as _uuid4

    header_checked = False
    total_size = 0
    hasher = hashlib.sha256()
    storage_path = Path(PDF_STORAGE_PATH)
    storage_path.mkdir(parents=True, exist_ok=True)
    temp_path = storage_path / f"_upload_{_uuid4().hex[:16]}.pdf"
    try:
        with temp_path.open("wb") as _f:
            while chunk := await file.read(8192):
                if not header_checked:
                    header_checked = True
                    if not chunk[:5].startswith(b"%PDF-"):
                        raise HTTPException(
                            status_code=400,
                            detail="File does not appear to be a valid PDF",
                        )
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_PDF_SIZE:
                    raise HTTPException(
                        status_code=400,
                        detail="File exceeds 50 MB size limit",
                    )
                hasher.update(chunk)
                _f.write(chunk)

        file_hash = hasher.hexdigest()
        external_id = f"local:{file_hash}"

        # Check for duplicate
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM papers WHERE external_id = $1", external_id
            )
            if existing:
                if user_id is not None:
                    await add_to_library(
                        conn,
                        user_id=user_id,
                        paper_id=existing["id"],
                        added_via="manual_save",
                    )
                return row_to_paper_response(existing)

            # Parse authors
            author_list = [a.strip() for a in authors.split(",") if a.strip()] if authors else []

            try:
                # The restore-shared lock remains held through transaction
                # commit. A failed commit removes the promoted request file
                # before restore can swap the numeric PDF set.
                async with pdf_publish_operation(storage_path) as publication:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            """
                            INSERT INTO papers (external_id, source_type, title, authors, abstract,
                                                url, metadata, discovered_by, discovery_origin)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'user_initiated')
                            RETURNING *
                            """,
                            external_id,
                            "local",
                            title,
                            author_list,
                            abstract or None,
                            f"local://{file_hash}",
                            {},
                            user_id,
                        )
                        paper_id = row["id"]
                        if user_id is not None:
                            await add_to_library(
                                conn,
                                user_id=user_id,
                                paper_id=paper_id,
                                added_via="manual_save",
                            )

                        pdf_path = storage_path / f"{paper_id}.pdf"
                        await publication.promote(temp_path, pdf_path)
                        updated = await conn.fetchrow(
                            """
                            UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $1
                            WHERE id = $2 RETURNING *
                            """,
                            str(pdf_path),
                            paper_id,
                        )
            except PDFPublishBlockedError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="A restore is in progress. Try uploading the PDF again shortly.",
                ) from exc
    finally:
        # No-op after successful rename (file no longer at temp_path); cleans up on any exception
        temp_path.unlink(missing_ok=True)

    return row_to_paper_response(updated)


# ---------------------------------------------------------------------------
# POST /api/scan-local-pdfs
# ---------------------------------------------------------------------------


@router.post(
    "/scan-local-pdfs",
    response_model=JobCreateResponse,
    status_code=202,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("2/minute")
async def scan_local_pdfs(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> JobCreateResponse:
    """Enqueue a local PDF directory scan job.

    The worker performs filesystem access and returns the import summary in
    the job result.
    """
    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    # Thread caller user_id for audit-trail attribution. The scan itself is
    # filesystem-wide; per-user PDF dirs are deferred until the multi-user
    # PDF storage spec lands.
    return await enqueue_job(KIND_TO_TASK["papers.scan_local"], user_id=user_id)


# ---------------------------------------------------------------------------
# POST /api/papers/batch-process
# ---------------------------------------------------------------------------


@router.post("/papers/batch-process", response_model=BatchProcessResponse)
@limiter.limit("2/minute")
async def batch_process_papers(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    force: bool = Query(default=False),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> dict[str, object]:
    """Queue unprocessed papers for chunk extraction + embedding.

    Finds papers with ``pdf_downloaded=True`` that have no ``paper_chunks`` rows,
    up to ``limit`` papers.  Enqueues a single ``papers.batch_process`` job
    that processes all selected papers, returning a ``job_id`` the caller
    can poll via ``GET /api/jobs/{job_id}``.

    When ``force=True``, includes ALL papers with downloaded PDFs (even those
    already processed), allowing re-extraction with updated models.

    In multi-user mode (``user_id`` is not None), only papers present in the
    caller's ``user_library`` are eligible — preventing cross-user data touch
    and DoS amplification via the full corpus.  In single-tenant mode
    (``user_id`` is None) the original full-corpus behaviour is preserved.

    Parameters
    ----------
    limit : int
        Maximum number of papers to queue (1-50, default 10).
    force : bool
        If True, include papers that already have chunks (re-process all).

    Returns
    -------
    dict
        ``{queued, total_unprocessed, skipped_missing_pdf, job_id}``
    """
    import uuid  # noqa: PLC0415

    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    async with db_pool.acquire() as conn:
        if user_id is not None:
            if force:
                rows = await conn.fetch(
                    """
                    SELECT p.id, p.pdf_local_path FROM papers p
                    JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $2
                    WHERE p.pdf_downloaded = TRUE
                      AND p.pdf_local_path IS NOT NULL
                    ORDER BY p.id
                    LIMIT $1
                    """,
                    limit,
                    user_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT p.id, p.pdf_local_path FROM papers p
                    JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $2
                    WHERE p.pdf_downloaded = TRUE
                      AND p.pdf_local_path IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id
                      )
                    ORDER BY p.id
                    LIMIT $1
                    """,
                    limit,
                    user_id,
                )
        else:
            if force:
                rows = await conn.fetch(
                    """
                    SELECT p.id, p.pdf_local_path FROM papers p
                    WHERE p.pdf_downloaded = TRUE
                      AND p.pdf_local_path IS NOT NULL
                    ORDER BY p.id
                    LIMIT $1
                    """,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT p.id, p.pdf_local_path FROM papers p
                    WHERE p.pdf_downloaded = TRUE
                      AND p.pdf_local_path IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id
                      )
                    ORDER BY p.id
                    LIMIT $1
                    """,
                    limit,
                )

    # Pre-flight filter: only enqueue ids whose PDFs are inside storage + exist.
    # This preserves the previous response-shape counts (queued/skipped_missing_pdf).
    queued_ids: list[int] = []
    skipped = 0
    for row in rows:
        paper_id = row["id"]
        pdf_path = Path(row["pdf_local_path"])
        if not check_pdf_path_safe(pdf_path, PDF_STORAGE_PATH):
            logger.warning("Skipping paper %d: pdf_local_path outside storage dir", paper_id)
            skipped += 1
            continue
        if not pdf_path.exists():
            skipped += 1
            continue
        queued_ids.append(paper_id)

    job_id: str | None = None
    if queued_ids:
        job_id = str(uuid.uuid4())
        await KIND_TO_TASK["papers.batch_process"].defer_async(
            job_id=job_id,
            user_id=user_id,
            paper_ids=queued_ids,
            force=force,
        )

    return {
        "queued": len(queued_ids),
        "total_unprocessed": len(rows),
        "skipped_missing_pdf": skipped,
        "job_id": job_id,
    }
