"""PDF processing pipeline.

Downloads PDFs via httpx, extracts text with Docling (Markdown + per-page
provenance), generates page snapshots at 150 DPI via pypdfium2, and orchestrates
page-bounded chunking + embedding storage.
"""

import asyncio
import fcntl
import logging
import os
import stat
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
import pypdfium2 as pdfium  # page snapshot generation only; text extraction uses Docling
from jarvis_common.maintenance import ensure_outbound_egress_allowed, maintenance_active
from jarvis_common.paths import secure_path
from jarvis_common.settings import get_core_settings

from paper_ingestion.config import ALLOWED_PDF_DOMAINS, get_paper_ingestion_settings
from paper_ingestion.ingestion.embedder import Embedder, EmbeddingRunContext
from paper_ingestion.ingestion.payload_schema import VectorVisibility
from paper_ingestion.models import ChunkForEmbedding

logger = logging.getLogger(__name__)

__all__ = [
    "PDFProcessor",
    "PDFPublishBlockedError",
    "PDF_STORAGE_PATH",
    "ALLOWED_PDF_DOMAINS",
    "MAX_PDF_PAGES",
    "MAX_PDF_SIZE",
    "MAX_UPLOAD_PDF_SIZE",
    "SNAPSHOT_DPI",
    "SNAPSHOT_STORAGE_PATH",
    "check_pdf_path_safe",
    "pdf_publish_operation",
    "publish_pdf",
    "quote_to_rects",
    "_validate_pdf_url",
]

_cfg = get_paper_ingestion_settings()
PDF_STORAGE_PATH = _cfg.pdf_storage_path
SNAPSHOT_STORAGE_PATH = _cfg.snapshot_storage_path
SNAPSHOT_DPI = 150
MAX_PDF_SIZE = 100 * 1024 * 1024  # 100 MB — disk imports and remote downloads
# Browser uploads additionally pass through the reverse proxy, whose per-location
# `client_max_body_size 50m` (frontend/nginx.conf:357, server default at :103) is
# the real cap on that path. Keeping the app limit in step means the rejection is
# reported by the API instead of as an opaque proxy error.
MAX_UPLOAD_PDF_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_PDF_PAGES = 500  # Reject PDFs with excessive page counts (anti-bomb)

# CGNAT shared address space (RFC 6598) — not reachable from the public internet
# but not flagged by ip.is_private/is_reserved on all Python versions.

# Sentinel: resolve the live module-level PDF_STORAGE_PATH at call time rather
# than freezing it as a default-arg value (keeps monkeypatch.setattr working).
_STORAGE_DEFAULT = object()
_PUBLISH_LOCK_NAME = ".publish.lock"


class PDFPublishBlockedError(RuntimeError):
    """Raised when restore maintenance prevents a PDF from being published."""


def check_pdf_path_safe(pdf_path: Path, storage: Path | str = _STORAGE_DEFAULT) -> bool:  # type: ignore[assignment]
    """True iff pdf_path resolves to a location inside `storage` (path-traversal guard).

    When `storage` is omitted, the live module-level PDF_STORAGE_PATH is used.
    """
    if storage is _STORAGE_DEFAULT:
        storage = PDF_STORAGE_PATH
    try:
        secure_path(storage, str(pdf_path))
    except ValueError:
        return False
    return True


def resolve_safe_pdf_path(
    local_path: str | None,
    storage: Path | str = _STORAGE_DEFAULT,  # type: ignore[assignment]
    *,
    require_exists: bool = True,
) -> Path | None:
    """Resolve and validate a paper's stored PDF path in one call.

    Consolidates the repeated ``Path(local_path)`` + `check_pdf_path_safe`
    + ``.exists()`` triplet duplicated across every job/pipeline call site
    that reads a paper's persisted ``pdf_local_path``.

    Parameters
    ----------
    local_path : str | None
        The paper's stored ``pdf_local_path`` value, or ``None``/empty when unset.
    storage : Path | str, optional
        The storage root the resolved path must not escape. Passed through to
        `check_pdf_path_safe`; when omitted the live module-level
        `PDF_STORAGE_PATH` is used. Callers that hold their own (possibly
        monkeypatched, e.g. in tests) copy of ``PDF_STORAGE_PATH`` should
        pass it explicitly, exactly as they previously did with
        `check_pdf_path_safe`.
    require_exists : bool, default True
        When ``True``, also require the resolved path to exist on disk. Pass
        ``False`` for callers that intentionally defer existence checking to
        a later processing stage.

    Returns
    -------
    Path | None
        The resolved, validated path, or ``None`` when `local_path` is
        unset, escapes `storage`, or (when `require_exists`) is absent
        from disk.
    """
    if not local_path:
        return None
    pdf_path = Path(local_path)
    if not check_pdf_path_safe(pdf_path, storage):
        return None
    if require_exists and not pdf_path.exists():
        return None
    return pdf_path


