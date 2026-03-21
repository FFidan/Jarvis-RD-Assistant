"""PDF processing pipeline.

Downloads PDFs via httpx, extracts text with PyMuPDF, generates page snapshots
at 150 DPI, and orchestrates chunking + embedding storage.
"""

import asyncio
import ipaddress
import logging
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import fitz  # PyMuPDF
import httpx

from app.embedder import Embedder
from app.models import ChunkForEmbedding

logger = logging.getLogger(__name__)

PDF_STORAGE_PATH = os.environ.get("PDF_STORAGE_PATH", "/data/pdfs")
SNAPSHOT_STORAGE_PATH = os.environ.get("SNAPSHOT_STORAGE_PATH", "/data/snapshots")
SNAPSHOT_DPI = 150
MAX_PDF_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_PDF_PAGES = 500  # Reject PDFs with excessive page counts (anti-bomb)

ALLOWED_PDF_DOMAINS: frozenset[str] = frozenset({
    "arxiv.org",
    "export.arxiv.org",
    "www.arxiv.org",
    "pdfs.semanticscholar.org",
    "www.semanticscholar.org",
})


async def _validate_pdf_url(url: str) -> None:
    """Validate a PDF URL against SSRF attacks.

    Checks domain allowlist and blocks private/reserved IP ranges.

    Raises
    ------
    ValueError
        If the URL fails validation.
    """
    parsed = urlparse(url)
    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    allowed_schemes = ("https", "http") if dev_mode else ("https",)
    if parsed.scheme not in allowed_schemes:
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}. HTTPS required.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    if hostname not in ALLOWED_PDF_DOMAINS:
        raise ValueError(
            f"Domain '{hostname}' is not allowed for PDF downloads"
        )

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
            redirect_url = head_resp.headers.get("location", "")
            await _validate_pdf_url(redirect_url)
            current_url = redirect_url
            head_resp = await self.http_client.request(
                "HEAD", current_url, timeout=30.0, follow_redirects=False
            )
        head_resp.raise_for_status()

        # Stream download directly to disk to avoid memory accumulation
        total_size = 0
        header_bytes = b""
        async with self.http_client.stream("GET", current_url, timeout=120.0, follow_redirects=False) as stream_resp:
            stream_resp.raise_for_status()
            with open(pdf_path, "wb") as f:
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
                    f.write(data)

        bytes_written = total_size

        logger.info("Downloaded PDF for paper %d (%d bytes) to %s", paper_id, bytes_written, pdf_path)
        return pdf_path

    def extract_text(self, pdf_path: Path) -> tuple[str, list[tuple[int, int]]]:
        """Extract full text from a PDF, tracking page boundaries.

        Parameters
        ----------
        pdf_path : Path
            Path to the PDF file.

        Returns
        -------
        tuple[str, list[tuple[int, int]]]
            ``(full_text, page_boundaries)`` where page_boundaries is a list
            of ``(start_char, end_char)`` tuples.  Index 0 = page 1
            (PyMuPDF is 0-indexed internally; we store 1-indexed per AGENTS.md).
        """
        doc = fitz.open(str(pdf_path))
        try:
            if len(doc) > MAX_PDF_PAGES:
                raise ValueError(
                    f"PDF has {len(doc)} pages, exceeding limit of {MAX_PDF_PAGES}"
                )
            full_text = ""
            page_boundaries: list[tuple[int, int]] = []

            for page_num in range(len(doc)):  # 0-indexed in PyMuPDF
                page = doc[page_num]
                page_text = page.get_text("text")
                start = len(full_text)
                full_text += page_text
                end = len(full_text)
                page_boundaries.append((start, end))

            return full_text, page_boundaries
        finally:
            doc.close()

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
                raise ValueError(
                    f"PDF has {len(doc)} pages, exceeding limit of {MAX_PDF_PAGES}"
                )
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
    ) -> tuple[str, list[ChunkForEmbedding], list[str]]:
        """Full PDF processing pipeline: extract, chunk, snapshot, embed.

        Parameters
        ----------
        pdf_path : Path
            Path to the already-downloaded PDF.
        paper_id : int
            Paper DB ID.

        Returns
        -------
        tuple[str, list[ChunkForEmbedding], list[str]]
            ``(full_text, chunks, qdrant_point_ids)``
        """
        # 1. Extract text and page boundaries (sync I/O — run in thread pool)
        full_text, page_boundaries = await asyncio.to_thread(self.extract_text, pdf_path)

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
        point_ids = await self.embedder.embed_and_store(paper_id, chunks)

        return full_text, chunks, point_ids
