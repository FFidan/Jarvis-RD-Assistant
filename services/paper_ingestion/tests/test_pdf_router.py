"""Tests for download_pdf releasing DB conn before HTTP download;
scan_local_pdfs using per-file connections.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.dependencies import utils as fastapi_dependency_utils
from jarvis_common.testing import make_conn as _make_conn
from jarvis_common.testing_db import make_multi_acquire_pool

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
fastapi_dependency_utils.ensure_multipart_is_installed = lambda: None

from paper_ingestion.routers import pdf_actions as pdf  # noqa: E402
from tests.conftest import FakeRecord  # noqa: E402


def _make_pool_multi_conn(*conns):
    """Return a pool mock that yields each connection in order on successive acquire() calls."""
    return make_multi_acquire_pool(list(conns))[0]


def _paper_row(**overrides):
    """Build a FakeRecord representing a papers table row."""
    defaults = dict(
        id=1,
        title="Neural ODEs",
        external_id="arxiv:1234",
        source_type="arxiv",
        authors=["Chen"],
        abstract="An abstract",
        published_date=None,
        url="https://arxiv.org/abs/1234",
        pdf_url="https://arxiv.org/pdf/1234.pdf",
        pdf_downloaded=False,
        pdf_local_path=None,
        citation_count=0,
        metadata={},
        created_at=datetime(2026, 3, 11, tzinfo=UTC),
        user_state=None,
        is_visible=True,
    )
    defaults.update(overrides)
    return FakeRecord(**defaults)


# ---------------------------------------------------------------------------
# download_pdf — connection lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_pdf_releases_conn_before_http(tmp_path: Path):
    """DB conn must be released (acquire() closes) before the HTTP download begins.

    Verifies that db_pool.acquire() is called exactly twice:
    - once to load the row
    - once to write back
    The download happens between those two calls, with no connection held.
    """
    load_row = _paper_row(pdf_downloaded=False)
    final_path = tmp_path / "1.pdf"
    staged_path = tmp_path / "_download_1.pdf"
    updated_row = _paper_row(pdf_downloaded=True, pdf_local_path=str(final_path))

    load_conn = _make_conn(fetchrow_return=load_row)
    writeback_conn = _make_conn(fetchrow_return=updated_row)
    pool = _make_pool_multi_conn(load_conn, writeback_conn)

    # Track when download happens relative to acquire() calls
    acquire_call_count_at_download: list[int] = []

    async def mock_download(url, paper_id):
        # At this point the load conn should already be released,
        # so acquire has been called once and the write-back has not started yet.
        acquire_call_count_at_download.append(pool.acquire.call_count)
        staged_path.write_bytes(b"%PDF-1.7\ncontent")
        return staged_path, final_path

    processor = MagicMock()
    processor.stage_pdf_download = AsyncMock(side_effect=mock_download)

    result = await pdf.download_pdf.__wrapped__(
        MagicMock(),
        paper_id=1,
        db_pool=pool,
        pdf_processor=processor,
        user_id=1,
    )

    # acquire() must have been called exactly twice
    assert pool.acquire.call_count == 2, (
        f"Expected 2 acquire() calls (load + write-back), got {pool.acquire.call_count}"
    )

    # During the download, exactly 1 acquire() call had been made (load done, write-back not yet)
    assert acquire_call_count_at_download == [1], (
        "DB connection was still held (or not yet released) when HTTP download ran"
    )

    assert result.pdf_downloaded is True
    processor.stage_pdf_download.assert_awaited_once_with("https://arxiv.org/pdf/1234.pdf", 1)


@pytest.mark.asyncio
async def test_download_pdf_catches_http_error():
    """download_pdf must convert any httpx.HTTPError subclass into a 502 response."""
    phase1_conn = _make_conn(fetchrow_return=_paper_row(pdf_downloaded=False))
    pool = _make_pool_multi_conn(phase1_conn)

    processor = MagicMock()
    processor.stage_pdf_download = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    with pytest.raises(HTTPException) as exc_info:
        await pdf.download_pdf.__wrapped__(
            MagicMock(),
            paper_id=1,
            db_pool=pool,
            pdf_processor=processor,
            user_id=1,
        )

    assert exc_info.value.status_code == 502
    assert "PDF download failed" in exc_info.value.detail
    # Only the load acquire() should have happened (write-back never reached)
    assert pool.acquire.call_count == 1


# ---------------------------------------------------------------------------
# process_pdf — library membership gate on force, and the synchronous bound
# ---------------------------------------------------------------------------


def _process_pdf_app(pool, *, user_id: int):
    """Return a FastAPI app serving the pdf router against mocked dependencies."""
    from fastapi import FastAPI
    from jarvis_common.auth import current_user_id_strict

    from paper_ingestion.deps import get_db_pool, get_embedder, get_pdf_processor

    # The route limiter is a process-wide singleton and every app-level test
    # here keys to the same bucket (no middleware stashes a transport peer), so
    # reset it or these tests spend the 5/minute quota other tests rely on.
    pdf.limiter.reset()

    app = FastAPI()
    app.include_router(pdf.router)
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_pdf_processor] = lambda: MagicMock()
    app.dependency_overrides[get_embedder] = lambda: MagicMock()
    app.dependency_overrides[current_user_id_strict] = lambda: user_id
    return app


def _process_pdf_client(app):
    """Return an in-process httpx client bound to *app*."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _stub_processable_pdf(tmp_path, monkeypatch):
    """Stage a downloaded PDF plus a stubbed workflow; return (row, workflow)."""
    pdf_file = tmp_path / "1.pdf"
    pdf_file.write_bytes(b"%PDF-1.7\ncontent")
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(tmp_path))
    workflow = AsyncMock(return_value={"paper_id": 1, "chunk_count": 3, "status": "processed"})
    monkeypatch.setattr(pdf, "run_process_pdf", workflow)
    return _paper_row(pdf_downloaded=True, pdf_local_path=str(pdf_file)), workflow


