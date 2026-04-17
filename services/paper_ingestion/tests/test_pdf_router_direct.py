"""Direct tests for the PDF router's high-risk branches."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.dependencies import utils as fastapi_dependency_utils

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "libs" / "jarvis_common"))
sys.modules.setdefault("fitz", MagicMock())
sys.modules.setdefault("tiktoken", MagicMock(get_encoding=MagicMock(return_value=MagicMock())))
if "app.embedder" not in sys.modules:
    fake_embedder = types.ModuleType("app.embedder")
    fake_embedder.Embedder = MagicMock()
    fake_embedder.COLLECTION_NAME = "paper_chunks"
    fake_embedder.EMBEDDING_MODEL_NAME = "embed-model"
    sys.modules["app.embedder"] = fake_embedder
sys.modules.setdefault("qdrant_client", MagicMock(AsyncQdrantClient=MagicMock()))
sys.modules.setdefault(
    "qdrant_client.models",
    MagicMock(
        Distance=MagicMock(),
        PointIdsList=MagicMock(),
        PointStruct=MagicMock(),
        VectorParams=MagicMock(),
    ),
)
fastapi_dependency_utils.ensure_multipart_is_installed = lambda: None

from app.routers import pdf  # noqa: E402


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
        url="https://arxiv.org/abs/1",
        pdf_url="https://arxiv.org/pdf/1.pdf",
        pdf_downloaded=False,
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

    with pytest.raises(HTTPException, match="Invalid PDF path") as exc_info:
        await pdf.process_pdf.__wrapped__(
            request,
            paper_id=1,
            force=False,
            sync=True,  # use sync path to exercise path-traversal protection
            db_pool=pool,
            embedder=embedder,
        )

    assert exc_info.value.status_code == 400


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
        embedder=embedder,
    )

    assert result == {"paper_id": 1, "chunk_count": 8, "status": "processed"}
    run_process_pdf.assert_awaited_once_with(1, paper_path, pool, processor, embedder, force=True)


@pytest.mark.asyncio
async def test_process_pdf_async_enqueues_job():
    """process_pdf without sync=True (default) enqueues a paper.process job."""
    from unittest.mock import patch as mock_patch

    job_id = "test-job-uuid"
    request = _request_with_state(pdf_processor=MagicMock(), embedder=MagicMock())
    pool = MagicMock()  # not used in async path — enqueue is patched

    # Patch jarvis_common.jobs.enqueue so no DB connection is needed
    with mock_patch("jarvis_common.jobs.enqueue", new=AsyncMock(return_value=job_id)):
        result = await pdf.process_pdf.__wrapped__(
            request,
            paper_id=42,
            force=False,
            sync=False,
            db_pool=pool,
            embedder=MagicMock(),
        )

    assert result["job_id"] == job_id
    assert result["status"] == "queued"


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

    monkeypatch.setattr(pdf, "LOCAL_PDF_SCAN_DIR", str(scan_dir))
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))

    result = await pdf.scan_local_pdfs.__wrapped__(MagicMock(), db_pool=pool)

    assert result["scanned"] == 3
    assert result["imported"] == 1
    assert result["skipped"] == 2
    assert (storage_dir / "7.pdf").exists()


@pytest.mark.asyncio
async def test_batch_process_papers_skips_invalid_and_missing_paths(tmp_path, monkeypatch):
    """batch_process_papers should only queue files inside storage that exist on disk."""
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
    background_tasks = BackgroundTasks()
    background_tasks.add_task = MagicMock()
    request = _request_with_state(pdf_processor=MagicMock(), embedder=MagicMock())

    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))

    result = await pdf.batch_process_papers.__wrapped__(
        request,
        background_tasks=background_tasks,
        limit=10,
        db_pool=pool,
    )

    assert result == {"queued": 1, "total_unprocessed": 3, "skipped_missing_pdf": 2}
    background_tasks.add_task.assert_called_once()
