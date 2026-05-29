"""PDF processing pipeline.

Downloads PDFs via httpx, extracts text with Docling (Markdown + per-page
provenance), generates page snapshots at 150 DPI via PyMuPDF, and orchestrates
page-bounded chunking + embedding storage.
"""

import asyncio
import ipaddress
import logging
import socket
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urljoin, urlparse

import fitz  # fitz (PyMuPDF) retained for page snapshot generation only; text extraction uses Docling
import httpx
from jarvis_common.settings import get_core_settings

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.models import ChunkForEmbedding

logger = logging.getLogger(__name__)

__all__ = [
    "PDFProcessor",
    "PDF_STORAGE_PATH",
    "ALLOWED_PDF_DOMAINS",
    "MAX_PDF_PAGES",
    "MAX_PDF_SIZE",
    "SNAPSHOT_DPI",
    "SNAPSHOT_STORAGE_PATH",
    "_validate_pdf_url",
]

_cfg = get_paper_ingestion_settings()
PDF_STORAGE_PATH = _cfg.pdf_storage_path
SNAPSHOT_STORAGE_PATH = _cfg.snapshot_storage_path
SNAPSHOT_DPI = 150
MAX_PDF_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_PDF_PAGES = 500  # Reject PDFs with excessive page counts (anti-bomb)

