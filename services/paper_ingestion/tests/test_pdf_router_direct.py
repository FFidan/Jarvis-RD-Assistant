"""Direct tests for the PDF router's high-risk branches."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.dependencies import utils as fastapi_dependency_utils

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
fastapi_dependency_utils.ensure_multipart_is_installed = lambda: None

from paper_ingestion.routers import pdf  # noqa: E402
from paper_ingestion.services import local_pdfs  # noqa: E402


class FakeRecord(dict):
    """Dict-like asyncpg.Record substitute for router tests."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _make_pool(conn):
    """Return a pool mock whose acquire() yields the given connection."""
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _request_with_state(**state_values):
    """Build a minimal request object with app.state members."""
    state = SimpleNamespace(**state_values)
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.asyncio
async def test_download_pdf_returns_existing_row_when_already_downloaded():
    """download_pdf should short-circuit when the paper already has a local PDF."""
    conn = AsyncMock()
    conn.fetchrow.return_value = FakeRecord(
        id=1,
        title="Paper",
        external_id="arxiv:1",
        source_type="arxiv",
        authors=["Ada"],
        abstract="A paper",
        published_date=None,
        url="https://arxiv.org/abs/1",
        pdf_url="https://arxiv.org/pdf/1.pdf",
        pdf_downloaded=True,
        pdf_local_path="/data/pdfs/1.pdf",
        citation_count=0,
        metadata={},
        created_at=datetime(2026, 3, 11, tzinfo=UTC),
    )
    pool = _make_pool(conn)
    processor = AsyncMock()

    response = await pdf.download_pdf.__wrapped__(
        MagicMock(),
        paper_id=1,
        db_pool=pool,
        pdf_processor=processor,
    )

    assert response.pdf_downloaded is True
    processor.download_pdf.assert_not_called()


@pytest.mark.asyncio
async def test_download_pdf_maps_upstream_http_failure_to_502():
    """download_pdf should convert HTTPStatusError into a stable API error."""
    conn = AsyncMock()
    conn.fetchrow.return_value = FakeRecord(
        id=1,
        title="Paper",
        external_id="arxiv:1",
        source_type="arxiv",
        authors=["Ada"],
        abstract="A paper",
        published_date=None,
        url="https://arxiv.org/abs/1",
        pdf_url="https://arxiv.org/pdf/1.pdf",
        pdf_downloaded=False,
        pdf_local_path=None,
        citation_count=0,
        metadata={},
        created_at="2026-03-11T00:00:00Z",
    )
    pool = _make_pool(conn)
    processor = AsyncMock()
    processor.download_pdf.side_effect = httpx.HTTPStatusError(
        "bad gateway",
        request=httpx.Request("GET", "https://arxiv.org/pdf/1.pdf"),
        response=httpx.Response(502),
    )

    with pytest.raises(HTTPException, match="PDF download failed") as exc_info:
        await pdf.download_pdf.__wrapped__(
            MagicMock(),
            paper_id=1,
            db_pool=pool,
            pdf_processor=processor,
        )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_download_pdf_rejects_unowned_paper(monkeypatch):
    """download_pdf must enforce the same canonical-corpus ownership guard as other paper actions."""
    conn = AsyncMock()
    conn.fetchrow.return_value = FakeRecord(
        id=1,
        title="Paper",
        external_id="arxiv:1",
        source_type="arxiv",
        authors=["Ada"],
        abstract="A paper",
        published_date=None,
        url="https://arxiv.org/abs/1",
        pdf_url="https://arxiv.org/pdf/1.pdf",
        pdf_downloaded=False,
        pdf_local_path=None,
        citation_count=0,
        metadata={},
        created_at="2026-03-11T00:00:00Z",
    )
    pool = _make_pool(conn)
    processor = AsyncMock()
    monkeypatch.setattr(pdf, "current_user_id_strict", AsyncMock(return_value=99))
    deny = HTTPException(status_code=403, detail="paper not owned by current user")
    ownership = AsyncMock(side_effect=deny)
    monkeypatch.setattr(pdf, "assert_paper_ownership", ownership)

    with pytest.raises(HTTPException) as exc_info:
        await pdf.download_pdf.__wrapped__(
            MagicMock(),
            paper_id=1,
            db_pool=pool,
            pdf_processor=processor,
        )

    assert exc_info.value.status_code == 403
    ownership.assert_awaited_once_with(conn, 1, 99)
    processor.download_pdf.assert_not_called()


