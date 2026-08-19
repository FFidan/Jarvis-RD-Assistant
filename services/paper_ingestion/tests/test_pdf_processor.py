"""Adversarial unit tests for pdf_processor.py.

Covers:
- SSRF guard (_validate_pdf_url): allowlist, private/reserved IPs, schemes, IDN,
  DNS failure, DNS rebinding, userinfo injection, malformed URLs
- Outbound quarantine: no HEAD, redirect or GET leaves a deployment awaiting
  restored-credential review, and the download endpoint reports 503
- Size cap (MAX_PDF_SIZE): streaming accumulation exceeds limit → ValueError
- Page cap (MAX_PDF_PAGES): pypdfium2.PdfDocument returns oversized doc → ValueError
- Malformed PDF bytes: empty, non-PDF, truncated
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import socket
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# pypdfium2/tiktoken/qdrant_client/docling are installed in the venv, so importing
# paper_ingestion.pdf_processor here is safe; the Docling converter is built
# lazily on first use, not at import time.
import paper_ingestion.pdf_processor as pdf_processor
import httpx
import pytest
from jarvis_common.maintenance import OutboundEgressBlockedError
from jarvis_common.pinned_transport import JARVIS_SERVICE_POLICY, PinnedAsyncTransport
from paper_ingestion.pdf_processor import (
    ALLOWED_PDF_DOMAINS,
    MAX_PDF_PAGES,
    MAX_PDF_SIZE,
    PDFPublishBlockedError,
    PDFProcessor,
    _validate_pdf_url,
    check_pdf_path_safe,
    pdf_publish_operation,
    publish_pdf,
    quote_to_rects,
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
# PDF publication lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_pdf_preserves_staged_and_existing_files_during_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maintenance refusal must happen before replacing either file."""
    staged = tmp_path / "_upload_abc.pdf"
    final = tmp_path / "17.pdf"
    staged.write_bytes(b"%PDF-1.7\nnew")
    final.write_bytes(b"%PDF-1.7\nold")
    monkeypatch.setattr(pdf_processor, "maintenance_active", lambda: True)

    with pytest.raises(PDFPublishBlockedError, match="maintenance"):
        await publish_pdf(staged, final)

    assert staged.read_bytes() == b"%PDF-1.7\nnew"
    assert final.read_bytes() == b"%PDF-1.7\nold"


@pytest.mark.asyncio
async def test_publish_pdf_checks_maintenance_after_contended_lock_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore that starts while a publisher waits must still block promotion."""
    staged = tmp_path / "7.tmp"
    final = tmp_path / "7.pdf"
    staged.write_bytes(b"%PDF-1.7\nnew")
    final.write_bytes(b"%PDF-1.7\nold")

    lock_path = tmp_path / ".publish.lock"
    lock_fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o644)
    real_flock = fcntl.flock
    real_flock(lock_fd, fcntl.LOCK_EX)

    lock_attempted = threading.Event()
    maintenance_calls: list[bool] = []
    maintenance_started = False

    def tracked_flock(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_EX:
            lock_attempted.set()
        real_flock(fd, operation)

    def maintenance_is_active() -> bool:
        maintenance_calls.append(maintenance_started)
        return maintenance_started

    monkeypatch.setattr(pdf_processor.fcntl, "flock", tracked_flock)
    monkeypatch.setattr(pdf_processor, "maintenance_active", maintenance_is_active)

    task = asyncio.create_task(publish_pdf(staged, final))
    try:
        assert await asyncio.to_thread(lock_attempted.wait, 1.0)
        assert maintenance_calls == []
        maintenance_started = True
        real_flock(lock_fd, fcntl.LOCK_UN)
        with pytest.raises(PDFPublishBlockedError, match="maintenance"):
            await task
    finally:
        real_flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        if not task.done():
            task.cancel()

    assert maintenance_calls == [True]
    assert staged.exists()
    assert final.read_bytes() == b"%PDF-1.7\nold"


@pytest.mark.asyncio
async def test_publish_pdf_uses_existing_read_only_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0644-style sidecar lock must not require write access from the app."""
    staged = tmp_path / "8.tmp"
    final = tmp_path / "8.pdf"
    staged.write_bytes(b"%PDF-1.7\nnew")
    lock_path = tmp_path / ".publish.lock"
    lock_path.touch(mode=0o644)
    lock_path.chmod(0o444)
    monkeypatch.setattr(pdf_processor, "maintenance_active", lambda: False)

    await publish_pdf(staged, final)

    assert not staged.exists()
    assert final.read_bytes() == b"%PDF-1.7\nnew"