def _lock_path_matches_fd(directory_fd: int, lock_fd: int) -> bool:
    """Return whether the stable-root lock path still names ``lock_fd``."""
    opened = os.fstat(lock_fd)
    try:
        current = os.stat(_PUBLISH_LOCK_NAME, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
    )


def _validate_pdf_publish_paths(staged_path: Path, final_path: Path) -> None:
    """Require distinct sibling paths and a numeric final PDF name."""
    if staged_path.parent != final_path.parent or staged_path.name == final_path.name:
        raise ValueError("PDF publication requires distinct sibling paths")
    if (
        final_path.suffix != ".pdf"
        or not final_path.stem.isascii()
        or not final_path.stem.isdecimal()
    ):
        raise ValueError("Published PDF filenames must be numeric")


def _validate_publish_files(directory_fd: int, staged_name: str, final_name: str) -> None:
    """Require a regular staged file and a regular target when one exists."""
    staged = os.stat(staged_name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(staged.st_mode):
        raise ValueError("Staged PDF must be a regular file")
    try:
        current = os.stat(final_name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode):
        raise ValueError("Published PDF target must be a regular file")


def _open_pdf_publish_operation(storage_path: Path) -> "PDFPublishOperation":
    """Acquire the storage-root writer lock and reject restore maintenance."""
    directory_flags = os.O_RDONLY | os.O_CLOEXEC
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(storage_path, directory_flags)
    lock_fd = -1
    locked = False
    try:
        lock_flags = os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(_PUBLISH_LOCK_NAME, lock_flags, 0o644, dir_fd=directory_fd)
        if not _lock_path_matches_fd(directory_fd, lock_fd):
            raise RuntimeError("PDF publication lock changed while opening")

        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        locked = True
        if not _lock_path_matches_fd(directory_fd, lock_fd):
            raise RuntimeError("PDF publication lock changed while waiting")
        if maintenance_active():
            raise PDFPublishBlockedError(
                "PDF publication is paused while restore maintenance is active"
            )
        if not _lock_path_matches_fd(directory_fd, lock_fd):
            raise RuntimeError("PDF publication lock changed before use")
        return PDFPublishOperation(storage_path, directory_fd, lock_fd)
    except BaseException:
        if locked:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)
        raise


