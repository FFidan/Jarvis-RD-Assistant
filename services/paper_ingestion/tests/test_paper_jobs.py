"""Tests for paper job handlers in app/paper_jobs.py.

Focused on the _SubCtx wiring: _paper_process_job must forward a
_SubCtx(ctx, 0.1, 1.0) to run_process_pdf so inner progress is scaled
into the outer 0.1→1.0 range.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub objects — created at module scope so tests can mutate their attributes,
# but NOT installed into sys.modules here (that would pollute collection).
# ---------------------------------------------------------------------------

_pdf_proc_stub = MagicMock()
_pdf_proc_stub.PDF_STORAGE_PATH = "/data/pdfs"

_main_stub = MagicMock()
_workflow_stub = MagicMock()


# ---------------------------------------------------------------------------
# Autouse fixture: install stubs + re-import stubbed module each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _install_stubs(monkeypatch):
    """Install heavy-module stubs into sys.modules for the duration of each test.

    monkeypatch.setitem auto-reverses on teardown, so sys.modules stays clean
    for test files collected/run after this one.
    """
    # Reset shared stubs so mutations from previous tests don't bleed through.
    _pdf_proc_stub.reset_mock()
    _pdf_proc_stub.PDF_STORAGE_PATH = "/data/pdfs"
    _main_stub.reset_mock()
    _workflow_stub.reset_mock()

    monkeypatch.setitem(sys.modules, "paper_ingestion.pdf_processor", _pdf_proc_stub)
    monkeypatch.setitem(sys.modules, "paper_ingestion.main", _main_stub)
    monkeypatch.setitem(sys.modules, "paper_ingestion.services", MagicMock())
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", _workflow_stub)
    # Force re-import of paper_jobs so it resolves against the freshly installed stubs.
    monkeypatch.delitem(sys.modules, "paper_ingestion.paper_jobs", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx() -> MagicMock:
    """Return a minimal JobContext-shaped mock."""
    ctx = MagicMock()
    ctx.update_progress = AsyncMock()
    ctx.is_cancelled = AsyncMock(return_value=False)
    return ctx


def _make_pool(row: dict) -> MagicMock:
    """Return an asyncpg pool mock that yields *row* from fetchrow."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_process_job_passes_sub_ctx_to_run_process_pdf(tmp_path):
    """_paper_process_job must forward a _SubCtx to run_process_pdf as ctx= kwarg."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_process_job, _SubCtx  # noqa: PLC0415

    # Create a PDF stub file so exists() passes.
    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")

    row = {"id": 42, "pdf_downloaded": True, "pdf_local_path": str(pdf_file)}
    pool = _make_pool(row)
    ctx = _make_ctx()

    mock_run = AsyncMock(return_value={"status": "processed", "chunk_count": 5})

    # Override PDF_STORAGE_PATH to tmp_path so path-traversal check passes.
    original_storage = pj.PDF_STORAGE_PATH
    pj.PDF_STORAGE_PATH = str(tmp_path)

    # app.main.app.state needs pdf_processor and embedder attributes.
    _main_stub.app = MagicMock()
    _main_stub.app.state.pdf_processor = MagicMock()
    _main_stub.app.state.embedder = MagicMock()

    # Inject our mock into both namespaces the handler might resolve.
    _workflow_stub.run_process_pdf = mock_run
    pj.run_process_pdf = mock_run  # type: ignore[attr-defined]  # patched locally

    try:
        await _paper_process_job(
            pool=pool,
            http_client=MagicMock(),
            payload={"paper_id": 42, "force": False},
            ctx=ctx,
        )
    finally:
        pj.PDF_STORAGE_PATH = original_storage

    mock_run.assert_awaited_once()
    assert mock_run.await_args is not None
    call_kwargs = mock_run.await_args.kwargs
    assert "ctx" in call_kwargs, (
        f"run_process_pdf was not called with ctx= kwarg; actual kwargs: {list(call_kwargs.keys())}"
    )
    assert isinstance(call_kwargs["ctx"], _SubCtx), (
        f"Expected _SubCtx instance, got {type(call_kwargs['ctx'])}"
    )


@pytest.mark.asyncio
async def test_paper_process_job_sub_ctx_scales_progress(tmp_path):
    """_SubCtx(ctx, 0.1, 1.0): inner=0.5 must produce outer=0.55 on the real ctx."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_process_job, _SubCtx  # noqa: PLC0415

    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")

    row = {"id": 99, "pdf_downloaded": True, "pdf_local_path": str(pdf_file)}
    pool = _make_pool(row)
    ctx = _make_ctx()

    captured: list[_SubCtx] = []

    async def _capturing_run(*args, **kwargs):
        sub = kwargs.get("ctx")
        if sub is not None:
            captured.append(sub)
            await sub.update_progress(0.5, "midpoint")
        return {"status": "processed", "chunk_count": 3}

    original_storage = pj.PDF_STORAGE_PATH
    pj.PDF_STORAGE_PATH = str(tmp_path)
    _main_stub.app = MagicMock()
    _main_stub.app.state.pdf_processor = MagicMock()
    _main_stub.app.state.embedder = MagicMock()
    _workflow_stub.run_process_pdf = _capturing_run
    pj.run_process_pdf = _capturing_run  # type: ignore[attr-defined]

    try:
        await _paper_process_job(
            pool=pool,
            http_client=MagicMock(),
            payload={"paper_id": 99, "force": False},
            ctx=ctx,
        )
    finally:
        pj.PDF_STORAGE_PATH = original_storage

    assert len(captured) == 1, "Expected exactly one ctx to be captured"

    outer_calls = [c.args[0] for c in ctx.update_progress.await_args_list]
    assert pytest.approx(0.55, abs=1e-9) in outer_calls, (
        f"Expected scaled value 0.55 in outer ctx.update_progress calls: {outer_calls}"
    )


@pytest.mark.asyncio
async def test_sub_ctx_scaling_math():
    """Unit-test _SubCtx.update_progress arithmetic in isolation."""
    from paper_ingestion.paper_jobs import _SubCtx  # noqa: PLC0415

    outer_ctx = _make_ctx()
    sub = _SubCtx(outer_ctx, 0.1, 1.0)

    # inner=0.0 → outer=0.1
    await sub.update_progress(0.0, "start")
    assert pytest.approx(outer_ctx.update_progress.await_args_list[-1].args[0], abs=1e-9) == 0.1

    # inner=0.5 → outer=0.1 + 0.5*(1.0-0.1) = 0.55
    await sub.update_progress(0.5, "mid")
    assert pytest.approx(outer_ctx.update_progress.await_args_list[-1].args[0], abs=1e-9) == 0.55

    # inner=1.0 → outer=1.0
    await sub.update_progress(1.0, "done")
    assert pytest.approx(outer_ctx.update_progress.await_args_list[-1].args[0], abs=1e-9) == 1.0