@pytest.mark.asyncio
async def test_publish_pdf_rejects_replaced_lock_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publisher must not proceed while holding an unlinked lock inode."""
    staged = tmp_path / "9.tmp"
    final = tmp_path / "9.pdf"
    staged.write_bytes(b"%PDF-1.7\nnew")
    final.write_bytes(b"%PDF-1.7\nold")

    lock_path = tmp_path / ".publish.lock"
    lock_fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o644)
    real_flock = fcntl.flock
    real_flock(lock_fd, fcntl.LOCK_EX)
    lock_attempted = threading.Event()

    def tracked_flock(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_EX:
            lock_attempted.set()
        real_flock(fd, operation)

    monkeypatch.setattr(pdf_processor.fcntl, "flock", tracked_flock)
    monkeypatch.setattr(pdf_processor, "maintenance_active", lambda: False)

    task = asyncio.create_task(publish_pdf(staged, final))
    try:
        assert await asyncio.to_thread(lock_attempted.wait, 1.0)
        lock_path.unlink()
        lock_path.touch(mode=0o644)
        real_flock(lock_fd, fcntl.LOCK_UN)
        with pytest.raises(RuntimeError, match="lock changed"):
            await task
    finally:
        real_flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        if not task.done():
            task.cancel()

    assert staged.exists()
    assert final.read_bytes() == b"%PDF-1.7\nold"


@pytest.mark.asyncio
async def test_publish_pdf_rejects_symlink_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock opener must never follow a storage-root symlink."""
    staged = tmp_path / "10.tmp"
    final = tmp_path / "10.pdf"
    outside = tmp_path / "outside.lock"
    staged.write_bytes(b"%PDF-1.7\nnew")
    outside.touch()
    (tmp_path / ".publish.lock").symlink_to(outside)
    monkeypatch.setattr(pdf_processor, "maintenance_active", lambda: False)

    with pytest.raises(OSError):
        await publish_pdf(staged, final)

    assert staged.exists()
    assert not final.exists()


@pytest.mark.asyncio
async def test_publish_operation_keeps_restore_out_until_failure_cleanup_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delayed DB failure must clean its file before restore can swap PDFs."""
    staged = tmp_path / "_upload_11.pdf"
    final = tmp_path / "11.pdf"
    staged.write_bytes(b"%PDF-1.7\nrequest")
    monkeypatch.setattr(pdf_processor, "maintenance_active", lambda: False)

    lock_attempted = threading.Event()
    lock_acquired = threading.Event()

    def restore_after_lock() -> None:
        lock_fd = os.open(tmp_path / ".publish.lock", os.O_RDONLY)
        try:
            lock_attempted.set()
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            lock_acquired.set()
            final.write_bytes(b"%PDF-1.7\nrestored")
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    restore_thread: threading.Thread | None = None
    with pytest.raises(RuntimeError, match="database commit failed"):
        async with pdf_publish_operation(tmp_path) as publication:
            await publication.promote(staged, final)
            restore_thread = threading.Thread(target=restore_after_lock)
            restore_thread.start()
            assert await asyncio.to_thread(lock_attempted.wait, 1.0)
            assert not await asyncio.to_thread(lock_acquired.wait, 0.1)
            raise RuntimeError("database commit failed")

    assert restore_thread is not None
    await asyncio.to_thread(restore_thread.join, 1.0)
    assert not restore_thread.is_alive()
    assert lock_acquired.is_set()
    assert final.read_bytes() == b"%PDF-1.7\nrestored"


@pytest.mark.asyncio
async def test_download_pdf_cleans_staged_file_when_publish_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The download caller must use the guarded publisher and clean its temp file."""

    async def fake_stream_bytes(chunk_size: int = 65536):  # type: ignore[override]
        yield b"%PDF-1.7\nnew"

    head_response = MagicMock(status_code=200)
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

    final = tmp_path / "23.pdf"
    final.write_bytes(b"%PDF-1.7\nold")
    publish = AsyncMock(side_effect=PDFPublishBlockedError("PDF maintenance is active"))
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(pdf_processor, "publish_pdf", publish)
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p: _fake_getaddrinfo("151.101.1.1"))

    processor = PDFProcessor(http_client=http_client, embedder=MagicMock())
    with pytest.raises(PDFPublishBlockedError, match="maintenance"):
        await processor.download_pdf("https://arxiv.org/pdf/test.pdf", paper_id=23)

    publish.assert_awaited_once()
    staged, published_final = publish.await_args.args
    assert published_final == final
    assert staged.parent == tmp_path
    assert staged.name.startswith(".23.download-")
    assert staged.suffix == ".tmp"
    assert final.read_bytes() == b"%PDF-1.7\nold"
    assert not staged.exists()


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
# SSRF guard — resolution is bound to the pinned transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pdf_validation_defers_dns_to_the_pinned_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL messaging checks must not pre-resolve before the pinned connection."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PDF URL validation must not resolve DNS")
        ),
    )
    await _validate_pdf_url("https://arxiv.org/pdf/test.pdf")