@pytest.mark.asyncio
async def test_process_pdf_force_rejects_caller_without_library_row(tmp_path, monkeypatch):
    """A public paper the caller never saved cannot have its content rebuilt."""
    row, workflow = _stub_processable_pdf(tmp_path, monkeypatch)
    conn = _make_conn(fetchrow_return=row)
    conn.fetch.return_value = []  # no user_library row for this caller
    pool = _make_pool_multi_conn(conn)

    async with _process_pdf_client(_process_pdf_app(pool, user_id=2)) as client:
        resp = await client.post("/api/process-pdf/1?sync=true&force=true")

    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"
    assert "library" in resp.json()["detail"].lower()
    assert workflow.await_count == 0


@pytest.mark.asyncio
async def test_process_pdf_force_allows_caller_with_library_row(tmp_path, monkeypatch):
    """The same request succeeds once the paper is in the caller's library."""
    row, workflow = _stub_processable_pdf(tmp_path, monkeypatch)
    conn = _make_conn(fetchrow_return=row)
    conn.fetch.return_value = [FakeRecord(id=1)]  # library row present
    pool = _make_pool_multi_conn(conn)

    async with _process_pdf_client(_process_pdf_app(pool, user_id=1)) as client:
        resp = await client.post("/api/process-pdf/1?sync=true&force=true")

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json()["status"] == "processed"
    assert workflow.await_count == 1


@pytest.mark.asyncio
async def test_process_pdf_async_force_rejects_caller_without_library_row():
    """The deferring branch refuses a rebuild the caller is not entitled to request."""
    import jarvis_common.task_registry as task_registry

    conn = _make_conn(fetchrow_return=_paper_row())
    conn.fetch.return_value = []  # no user_library row for this caller
    pool = _make_pool_multi_conn(conn)

    # Registered so a refusal that stopped working surfaces as a queued 200
    # rather than an unrelated KeyError.
    task = MagicMock()
    task.defer_async = AsyncMock()
    with patch.dict(task_registry._TASK_MAP, {"paper.process": task}):
        async with _process_pdf_client(_process_pdf_app(pool, user_id=2)) as client:
            resp = await client.post("/api/process-pdf/1?force=true")  # sync defaults to False

    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"
    assert "library" in resp.json()["detail"].lower()
    task.defer_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_pdf_async_force_still_defers_for_library_holder():
    """A holder keeps the deferral semantics and the {job_id, status} response shape."""
    import jarvis_common.task_registry as task_registry

    conn = _make_conn(fetchrow_return=_paper_row())
    conn.fetch.return_value = [FakeRecord(id=1)]  # library row present
    pool = _make_pool_multi_conn(conn)

    task = MagicMock()
    task.defer_async = AsyncMock()
    with patch.dict(task_registry._TASK_MAP, {"paper.process": task}):
        async with _process_pdf_client(_process_pdf_app(pool, user_id=1)) as client:
            resp = await client.post("/api/process-pdf/1?force=true")

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    assert set(resp.json()) == {"job_id", "status"}
    assert resp.json()["status"] == "queued"
    task.defer_async.assert_awaited_once()
    assert task.defer_async.await_args.kwargs["force"] is True


@pytest.mark.asyncio
async def test_process_pdf_sync_refuses_when_bound_is_saturated():
    """A saturated synchronous bound is refused at once, without taking a connection."""
    conn = _make_conn(fetchrow_return=_paper_row(pdf_downloaded=True))
    pool = _make_pool_multi_conn(conn)

    held = 0
    try:
        while not pdf.SYNC_PROCESS_SLOTS.locked():
            await pdf.SYNC_PROCESS_SLOTS.acquire()
            held += 1
        async with _process_pdf_client(_process_pdf_app(pool, user_id=1)) as client:
            resp = await client.post("/api/process-pdf/1?sync=true")
    finally:
        for _ in range(held):
            pdf.SYNC_PROCESS_SLOTS.release()

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert pool.acquire.call_count == 0, "a refused request must not consume a pool connection"
