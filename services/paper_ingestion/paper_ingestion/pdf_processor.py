"""PDF processing pipeline.

Downloads PDFs via httpx, extracts text with Marker (Markdown + LaTeX math),
generates page snapshots at 150 DPI via PyMuPDF, and orchestrates chunking +
embedding storage.
"""

import asyncio
import functools
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urljoin, urlparse

import fitz  # fitz (PyMuPDF) retained for page snapshot generation only; text extraction uses Marker
import httpx
from jarvis_common.settings import get_core_settings
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict

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
    "extract_text",
    "_extract_text_sync",
    "_get_marker_models",
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
# Marker PDF text extraction (replaces fitz-based text extraction)
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _get_marker_models():
    """Lazy-load Marker models (expensive, cached after first call).

    ``@lru_cache(maxsize=1)`` ensures the expensive ``create_model_dict()``
    call runs at most once per process. Python's GIL plus lru_cache's internal
    lock makes this safe under concurrent calls without a separate threading.Lock.
    """
    logger.info("Loading Marker PDF models (first call, may take ~30s)...")
    models = create_model_dict()
    logger.info("Marker models loaded.")
    return models


def _extract_text_sync(pdf_path: Path) -> tuple[str, list[tuple[int, int]]]:
    """Synchronous Marker extraction (runs in thread pool).

    Returns
    -------
    tuple[str, list[tuple[int, int]]]
        ``(full_text, page_boundaries)`` where full_text is Markdown with
        LaTeX math and page_boundaries is a list of ``(start_char, end_char)``
        tuples (index 0 = page 1).
    """
    models = _get_marker_models()
    converter = PdfConverter(artifact_dict=models)
    rendered = converter(str(pdf_path))
    full_text = rendered.markdown

    # Build page boundaries from metadata
    page_boundaries: list[tuple[int, int]] = []
    if hasattr(rendered, "metadata") and rendered.metadata:
        page_stats = rendered.metadata.get("page_stats", [])
        if not page_stats:
            page_boundaries = [(0, len(full_text))]
        else:
            # Approximate: divide text evenly across pages
            total_pages = len(page_stats)
            chars_per_page = len(full_text) // max(total_pages, 1)
            for i in range(total_pages):
                start = i * chars_per_page
                end = len(full_text) if i == total_pages - 1 else (i + 1) * chars_per_page
                page_boundaries.append((start, end))
    else:
        page_boundaries = [(0, len(full_text))]

    return full_text, page_boundaries


async def extract_text(pdf_path: Path) -> tuple[str, list[tuple[int, int]]]:
    """Extract text from PDF using Marker, returning Markdown with LaTeX math.

    Runs Marker in a thread pool since it is CPU-bound.

    Returns
    -------
    tuple[str, list[tuple[int, int]]]
        ``(full_text, page_boundaries)`` where page_boundaries is a list of
        ``(start_char, end_char)`` tuples for each page (1-indexed).
    """
    loop = asyncio.get_running_loop()
    full_text, page_boundaries = await loop.run_in_executor(None, _extract_text_sync, pdf_path)
    return full_text, page_boundaries


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

    # fitz (PyMuPDF) retained for page snapshot generation only; text extraction uses Marker

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
        # 1. Extract text and page boundaries via Marker (CPU-bound, runs in thread pool)
        full_text, page_boundaries = await extract_text(pdf_path)

        # Strip null bytes — common in PDF text, causes PostgreSQL UTF-8 errors
        full_text = full_text.replace("\x00", "")

        if not full_text.strip():
            logger.warning("No text extracted from PDF for paper %d", paper_id)
            return full_text, [], []

        # 2. Generate page snapshots (sync I/O — run in thread pool)
        await asyncio.to_thread(self.generate_snapshots, pdf_path, paper_id)

        # 3. Chunk text
        chunks = await asyncio.to_thread(self.embedder.chunk_text, full_text, page_boundaries)

        # 4. Embed and store in Qdrant
        point_ids = await self.embedder.embed_and_store(paper_id, chunks, user_id=user_id)

        return full_text, chunks, point_ids