@pytest.mark.asyncio
async def test_pdf_redirect_and_final_get_use_the_pinned_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real downloader re-pins the redirect target and streamed GET."""
    requests: list[tuple[str, str, str]] = []
    resolved: list[str] = []
    server: asyncio.AbstractServer

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("ascii").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            host = next(line[6:] for line in lines[1:] if line.lower().startswith("host: "))
            requests.append((method, target, host))
            port = server.sockets[0].getsockname()[1]
            if target == "/start.pdf":
                response = (
                    "HTTP/1.1 302 Found\r\n"
                    f"Location: http://host.docker.internal:{port}/final.pdf\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
            elif method == "HEAD":
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            else:
                body = b"%PDF-1.7\npinned"
                response = (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode("ascii") + body
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        resolved.append(host)
        return [(socket.AF_INET, "127.0.0.1")]

    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(tmp_path))
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            transport=PinnedAsyncTransport(
                JARVIS_SERVICE_POLICY,
                resolver=resolver,
            ),
            trust_env=False,
        ) as client:
            processor = PDFProcessor(http_client=client, embedder=MagicMock())
            staged, final = await processor.stage_pdf_download(
                f"http://localhost:{port}/start.pdf", paper_id=91
            )
    finally:
        server.close()
        await server.wait_closed()

    assert staged.read_bytes() == b"%PDF-1.7\npinned"
    assert final == tmp_path / "91.pdf"
    assert resolved == ["localhost", "host.docker.internal", "host.docker.internal"]
    assert requests == [
        ("HEAD", "/start.pdf", f"localhost:{port}"),
        ("HEAD", "/final.pdf", f"host.docker.internal:{port}"),
        ("GET", "/final.pdf", f"host.docker.internal:{port}"),
    ]


# ---------------------------------------------------------------------------
# Outbound quarantine — no PDF request may leave a deployment awaiting review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_sends", [0, 1, 2])
async def test_pdf_download_stops_before_every_quarantined_send(
    allowed_sends: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarantine must stop the download before the next request leaves.

    The first HEAD, each redirect re-request and the streamed GET open their own
    connection, so a quarantine that begins mid-download has to be observed at
    each one. ``allowed_sends`` is how many requests complete before the
    sentinel appears: 0 blocks the first HEAD, 1 the redirect HEAD, 2 the GET.
    """
    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(tmp_path))
    if allowed_sends == 0:
        quarantine.touch()

    sent: list[str] = []

    async def fake_request(method: str, url: str, **_kwargs: Any) -> MagicMock:
        sent.append(method)
        if len(sent) == allowed_sends:
            quarantine.touch()
        response = MagicMock()
        response.status_code = 302 if url.endswith("start.pdf") else 200
        response.headers = {"Location": "https://arxiv.org/pdf/final.pdf"}
        return response

    client = MagicMock()
    client.request = fake_request
    client.stream = MagicMock(side_effect=AssertionError("quarantined GET must not be sent"))
    processor = PDFProcessor(http_client=client, embedder=MagicMock())

    with pytest.raises(OutboundEgressBlockedError):
        await processor.stage_pdf_download("https://arxiv.org/pdf/start.pdf", paper_id=11)

    assert len(sent) == allowed_sends
    assert not list(tmp_path.glob("*.tmp"))


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


@pytest.mark.asyncio
async def test_dev_hostname_rejected_at_the_domain_gate_outside_dev_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The development hostnames widen the domain allowlist only in dev mode."""
    monkeypatch.delenv("DEV_MODE", raising=False)

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        return _fake_getaddrinfo("151.101.1.1")  # public, so only the domain gate can refuse

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    with pytest.raises(ValueError, match="not allowed"):
        await _validate_pdf_url("https://host.docker.internal/paper.pdf")


@pytest.mark.asyncio
async def test_dev_hostname_passes_the_domain_gate_in_dev_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In dev mode the same hostname clears the domain gate."""
    monkeypatch.setenv("DEV_MODE", "true")

    def fake_gai(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        return _fake_getaddrinfo("151.101.1.1")

    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    # Should not raise
    await _validate_pdf_url("https://host.docker.internal/paper.pdf")


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

    monkeypatch.setattr(pdf_processor.pdfium, "PdfDocument", MagicMock(return_value=fake_doc))
    monkeypatch.setattr(pdf_processor, "SNAPSHOT_STORAGE_PATH", str(tmp_path))

    dummy_pdf = tmp_path / "test.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.7\n")

    embedder = MagicMock()
    http_client = MagicMock()
    processor = PDFProcessor(http_client=http_client, embedder=embedder)

    with pytest.raises(ValueError, match="exceeding limit"):
        processor.generate_snapshots(dummy_pdf, paper_id=1)

    fake_doc.close.assert_called_once()


