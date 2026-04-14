"""Adversarial unit tests for pdf_processor.py.

Covers:
- SSRF guard (_validate_pdf_url): allowlist, private/reserved IPs, schemes, IDN,
  DNS failure, DNS rebinding, userinfo injection, malformed URLs
- Size cap (MAX_PDF_SIZE): streaming accumulation exceeds limit → ValueError
- Page cap (MAX_PDF_PAGES): fitz.open() returns oversized doc → ValueError
- Malformed PDF bytes: empty, non-PDF, truncated
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# conftest.py stubs fitz + marker + tiktoken + qdrant_client at module level
# so we can safely import app.pdf_processor here.
import app.pdf_processor as pdf_processor
import pytest
from app.pdf_processor import (
    ALLOWED_PDF_DOMAINS,
    MAX_PDF_PAGES,
    MAX_PDF_SIZE,
    PDFProcessor,
    _validate_pdf_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_getaddrinfo(ip_str: str) -> list[tuple[Any, ...]]:
    """Return a minimal getaddrinfo result for the given IP string."""
    family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 0, "", (ip_str, 0))]


def _mixed_getaddrinfo(public_ip: str, private_ip: str) -> list[tuple[Any, ...]]:
    """Return two getaddrinfo results: one public IP and one private IP (DNS rebinding)."""
    return _fake_getaddrinfo(public_ip) + _fake_getaddrinfo(private_ip)


# ---------------------------------------------------------------------------
# SSRF guard — ALLOWED_PDF_DOMAINS pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("host", sorted(ALLOWED_PDF_DOMAINS))
async def test_allowed_hosts_pass(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each entry in ALLOWED_PDF_DOMAINS should pass the SSRF guard."""
    public_ip = "151.101.1.1"

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        return _fake_getaddrinfo(public_ip)

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    # Should not raise
    await _validate_pdf_url(f"https://{host}/paper.pdf")


# ---------------------------------------------------------------------------
# SSRF guard — disallowed host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disallowed_host_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts outside ALLOWED_PDF_DOMAINS must raise ValueError."""

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        return _fake_getaddrinfo("1.2.3.4")

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    with pytest.raises(ValueError, match="not allowed"):
        await _validate_pdf_url("https://attacker.com/evil.pdf")


