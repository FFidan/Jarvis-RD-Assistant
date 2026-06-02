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

from jarvis_common.testing import make_pool_and_conn  # noqa: E402
from paper_ingestion.routers import pdf  # noqa: E402
from paper_ingestion.services import local_pdfs  # noqa: E402
from tests.conftest import FakeRecord  # noqa: E402


def _request_with_state(**state_values):
    """Build a minimal request object with app.state members."""
    state = SimpleNamespace(**state_values)
    return SimpleNamespace(app=SimpleNamespace(state=state))


# Cluster 4 deletion (2026-05-22): superseded by test_pi_pdf_contract.py (P-01..P-07).


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
    pool, _ = make_pool_and_conn(conn=conn)
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
            user_id=1,
        )

    assert exc_info.value.status_code == 502


# Cluster 4 deletion (2026-05-22): superseded by test_pi_pdf_contract.py (P-01..P-07).


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
    pool, _ = make_pool_and_conn(conn=conn)
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
            user_id=1,
        )

    assert exc_info.value.status_code == 400


# Cluster 4 deletion (2026-05-22): superseded by test_pi_pdf_contract.py (P-01..P-07).
# PR5-T5 traversal coverage: the route-level guard is exercised by
# test_process_pdf_rejects_paths_outside_storage above (now via check_pdf_path_safe),
# and the helper's `..`/outside/absolute cases are unit-tested in test_pdf_processor.py.


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
    pool, _ = make_pool_and_conn(conn=conn)
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
        user_id=1,
    )

    assert result == {"paper_id": 1, "chunk_count": 8, "status": "processed"}
    run_process_pdf.assert_awaited_once_with(1, paper_path, pool, processor, embedder, force=True)


# Cluster 4 deletion (2026-05-22): superseded by test_pi_pdf_contract.py (P-01..P-07).


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
    pool, _ = make_pool_and_conn(conn=conn)

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
    pool, _ = make_pool_and_conn(conn=conn)
    request = _request_with_state(pdf_processor=MagicMock(), embedder=MagicMock())

    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))

    fake_uuid = "job-abc123"
    mock_defer = AsyncMock()

    from unittest.mock import patch as mock_patch  # noqa: PLC0415

    import jarvis_common.task_registry as task_registry

    mock_task_bp = MagicMock()
    mock_task_bp.defer_async = mock_defer
    with (
        mock_patch.dict(task_registry._TASK_MAP, {"papers.batch_process": mock_task_bp}),
        mock_patch("uuid.uuid4", return_value=fake_uuid),
    ):
        result = await pdf.batch_process_papers.__wrapped__(
            request,
            limit=10,
            force=False,
            db_pool=pool,
            user_id=1,
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

# Cluster 4 deletion (2026-05-22): superseded by test_pi_pdf_contract.py (P-01..P-07).


# Cluster 4 deletion (2026-05-22): superseded by test_pi_pdf_contract.py (P-01..P-07).


# Cluster 4 deletion (2026-05-22): superseded by test_pi_pdf_contract.py (P-01..P-07).


# ---------------------------------------------------------------------------
# dev-mode inline refactor smoke tests (structural, behaviour unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_pdf_dev_mode_exposes_error_detail(tmp_path, monkeypatch):
    """When get_core_settings().dev_mode is True, RuntimeError detail is exposed."""
    from jarvis_common.settings import CoreSettings
    from unittest.mock import patch as mock_patch

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
    pool, _ = make_pool_and_conn(conn=conn)
    processor = MagicMock()
    embedder = MagicMock()
    request = _request_with_state(pdf_processor=processor, embedder=embedder)
    request.state = SimpleNamespace(request_id="req-test-1")

    boom = RuntimeError("internal failure detail")
    run_process_pdf_mock = AsyncMock(side_effect=boom)

    dev_settings = CoreSettings(dev_mode=True)

    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(pdf, "run_process_pdf", run_process_pdf_mock)

    with mock_patch("paper_ingestion.routers.pdf.get_core_settings", return_value=dev_settings):
        with pytest.raises(HTTPException) as exc_info:
            await pdf.process_pdf.__wrapped__(
                request,
                paper_id=1,
                force=False,
                sync=True,
                db_pool=pool,
                pdf_processor=processor,
                embedder=embedder,
                user_id=1,
            )

    assert exc_info.value.status_code == 502
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert "internal failure detail" in detail["detail"]
    assert detail["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_debug_pulse_returns_404_when_dev_mode_false():
    """debug_pulse must return HTTP 404 when get_core_settings().dev_mode is False."""
    from jarvis_common.settings import CoreSettings
    from unittest.mock import patch as mock_patch
    from paper_ingestion.routers import pulse

    prod_settings = CoreSettings(dev_mode=False)

    with mock_patch("paper_ingestion.routers.pulse.get_core_settings", return_value=prod_settings):
        with pytest.raises(HTTPException) as exc_info:
            await pulse.debug_pulse.__wrapped__(
                MagicMock(),
                db_pool=MagicMock(),
                caller_id=1,
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_upload_pdf_single_user_mode_does_not_write_library(tmp_path, monkeypatch):
    """API-key/single-user uploads keep the legacy NULL-user behavior."""
    import io

    from fastapi import UploadFile

    storage_dir = tmp_path / "pdfs"
    storage_dir.mkdir()
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
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
    pool, _ = make_pool_and_conn(conn=conn)

    await pdf.upload_pdf.__wrapped__(
        MagicMock(),
        file=upload_file,
        title="Legacy Upload",
        authors="",
        abstract="",
        db_pool=pool,
        user_id=None,
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
    from jarvis_common.auth import current_user_id_strict
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
    # process_pdf resolves the caller via Depends(current_user_id_strict).
    app.dependency_overrides[current_user_id_strict] = lambda: 1

    import jarvis_common.task_registry as task_registry

    mock_task_proc = MagicMock()
    mock_task_proc.defer_async = AsyncMock()
    with (
        mock_patch.dict(task_registry._TASK_MAP, {"paper.process": mock_task_proc}),
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

# Cluster 4 deletion (2026-05-22): superseded by test_pi_pdf_contract.py (P-01..P-07).


# Cluster 4 deletion (2026-05-22): superseded by test_pi_pdf_contract.py (P-01..P-07).