def test_generate_snapshots_zero_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_snapshots with 0 pages must return an empty list without crashing."""
    fake_doc = MagicMock()
    fake_doc.__len__ = MagicMock(return_value=0)
    fake_doc.__getitem__ = MagicMock(side_effect=IndexError)
    fake_doc.close = MagicMock()

    monkeypatch.setattr(pdf_processor.pdfium, "PdfDocument", MagicMock(return_value=fake_doc))
    monkeypatch.setattr(pdf_processor, "SNAPSHOT_STORAGE_PATH", str(tmp_path))

    dummy_pdf = tmp_path / "empty.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.7\n")

    processor = PDFProcessor(http_client=MagicMock(), embedder=MagicMock())
    result = processor.generate_snapshots(dummy_pdf, paper_id=99)

    assert result == []
    fake_doc.close.assert_called_once()


def test_generate_snapshots_real_pdf_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Render a real multi-page PDF: one valid PNG per page, 1-indexed, dims = points*DPI/72."""
    import pypdfium2 as pdfium
    from paper_ingestion.pdf_processor import SNAPSHOT_DPI
    from PIL import Image

    page_sizes = [(595.0, 842.0), (612.0, 792.0), (595.0, 842.0)]  # A4, Letter, A4 (points)
    src = pdfium.PdfDocument.new()
    for width, height in page_sizes:
        src.new_page(width, height)
    pdf_path = tmp_path / "sample.pdf"
    src.save(str(pdf_path))
    src.close()

    monkeypatch.setattr(pdf_processor, "SNAPSHOT_STORAGE_PATH", str(tmp_path / "snapshots"))

    processor = PDFProcessor(http_client=MagicMock(), embedder=MagicMock())
    paths = processor.generate_snapshots(pdf_path, paper_id=7)

    assert len(paths) == len(page_sizes)
    scale = SNAPSHOT_DPI / 72
    for i, ((width, height), path) in enumerate(zip(page_sizes, paths, strict=True), start=1):
        assert path.name == f"page_{i}.png"
        assert path.exists()
        with Image.open(path) as img:
            assert img.format == "PNG"
            assert abs(img.width - round(width * scale)) <= 2
            assert abs(img.height - round(height * scale)) <= 2


def test_generate_snapshots_removes_pages_past_a_shorter_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regenerating for a shorter document must not leave the old tail servable.

    The snapshot directory is keyed only by paper id, so a replacement source
    with fewer pages overwrites page_1 and leaves the previous document's higher
    pages behind. Nothing else removes them once the paper's PDF pointer is
    restored, so they would keep serving under the current paper's identity.
    """
    import pypdfium2 as pdfium

    snapshots = tmp_path / "snapshots"
    monkeypatch.setattr(pdf_processor, "SNAPSHOT_STORAGE_PATH", str(snapshots))
    processor = PDFProcessor(http_client=MagicMock(), embedder=MagicMock())

    def _write_pdf(name: str, pages: int) -> Path:
        doc = pdfium.PdfDocument.new()
        for _ in range(pages):
            doc.new_page(595.0, 842.0)
        path = tmp_path / name
        doc.save(str(path))
        doc.close()
        return path

    long_paths = processor.generate_snapshots(_write_pdf("long.pdf", 3), paper_id=11)
    assert [p.name for p in long_paths] == ["page_1.png", "page_2.png", "page_3.png"]

    short_paths = processor.generate_snapshots(_write_pdf("short.pdf", 1), paper_id=11)

    assert [p.name for p in short_paths] == ["page_1.png"]
    remaining = sorted(p.name for p in (snapshots / "11").glob("page_*.png"))
    assert remaining == ["page_1.png"], (
        f"pages from the superseded document are still servable: {remaining}"
    )


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


# ---------------------------------------------------------------------------
# Docling extraction → exact per-page anchors (mocked converter; no models)
# ---------------------------------------------------------------------------


def _fake_docling_doc(pages: dict[int, str]) -> MagicMock:
    """Stand-in DoclingDocument: ``.pages`` keys + ``.export_to_markdown(page_no=)``."""
    doc = MagicMock()
    doc.pages = pages
    doc.export_to_markdown = MagicMock(side_effect=lambda page_no: pages[page_no])
    return doc


def _patch_converter(monkeypatch: pytest.MonkeyPatch, doc: MagicMock) -> None:
    converter = MagicMock()
    converter.convert = MagicMock(return_value=MagicMock(document=doc))
    monkeypatch.setattr(pdf_processor, "_get_docling_converter", lambda: converter)


def test_extract_text_sync_builds_exact_page_anchors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anchors are exact char ranges over full_text, one per page, carrying the real page_no."""
    pages = {1: "## A\n\nalpha text", 2: "## B\n\nbeta text", 3: "## C\n\ngamma text"}
    _patch_converter(monkeypatch, _fake_docling_doc(pages))

    full_text, anchors = pdf_processor._extract_text_sync(Path("/x.pdf"))

    assert [page_no for _, _, page_no in anchors] == [1, 2, 3]
    assert anchors == sorted(anchors)  # ascending start_char
    for start, end, page_no in anchors:
        assert full_text[start:end] == pages[page_no]


