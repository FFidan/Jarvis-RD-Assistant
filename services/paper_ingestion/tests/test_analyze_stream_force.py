from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jarvis_common.testing import make_pool_and_conn

from paper_ingestion.routers import analyze
from tests.conftest import FakeRecord

_PAPER_ID = 7
_HOLDER_ID = 1
_NON_HOLDER_ID = 2


def _local_paper_row(tmp_path):
    """Return a downloaded local paper row: the shape that reaches the process step.

    Any other shape routes through the download branch, which never reaches the
    process call these tests target. ``is_visible`` makes the paper public, which
    is exactly the state a force run must not be allowed to rely on.
    """
    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")
    return FakeRecord(
        id=_PAPER_ID,
        source_type="local",
        pdf_url=None,
        pdf_downloaded=True,
        pdf_local_path=str(pdf_file),
        is_visible=True,
    )


def _stub_analyze_pipeline(tmp_path, monkeypatch, *, user_id: int) -> AsyncMock:
    """Stub what the stream imports at call time; return the workflow mock.

    config, pdf_workflow and summarization are imported inside the generator, so a
    setattr on the analyze module would not be seen — the stubs must be installed
    in sys.modules to intercept the call-time import.
    """
    monkeypatch.setattr(analyze, "current_user_id_strict", AsyncMock(return_value=user_id))
    monkeypatch.setattr(analyze, "check_pdf_path_safe", lambda *a, **k: True)

    settings_stub = MagicMock()
    settings_stub.get_paper_ingestion_settings = MagicMock(
        return_value=SimpleNamespace(pdf_storage_path=str(tmp_path))
    )
    monkeypatch.setitem(sys.modules, "paper_ingestion.config", settings_stub)

    mock_rpp = AsyncMock(
        return_value={"paper_id": _PAPER_ID, "chunk_count": 3, "status": "processed"}
    )
    workflow_stub = MagicMock()
    workflow_stub.run_process_pdf = mock_rpp
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", workflow_stub)
    summ_stub = MagicMock()
    summ_stub.generate_paper_summary = AsyncMock()
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", summ_stub)
    return mock_rpp


def _analyze_app(pool, *, user_id: int):
    """Return a FastAPI app serving the analyze router against mocked dependencies."""
    from fastapi import FastAPI
    from jarvis_common.auth import current_user_id_strict

    from paper_ingestion.deps import (
        get_db_pool,
        get_embedder,
        get_http_client,
        get_pdf_processor,
        get_verifier,
    )

    # The route limiter is a process-wide singleton and every app-level test here
    # keys to the same bucket, so reset it or these tests spend the 5/minute quota.
    analyze.limiter.reset()

    app = FastAPI()
    app.include_router(analyze.router)
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_http_client] = lambda: MagicMock()
    app.dependency_overrides[get_pdf_processor] = lambda: MagicMock()
    app.dependency_overrides[get_embedder] = lambda: MagicMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()
    app.dependency_overrides[current_user_id_strict] = lambda: user_id
    return app


def _analyze_client(app):
    """Return an in-process httpx client bound to *app*."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_analyze_stream_forwards_force_to_run_process_pdf(tmp_path, monkeypatch):
    """The stream threads both the force flag and the requester into the workflow."""
    monkeypatch.setattr(analyze, "assert_paper_ownership", AsyncMock(return_value=None))
    mock_rpp = _stub_analyze_pipeline(tmp_path, monkeypatch, user_id=_HOLDER_ID)
    pool, _ = make_pool_and_conn(fetchrow_return=_local_paper_row(tmp_path))

    gen = analyze._analyze_stream(
        request=MagicMock(),
        paper_id=_PAPER_ID,
        db_pool=pool,
        http_client=MagicMock(),
        pdf_processor=MagicMock(),
        embedder=MagicMock(),
        verifier=MagicMock(),
        force=True,
    )
    async for _ in gen:
        pass

    mock_rpp.assert_awaited_once()
    rpp_call = mock_rpp.await_args
    assert rpp_call is not None
    assert rpp_call.kwargs.get("force") is True, (
        f"run_process_pdf must be called with force=True; got {rpp_call.kwargs}"
    )
    assert rpp_call.kwargs.get("requester_id") == _HOLDER_ID, (
        f"run_process_pdf must name the requester; got {rpp_call.kwargs}"
    )


@pytest.mark.asyncio
async def test_analyze_force_refuses_caller_without_library_row(tmp_path, monkeypatch):
    """A public paper the caller never saved cannot be rebuilt through analyze."""
    mock_rpp = _stub_analyze_pipeline(tmp_path, monkeypatch, user_id=_NON_HOLDER_ID)
    pool, _ = make_pool_and_conn(
        fetchrow_return=_local_paper_row(tmp_path),
        fetch_return=[],  # no user_library row for this caller
    )

    async with _analyze_client(_analyze_app(pool, user_id=_NON_HOLDER_ID)) as client:
        resp = await client.post(f"/api/papers/{_PAPER_ID}/analyze?force=true")

    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"
    assert "library" in resp.json()["detail"].lower()
    mock_rpp.assert_not_awaited()


@pytest.mark.asyncio
async def test_analyze_force_still_rebuilds_for_library_holder(tmp_path, monkeypatch):
    """The same request runs to completion once the paper is in the caller's library."""
    mock_rpp = _stub_analyze_pipeline(tmp_path, monkeypatch, user_id=_HOLDER_ID)
    pool, _ = make_pool_and_conn(
        fetchrow_return=_local_paper_row(tmp_path),
        fetch_return=[FakeRecord(id=_PAPER_ID)],  # library row present
    )

    async with _analyze_client(_analyze_app(pool, user_id=_HOLDER_ID)) as client:
        resp = await client.post(f"/api/papers/{_PAPER_ID}/analyze?force=true")

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    assert '"type": "complete"' in resp.text, f"stream did not complete: {resp.text}"
    mock_rpp.assert_awaited_once()
    assert mock_rpp.await_args.kwargs["requester_id"] == _HOLDER_ID


@pytest.mark.asyncio
async def test_analyze_async_force_refuses_caller_without_library_row(tmp_path):
    """The deferring branch refuses instead of queueing a job the worker would reject."""
    import jarvis_common.task_registry as task_registry

    pool, _ = make_pool_and_conn(
        fetchrow_return=_local_paper_row(tmp_path),
        fetch_return=[],  # no user_library row for this caller
    )

    # Registered so a refusal that stopped working surfaces as a queued 200
    # rather than an unrelated KeyError.
    task = MagicMock()
    task.defer_async = AsyncMock()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": task}):
        async with _analyze_client(_analyze_app(pool, user_id=_NON_HOLDER_ID)) as client:
            resp = await client.post(f"/api/papers/{_PAPER_ID}/analyze?async=true&force=true")

    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"
    assert "library" in resp.json()["detail"].lower()
    task.defer_async.assert_not_awaited()