@pytest.mark.asyncio
async def test_process_pdf_rejects_paths_outside_storage(tmp_path, monkeypatch):
    """process_pdf should reject paths that escape the configured storage root."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    outside_file = tmp_path / "outside.pdf"
    outside_file.write_bytes(b"%PDF-1.7\n")

    conn = AsyncMock()
    conn.fetchrow.return_value = FakeRecord(
        id=1,
        pdf_downloaded=True,
        pdf_local_path=str(outside_file),
    )
    pool = _make_pool(conn)
    request = _request_with_state(pdf_processor=MagicMock(), embedder=MagicMock())
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    embedder = MagicMock()

    pdf_processor = MagicMock()
    with pytest.raises(HTTPException, match="Invalid PDF path") as exc_info:
        await pdf.process_pdf.__wrapped__(
            request,
            paper_id=1,
            force=False,
            sync=True,  # use sync path to exercise path-traversal protection
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_process_pdf_sync_rejects_unowned_paper(tmp_path, monkeypatch):
    """The synchronous backward-compat path must not bypass paper ownership."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    paper_path = storage_dir / "1.pdf"
    paper_path.write_bytes(b"%PDF-1.7\ncontent")

    conn = AsyncMock()
    conn.fetchrow.return_value = FakeRecord(
        id=1,
        pdf_downloaded=True,
        pdf_local_path=str(paper_path),
    )
    pool = _make_pool(conn)
    request = _request_with_state(pdf_processor=MagicMock(), embedder=MagicMock())
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(pdf, "current_user_id_strict", AsyncMock(return_value=99))
    deny = HTTPException(status_code=403, detail="paper not owned by current user")
    ownership = AsyncMock(side_effect=deny)
    monkeypatch.setattr(pdf, "assert_paper_ownership", ownership)
    run_process_pdf = AsyncMock()
    monkeypatch.setattr(pdf, "run_process_pdf", run_process_pdf)

    with pytest.raises(HTTPException) as exc_info:
        await pdf.process_pdf.__wrapped__(
            request,
            paper_id=1,
            force=False,
            sync=True,
            db_pool=pool,
            pdf_processor=MagicMock(),
            embedder=MagicMock(),
        )

    assert exc_info.value.status_code == 403
    ownership.assert_awaited_once_with(conn, 1, 99)
    run_process_pdf.assert_not_called()


