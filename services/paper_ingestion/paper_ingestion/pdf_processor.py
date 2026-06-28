"""PDF processing pipeline.

Downloads PDFs via httpx, extracts text with Docling (Markdown + per-page
provenance), generates page snapshots at 150 DPI via pypdfium2, and orchestrates
page-bounded chunking + embedding storage.
"""

import asyncio
import ipaddress
import logging
import os
import socket
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import pypdfium2 as pdfium  # page snapshot generation only; text extraction uses Docling
from jarvis_common.settings import get_core_settings

from paper_ingestion.config import ALLOWED_PDF_DOMAINS, get_paper_ingestion_settings
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
    "check_pdf_path_safe",
    "quote_to_rects",
    "_validate_pdf_url",
]

_cfg = get_paper_ingestion_settings()
PDF_STORAGE_PATH = _cfg.pdf_storage_path
SNAPSHOT_STORAGE_PATH = _cfg.snapshot_storage_path
SNAPSHOT_DPI = 150
MAX_PDF_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_PDF_PAGES = 500  # Reject PDFs with excessive page counts (anti-bomb)

# CGNAT shared address space (RFC 6598) — not reachable from the public internet
# but not flagged by ip.is_private/is_reserved on all Python versions.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Sentinel: resolve the live module-level PDF_STORAGE_PATH at call time rather
# than freezing it as a default-arg value (keeps monkeypatch.setattr working).
_STORAGE_DEFAULT = object()


def check_pdf_path_safe(pdf_path: Path, storage: Path | str = _STORAGE_DEFAULT) -> bool:  # type: ignore[assignment]
    """True iff pdf_path resolves to a location inside `storage` (path-traversal guard).

    When `storage` is omitted, the live module-level PDF_STORAGE_PATH is used.
    """
    if storage is _STORAGE_DEFAULT:
        storage = PDF_STORAGE_PATH
    return pdf_path.resolve().is_relative_to(Path(storage).resolve())


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
        if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip in _CGNAT:
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
        tmp_path = pdf_path.with_suffix(".tmp")

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
            """Append a PDF chunk to the temp file (blocking I/O for async context)."""
            with open(path, "ab") as f:
                f.write(data)

        # Stream to a temp sibling and atomically promote it on success, so a
        # mid-stream failure never leaves a partial that a retry would append to
        # (which would produce a corrupt, concatenated PDF).
        try:
            # Drop any stale temp so the first chunk starts a fresh file at byte 0.
            await asyncio.to_thread(tmp_path.unlink, missing_ok=True)
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

            # Atomically promote the validated temp file to its final name.
            await asyncio.to_thread(os.replace, tmp_path, pdf_path)
        finally:
            # On any failure path, ensure no stale temp survives. After a
            # successful os.replace the temp no longer exists, so this is a no-op.
            await asyncio.to_thread(tmp_path.unlink, missing_ok=True)

        logger.info(
            "Downloaded PDF for paper %d (%d bytes) to %s", paper_id, bytes_written, pdf_path
        )
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
            return paths
        finally:
            pdf.close()

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