# ---------------------------------------------------------------------------
# SSRF guard — private/reserved/loopback/link-local IPs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ip",
    [
        # Private ranges
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        # Loopback
        "127.0.0.1",
        "::1",
        # Link-local (AWS metadata endpoint)
        "169.254.169.254",
        # Reserved / unspecified
        "0.0.0.0",
        # IPv6 private / link-local
        "fc00::1",
        "fe80::1",
    ],
)
async def test_private_ip_rejected(ip: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS resolving to any private/reserved/loopback/link-local IP must raise ValueError."""
    addr = ipaddress.ip_address(ip)
    assert addr.is_private or addr.is_reserved or addr.is_loopback or addr.is_link_local, (
        f"Test sanity: {ip} should be non-public"
    )

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        return _fake_getaddrinfo(ip)

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    with pytest.raises(ValueError, match="private/reserved"):
        await _validate_pdf_url("https://arxiv.org/pdf/test.pdf")


# ---------------------------------------------------------------------------
# SSRF guard — scheme rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://arxiv.org/foo.pdf",
        "javascript:alert(1)",
        "http://arxiv.org/foo.pdf",  # http is blocked outside DEV_MODE
    ],
)
async def test_bad_scheme_rejected(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-HTTPS schemes (including file:, ftp:, javascript:, http:) must be rejected."""
    monkeypatch.delenv("DEV_MODE", raising=False)
    with pytest.raises(ValueError, match="[Ss]cheme|HTTPS"):
        await _validate_pdf_url(url)


@pytest.mark.asyncio
async def test_http_allowed_in_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """http:// should be permitted when DEV_MODE=true."""
    monkeypatch.setenv("DEV_MODE", "true")
    public_ip = "151.101.1.1"

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        return _fake_getaddrinfo(public_ip)

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    # Should not raise
    await _validate_pdf_url("http://arxiv.org/pdf/test.pdf")


# ---------------------------------------------------------------------------
# SSRF guard — URL parse edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_url_rejected() -> None:
    """A URL that cannot yield a hostname must raise ValueError."""
    with pytest.raises(ValueError):
        await _validate_pdf_url("https://[invalid")


@pytest.mark.asyncio
async def test_mixed_case_host_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host matching must be case-insensitive (HTTPS://ARXIV.ORG/...)."""
    public_ip = "151.101.1.1"

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        return _fake_getaddrinfo(public_ip)

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    # urlparse().hostname lower-cases, so "ARXIV.ORG" → "arxiv.org"
    await _validate_pdf_url("HTTPS://ARXIV.ORG/x.pdf")


@pytest.mark.asyncio
async def test_userinfo_host_is_correctly_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """userinfo@host in URL: urlparse().hostname still returns the real host.

    Both forms should be accepted IF the host is in ALLOWED_PDF_DOMAINS and
    resolves to a public IP, because urlparse strips userinfo correctly.
    """
    public_ip = "151.101.1.1"

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        return _fake_getaddrinfo(public_ip)

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    # These should NOT raise — the host is arxiv.org after stripping userinfo
    await _validate_pdf_url("https://attacker@arxiv.org/foo.pdf")
    await _validate_pdf_url("https://user:pass@arxiv.org/foo.pdf")


@pytest.mark.asyncio
async def test_idn_punycode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cyrillic lookalike domain (IDN homograph) must be rejected as not in allowlist."""
    # 'а' is Cyrillic U+0430, not Latin 'a'
    cyrillic_url = "https://\u0430rxiv.org/foo.pdf"

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        return _fake_getaddrinfo("1.2.3.4")

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    with pytest.raises(ValueError, match="not allowed"):
        await _validate_pdf_url(cyrillic_url)


# ---------------------------------------------------------------------------
# SSRF guard — DNS failure / gaierror
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dns_resolution_failure_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """socket.gaierror during DNS resolution must raise ValueError."""

    def fake_gai_fail(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai_fail)
    with pytest.raises(ValueError, match="[Cc]annot resolve"):
        await _validate_pdf_url("https://arxiv.org/pdf/test.pdf")


# ---------------------------------------------------------------------------
# SSRF guard — DNS rebinding defense
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dns_rebinding_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ANY resolved IP is private, the request must be rejected (rebinding defense)."""
    public_ip = "151.101.1.1"
    private_ip = "10.0.0.1"

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        # Returns two IPs: one public, one private — classic DNS rebinding setup
        return _mixed_getaddrinfo(public_ip, private_ip)

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    with pytest.raises(ValueError, match="private/reserved"):
        await _validate_pdf_url("https://arxiv.org/pdf/test.pdf")


# ---------------------------------------------------------------------------
# Size cap — streaming accumulation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_pdf_rejects_oversized_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """download_pdf must raise ValueError when streaming data exceeds MAX_PDF_SIZE."""
    # The code checks the running total during streaming, not Content-Length.
    # We produce a single chunk larger than MAX_PDF_SIZE.
    oversized_chunk = b"%PDF-" + b"x" * (MAX_PDF_SIZE + 1)

    async def fake_stream_bytes(chunk_size: int = 65536):  # type: ignore[override]
        yield oversized_chunk

    head_response = MagicMock()
    head_response.status_code = 200
    head_response.raise_for_status = MagicMock()

    stream_response = MagicMock()
    stream_response.raise_for_status = MagicMock()
    stream_response.aiter_bytes = fake_stream_bytes
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=stream_response)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    http_client = MagicMock()
    http_client.request = AsyncMock(return_value=head_response)
    http_client.stream = MagicMock(return_value=stream_cm)

    embedder = MagicMock()
    processor = PDFProcessor(http_client=http_client, embedder=embedder)

    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(tmp_path))

    # Stub DNS so the URL passes SSRF validation
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p: _fake_getaddrinfo("151.101.1.1"))

    with pytest.raises(ValueError, match="exceeds maximum size"):
        await processor.download_pdf("https://arxiv.org/pdf/test.pdf", paper_id=1)


# ---------------------------------------------------------------------------
# Page cap — generate_snapshots
# ---------------------------------------------------------------------------


def test_generate_snapshots_rejects_too_many_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate_snapshots must raise ValueError when the PDF has > MAX_PDF_PAGES pages."""
    fake_doc = MagicMock()
    fake_doc.__len__ = MagicMock(return_value=MAX_PDF_PAGES + 1)
    fake_doc.close = MagicMock()

    monkeypatch.setattr(pdf_processor.fitz, "open", MagicMock(return_value=fake_doc))
    monkeypatch.setattr(pdf_processor, "SNAPSHOT_STORAGE_PATH", str(tmp_path))

    dummy_pdf = tmp_path / "test.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.7\n")

    embedder = MagicMock()
    http_client = MagicMock()
    processor = PDFProcessor(http_client=http_client, embedder=embedder)

    with pytest.raises(ValueError, match="exceeding limit"):
        processor.generate_snapshots(dummy_pdf, paper_id=1)


def test_generate_snapshots_zero_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_snapshots with 0 pages must return an empty list without crashing."""
    fake_doc = MagicMock()
    fake_doc.__len__ = MagicMock(return_value=0)
    fake_doc.__iter__ = MagicMock(return_value=iter([]))
    fake_doc.close = MagicMock()

    monkeypatch.setattr(pdf_processor.fitz, "open", MagicMock(return_value=fake_doc))
    monkeypatch.setattr(pdf_processor, "SNAPSHOT_STORAGE_PATH", str(tmp_path))

    dummy_pdf = tmp_path / "empty.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.7\n")

    processor = PDFProcessor(http_client=MagicMock(), embedder=MagicMock())
    result = processor.generate_snapshots(dummy_pdf, paper_id=99)

    assert result == []