@pytest.mark.asyncio
async def test_process_pdf_delegates_to_run_process_pdf(tmp_path, monkeypatch):
    """process_pdf with sync=True should hand valid requests to the shared workflow helper."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    paper_path = storage_dir / "1.pdf"
    paper_path.write_bytes(b"%PDF-1.7\ncontent")

    conn = AsyncMock()
    conn.fetchrow.return_value = FakeRecord(
        id=1,
        pdf_downloaded=True,
        pdf_local_path=str(paper_path),
    )
    pool = _make_pool(conn)
    processor = MagicMock()
    embedder = MagicMock()
    request = _request_with_state(pdf_processor=processor, embedder=embedder)
    run_process_pdf = AsyncMock(
        return_value={"paper_id": 1, "chunk_count": 8, "status": "processed"}
    )

    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(pdf, "run_process_pdf", run_process_pdf)

    # sync=True exercises the synchronous (backward-compat) code path
    result = await pdf.process_pdf.__wrapped__(
        request,
        paper_id=1,
        force=True,
        sync=True,
        db_pool=pool,
        pdf_processor=processor,
        embedder=embedder,
    )

    assert result == {"paper_id": 1, "chunk_count": 8, "status": "processed"}
    run_process_pdf.assert_awaited_once_with(1, paper_path, pool, processor, embedder, force=True)


@pytest.mark.asyncio
async def test_process_pdf_async_enqueues_job():
    """process_pdf without sync=True (default) defers a paper_process task."""
    from unittest.mock import patch as mock_patch

    fake_uuid = "test-job-uuid"
    request = _request_with_state(pdf_processor=MagicMock(), embedder=MagicMock())
    pool = MagicMock()  # not used in async path — defer_async is patched

    import jarvis_common.task_registry as task_registry

    mock_task = MagicMock()
    mock_defer = AsyncMock()
    mock_task.defer_async = mock_defer
    with (
        mock_patch.dict(task_registry.KIND_TO_TASK, {"paper.process": mock_task}),
        mock_patch("uuid.uuid4", return_value=fake_uuid),
    ):
        result = await pdf.process_pdf.__wrapped__(
            request,
            paper_id=42,
            force=False,
            sync=False,
            db_pool=pool,
            embedder=MagicMock(),
        )

    assert result["job_id"] == fake_uuid
    assert result["status"] == "queued"
    mock_defer.assert_awaited_once_with(job_id=fake_uuid, user_id=1, paper_id=42, force=False)


@pytest.mark.asyncio
async def test_scan_local_pdfs_skips_symlinks_and_non_pdfs(tmp_path, monkeypatch):
    """scan_local_pdfs should count malformed local files as skipped imports."""
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    valid_pdf = scan_dir / "paper.pdf"
    valid_pdf.write_bytes(b"%PDF-1.7\nvalid")
    invalid_pdf = scan_dir / "not-really.pdf"
    invalid_pdf.write_bytes(b"plain text")
    symlink_pdf = scan_dir / "linked.pdf"
    symlink_pdf.symlink_to(valid_pdf)

    conn = AsyncMock()
    conn.fetchrow.side_effect = [None]
    conn.fetchrow.return_value = None
    conn.execute = AsyncMock()
    pool = _make_pool(conn)

    inserted_row = FakeRecord(id=7)
    conn.fetchrow.side_effect = [None, inserted_row]

    monkeypatch.setattr(local_pdfs, "LOCAL_PDF_SCAN_DIR", str(scan_dir))
    monkeypatch.setattr(local_pdfs, "PDF_STORAGE_PATH", str(storage_dir))

    result = await local_pdfs.scan_local_pdf_directory(pool)

    assert result["scanned"] == 3
    assert result["imported"] == 1
    assert result["skipped"] == 2
    assert (storage_dir / "7.pdf").exists()


@pytest.mark.asyncio
async def test_batch_process_papers_skips_invalid_and_missing_paths(tmp_path, monkeypatch):
    """batch_process_papers should enqueue a single job for valid papers only."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    good_pdf = storage_dir / "10.pdf"
    good_pdf.write_bytes(b"%PDF-1.7\nvalid")
    missing_pdf = storage_dir / "11.pdf"
    outside_pdf = tmp_path / "outside.pdf"
    outside_pdf.write_bytes(b"%PDF-1.7\noutside")

    conn = AsyncMock()
    conn.fetch.return_value = [
        {"id": 10, "pdf_local_path": str(good_pdf)},
        {"id": 11, "pdf_local_path": str(missing_pdf)},
        {"id": 12, "pdf_local_path": str(outside_pdf)},
    ]
    pool = _make_pool(conn)
    request = _request_with_state(pdf_processor=MagicMock(), embedder=MagicMock())

    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))

    fake_uuid = "job-abc123"
    mock_defer = AsyncMock()

    from unittest.mock import patch as mock_patch  # noqa: PLC0415

    import jarvis_common.task_registry as task_registry

    mock_task_bp = MagicMock()
    mock_task_bp.defer_async = mock_defer
    with (
        mock_patch.dict(task_registry.KIND_TO_TASK, {"papers.batch_process": mock_task_bp}),
        mock_patch("uuid.uuid4", return_value=fake_uuid),
    ):
        result = await pdf.batch_process_papers.__wrapped__(
            request,
            limit=10,
            force=False,
            db_pool=pool,
        )

    assert result == {
        "queued": 1,
        "total_unprocessed": 3,
        "skipped_missing_pdf": 2,
        "job_id": fake_uuid,
    }
    mock_defer.assert_awaited_once_with(job_id=fake_uuid, user_id=1, paper_ids=[10], force=False)