ALLOWED_PDF_DOMAINS: frozenset[str] = frozenset(
    {
        "arxiv.org",
        "export.arxiv.org",
        "www.arxiv.org",
        "pdfs.semanticscholar.org",
        "www.semanticscholar.org",
    }
)

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

    if hostname not in ALLOWED_PDF_DOMAINS and hostname not in DEV_HTTP_ALLOWLIST:
        raise ValueError(f"Domain '{hostname}' is not allowed for PDF downloads")

    # Resolve hostname and block private IPs
    # Run DNS resolution in thread pool to avoid blocking the event loop
    try:
        loop = asyncio.get_running_loop()
        addr_info = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {hostname}") from None

    for family, _type, _proto, _canonname, sockaddr in addr_info:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"URL resolves to private/reserved IP: {ip}")

    # NOTE: DNS rebinding is mitigated by the narrow ALLOWED_PDF_DOMAINS allowlist
    # (arxiv.org only). Pinning resolved IPs would require custom httpx transport
    # and complex HTTPS/SNI handling — not warranted for this threat model.


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

    async def download_pdf(self, pdf_url: str, paper_id: int) -> Path:
        """Download a PDF to local storage.

        Parameters
        ----------
        pdf_url : str
            URL to download the PDF from.
        paper_id : int
            Paper DB ID, used to name the file.

        Returns
        -------
        Path
            Absolute path to the downloaded PDF.

        Raises
        ------
        ValueError
            If the URL fails SSRF validation or exceeds size limit.
        httpx.HTTPStatusError
            If the download fails.
        """
        await _validate_pdf_url(pdf_url)

        pdf_dir = Path(PDF_STORAGE_PATH)
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = pdf_dir / f"{paper_id}.pdf"

        bytes_written = 0
        # Resolve redirects manually to re-validate each target against SSRF
        current_url = pdf_url
        head_resp = await self.http_client.request(
            "HEAD", current_url, timeout=30.0, follow_redirects=False
        )
        for _ in range(4):  # Up to 4 additional redirects
            if head_resp.status_code not in (301, 302, 303, 307, 308):
                break
            location = head_resp.headers.get("Location") or head_resp.headers.get("location")
            if not location:
                break
            redirect_url = urljoin(current_url, location)
            await _validate_pdf_url(redirect_url)
            current_url = redirect_url
            head_resp = await self.http_client.request(
                "HEAD", current_url, timeout=30.0, follow_redirects=False
            )
        head_resp.raise_for_status()

        # Stream download directly to disk to avoid memory accumulation
        total_size = 0
        header_bytes = b""

        def _write_pdf_chunk(path: Path, data: bytes) -> None:
            """Write PDF chunk to disk (blocking I/O wrapped for async context)."""
            with open(path, "ab") as f:
                f.write(data)

        async with self.http_client.stream(
            "GET", current_url, timeout=120.0, follow_redirects=False
        ) as stream_resp:
            stream_resp.raise_for_status()
            # Pre-create empty file
            await asyncio.to_thread(pdf_path.touch)
            async for data in stream_resp.aiter_bytes(chunk_size=65536):
                total_size += len(data)
                if total_size > MAX_PDF_SIZE:
                    pdf_path.unlink(missing_ok=True)
                    raise ValueError(
                        f"PDF exceeds maximum size of {MAX_PDF_SIZE // (1024 * 1024)} MB"
                    )
                if not header_bytes:
                    header_bytes = data[:5]
                    if not header_bytes.startswith(b"%PDF-"):
                        pdf_path.unlink(missing_ok=True)
                        raise ValueError("Downloaded file is not a valid PDF (missing %PDF header)")
                await asyncio.to_thread(_write_pdf_chunk, pdf_path, data)

        bytes_written = total_size

        logger.info(
            "Downloaded PDF for paper %d (%d bytes) to %s", paper_id, bytes_written, pdf_path
        )
        return pdf_path

    # fitz (PyMuPDF) retained for page snapshot generation only; text extraction uses Docling

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

        doc = fitz.open(str(pdf_path))
        try:
            if len(doc) > MAX_PDF_PAGES:
                raise ValueError(f"PDF has {len(doc)} pages, exceeding limit of {MAX_PDF_PAGES}")
            paths: list[Path] = []

            MAX_PIXMAP_DIMENSION = 4096  # Cap oversized pages
            for page_num in range(len(doc)):
                page = doc[page_num]
                scale = SNAPSHOT_DPI / 72
                # Cap scale if page would exceed max dimension
                page_rect = page.rect
                max_side = max(page_rect.width, page_rect.height) * scale
                if max_side > MAX_PIXMAP_DIMENSION:
                    scale = MAX_PIXMAP_DIMENSION / max(page_rect.width, page_rect.height)
                mat = fitz.Matrix(scale, scale)
                pix = page.get_pixmap(matrix=mat)
                # Store as 1-indexed page numbers
                snapshot_path = snapshot_dir / f"page_{page_num + 1}.png"
                pix.save(str(snapshot_path))
                pix = None
                paths.append(snapshot_path)

            logger.info("Generated %d snapshots for paper %d", len(paths), paper_id)
            return paths
        finally:
            doc.close()

    async def process(
        self,
        pdf_path: Path,
        paper_id: int,
        *,
        user_id: int | None = None,
        progress_callback: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str, list[ChunkForEmbedding], list[str]]:
        """Full PDF processing pipeline: extract, chunk, snapshot, embed.

        Parameters
        ----------
        pdf_path : Path
            Path to the already-downloaded PDF.
        paper_id : int
            Paper DB ID.
        user_id : int | None
            Owner of the source paper (resolved by the caller from
            ``papers.discovered_by``). NULL = canonical/shared chunk.

        Returns
        -------
        tuple[str, list[ChunkForEmbedding], list[str]]
            ``(full_text, chunks, qdrant_point_ids)``
        """
        # 1. Extract Markdown + per-page anchors via Docling (null bytes already
        #    stripped per page inside _extract_text_sync so anchors stay aligned).
        full_text, page_anchors = await extract_text(pdf_path)

        if not full_text.strip():
            logger.warning("No text extracted from PDF for paper %d", paper_id)
            return full_text, [], []

        # 2. Generate page snapshots (sync I/O — run in thread pool)
        await asyncio.to_thread(self.generate_snapshots, pdf_path, paper_id)

        # 3. Chunk text (page-bounded: each chunk lies on exactly one page)
        chunks = await asyncio.to_thread(self.embedder.chunk_text, full_text, page_anchors)

        # 4. Embed and store in Qdrant
        point_ids = await self.embedder.embed_and_store(paper_id, chunks, user_id=user_id)

        return full_text, chunks, point_ids