class PDFPublishOperation:
    """One locked filesystem/DB publication boundary.

    Callers keep this context open until the matching database transaction has
    committed. If that transaction fails, every promoted file is removed (and
    any prior regular target is restored) before the shared restore lock is
    released.
    """

    def __init__(self, storage_path: Path, directory_fd: int, lock_fd: int) -> None:
        self._storage_path = storage_path
        self._directory_fd = directory_fd
        self._lock_fd = lock_fd
        self._promoted: list[tuple[str, tuple[int, int], str | None]] = []
        self._closed = False

    def _require_matching_parent(self, staged_path: Path, final_path: Path) -> None:
        _validate_pdf_publish_paths(staged_path, final_path)
        if staged_path.parent != self._storage_path:
            raise ValueError("PDF publication paths must use the locked storage root")

    def _promote_sync(self, staged_path: Path, final_path: Path) -> None:
        self._require_matching_parent(staged_path, final_path)
        _validate_publish_files(self._directory_fd, staged_path.name, final_path.name)

        previous_name: str | None = None
        try:
            os.stat(final_path.name, dir_fd=self._directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            previous_name = f".{final_path.name}.publish-backup-{uuid.uuid4().hex}"
            os.replace(
                final_path.name,
                previous_name,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )

        try:
            os.replace(
                staged_path.name,
                final_path.name,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            published = os.stat(
                final_path.name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
        except BaseException:
            if previous_name is not None:
                os.replace(
                    previous_name,
                    final_path.name,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                )
            raise
        self._promoted.append(
            (final_path.name, (published.st_dev, published.st_ino), previous_name)
        )

    async def promote(self, staged_path: Path, final_path: Path) -> Path:
        """Promote one staged PDF while retaining rollback ownership."""
        if self._closed:
            raise RuntimeError("PDF publication operation is closed")
        await asyncio.to_thread(self._promote_sync, staged_path, final_path)
        return final_path

    def _rollback_sync(self) -> None:
        for final_name, published_id, previous_name in reversed(self._promoted):
            try:
                current = os.stat(
                    final_name,
                    dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                current = None
            if current is not None and (current.st_dev, current.st_ino) == published_id:
                os.unlink(final_name, dir_fd=self._directory_fd)
            if previous_name is not None:
                os.replace(
                    previous_name,
                    final_name,
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                )
        self._promoted.clear()

    def _commit_sync(self) -> None:
        for _final_name, _published_id, previous_name in self._promoted:
            if previous_name is not None:
                try:
                    os.unlink(previous_name, dir_fd=self._directory_fd)
                except FileNotFoundError:
                    pass
        self._promoted.clear()

    def _close_sync(self) -> None:
        if self._closed:
            return
        self._closed = True
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        os.close(self._directory_fd)


@asynccontextmanager
async def pdf_publish_operation(storage_path: Path) -> AsyncIterator[PDFPublishOperation]:
    """Hold the restore-shared lock through file promotion and DB commit."""
    operation = await asyncio.to_thread(_open_pdf_publish_operation, storage_path)
    try:
        yield operation
    except BaseException:
        try:
            await asyncio.to_thread(operation._rollback_sync)
        finally:
            await asyncio.to_thread(operation._close_sync)
        raise
    else:
        try:
            await asyncio.to_thread(operation._commit_sync)
        finally:
            await asyncio.to_thread(operation._close_sync)


async def publish_pdf(staged_path: Path, final_path: Path) -> None:
    """Atomically publish a numeric PDF unless restore maintenance is active."""
    async with pdf_publish_operation(staged_path.parent) as publication:
        await publication.promote(staged_path, final_path)


# ---------------------------------------------------------------------------
# Docling PDF text extraction (page-bounded provenance; replaces Marker)
# ---------------------------------------------------------------------------

# Separator between per-page markdown segments in the assembled full_text.
_PAGE_SEP = "\n\n"

_converter = None
_converter_lock = threading.Lock()


def _build_converter():
    """Construct the Docling converter (RapidOCR; optional offline model dir)."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        do_ocr=True,
        ocr_options=RapidOcrOptions(),
        artifacts_path=get_paper_ingestion_settings().docling_artifacts_path,
    )
    logger.info("Building Docling converter (first call may download models, ~30s)...")
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def _get_docling_converter():
    """Return the process-wide Docling converter, building it once on first use.

    Double-checked locking: the converter loads layout/OCR models (expensive)
    but is reusable once built; this serializes the one-time build without
    locking steady-state conversions.
    """
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                _converter = _build_converter()
    return _converter


def _extract_text_sync(pdf_path: Path) -> tuple[str, list[tuple[int, int, int]]]:
    """Synchronous Docling extraction (runs in thread pool).

    Assembles ``full_text`` by concatenating each page's Markdown export and
    records an exact char-range anchor per page, so page numbers come straight
    from Docling provenance (the 1-indexed physical page, matching snapshots).

    Null bytes (common in PDF text, and rejected by PostgreSQL) are stripped
    per page *before* measuring, so anchors stay aligned with ``full_text``.

    Returns
    -------
    tuple[str, list[tuple[int, int, int]]]
        ``(full_text, page_anchors)`` where each anchor is
        ``(start_char, end_char, page_no)`` over ``full_text`` in ascending
        ``start_char`` order.
    """
    document = _get_docling_converter().convert(str(pdf_path)).document

    parts: list[str] = []
    page_anchors: list[tuple[int, int, int]] = []
    cursor = 0
    for page_no in sorted(document.pages):
        page_md = document.export_to_markdown(page_no=page_no).replace("\x00", "")
        if not page_md:
            continue
        parts.append(page_md)
        page_anchors.append((cursor, cursor + len(page_md), page_no))
        cursor += len(page_md) + len(_PAGE_SEP)

    return _PAGE_SEP.join(parts), page_anchors


async def extract_text(pdf_path: Path) -> tuple[str, list[tuple[int, int, int]]]:
    """Extract Markdown + per-page anchors from a PDF using Docling.

    Runs the (CPU/GPU-bound) conversion in a thread pool.

    Returns
    -------
    tuple[str, list[tuple[int, int, int]]]
        ``(full_text, page_anchors)`` — see :func:`_extract_text_sync`.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract_text_sync, pdf_path)


async def _validate_pdf_url(url: str) -> None:
    """Validate a PDF URL against SSRF attacks.

    Checks domain allowlist and blocks private/reserved IP ranges.

    Raises
    ------
    ValueError
        If the URL fails validation.
    """
    parsed = urlparse(url)
    dev_mode = get_core_settings().dev_mode

    # Per-domain HTTP allowlist for development environments.
    # In dev mode, http:// is accepted for these local service hostnames
    # and for domains in ALLOWED_PDF_DOMAINS (to support local testing).
    # Production always requires HTTPS.
    DEV_HTTP_ALLOWLIST = {"localhost", "127.0.0.1", "host.docker.internal"}

    hostname = parsed.hostname

    # Scheme check first so non-http/https schemes get the scheme error message
    if (
        parsed.scheme == "http"
        and dev_mode
        and hostname is not None
        and (hostname in DEV_HTTP_ALLOWLIST or hostname in ALLOWED_PDF_DOMAINS)
    ):
        pass  # allowed in dev mode
    elif parsed.scheme != "https":
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}. HTTPS required.")

    if not hostname:
        raise ValueError("URL has no hostname")

    # The dev hostnames widen the domain gate only in dev mode; in production the
    # allowlist below is the single narrow domain set the rebinding note relies on.
    if hostname not in ALLOWED_PDF_DOMAINS and not (dev_mode and hostname in DEV_HTTP_ALLOWLIST):
        raise ValueError(f"Domain '{hostname}' is not allowed for PDF downloads")

    # The shared pinned transport resolves and validates immediately before its
    # TCP connection. Do not resolve here: validating one DNS answer and later
    # connecting through a hostname would recreate the rebinding race.


async def _resolve_validated_pdf_url(
    http_client: httpx.AsyncClient,
    pdf_url: str,
) -> str:
    """Follow the bounded HEAD redirect chain, validating every target.

    Raises
    ------
    OutboundEgressBlockedError
        If outbound quarantine begins before any of the requests is sent.
    """
    current_url = pdf_url
    ensure_outbound_egress_allowed("PDF download")
    response = await http_client.request("HEAD", current_url, timeout=30.0, follow_redirects=False)
    for _ in range(4):
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        location = response.headers.get("Location") or response.headers.get("location")
        if not location:
            break
        current_url = urljoin(current_url, location)
        await _validate_pdf_url(current_url)
        ensure_outbound_egress_allowed("PDF download")
        response = await http_client.request(
            "HEAD", current_url, timeout=30.0, follow_redirects=False
        )
    response.raise_for_status()
    return current_url


class PDFProcessor:
    """Handles PDF download, text extraction, chunking, snapshotting, and embedding.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client for PDF downloads.
    embedder : Embedder
        Embedder instance for chunking and vector storage.
    """

    def __init__(self, http_client: httpx.AsyncClient, embedder: Embedder) -> None:
        self.http_client = http_client
        self.embedder = embedder

    async def stage_pdf_download(self, pdf_url: str, paper_id: int) -> tuple[Path, Path]:
        """Download a PDF to a unique staged sibling of its numeric path.

        Parameters
        ----------
        pdf_url : str
            URL to download the PDF from.
        paper_id : int
            Paper DB ID, used to name the file.

        Returns
        -------
        tuple[Path, Path]
            ``(staged_path, final_path)``. The caller owns the staged file and
            must promote it through :func:`pdf_publish_operation`.

        Raises
        ------
        ValueError
            If the URL fails SSRF validation or exceeds size limit.
        httpx.HTTPStatusError
            If the download fails.
        jarvis_common.maintenance.OutboundEgressBlockedError
            If outbound quarantine begins before any of the requests is sent.
        """
        await _validate_pdf_url(pdf_url)

        pdf_dir = Path(PDF_STORAGE_PATH)
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / f"{paper_id}.pdf"
        tmp_path = pdf_dir / f".{paper_id}.download-{uuid.uuid4().hex}.tmp"

        bytes_written = 0
        # Resolve redirects manually to re-validate each target against SSRF.
        current_url = await _resolve_validated_pdf_url(self.http_client, pdf_url)

        # Stream download directly to disk to avoid memory accumulation
        total_size = 0
        header_bytes = b""

        def _write_pdf_chunk(path: Path, data: bytes) -> None:
            """Append a PDF chunk to the temp file (blocking I/O for async context)."""
            with open(path, "ab") as f:
                f.write(data)

        # Stream to a temp sibling and atomically promote it on success, so a
        # mid-stream failure never leaves a partial that a retry would append to
        # (which would produce a corrupt, concatenated PDF).
        try:
            # Drop any stale temp so the first chunk starts a fresh file at byte 0.
            await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
            ensure_outbound_egress_allowed("PDF download")
            async with self.http_client.stream(
                "GET", current_url, timeout=120.0, follow_redirects=False
            ) as stream_resp:
                stream_resp.raise_for_status()
                async for data in stream_resp.aiter_bytes(chunk_size=65536):
                    total_size += len(data)
                    if total_size > MAX_PDF_SIZE:
                        raise ValueError(
                            f"PDF exceeds maximum size of {MAX_PDF_SIZE // (1024 * 1024)} MB"
                        )
                    if not header_bytes:
                        header_bytes = data[:5]
                        if not header_bytes.startswith(b"%PDF-"):
                            raise ValueError(
                                "Downloaded file is not a valid PDF (missing %PDF header)"
                            )
                    await asyncio.to_thread(_write_pdf_chunk, tmp_path, data)

            bytes_written = total_size

            if bytes_written == 0:
                raise ValueError("PDF download resulted in 0 bytes")
        except BaseException:
            await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
            raise

        logger.info("Staged PDF for paper %d (%d bytes)", paper_id, bytes_written)
        return tmp_path, pdf_path

    async def download_pdf(self, pdf_url: str, paper_id: int) -> Path:
        """Download and publish a PDF without a coupled database write.

        Database-backed callers should use ``stage_pdf_download`` and retain a
        :func:`pdf_publish_operation` until their transaction commits.
        """
        tmp_path, pdf_path = await self.stage_pdf_download(pdf_url, paper_id)
        try:
            await publish_pdf(tmp_path, pdf_path)
        finally:
            # On any failure path, ensure no staged download survives. After a
            # successful publish the path no longer exists, so this is a no-op.
            await asyncio.to_thread(tmp_path.unlink, missing_ok=True)

        logger.info("Downloaded PDF for paper %d to %s", paper_id, pdf_path)
        return pdf_path

    # pypdfium2 used for page snapshot generation only; text extraction uses Docling

    def generate_snapshots(self, pdf_path: Path, paper_id: int) -> list[Path]:
        """Generate PNG snapshots of each page at 150 DPI.

        Parameters
        ----------
        pdf_path : Path
            Path to the PDF file.
        paper_id : int
            Paper DB ID, used to organize snapshot files.

        Returns
        -------
        list[Path]
            Paths to generated PNG files (index 0 = page 1).
        """
        snapshot_dir = Path(SNAPSHOT_STORAGE_PATH) / str(paper_id)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            if len(pdf) > MAX_PDF_PAGES:
                raise ValueError(f"PDF has {len(pdf)} pages, exceeding limit of {MAX_PDF_PAGES}")
            paths: list[Path] = []

            MAX_PIXMAP_DIMENSION = 4096  # Cap oversized pages
            for page_num in range(len(pdf)):
                page = pdf[page_num]
                try:
                    width, height = page.get_size()
                    scale = SNAPSHOT_DPI / 72
                    # Cap scale if page would exceed max dimension
                    if max(width, height) * scale > MAX_PIXMAP_DIMENSION:
                        scale = MAX_PIXMAP_DIMENSION / max(width, height)
                    # render accepts a float scale; the stub's int default narrows it.
                    pil_image = page.render(scale=scale).to_pil()  # type: ignore[arg-type]
                    # Store as 1-indexed page numbers
                    snapshot_path = snapshot_dir / f"page_{page_num + 1}.png"
                    pil_image.save(str(snapshot_path))
                    paths.append(snapshot_path)
                finally:
                    page.close()

            logger.info("Generated %d snapshots for paper %d", len(paths), paper_id)
            self._prune_snapshots_beyond(snapshot_dir, len(paths), paper_id)
            return paths
        finally:
            pdf.close()

    @staticmethod
    def _prune_snapshots_beyond(snapshot_dir: Path, page_count: int, paper_id: int) -> None:
        """Delete page images left over from a longer previous document.

        The snapshot directory is keyed only by paper id, so regenerating writes
        ``page_1``..``page_N`` over whatever was there and leaves every higher
        page of the previous document untouched. Those files stay servable under
        the paper's current identity as soon as its PDF pointer is restored, so
        a replacement source shorter than the one it superseded would otherwise
        keep serving the old document's tail with nothing left to remove it.
        """
        pruned = 0
        for stale in snapshot_dir.glob("page_*.png"):
            try:
                index = int(stale.stem.removeprefix("page_"))
            except ValueError:
                continue
            if index > page_count:
                stale.unlink(missing_ok=True)
                pruned += 1
        if pruned:
            logger.info(
                "Removed %d page image(s) past page %d for paper %d",
                pruned,
                page_count,
                paper_id,
            )

    async def process(  # noqa: PLR0913 - explicit pipeline inputs
        self,
        pdf_path: Path,
        paper_id: int,
        *,
        user_id: int | None = None,
        visibility: VectorVisibility | None = None,
        progress_callback: Callable[
            [Literal["extracted", "chunked", "embedding"], int, int],
            Awaitable[None],
        ]
        | None = None,
        resume_content: dict[int, str] | None = None,
    ) -> tuple[str, list[ChunkForEmbedding], list[str]]:
        """Full PDF processing pipeline: extract, chunk, snapshot, embed.

        Parameters
        ----------
        pdf_path : Path
            Path to the already-downloaded PDF.
        paper_id : int
            Paper DB ID.
        user_id : int | None
            Legacy audit owner retained for payload compatibility. It never
            grants access to the vector.
        visibility : VectorVisibility | None
            Persisted source, scope, and current deployment generation.
            Production calls always provide it; isolated callers without a
            checkpoint receive fail-closed metadata from ``embed_and_store``.
        progress_callback : Callable | None
            Receives real phase completions. Extraction and chunking emit
            ``(phase, 1, 1)`` after completing; embedding emits one event per
            successful or valid resume-skipped batch.
        resume_content : dict[int, str] | None
            chunk_index -> content already embedded by the current model in a
            prior run (see ``run_process_pdf``); threaded through to
            ``embed_and_store`` so unchanged chunks are skipped instead of
            re-embedded.

        Returns
        -------
        tuple[str, list[ChunkForEmbedding], list[str]]
            ``(full_text, chunks, qdrant_point_ids)``
        """
        # 1. Extract Markdown + per-page anchors via Docling (null bytes already
        #    stripped per page inside _extract_text_sync so anchors stay aligned).
        full_text, page_anchors = await extract_text(pdf_path)
        if progress_callback is not None:
            await progress_callback("extracted", 1, 1)

        if not full_text.strip():
            logger.warning("No text extracted from PDF for paper %d", paper_id)
            return full_text, [], []

        # 2. Generate page snapshots (sync I/O — run in thread pool)
        await asyncio.to_thread(self.generate_snapshots, pdf_path, paper_id)

        # 3. Chunk text (page-bounded: each chunk lies on exactly one page)
        chunks = await asyncio.to_thread(self.embedder.chunk_text, full_text, page_anchors)
        if progress_callback is not None:
            await progress_callback("chunked", 1, 1)

        async def _report_embedding_batch(completed: int, total: int) -> None:
            if progress_callback is not None:
                await progress_callback("embedding", completed, total)

        # 4. Embed and store in Qdrant
        point_ids = await self.embedder.embed_and_store(
            paper_id,
            chunks,
            user_id=user_id,
            visibility=visibility,
            run_context=EmbeddingRunContext(
                resume_content=resume_content or {},
                progress_callback=(
                    _report_embedding_batch if progress_callback is not None else None
                ),
            ),
        )

        return full_text, chunks, point_ids


# ---------------------------------------------------------------------------
# Quote → rect bridge (locate a verified quote for the in-PDF reader highlight)
# ---------------------------------------------------------------------------

# A normalized highlight rectangle: every coord in [0, 1], TOP-origin (y grows
# downward, matching canvas / pdf.js), with x0 < x1 and y0 < y1. Named distinctly
# from the validated ``paper_ingestion.models.Rect`` Pydantic model (same concept,
# different layer): this is the bridge's internal dict shape, not a public type.
NormRect = dict[str, float]

# A char's tight bounding box from pypdfium2: (left, bottom, right, top) in PDF
# points, BOTTOM-origin.
_CharBox = tuple[float, float, float, float]

# Two char boxes belong to the same text line when their vertical bands overlap
# by at least this fraction of the shorter box's height — robust to descenders
# and baseline jitter while still separating distinct lines.
_LINE_OVERLAP_FRACTION = 0.5
# A char whose left edge jumps back more than this (PDF points) past the
# previous char marks a carriage return → a new line sharing the same band.
_CARRIAGE_RETURN_TOL = 1.0


def _group_into_lines(boxes: list[_CharBox]) -> list[list[_CharBox]]:
    """Split a document-order run of char boxes into per-line groups."""
    lines: list[list[_CharBox]] = []
    current: list[_CharBox] = []
    band_bottom = band_top = prev_left = 0.0
    for box in boxes:
        left, bottom, top = box[0], box[1], box[3]
        if current:
            overlap = min(band_top, top) - max(band_bottom, bottom)
            shorter = min(band_top - band_bottom, top - bottom)
            same_band = shorter > 0 and overlap >= _LINE_OVERLAP_FRACTION * shorter
            carriage_return = left < prev_left - _CARRIAGE_RETURN_TOL
            if not same_band or carriage_return:
                lines.append(current)
                current = []
        if not current:
            band_bottom, band_top = bottom, top
        else:
            band_bottom, band_top = min(band_bottom, bottom), max(band_top, top)
        current.append(box)
        prev_left = left
    if current:
        lines.append(current)
    return lines


def _union_to_rect(line: list[_CharBox], width: float, height: float) -> NormRect:
    """Union one line's char boxes into a single normalized, top-origin Rect."""
    left = min(box[0] for box in line)
    bottom = min(box[1] for box in line)
    right = max(box[2] for box in line)
    top = max(box[3] for box in line)
    # pypdfium2 is bottom-origin; canvas / pdf.js is top-origin → flip via (H - y)/H.
    return {
        "x0": left / width,
        "y0": (height - top) / height,
        "x1": right / width,
        "y1": (height - bottom) / height,
    }


def _quote_to_rects_sync(pdf_path: str | Path, page: int, quote: str) -> list[NormRect]:
    """Synchronous, CPU-bound core of :func:`quote_to_rects`."""
    if not quote.strip():
        return []

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page_index = page - 1  # contract: `page` is 1-indexed; pypdfium2 is 0-indexed
        if page_index < 0 or page_index >= len(pdf):
            return []
        pdf_page = pdf[page_index]
        try:
            width, height = pdf_page.get_size()
            textpage = pdf_page.get_textpage()
            try:
                searcher = textpage.search(quote, match_case=False)
                try:
                    # First occurrence only — one highlight per call is sufficient.
                    match = searcher.get_next()
                finally:
                    searcher.close()
                if match is None:
                    return []
                start, count = match
                # Tight char boxes for the match; drop zero-area boxes (control
                # chars / collapsed line breaks contribute no visible glyph).
                boxes: list[_CharBox] = []
                for i in range(start, start + count):
                    left, bottom, right, top = textpage.get_charbox(i)
                    if right - left > 0 and top - bottom > 0:
                        boxes.append((left, bottom, right, top))
                if not boxes:
                    return []
                return [_union_to_rect(line, width, height) for line in _group_into_lines(boxes)]
            finally:
                textpage.close()
        finally:
            pdf_page.close()
    finally:
        pdf.close()


async def quote_to_rects(pdf_path: str | Path, page: int, quote: str) -> list[NormRect]:
    """Locate a verbatim ``quote`` on a 1-indexed ``page`` and return its
    on-screen highlight rectangles.

    Returns one :class:`Rect` per text line of the *first* match, each
    normalized to ``[0, 1]`` with the PDF→canvas y-flip applied. An absent
    quote — or one pypdfium2's literal substring search cannot match exactly —
    yields ``[]``; no fuzzy repair is attempted, so a highlight is only ever
    drawn at a real location. Runs the sync pypdfium2 work off the event loop.
    """
    return await asyncio.to_thread(_quote_to_rects_sync, pdf_path, page, quote)