# ---------------------------------------------------------------------------
# H10: batch_process_papers must scope to user_library when user_id is set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_process_papers_scopes_to_user_library(tmp_path, monkeypatch):
    """batch_process_papers must only process papers in the caller's user_library.

    H10 audit finding: when user_id is not None the SQL must JOIN user_library
    so that user A cannot trigger re-embedding of the whole corpus.

    Setup: corpus has 5 downloaded papers; user A owns 2 of them.
    The conn.fetch mock returns only those 2 rows (simulating the JOIN).
    We assert:
      - conn.fetch was called with a query that contains 'user_library'
      - only 2 papers were queued
      - the defer call carries user_id=7
    """
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()

    # Two PDFs that belong to user A
    pdf_a = storage_dir / "20.pdf"
    pdf_a.write_bytes(b"%PDF-1.7\nuser_a_paper")
    pdf_b = storage_dir / "21.pdf"
    pdf_b.write_bytes(b"%PDF-1.7\nuser_a_paper_2")

    # conn.fetch simulates the DB returning only user A's 2 papers
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"id": 20, "pdf_local_path": str(pdf_a)},
        {"id": 21, "pdf_local_path": str(pdf_b)},
    ]
    pool = _make_pool(conn)
    request = _request_with_state(pdf_processor=MagicMock(), embedder=MagicMock())

    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(pdf, "current_user_id_strict", AsyncMock(return_value=7))

    fake_uuid = "job-scoped-123"
    mock_defer = AsyncMock()

    from unittest.mock import patch as mock_patch  # noqa: PLC0415

    import jarvis_common.task_registry as task_registry

    mock_task_bp = MagicMock()
    mock_task_bp.defer_async = mock_defer
    with (
        mock_patch.dict(task_registry.KIND_TO_TASK, {"papers.batch_process": mock_task_bp}),
        mock_patch("uuid.uuid4", return_value=fake_uuid),
    ):
        result = await pdf.batch_process_papers.__wrapped__(
            request,
            limit=10,
            force=False,
            db_pool=pool,
        )

    # Only the 2 user-library papers should have been queued
    assert result["queued"] == 2
    assert result["total_unprocessed"] == 2
    assert result["skipped_missing_pdf"] == 0
    assert result["job_id"] == fake_uuid

    # The SQL sent to the DB must include user_library JOIN
    fetch_call_sql = conn.fetch.await_args_list[0].args[0]
    assert "user_library" in fetch_call_sql, (
        "batch_process_papers must JOIN user_library when user_id is set"
    )

    # user_id must be threaded to the job
    mock_defer.assert_awaited_once_with(
        job_id=fake_uuid, user_id=7, paper_ids=[20, 21], force=False
    )


# ---------------------------------------------------------------------------
# PI-015: upload_pdf atomicity — dangling-file rollback on UPDATE failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_pdf_unlinks_renamed_file_on_db_update_failure(tmp_path, monkeypatch):
    """upload_pdf must remove the renamed pdf file if the UPDATE papers fails.

    PI-015: after temp_path.rename(pdf_path) the UPDATE can fail; the
    transaction rolls back the DB insert but the file stays on disk.
    The fix wraps the UPDATE in try/except and unlinks pdf_path on failure.
    """
    import io

    from fastapi import UploadFile

    storage_dir = tmp_path / "pdfs"
    storage_dir.mkdir()
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))

    # Build a minimal PDF UploadFile (valid %PDF- header)
    pdf_content = b"%PDF-1.7\n" + b"x" * 100
    upload_file = UploadFile(filename="paper.pdf", file=io.BytesIO(pdf_content))

    # Conn: INSERT returns a row, but UPDATE raises
    inserted_row = FakeRecord(
        id=99,
        external_id="local:abc123",
        source_type="local",
        title="Test Paper",
        authors=[],
        abstract=None,
        published_date=None,
        url="local://abc123",
        pdf_url=None,
        pdf_downloaded=False,
        pdf_local_path=None,
        citation_count=0,
        metadata={},
        created_at=None,
    )

    conn = AsyncMock()
    # First fetchrow → None (duplicate check), second fetchrow → inserted_row (INSERT)
    conn.fetchrow = AsyncMock(side_effect=[None, inserted_row, RuntimeError("UPDATE exploded")])

    pool = _make_pool(conn)
    request = MagicMock()

    with pytest.raises((RuntimeError, Exception)):
        await pdf.upload_pdf.__wrapped__(
            request,
            file=upload_file,
            title="Test Paper",
            authors="",
            abstract="",
            db_pool=pool,
        )

    # The renamed file (storage_dir/99.pdf) must have been cleaned up
    assert not (storage_dir / "99.pdf").exists(), (
        "Dangling file left on disk after DB UPDATE failure"
    )