def test_extract_text_sync_skips_empty_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty-rendering page is dropped without shifting the other anchors."""
    pages = {1: "## A\n\nalpha", 2: "", 3: "## C\n\ngamma"}
    _patch_converter(monkeypatch, _fake_docling_doc(pages))

    full_text, anchors = pdf_processor._extract_text_sync(Path("/x.pdf"))

    assert [page_no for _, _, page_no in anchors] == [1, 3]
    for start, end, page_no in anchors:
        assert full_text[start:end] == pages[page_no]


def test_extract_text_sync_empty_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """A document with no pages yields ('', [])."""
    _patch_converter(monkeypatch, _fake_docling_doc({}))
    assert pdf_processor._extract_text_sync(Path("/x.pdf")) == ("", [])


def test_extract_text_sync_strips_null_bytes_keeping_anchors_aligned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Null bytes are stripped per page BEFORE anchoring, so full_text[start:end]
    still reconstructs each page (PostgreSQL rejects \\x00; stale offsets here
    would misattribute page numbers — the exact failure this test prevents)."""
    pages = {1: "## A\x00\n\nalpha\x00 text", 2: "## B\n\nbeta\x00"}
    _patch_converter(monkeypatch, _fake_docling_doc(pages))

    full_text, anchors = pdf_processor._extract_text_sync(Path("/x.pdf"))

    assert "\x00" not in full_text
    for start, end, page_no in anchors:
        assert full_text[start:end] == pages[page_no].replace("\x00", "")


def test_chunk_text_page_bounded_reindex_and_pages() -> None:
    """Per-page chunking: chunk_index is globally unique and each chunk's page is exact."""
    from paper_ingestion.ingestion.embedder import Embedder

    embedder = Embedder(http_client=MagicMock(), qdrant_client=MagicMock())
    page1 = "## Introduction\n\n" + "alpha sentence. " * 60
    page2 = "## Methods\n\n" + "beta sentence. " * 60
    full_text = page1 + "\n\n" + page2
    anchors = [(0, len(page1), 1), (len(page1) + 2, len(page1) + 2 + len(page2), 2)]

    chunks = embedder.chunk_text(full_text, anchors)

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))  # unique + monotonic
    assert all(c.page_number in (1, 2) for c in chunks)
    assert {c.page_number for c in chunks} == {1, 2}  # both pages produced chunks