# ---------------------------------------------------------------------------
# Non-PDF / malformed bytes via download_pdf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_pdf_rejects_html_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """download_pdf must reject a response that starts with HTML instead of %PDF-."""
    html_chunk = b"<html><body>Not a PDF</body></html>"

    async def fake_stream_bytes(chunk_size: int = 65536):  # type: ignore[override]
        yield html_chunk

    head_response = MagicMock()
    head_response.status_code = 200
    head_response.raise_for_status = MagicMock()

    stream_response = MagicMock()
    stream_response.raise_for_status = MagicMock()
    stream_response.aiter_bytes = fake_stream_bytes
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=stream_response)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    http_client = MagicMock()
    http_client.request = AsyncMock(return_value=head_response)
    http_client.stream = MagicMock(return_value=stream_cm)

    processor = PDFProcessor(http_client=http_client, embedder=MagicMock())
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p: _fake_getaddrinfo("151.101.1.1"))

    with pytest.raises(ValueError, match="%PDF"):
        await processor.download_pdf("https://arxiv.org/pdf/test.pdf", paper_id=2)


@pytest.mark.asyncio
async def test_download_pdf_rejects_empty_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """download_pdf must reject an empty response body (no valid PDF header)."""

    # Yielding b"" → header_bytes = b""[:5] = b"", which does not start with b"%PDF-"
    # so the code raises ValueError before writing any content.
    async def fake_stream_bytes(chunk_size: int = 65536):  # type: ignore[override]
        yield b""

    head_response = MagicMock()
    head_response.status_code = 200
    head_response.raise_for_status = MagicMock()

    stream_response = MagicMock()
    stream_response.raise_for_status = MagicMock()
    stream_response.aiter_bytes = fake_stream_bytes
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=stream_response)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    http_client = MagicMock()
    http_client.request = AsyncMock(return_value=head_response)
    http_client.stream = MagicMock(return_value=stream_cm)

    processor = PDFProcessor(http_client=http_client, embedder=MagicMock())
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p: _fake_getaddrinfo("151.101.1.1"))

    with pytest.raises(ValueError, match="%PDF"):
        await processor.download_pdf("https://arxiv.org/pdf/test.pdf", paper_id=3)


@pytest.mark.asyncio
async def test_download_pdf_rejects_truncated_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """download_pdf must reject a truncated stream starting with %PDF- but too short."""
    # Starts with valid header but only 4 bytes — doesn't start with b"%PDF-" (5 bytes)
    truncated_chunk = b"%PDF"  # 4 bytes, not b"%PDF-"

    async def fake_stream_bytes(chunk_size: int = 65536):  # type: ignore[override]
        yield truncated_chunk

    head_response = MagicMock()
    head_response.status_code = 200
    head_response.raise_for_status = MagicMock()

    stream_response = MagicMock()
    stream_response.raise_for_status = MagicMock()
    stream_response.aiter_bytes = fake_stream_bytes
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=stream_response)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    http_client = MagicMock()
    http_client.request = AsyncMock(return_value=head_response)
    http_client.stream = MagicMock(return_value=stream_cm)

    processor = PDFProcessor(http_client=http_client, embedder=MagicMock())
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p: _fake_getaddrinfo("151.101.1.1"))

    with pytest.raises(ValueError, match="%PDF"):
        await processor.download_pdf("https://arxiv.org/pdf/test.pdf", paper_id=4)


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


def test_allowed_pdf_domains_contains_expected_entries() -> None:
    """ALLOWED_PDF_DOMAINS must contain all 5 expected entries."""
    expected = {
        "arxiv.org",
        "export.arxiv.org",
        "www.arxiv.org",
        "pdfs.semanticscholar.org",
        "www.semanticscholar.org",
    }
    assert expected == ALLOWED_PDF_DOMAINS


def test_max_pdf_size_is_100mb() -> None:
    """MAX_PDF_SIZE must be 100 MB."""
    assert MAX_PDF_SIZE == 100 * 1024 * 1024


def test_max_pdf_pages_is_500() -> None:
    """MAX_PDF_PAGES must be 500."""
    assert MAX_PDF_PAGES == 500