@pytest.mark.asyncio
async def test_upload_pdf_authenticated_user_stamps_discoverer_and_library(tmp_path, monkeypatch):
    """Authenticated uploads must be private library entries, not global system papers."""
    import io

    from fastapi import UploadFile

    storage_dir = tmp_path / "pdfs"
    storage_dir.mkdir()
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(pdf, "current_user_id_strict", AsyncMock(return_value=42))
    add_to_library = AsyncMock()
    monkeypatch.setattr(pdf, "add_to_library", add_to_library, raising=False)

    pdf_content = b"%PDF-1.7\n" + b"x" * 100
    upload_file = UploadFile(filename="paper.pdf", file=io.BytesIO(pdf_content))
    inserted_row = FakeRecord(
        id=101,
        external_id="local:abc123",
        source_type="local",
        title="Private Upload",
        authors=[],
        abstract=None,
        published_date=None,
        url="local://abc123",
        pdf_url=None,
        pdf_downloaded=False,
        pdf_local_path=None,
        citation_count=0,
        metadata={},
        created_at=datetime(2026, 5, 12, tzinfo=UTC),
        discovered_by=42,
    )
    updated_row = FakeRecord({**inserted_row, "pdf_downloaded": True, "pdf_local_path": "x"})

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, inserted_row, updated_row])
    pool = _make_pool(conn)

    await pdf.upload_pdf.__wrapped__(
        MagicMock(),
        file=upload_file,
        title="Private Upload",
        authors="",
        abstract="",
        db_pool=pool,
    )

    insert_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "discovered_by" in insert_sql
    assert conn.fetchrow.await_args_list[1].args[-1] == 42
    add_to_library.assert_awaited_once_with(
        conn,
        user_id=42,
        paper_id=101,
        added_via="manual_save",
    )


@pytest.mark.asyncio
async def test_upload_pdf_single_user_mode_does_not_write_library(tmp_path, monkeypatch):
    """API-key/single-user uploads keep the legacy NULL-user behavior."""
    import io

    from fastapi import UploadFile

    storage_dir = tmp_path / "pdfs"
    storage_dir.mkdir()
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(pdf, "current_user_id_strict", AsyncMock(return_value=None))
    add_to_library = AsyncMock()
    monkeypatch.setattr(pdf, "add_to_library", add_to_library, raising=False)

    pdf_content = b"%PDF-1.7\n" + b"x" * 100
    upload_file = UploadFile(filename="paper.pdf", file=io.BytesIO(pdf_content))
    inserted_row = FakeRecord(
        id=102,
        external_id="local:abc123",
        source_type="local",
        title="Legacy Upload",
        authors=[],
        abstract=None,
        published_date=None,
        url="local://abc123",
        pdf_url=None,
        pdf_downloaded=False,
        pdf_local_path=None,
        citation_count=0,
        metadata={},
        created_at=datetime(2026, 5, 12, tzinfo=UTC),
        discovered_by=None,
    )
    updated_row = FakeRecord({**inserted_row, "pdf_downloaded": True, "pdf_local_path": "x"})

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, inserted_row, updated_row])
    pool = _make_pool(conn)

    await pdf.upload_pdf.__wrapped__(
        MagicMock(),
        file=upload_file,
        title="Legacy Upload",
        authors="",
        abstract="",
        db_pool=pool,
    )

    add_to_library.assert_not_awaited()


# ---------------------------------------------------------------------------
# API-001 — process-pdf async branch must return 200 with {job_id, status}
# (response_model=ProcessPdfResponse was causing 500 ResponseValidationError)
# ---------------------------------------------------------------------------