@pytest.mark.asyncio
async def test_process_reports_phases_only_after_their_real_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Progress events follow extraction, chunking, and persisted vector batches."""
    from paper_ingestion.models import ChunkForEmbedding

    timeline: list[str] = []
    chunk = ChunkForEmbedding(
        chunk_index=0,
        content="chunk",
        page_number=1,
        start_char=0,
        end_char=5,
    )

    async def _extract(_pdf_path: Path):
        timeline.append("extraction complete")
        return "full text", [(0, 9, 1)]

    def _snapshots(_pdf_path: Path, _paper_id: int):
        timeline.append("snapshots complete")
        return []

    def _chunk(_text: str, _anchors: list[tuple[int, int, int]]):
        timeline.append("chunking complete")
        return [chunk]

    async def _embed(
        _paper_id: int,
        _chunks: list[ChunkForEmbedding],
        *,
        user_id: int | None,
        visibility,
        run_context,
    ):
        assert user_id is None
        assert visibility is None
        assert run_context.resume_content == {}
        assert run_context.progress_callback is not None
        timeline.append("embedding started")
        await run_context.progress_callback(1, 1)
        return ["point-1"]

    async def _record_progress(
        phase: str,
        completed: int,
        total: int,
    ) -> None:
        timeline.append(f"progress:{phase}:{completed}/{total}")

    embedder = MagicMock()
    embedder.chunk_text = MagicMock(side_effect=_chunk)
    embedder.embed_and_store = AsyncMock(side_effect=_embed)
    processor = PDFProcessor(http_client=MagicMock(), embedder=embedder)
    monkeypatch.setattr(pdf_processor, "extract_text", _extract)
    monkeypatch.setattr(processor, "generate_snapshots", _snapshots)

    await processor.process(
        Path("/tmp/paper.pdf"),
        14,
        progress_callback=_record_progress,
    )

    assert timeline == [
        "extraction complete",
        "progress:extracted:1/1",
        "snapshots complete",
        "chunking complete",
        "progress:chunked:1/1",
        "embedding started",
        "progress:embedding:1/1",
    ]


@pytest.mark.asyncio
async def test_process_does_not_report_an_unfinished_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An extraction failure cannot be advertised as extracted."""
    events: list[tuple[str, int, int]] = []

    async def _fail_extraction(_pdf_path: Path):
        raise RuntimeError("extraction failed")

    async def _record_progress(phase: str, completed: int, total: int) -> None:
        events.append((phase, completed, total))

    processor = PDFProcessor(http_client=MagicMock(), embedder=MagicMock())
    monkeypatch.setattr(pdf_processor, "extract_text", _fail_extraction)

    with pytest.raises(RuntimeError, match="extraction failed"):
        await processor.process(
            Path("/tmp/paper.pdf"),
            15,
            progress_callback=_record_progress,
        )

    assert events == []


# ---------------------------------------------------------------------------
# ING-004: download_pdf null-location guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_pdf_no_location_header_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect response with no Location header must not crash (break out of loop)."""
    # First HEAD returns a 302 with NO Location header; second returns 200.
    head_302 = MagicMock()
    head_302.status_code = 302
    head_302.headers = {}  # no Location
    head_302.raise_for_status = MagicMock()

    head_200 = MagicMock()
    head_200.status_code = 200
    head_200.raise_for_status = MagicMock()

    async def fake_stream_bytes(chunk_size: int = 65536):  # type: ignore[override]
        yield b"%PDF-" + b"a" * 16

    stream_response = MagicMock()
    stream_response.raise_for_status = MagicMock()
    stream_response.aiter_bytes = fake_stream_bytes
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=stream_response)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    http_client = MagicMock()
    # First request() call → 302 with no Location → should break and then use current_url
    http_client.request = AsyncMock(side_effect=[head_302, head_200])
    http_client.stream = MagicMock(return_value=stream_cm)

    processor = PDFProcessor(http_client=http_client, embedder=MagicMock())
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p: _fake_getaddrinfo("151.101.1.1"))

    # Should not raise — broken out of redirect loop, then downloads from current_url
    result = await processor.download_pdf("https://arxiv.org/pdf/test.pdf", paper_id=42)
    assert result == tmp_path / "42.pdf"


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


# ---------------------------------------------------------------------------
# PR5-T5: check_pdf_path_safe — single path-traversal guard helper
# ---------------------------------------------------------------------------


def test_check_pdf_path_safe_inside_storage_returns_true(tmp_path: Path) -> None:
    """A file genuinely inside the storage root resolves as safe."""
    storage = tmp_path / "storage"
    storage.mkdir()
    inside = storage / "1.pdf"
    assert check_pdf_path_safe(inside, storage) is True


def test_check_pdf_path_safe_traversal_path_returns_false(tmp_path: Path) -> None:
    """A `storage/../etc/passwd`-style traversal escapes the root → unsafe."""
    storage = tmp_path / "storage"
    storage.mkdir()
    traversal = storage / ".." / "etc" / "passwd"
    assert check_pdf_path_safe(traversal, storage) is False


def test_check_pdf_path_safe_absolute_outside_returns_false(tmp_path: Path) -> None:
    """An absolute path outside the storage root → unsafe."""
    storage = tmp_path / "storage"
    storage.mkdir()
    outside = tmp_path / "elsewhere" / "leak.pdf"
    assert check_pdf_path_safe(outside, storage) is False


def test_check_pdf_path_safe_accepts_str_storage(tmp_path: Path) -> None:
    """`storage` may be passed as a str (callers pass the configured path string)."""
    storage = tmp_path / "storage"
    storage.mkdir()
    inside = storage / "2.pdf"
    assert check_pdf_path_safe(inside, str(storage)) is True


