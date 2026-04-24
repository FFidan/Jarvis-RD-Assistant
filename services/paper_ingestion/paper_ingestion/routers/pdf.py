"""PDF download, processing, upload, and scan endpoints."""

import asyncio
import hashlib
import logging
import os
import shutil
from pathlib import Path

import asyncpg
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)

from paper_ingestion.converters import row_to_paper_response
from paper_ingestion.deps import get_db_pool, get_embedder, get_pdf_processor, limiter
from paper_ingestion.models import (
    BatchProcessResponse,
    PaperResponse,
    ProcessPdfResponse,
    ScanLocalPdfsResponse,
)
from paper_ingestion.pdf_processor import MAX_PDF_SIZE, PDF_STORAGE_PATH, PDFProcessor
from paper_ingestion.services.pdf_workflow import run_process_pdf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["pdf"])

LOCAL_PDF_SCAN_DIR = os.environ.get("LOCAL_PDF_SCAN_DIR", "/data/local_pdfs")


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

    # Phase 1: load and validate (short transaction — no lock held during I/O)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="Paper not found")
    if not row["pdf_url"]:
        raise HTTPException(status_code=400, detail="Paper has no PDF URL")
    if row["pdf_downloaded"]:
        return row_to_paper_response(row)

    # Phase 2: download (no DB connection held across slow HTTP)
    try:
        pdf_path = await pdf_processor.download_pdf(row["pdf_url"], paper_id)
    except ValueError as e:
        request_id = getattr(request.state, "request_id", None) or str(paper_id)
        logger.exception("PDF download validation failed", extra={"request_id": request_id})
        raise HTTPException(
            status_code=400,
            detail=f"PDF download failed (request_id={request_id}). Please check the paper URL.",
        ) from e
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="PDF download failed")

    # Phase 3: write back (new short transaction — pdf_downloaded=TRUE is idempotent guard)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchrow(
                """
                UPDATE papers SET pdf_local_path = $1, pdf_downloaded = TRUE
                WHERE id = $2 RETURNING *
                """,
                str(pdf_path),
                paper_id,
            )
    return row_to_paper_response(updated)


# ---------------------------------------------------------------------------
# POST /api/process-pdf/{paper_id}
# ---------------------------------------------------------------------------


@router.post("/process-pdf/{paper_id}", response_model=ProcessPdfResponse)
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
):
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
    """
    if not sync:
        from jarvis_common.jobs import enqueue

        job_id = await enqueue(db_pool, "paper.process", {"paper_id": paper_id, "force": force})
        return {"job_id": job_id, "status": "queued"}

    # Synchronous path (sync=True) — original blocking behaviour
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="Paper not found")
    if not row["pdf_downloaded"] or not row["pdf_local_path"]:
        raise HTTPException(status_code=400, detail="PDF not yet downloaded")

    pdf_path = Path(row["pdf_local_path"])

    # Path traversal protection (S-12)
    if not pdf_path.resolve().is_relative_to(Path(PDF_STORAGE_PATH).resolve()):
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
        )
    except RuntimeError as exc:
        request_id = getattr(request.state, "request_id", None) or str(paper_id)
        logger.exception("PDF processing failed", extra={"request_id": request_id})
        raise HTTPException(
            status_code=502,
            detail=(
                f"PDF processing failed (request_id={request_id}). Please retry or contact support."
            ),
        ) from exc


# ---------------------------------------------------------------------------
# POST /api/upload-pdf
# ---------------------------------------------------------------------------


@router.post("/upload-pdf", response_model=PaperResponse)
@limiter.limit("10/minute")
async def upload_pdf(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    authors: str = Form(default=""),
    abstract: str = Form(default=""),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
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
                if total_size > MAX_PDF_SIZE:
                    raise HTTPException(
                        status_code=400,
                        detail="File exceeds 100 MB size limit",
                    )
                hasher.update(chunk)
                _f.write(chunk)

        file_hash = hasher.hexdigest()
        external_id = f"local:{file_hash[:16]}"

        # Check for duplicate
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM papers WHERE external_id = $1", external_id
            )
            if existing:
                raise HTTPException(status_code=409, detail="This PDF has already been uploaded")

            # Parse authors
            author_list = [a.strip() for a in authors.split(",") if a.strip()] if authors else []

            # Insert paper + rename atomically inside a transaction
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO papers (external_id, source_type, title, authors, abstract,
                                        url, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING *
                    """,
                    external_id,
                    "local",
                    title,
                    author_list,
                    abstract or None,
                    f"local://{file_hash}",
                    {},
                )
                paper_id = row["id"]

                # Rename to final path using paper_id (atomic on Linux same filesystem)
                pdf_path = storage_path / f"{paper_id}.pdf"
                temp_path.rename(pdf_path)

                try:
                    updated = await conn.fetchrow(
                        """
                        UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $1
                        WHERE id = $2 RETURNING *
                        """,
                        str(pdf_path),
                        paper_id,
                    )
                except Exception:
                    # DB update failed — remove the renamed file to avoid dangling artifact
                    try:
                        pdf_path.unlink()
                    except OSError:
                        pass
                    raise
    finally:
        # No-op after successful rename (file no longer at temp_path); cleans up on any exception
        temp_path.unlink(missing_ok=True)

    return row_to_paper_response(updated)


# ---------------------------------------------------------------------------
# POST /api/scan-local-pdfs
# ---------------------------------------------------------------------------