def test_process_pdf_async_response_model_no_500():
    """API-001: POST /api/process-pdf/{id} default (sync=False) must return 200.

    Before the fix, FastAPI would validate the returned dict {job_id, status}
    against ProcessPdfResponse (which requires paper_id + chunk_count) and raise
    a ResponseValidationError → 500.  After dropping response_model the route
    serialises whatever dict is returned without validation.
    """
    from unittest.mock import patch as mock_patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from paper_ingestion.deps import get_db_pool, get_embedder, get_pdf_processor
    from paper_ingestion.routers.pdf import router

    app = FastAPI()
    app.include_router(router)

    fake_job_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    fake_pool = MagicMock()

    # Override FastAPI dependencies so no real DB/embedder is needed
    app.dependency_overrides[get_db_pool] = lambda: fake_pool
    app.dependency_overrides[get_pdf_processor] = lambda: MagicMock()
    app.dependency_overrides[get_embedder] = lambda: MagicMock()

    import jarvis_common.task_registry as task_registry

    mock_task_proc = MagicMock()
    mock_task_proc.defer_async = AsyncMock()
    with (
        mock_patch.dict(task_registry.KIND_TO_TASK, {"paper.process": mock_task_proc}),
        mock_patch("uuid.uuid4", return_value=fake_job_id),
        TestClient(app, raise_server_exceptions=True) as client,
    ):
        resp = client.post("/api/process-pdf/1")  # sync defaults to False

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["job_id"] == fake_job_id
    assert body["status"] == "queued"


# ---------------------------------------------------------------------------
# DOM-A-04: download_pdf null-row guard must run before ownership check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_paper_ownership_runs_after_null_row_guard_in_pdf_router(monkeypatch):
    """download_pdf must return 404 for unknown paper_id before calling ownership.

    DOM-A-04: previously assert_paper_ownership was called while row was still
    None (fetchrow returned None), causing it to run on a non-existent paper.
    After the fix, the null-row guard raises HTTPException(404) first.
    """
    conn = AsyncMock()
    conn.fetchrow.return_value = None  # paper does not exist

    pool = _make_pool(conn)

    # Ownership must never be called for a non-existent paper
    ownership = AsyncMock()
    monkeypatch.setattr(pdf, "assert_paper_ownership", ownership)
    monkeypatch.setattr(pdf, "current_user_id_strict", AsyncMock(return_value=99))

    processor = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await pdf.download_pdf.__wrapped__(
            MagicMock(),
            paper_id=9999,
            db_pool=pool,
            pdf_processor=processor,
        )

    assert exc_info.value.status_code == 404
    assert "Paper not found" in exc_info.value.detail
    # Ownership must NOT have been called — the null guard fires first
    ownership.assert_not_awaited()


# ---------------------------------------------------------------------------
# DOM-A-10: upload_pdf form fields must enforce max_length limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_pdf_rejects_oversized_title(tmp_path, monkeypatch):
    """upload_pdf must reject a title longer than 500 characters with HTTP 422.

    DOM-A-10: without max_length on the Form(...) parameter, an oversized title
    would reach the DB and cause either a silent truncation or a constraint
    violation.  After adding max_length=500 FastAPI/Pydantic rejects the request
    before the handler body executes.
    """
    import io

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from paper_ingestion.deps import get_db_pool, get_embedder, get_pdf_processor
    from paper_ingestion.routers.pdf import router

    app = FastAPI()
    app.include_router(router)

    fake_pool = MagicMock()
    app.dependency_overrides[get_db_pool] = lambda: fake_pool
    app.dependency_overrides[get_pdf_processor] = lambda: MagicMock()
    app.dependency_overrides[get_embedder] = lambda: MagicMock()

    oversized_title = "A" * 601  # exceeds 500-char limit

    pdf_bytes = b"%PDF-1.7\n" + b"x" * 100

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/upload-pdf",
            data={"title": oversized_title, "authors": "", "abstract": ""},
            files={"file": ("paper.pdf", io.BytesIO(pdf_bytes), "application/pdf")},
        )

    assert resp.status_code == 422, (
        f"Expected 422 for oversized title, got {resp.status_code}: {resp.text}"
    )