def test_check_pdf_path_safe_default_uses_live_module_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting `storage` reads the live module-level PDF_STORAGE_PATH at call time."""
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(storage))
    assert check_pdf_path_safe(storage / "3.pdf") is True
    assert check_pdf_path_safe(tmp_path / "outside.pdf") is False


# ---------------------------------------------------------------------------
# PI-DISC-001: download_pdf zero-byte guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_pdf_zero_bytes_raises_and_unlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """download_pdf must raise ValueError and unlink the file when the stream yields zero bytes."""

    async def fake_stream_bytes(chunk_size: int = 65536):  # type: ignore[override]
        return
        yield  # make it an async generator that yields nothing

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

    with pytest.raises(ValueError, match="0 bytes"):
        await processor.download_pdf("https://arxiv.org/pdf/test.pdf", paper_id=99)

    # File must have been cleaned up
    assert not (tmp_path / "99.pdf").exists()


# ---------------------------------------------------------------------------
# CORE-1: atomic download — a mid-stream failure must not leave a partial that a
# retry appends to (which would corrupt the file via concatenation).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_pdf_retry_after_partial_does_not_concatenate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-stream failure leaves no partial; a retry must not concatenate.

    The first download streams two valid ``%PDF-`` chunks then raises mid-loop:
    afterwards neither the final ``{paper_id}.pdf`` nor a ``.tmp`` sibling may
    survive. A second, complete download must yield a file whose bytes are ONLY
    the second download's content (no stale prefix from the aborted attempt).
    """
    import httpx

    first_chunks = [b"%PDF-1.5 first-attempt-partial-A", b"-more-first-attempt-partial-B"]
    second_pdf = b"%PDF-1.7 complete-second-download\nbody\n%%EOF\n"

    async def fail_midstream(chunk_size: int = 65536):  # type: ignore[override]
        for chunk in first_chunks:
            yield chunk
        raise httpx.StreamError("connection dropped mid-stream")

    async def complete_stream(chunk_size: int = 65536):  # type: ignore[override]
        yield second_pdf

    head_response = MagicMock()
    head_response.status_code = 200
    head_response.raise_for_status = MagicMock()

    def _make_stream_cm(aiter_bytes: Any) -> MagicMock:
        stream_response = MagicMock()
        stream_response.raise_for_status = MagicMock()
        stream_response.aiter_bytes = aiter_bytes
        stream_cm = MagicMock()
        stream_cm.__aenter__ = AsyncMock(return_value=stream_response)
        stream_cm.__aexit__ = AsyncMock(return_value=False)
        return stream_cm

    http_client = MagicMock()
    http_client.request = AsyncMock(return_value=head_response)
    http_client.stream = MagicMock(
        side_effect=[_make_stream_cm(fail_midstream), _make_stream_cm(complete_stream)]
    )

    processor = PDFProcessor(http_client=http_client, embedder=MagicMock())
    monkeypatch.setattr(pdf_processor, "PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p: _fake_getaddrinfo("151.101.1.1"))

    # First attempt fails mid-stream.
    with pytest.raises(httpx.StreamError):
        await processor.download_pdf("https://arxiv.org/pdf/test.pdf", paper_id=7)

    # Neither a partial target nor a stale temp may remain after the failure.
    assert not (tmp_path / "7.pdf").exists()
    assert not (tmp_path / "7.tmp").exists()

    # Retry completes; final bytes must equal ONLY the second download.
    result = await processor.download_pdf("https://arxiv.org/pdf/test.pdf", paper_id=7)
    assert result == tmp_path / "7.pdf"
    assert (tmp_path / "7.pdf").read_bytes() == second_pdf
    assert not (tmp_path / "7.tmp").exists()


# ---------------------------------------------------------------------------
# P7b.2: quote_to_rects — verified-quote → normalized highlight rectangles
#
# Fixture approach: build a one-page PDF *in memory* with pypdfium2's raw
# text-insertion API and place KNOWN text at KNOWN baselines (reportlab is not
# installed; pypdfium2 has no high-level text-object helper in 4.30.0). This
# avoids committing a binary fixture and keeps the known text + positions
# visible in the test. Assertions are independent of the extractor: we know the
# baselines/x-offsets we drew, then assert the returned normalized geometry
# (incl. the bottom-origin → top-origin y-flip).
# ---------------------------------------------------------------------------

# Single-page geometry shared by the quote_to_rects fixtures (PDF points).
_FIXTURE_W = 300.0
_FIXTURE_H = 400.0


def _build_text_pdf(path: Path, items: list[tuple[str, float, float]]) -> None:
    """Write a one-page PDF to `path` containing `items` of (text, x, baseline_y).

    `x`/`baseline_y` are PDF points, bottom-origin (y grows upward) — the native
    pypdfium2 coordinate system, so a larger `baseline_y` sits higher on the page.
    """
    import ctypes

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    pdf = pdfium.PdfDocument.new()
    try:
        page = pdf.new_page(_FIXTURE_W, _FIXTURE_H)
        font = raw.FPDFText_LoadStandardFont(pdf, b"Helvetica")
        for text, x, baseline_y in items:
            text_obj = raw.FPDFPageObj_CreateTextObj(pdf, font, 14.0)
            encoded = (text + "\x00").encode("utf-16-le")
            buffer = ctypes.create_string_buffer(encoded, len(encoded))
            raw.FPDFText_SetText(text_obj, ctypes.cast(buffer, raw.FPDF_WIDESTRING))
            raw.FPDFPageObj_Transform(text_obj, 1, 0, 0, 1, x, baseline_y)
            raw.FPDFPage_InsertObject(page.raw, text_obj)
        raw.FPDFPage_GenerateContent(page.raw)
        pdf.save(str(path))
    finally:
        pdf.close()


@pytest.mark.asyncio
async def test_quote_to_rects_single_line_normalizes_with_y_flip(tmp_path: Path) -> None:
    """A single-line quote near the page top → one rect with correct, flipped coords."""
    pdf_path = tmp_path / "single.pdf"
    # Drawn at x=60, baseline y=350 (near the TOP in bottom-origin terms).
    _build_text_pdf(pdf_path, [("Alpha Bravo", 60.0, 350.0)])

    rects = await quote_to_rects(pdf_path, page=1, quote="Alpha")

    assert len(rects) == 1
    rect = rects[0]
    # Every coord normalized into [0, 1], ordered, non-degenerate.
    assert all(0.0 <= rect[k] <= 1.0 for k in ("x0", "y0", "x1", "y1"))
    assert rect["x0"] < rect["x1"]
    assert rect["y0"] < rect["y1"]
    # x0 ≈ drawn x (60) / page width (300) = 0.2.
    assert rect["x0"] == pytest.approx(60.0 / _FIXTURE_W, abs=0.02)
    # y-flip: text near the TOP (high bottom-origin y) → SMALL top-origin y.
    assert rect["y0"] < 0.25


@pytest.mark.asyncio
async def test_quote_to_rects_multi_line_returns_rect_per_line(tmp_path: Path) -> None:
    """A quote spanning two visual lines → one rect per line, ordered top→bottom."""
    pdf_path = tmp_path / "multi.pdf"
    _build_text_pdf(
        pdf_path,
        [
            ("Alpha Bravo", 50.0, 350.0),  # top line
            ("Charlie Delta", 50.0, 60.0),  # bottom line
        ],
    )

    # The match spans the line break (pypdfium2 matches the query space against
    # the document's line break), so the run covers chars on both bands.
    rects = await quote_to_rects(pdf_path, page=1, quote="Bravo Charlie")

    assert len(rects) == 2
    top_rect, bottom_rect = rects
    # y increases downward: the upper line's whole band sits above the lower one.
    assert top_rect["y1"] <= bottom_rect["y0"]
    assert top_rect["y0"] < 0.5 < bottom_rect["y0"]


@pytest.mark.asyncio
async def test_quote_to_rects_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocking pypdfium2 search is dispatched via asyncio.to_thread, off the loop."""
    pdf_path = tmp_path / "offthread.pdf"
    _build_text_pdf(pdf_path, [("Alpha Bravo", 60.0, 350.0)])

    real_to_thread = pdf_processor.asyncio.to_thread
    dispatched: list[Any] = []

    async def _spy(fn: Any, *args: Any, **kwargs: Any) -> Any:
        dispatched.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(pdf_processor.asyncio, "to_thread", _spy)
    rects = await quote_to_rects(pdf_path, page=1, quote="Alpha")

    assert dispatched == [pdf_processor._quote_to_rects_sync]
    assert len(rects) == 1


@pytest.mark.asyncio
async def test_quote_to_rects_absent_quote_returns_empty(tmp_path: Path) -> None:
    """A quote that does not appear on the page → [] (no fuzzy repair)."""
    pdf_path = tmp_path / "absent.pdf"
    _build_text_pdf(pdf_path, [("Alpha Bravo", 50.0, 350.0)])

    assert await quote_to_rects(pdf_path, page=1, quote="Nonexistent phrase") == []


@pytest.mark.asyncio
async def test_quote_to_rects_blank_quote_returns_empty(tmp_path: Path) -> None:
    """A whitespace-only quote short-circuits to [] without touching the PDF."""
    pdf_path = tmp_path / "blank.pdf"
    _build_text_pdf(pdf_path, [("Alpha Bravo", 50.0, 350.0)])

    assert await quote_to_rects(pdf_path, page=1, quote="   ") == []