@router.post("/scan-local-pdfs", response_model=ScanLocalPdfsResponse)
@limiter.limit("2/minute")
async def scan_local_pdfs(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Scan a local directory for PDF files and import new ones.

    Scans ``/data/local_pdfs`` for ``*.pdf`` files, computes a SHA-256 hash
    for each, and imports any that have not already been registered.

    Returns
    -------
    dict
        Summary with ``scanned``, ``imported``, and ``skipped`` counts.
    """
    scan_dir = Path(LOCAL_PDF_SCAN_DIR)
    if not scan_dir.is_dir():
        raise HTTPException(
            status_code=400, detail=f"Scan directory does not exist: {LOCAL_PDF_SCAN_DIR}"
        )

    pdf_files = list(scan_dir.glob("*.pdf"))
    scanned = len(pdf_files)
    imported = 0
    skipped = 0

    storage_path = Path(PDF_STORAGE_PATH)
    storage_path.mkdir(parents=True, exist_ok=True)

    for pdf_file in pdf_files:
        # H-4: reject symlinks to prevent directory traversal via symlink
        if pdf_file.is_symlink():
            skipped += 1
            continue

        # C-7: stat before read to avoid loading large files into RAM.
        # Synchronous stat() is acceptable here -- local filesystem syscall, no I/O wait.
        # asyncio.to_thread(lambda: ...) is NOT used to avoid lambda closure bugs in loops.
        try:
            file_size = pdf_file.stat().st_size
        except OSError:
            skipped += 1
            continue
        if file_size > MAX_PDF_SIZE:
            skipped += 1
            continue

        # Read file for magic bytes and hashing
        try:
            content = await asyncio.to_thread(pdf_file.read_bytes)
        except OSError:
            skipped += 1
            continue

        # Check magic bytes
        if not content.startswith(b"%PDF-"):
            skipped += 1
            continue

        # Compute hash
        file_hash = hashlib.sha256(content).hexdigest()
        external_id = f"local:{file_hash[:16]}"

        # Each file gets its own short-lived connection — avoids holding a pool connection
        # across file I/O and copy operations.
        async with db_pool.acquire() as file_conn:
            # Check if already imported
            existing = await file_conn.fetchrow(
                "SELECT id FROM papers WHERE external_id = $1", external_id
            )
            if existing:
                skipped += 1
                continue

            # Derive title from filename
            title = pdf_file.stem.replace("-", " ").replace("_", " ").title()

            # Copy PDF to storage first (filesystem before DB to avoid orphaned rows)
            temp_name = f"_importing_{file_hash[:16]}.pdf"
            dest_path = storage_path / temp_name
            try:
                await asyncio.to_thread(shutil.copy2, str(pdf_file), str(dest_path))
            except OSError:
                skipped += 1
                continue

            try:
                async with file_conn.transaction():
                    # Insert paper
                    row = await file_conn.fetchrow(
                        """
                        INSERT INTO papers (external_id, source_type, title, authors, abstract,
                                            url, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        RETURNING *
                        """,
                        external_id,
                        "local",
                        title,
                        [],
                        None,
                        f"local://{file_hash}",
                        {},
                    )
                    paper_id = row["id"]

                    # Rename to final path using paper_id
                    final_path = storage_path / f"{paper_id}.pdf"
                    dest_path.rename(final_path)
                    dest_path = final_path

                    # Mark as downloaded -- inside the same transaction so INSERT+UPDATE are atomic
                    await file_conn.execute(
                        """
                        UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $1
                        WHERE id = $2
                        """,
                        str(dest_path),
                        paper_id,
                    )
            except Exception:
                dest_path.unlink(missing_ok=True)
                skipped += 1
                continue

        imported += 1

    return {"scanned": scanned, "imported": imported, "skipped": skipped}


# ---------------------------------------------------------------------------
# POST /api/papers/batch-process
# ---------------------------------------------------------------------------


@router.post("/papers/batch-process", response_model=BatchProcessResponse)
@limiter.limit("2/minute")
async def batch_process_papers(
    request: Request,
    background_tasks: BackgroundTasks,
    limit: int = Query(default=10, ge=1, le=50),
    force: bool = Query(default=False),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Queue unprocessed papers for chunk extraction + embedding.

    Finds papers with ``pdf_downloaded=True`` that have no ``paper_chunks`` rows,
    up to ``limit`` papers.  Enqueues a single ``papers.batch_process`` job
    that processes all selected papers, returning a ``job_id`` the caller
    can poll via ``GET /api/jobs/{job_id}``.

    When ``force=True``, includes ALL papers with downloaded PDFs (even those
    already processed), allowing re-extraction with updated models.

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
    from jarvis_common import jobs as jobs_lib  # noqa: PLC0415

    async with db_pool.acquire() as conn:
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
        if not pdf_path.resolve().is_relative_to(Path(PDF_STORAGE_PATH).resolve()):
            logger.warning("Skipping paper %d: pdf_local_path outside storage dir", paper_id)
            skipped += 1
            continue
        if not pdf_path.exists():
            skipped += 1
            continue
        queued_ids.append(paper_id)

    job_id: str | None = None
    if queued_ids:
        job_id = await jobs_lib.enqueue(
            db_pool,
            "papers.batch_process",
            payload={"paper_ids": queued_ids},
        )

    return {
        "queued": len(queued_ids),
        "total_unprocessed": len(rows),
        "skipped_missing_pdf": skipped,
        "job_id": job_id,
    }
