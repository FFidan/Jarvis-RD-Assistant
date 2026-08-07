"""Tests for paper job handlers in app/paper_jobs.py.

Focused on the _SubCtx wiring: _paper_process_job must forward a
_SubCtx(ctx, 0.1, 1.0) to run_process_pdf so inner progress is scaled
into the outer 0.1→1.0 range.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import httpx
import pytest
from jarvis_common.testing import make_pool_and_conn

# ---------------------------------------------------------------------------
# Stub objects — created at module scope so tests can mutate their attributes,
# but NOT installed into sys.modules here (that would pollute collection).
# ---------------------------------------------------------------------------

# Import the real path-resolution guard so the stubbed pdf_processor module
# exposes genuine traversal semantics (a bare MagicMock attribute would always
# be truthy and silently disable the guard).
from paper_ingestion.ingestion.payload_schema import VectorVisibility
from paper_ingestion.pdf_processor import resolve_safe_pdf_path as _real_resolve_safe_pdf_path
from paper_ingestion.routers import jobs as _jobs_router_module
from paper_ingestion.services import embedding_reconcile as _real_embedding_reconcile
from paper_ingestion.services import pdf_workflow as _real_pdf_workflow
from paper_ingestion.services.pdf_workflow import (
    PDFRecordMissingError as _RealPDFRecordMissingError,
)

_pdf_proc_stub = MagicMock()
_pdf_proc_stub.PDF_STORAGE_PATH = "/data/pdfs"
_pdf_proc_stub.resolve_safe_pdf_path = _real_resolve_safe_pdf_path

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
    _pdf_proc_stub.resolve_safe_pdf_path = _real_resolve_safe_pdf_path
    _main_stub.reset_mock()
    _workflow_stub.reset_mock()
    _workflow_stub.PDFRecordMissingError = _RealPDFRecordMissingError
    # Resolved from the live module rather than bound at import time. The
    # classes are defined in services.pdf_errors and re-imported unchanged by
    # a pdf_workflow reload, so both bindings stay identical either way.
    _workflow_stub.PDFRebuildNotPermittedError = _real_pdf_workflow.PDFRebuildNotPermittedError
    _workflow_stub.PDFUserFacingError = _real_pdf_workflow.PDFUserFacingError
    _workflow_stub.download_and_store_pdf = AsyncMock()

    monkeypatch.setitem(sys.modules, "paper_ingestion.pdf_processor", _pdf_proc_stub)
    monkeypatch.setitem(sys.modules, "paper_ingestion.main", _main_stub)
    monkeypatch.setitem(sys.modules, "paper_ingestion.services", MagicMock())
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", _workflow_stub)
    # Force re-import of paper_jobs so it resolves against the freshly installed stubs.
    monkeypatch.delitem(sys.modules, "paper_ingestion.paper_jobs", raising=False)
    # Also clear _state module so svc starts fresh each test.
    monkeypatch.delitem(sys.modules, "paper_ingestion._state", raising=False)


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
    return make_pool_and_conn(fetchrow_return=row, with_transaction=False)[0]


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

    # Populate svc so the handler resolves pdf_processor and embedder.
    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()

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


def _force_run_pool(tmp_path, *, library_rows: list):
    """Return a pool for a downloaded paper whose library membership is *library_rows*."""
    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")
    # is_visible satisfies assert_paper_ownership: the paper is public, which is
    # exactly the state a force run must not be allowed to rely on.
    row = {
        "id": 42,
        "pdf_downloaded": True,
        "pdf_local_path": str(pdf_file),
        "is_visible": True,
    }
    pool, conn = make_pool_and_conn(fetchrow_return=row, with_transaction=False)
    conn.fetch = AsyncMock(return_value=library_rows)
    # Populate svc so a run that gets past the membership gate reaches
    # run_process_pdf rather than dying on uninitialised services — otherwise a
    # removed gate would surface as an unrelated RuntimeError.
    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()
    return pool


@pytest.mark.asyncio
async def test_paper_process_job_refuses_force_without_library_row(tmp_path):
    """The worker refuses a force run for a caller who does not hold the paper."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from jarvis_common.jobs import JobError  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_process_job  # noqa: PLC0415

    pool = _force_run_pool(tmp_path, library_rows=[])
    mock_run = AsyncMock(return_value={"status": "processed", "chunk_count": 5})
    _workflow_stub.run_process_pdf = mock_run
    pj.run_process_pdf = mock_run  # type: ignore[attr-defined]  # patched locally

    original_storage = pj.PDF_STORAGE_PATH
    pj.PDF_STORAGE_PATH = str(tmp_path)
    try:
        with pytest.raises(JobError, match="library"):
            await _paper_process_job(
                pool=pool,
                http_client=MagicMock(),
                payload={"paper_id": 42, "user_id": 7, "force": True},
                ctx=_make_ctx(),
            )
    finally:
        pj.PDF_STORAGE_PATH = original_storage

    mock_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_paper_process_job_allows_force_for_library_holder(tmp_path):
    """The same force run proceeds once the caller holds the paper."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_process_job  # noqa: PLC0415

    pool = _force_run_pool(tmp_path, library_rows=[{"?column?": 1}])
    mock_run = AsyncMock(return_value={"status": "processed", "chunk_count": 5})
    _workflow_stub.run_process_pdf = mock_run
    pj.run_process_pdf = mock_run  # type: ignore[attr-defined]  # patched locally

    original_storage = pj.PDF_STORAGE_PATH
    pj.PDF_STORAGE_PATH = str(tmp_path)
    try:
        await _paper_process_job(
            pool=pool,
            http_client=MagicMock(),
            payload={"paper_id": 42, "user_id": 7, "force": True},
            ctx=_make_ctx(),
        )
    finally:
        pj.PDF_STORAGE_PATH = original_storage

    mock_run.assert_awaited_once()
    assert mock_run.await_args is not None
    assert mock_run.await_args.kwargs["force"] is True


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
    # Populate svc so the handler resolves pdf_processor and embedder.
    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()
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


# ---------------------------------------------------------------------------
# PI-CORR-01 — TOCTOU in _paper_analyze_job: paper deleted mid-download
# ---------------------------------------------------------------------------


def _make_pool_with_side_effects(side_effects: list) -> MagicMock:
    """Return an asyncpg pool mock whose fetchrow yields *side_effects* in order.

    Each ``pool.acquire()`` call returns the same conn (async-CM yielding itself),
    so successive fetchrow calls across acquire blocks consume the side-effect list.
    """
    return make_pool_and_conn(fetchrow_side_effects=side_effects, with_transaction=False)[0]


@pytest.mark.asyncio
async def test_paper_analyze_job_raises_job_error_when_row_deleted_mid_download(
    tmp_path, monkeypatch
):
    """PI-CORR-01: if the paper row is deleted between the initial load and the
    post-download UPDATE, the UPDATE ... RETURNING returns None.

    Before the fix the handler dereferenced ``row["pdf_local_path"]`` on a None
    row → TypeError. After the fix it must raise JobError instead so the job is
    marked failed cleanly (not as an unexpected crash).

    user_id=None → assert_paper_ownership short-circuits (single-user mode), so
    the only conn.fetchrow calls are: [0] initial paper load, [1] post-download
    UPDATE RETURNING (the deletion race returns None here).
    """
    from jarvis_common.jobs import JobError  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_analyze_job  # noqa: PLC0415

    # _paper_analyze_job imports run_process_pdf + generate_paper_summary from
    # paper_ingestion.services.*; the autouse _install_stubs fixture replaces
    # paper_ingestion.services with a bare MagicMock (not a package), so stub the
    # submodules the handler imports. Use monkeypatch.setitem so the stubs are
    # auto-removed at teardown (a bare sys.modules assignment would leak into
    # later tests that import the real summarization module). The None-row guard
    # fires before either submodule is invoked, so these are import stubs only.
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", _workflow_stub)
    _workflow_stub.PDFRecordMissingError = _RealPDFRecordMissingError
    _workflow_stub.download_and_store_pdf = AsyncMock(
        side_effect=_RealPDFRecordMissingError("paper deleted")
    )
    _summ_stub = MagicMock()
    _summ_stub.generate_paper_summary = AsyncMock()
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", _summ_stub)

    # Initial load: remote paper, not yet downloaded → enters the download branch.
    initial_row = {
        "id": 7,
        "source_type": "arxiv",
        "pdf_url": "https://example.test/a.pdf",
        "pdf_downloaded": False,
        "pdf_local_path": None,
    }
    # fetchrow[0] = initial load; fetchrow[1] = post-download UPDATE → None (deleted).
    pool = _make_pool_with_side_effects([initial_row, None])
    ctx = _make_ctx()

    # Populate svc so the handler resolves pdf_processor/embedder/verifier.
    from paper_ingestion._state import svc  # noqa: PLC0415

    pdf_proc = MagicMock()
    # download_pdf is awaited; return a path inside tmp_path so a (would-be) later
    # path check would pass — but the None row must short-circuit before that.
    pdf_proc.download_pdf = AsyncMock(return_value=tmp_path / "a.pdf")
    svc.pdf_processor = pdf_proc
    svc.embedder = MagicMock()
    svc.verifier = MagicMock()

    with pytest.raises(JobError):
        await _paper_analyze_job(
            pool=pool,
            http_client=MagicMock(),
            payload={"paper_id": 7},  # user_id absent → None → single-user mode
            ctx=ctx,
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


# ---------------------------------------------------------------------------
# PI-UID-01 — _paper_summarize_job must forward user_id to generate_paper_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_summarize_job_forwards_user_id(monkeypatch):
    """_paper_summarize_job must pass the payload's user_id through to
    generate_paper_summary as a keyword.

    Before the fix the 4 call sites omitted user_id, so background-job summaries
    were written with user_id=NULL regardless of the authenticated caller. We
    stub the heavy summarization module (matching this file's convention) and
    assert the handler forwards user_id= to it.
    """
    from paper_ingestion.paper_jobs import _paper_summarize_job  # noqa: PLC0415

    user_id = 42

    # Stub the heavy summarization module (autouse _install_stubs replaces the
    # `paper_ingestion.services` package with a bare MagicMock; install the
    # submodule the handler imports). setitem auto-reverts at teardown.
    _summ_stub = MagicMock()
    fake_result = MagicMock()
    fake_result.summary.id = 1
    fake_result.coverage = 1.0
    fake_result.passes = 1
    _summ_stub.generate_paper_summary = AsyncMock(return_value=fake_result)
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", _summ_stub)

    # The central policy query reports that the paper is visible to this caller.
    pool = _make_pool({"id": 7, "is_visible": True})

    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.verifier = MagicMock()
    svc.embedder = MagicMock()

    await _paper_summarize_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"paper_id": 7, "user_id": user_id},
        ctx=_make_ctx(),
    )

    _summ_stub.generate_paper_summary.assert_awaited_once()
    assert _summ_stub.generate_paper_summary.await_args.kwargs.get("user_id") == user_id


@pytest.mark.asyncio
async def test_papers_batch_summarize_job_cancelled_mid_run_is_not_unqualified_success(
    monkeypatch,
):
    """A batch summarize run stopped by cancellation must report ``cancelled``
    — never say ``Done`` — so the papers it never reached cannot read as a
    clean completion."""
    from paper_ingestion.paper_jobs import _papers_batch_summarize_job  # noqa: PLC0415

    _summ_stub = MagicMock()
    _summ_stub.generate_paper_summary = AsyncMock(return_value=None)
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", _summ_stub)

    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.verifier = MagicMock()
    svc.embedder = MagicMock()

    ctx = _make_ctx()
    ctx.is_cancelled = AsyncMock(side_effect=[False, True])

    result = await _papers_batch_summarize_job(
        pool=MagicMock(),
        http_client=MagicMock(),
        payload={"paper_ids": [1, 2, 3]},
        ctx=ctx,
    )

    assert result["status"] == "cancelled"
    assert result["total"] == 3
    assert result["summarized"] == 1
    terminal_message = ctx.update_progress.await_args_list[-1].args[1]
    assert "Done" not in terminal_message


_REMEDIATION_TEXT = (
    "Embedding service error: the backend closed the connection. "
    "Check LiteLLM/Ollama health. (3 chunks saved — retry to resume)."
)


def _summarization_stub(monkeypatch, *, side_effect=None):
    """Install a stubbed summarization module and return it."""
    stub = MagicMock()
    result = MagicMock()
    result.summary.id = 1
    stub.generate_paper_summary = AsyncMock(return_value=result, side_effect=side_effect)
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", stub)
    return stub


def _process_job_pool(tmp_path, monkeypatch, failure):
    """Wire a downloaded-paper process job whose workflow call raises *failure*."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415

    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")
    pool = _make_pool({"id": 42, "pdf_downloaded": True, "pdf_local_path": str(pdf_file)})

    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()

    failing = AsyncMock(side_effect=failure)
    _workflow_stub.run_process_pdf = failing
    monkeypatch.setattr(pj, "run_process_pdf", failing, raising=False)
    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    return pool


@pytest.mark.asyncio
async def test_process_job_preserves_the_workflow_remediation_text(tmp_path, monkeypatch):
    """A message written for the requester survives the job handoff intact."""
    from jarvis_common.jobs import JobError  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_process_job  # noqa: PLC0415

    pool = _process_job_pool(
        tmp_path, monkeypatch, _real_pdf_workflow.PDFUserFacingError(_REMEDIATION_TEXT)
    )

    with pytest.raises(JobError) as raised:
        await _paper_process_job(
            pool=pool,
            http_client=MagicMock(),
            payload={"paper_id": 42, "force": False},
            ctx=_make_ctx(),
        )

    assert str(raised.value) == _REMEDIATION_TEXT


@pytest.mark.asyncio
async def test_process_job_does_not_widen_to_arbitrary_runtime_errors(tmp_path, monkeypatch):
    """An unclassified failure still collapses to the generic payload.

    This is the property the marker type exists to keep narrow: without it the
    translation would have to catch ``RuntimeError``, and every internal detail
    would reach the user.
    """
    from jarvis_common.task_registry import _terminal_error_payload  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_process_job  # noqa: PLC0415

    pool = _process_job_pool(
        tmp_path, monkeypatch, RuntimeError("asyncpg refused the pooled connection")
    )

    with pytest.raises(RuntimeError) as raised:
        await _paper_process_job(
            pool=pool,
            http_client=MagicMock(),
            payload={"paper_id": 42, "force": False},
            ctx=_make_ctx(),
        )

    assert _terminal_error_payload(raised.value) == {"message": "Job failed", "code": "JOB_FAILED"}


@pytest.mark.asyncio
async def test_analyze_job_preserves_a_summarize_step_remediation(tmp_path, monkeypatch):
    """The analyze chain's summarize step is covered too, not only its process step."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from jarvis_common.jobs import JobError  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_analyze_job  # noqa: PLC0415

    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")
    pool = _make_pool(
        {
            "id": 7,
            "source_type": "local",
            "pdf_url": None,
            "pdf_downloaded": True,
            "pdf_local_path": str(pdf_file),
        }
    )

    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", _workflow_stub)
    _workflow_stub.run_process_pdf = AsyncMock(
        return_value={"paper_id": 7, "chunk_count": 5, "status": "processed"}
    )
    _summarization_stub(
        monkeypatch, side_effect=_real_pdf_workflow.PDFUserFacingError(_REMEDIATION_TEXT)
    )

    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()
    svc.verifier = MagicMock()
    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))

    with pytest.raises(JobError) as raised:
        await _paper_analyze_job(
            pool=pool,
            http_client=MagicMock(),
            payload={"paper_id": 7},
            ctx=_make_ctx(),
        )

    assert str(raised.value) == _REMEDIATION_TEXT


@pytest.mark.asyncio
async def test_summarize_job_preserves_the_remediation_text(monkeypatch):
    """The single-paper summarize job translates the same failure the same way."""
    from jarvis_common.jobs import JobError  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_summarize_job  # noqa: PLC0415

    _summarization_stub(
        monkeypatch, side_effect=_real_pdf_workflow.PDFUserFacingError(_REMEDIATION_TEXT)
    )
    pool = _make_pool({"id": 7, "is_visible": True})

    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.verifier = MagicMock()
    svc.embedder = MagicMock()

    with pytest.raises(JobError) as raised:
        await _paper_summarize_job(
            pool=pool,
            http_client=MagicMock(),
            payload={"paper_id": 7},
            ctx=_make_ctx(),
        )

    assert str(raised.value) == _REMEDIATION_TEXT


@pytest.mark.asyncio
async def test_batch_summarize_keeps_reporting_partial_results(monkeypatch):
    """One paper's purpose-built failure must not abort the whole batch.

    On batch paths the per-paper error string IS the user-facing truth, so
    translating there would turn "one failed, the rest fine" into a failed job.
    """
    from paper_ingestion.paper_jobs import _papers_batch_summarize_job  # noqa: PLC0415

    _summarization_stub(
        monkeypatch,
        side_effect=[None, _real_pdf_workflow.PDFUserFacingError(_REMEDIATION_TEXT), None],
    )

    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.verifier = MagicMock()
    svc.embedder = MagicMock()

    result = await _papers_batch_summarize_job(
        pool=MagicMock(),
        http_client=MagicMock(),
        payload={"paper_ids": [1, 2, 3]},
        ctx=_make_ctx(),
    )

    assert result["summarized"] == 2
    assert result["failed"] == 1
    assert any("Paper 2" in message for message in result["errors"])


# ---------------------------------------------------------------------------
# W1-T4 — _paper_analyze_job must thread process-step warnings into its
# composite result (and omit the key entirely when the process step is clean).
# ---------------------------------------------------------------------------


async def _run_analyze_job_with_process_result(tmp_path, monkeypatch, process_result: dict):
    """Drive _paper_analyze_job through the local-paper happy path; return its result.

    run_process_pdf is stubbed to return *process_result*; the heavy
    summarization module is stubbed out (matching this file's convention).
    The paper row is local + already downloaded, so the download step is
    skipped and the only fetchrow call is the initial load (user_id absent →
    assert_paper_ownership short-circuits in single-user mode).
    """
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_analyze_job  # noqa: PLC0415

    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")

    row = {
        "id": 7,
        "source_type": "local",
        "pdf_url": None,
        "pdf_downloaded": True,
        "pdf_local_path": str(pdf_file),
    }
    pool = _make_pool(row)

    # The autouse _install_stubs fixture replaces paper_ingestion.services with
    # a bare MagicMock; install the submodules the handler imports (setitem
    # auto-reverts at teardown).
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", _workflow_stub)
    _workflow_stub.run_process_pdf = AsyncMock(return_value=process_result)
    _summ_stub = MagicMock()
    _summ_stub.generate_paper_summary = AsyncMock()
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", _summ_stub)

    # Populate svc so the handler resolves pdf_processor/embedder/verifier.
    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()
    svc.verifier = MagicMock()

    # Override PDF_STORAGE_PATH to tmp_path so the path-traversal check passes.
    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))

    return await _paper_analyze_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"paper_id": 7},  # user_id absent → None → single-user mode
        ctx=_make_ctx(),
    )


@pytest.mark.asyncio
async def test_paper_analyze_job_composite_carries_process_warnings(tmp_path, monkeypatch):
    """When the process step reports warnings (e.g. Qdrant stale-vector cleanup
    failure), the analyze-chain composite result must carry them through —
    before the fix only chunk_count/process_status were copied, silently
    dropping warnings on the analyze path."""
    warnings = [
        "Stale-vector cleanup failed: 3 stale vector(s) may remain in Qdrant"
        " (DB chunk rows are authoritative; see service logs)."
    ]
    result = await _run_analyze_job_with_process_result(
        tmp_path,
        monkeypatch,
        {"paper_id": 7, "chunk_count": 5, "status": "processed", "warnings": warnings},
    )

    assert result["warnings"] == warnings
    assert result["chunk_count"] == 5
    assert result["process_status"] == "processed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "process_result",
    [
        {"paper_id": 7, "chunk_count": 5, "status": "processed"},
        {"paper_id": 7, "chunk_count": 5, "status": "processed", "warnings": []},
    ],
    ids=["no-warnings-key", "empty-warnings-list"],
)
async def test_paper_analyze_job_composite_omits_warnings_when_clean(
    tmp_path, monkeypatch, process_result
):
    """The composite result must NOT contain a warnings key when the process
    step reported none — no empty-list field is introduced."""
    result = await _run_analyze_job_with_process_result(tmp_path, monkeypatch, process_result)

    assert "warnings" not in result
    assert result["process_status"] == "processed"


# ---------------------------------------------------------------------------
# Gap 4 — _paper_analyze_job forwards force=True to generate_paper_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_analyze_job_forwards_force(tmp_path, monkeypatch):
    """_paper_analyze_job must pass force=True from the payload to
    generate_paper_summary when the caller requests a forced re-summarization.

    The generate_paper_summary import lives inside the function body, so it is
    patched via the summarization submodule stub — matching this file's
    autouse convention (monkeypatch.setitem auto-reverts at teardown).
    """
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _paper_analyze_job  # noqa: PLC0415

    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")

    row = {
        "id": 7,
        "source_type": "local",
        "pdf_url": None,
        "pdf_downloaded": True,
        "pdf_local_path": str(pdf_file),
    }
    pool = _make_pool(row)

    # Stub the pdf_workflow and summarization submodules (matching _run_analyze_job_with_process_result).
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", _workflow_stub)
    _workflow_stub.run_process_pdf = AsyncMock(
        return_value={"paper_id": 7, "chunk_count": 3, "status": "processed"}
    )
    mock_generate_summary = AsyncMock()
    _summ_stub = MagicMock()
    _summ_stub.generate_paper_summary = mock_generate_summary
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", _summ_stub)

    # Populate svc so the handler resolves pdf_processor/embedder/verifier.
    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()
    svc.verifier = MagicMock()

    # Override PDF_STORAGE_PATH to tmp_path so path-traversal check passes.
    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))

    await _paper_analyze_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"paper_id": 7, "user_id": None, "force": True},
        ctx=_make_ctx(),
    )

    mock_generate_summary.assert_awaited_once()
    call_kwargs = mock_generate_summary.await_args.kwargs
    assert call_kwargs.get("force") is True, (
        f"generate_paper_summary must be called with force=True; got kwargs: {call_kwargs}"
    )
    # ING-2: the process step must also receive force=True, else a forced
    # re-analyze refreshes the summary but never re-chunks a stuck paper.
    rpp_call = _workflow_stub.run_process_pdf.await_args
    assert rpp_call is not None
    assert rpp_call.kwargs.get("force") is True, (
        f"run_process_pdf must be called with force=True; got kwargs={rpp_call.kwargs}"
    )


# ---------------------------------------------------------------------------
# _papers_process_library_job: whole-library per-paper stage machine
# ---------------------------------------------------------------------------


def _lib_row(
    paper_id: int,
    *,
    source_type: str = "local",
    pdf_url: str | None = None,
    pdf_downloaded: bool = True,
    pdf_local_path: str | None = None,
    needs_process: bool = True,
    needs_reconcile: bool = False,
    needs_summary: bool = False,
) -> dict:
    """One selection row as the process-library SELECT would return it."""
    return {
        "id": paper_id,
        "source_type": source_type,
        "pdf_url": pdf_url,
        "pdf_downloaded": pdf_downloaded,
        "pdf_local_path": pdf_local_path,
        "needs_process": needs_process,
        "needs_reconcile": needs_reconcile,
        "needs_summary": needs_summary,
    }


def _make_library_pool(select_rows: list[dict], update_rows: list[dict], user_id: int) -> MagicMock:
    """Pool mock for _papers_process_library_job.

    ``conn.fetch`` yields selection rows followed by the central visibility
    projection. ``conn.fetchrow`` yields post-download ``UPDATE ... RETURNING``
    rows in order (one per downloaded paper).
    """
    ownership_rows = [{"id": r["id"], "is_visible": True} for r in select_rows]
    pool, conn = make_pool_and_conn(
        fetchval_return=len(select_rows),
        fetchrow_side_effects=list(update_rows),
        with_transaction=False,
    )
    conn.fetch = AsyncMock(side_effect=[list(select_rows), ownership_rows])
    return pool


def _install_library_service_stubs(
    monkeypatch,
    *,
    run_process_pdf,
    reconcile_embeddings=None,
    download_pdf=None,
):
    """Wire the pdf_workflow/summarization submodule stubs + svc for a library job.

    Returns the summarization stub so callers can assert on generate_paper_summary.
    """
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", _workflow_stub)
    _workflow_stub.run_process_pdf = run_process_pdf
    _workflow_stub.reconcile_paper_embeddings = (
        reconcile_embeddings if reconcile_embeddings is not None else AsyncMock()
    )
    _summ_stub = MagicMock()
    _summ_stub.generate_paper_summary = AsyncMock()
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", _summ_stub)

    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.pdf_processor.download_pdf = download_pdf if download_pdf is not None else AsyncMock()

    async def fake_download_and_store(_pool, processor, url, paper_id):
        path = await processor.download_pdf(url, paper_id)
        return {"pdf_local_path": str(path)}

    _workflow_stub.PDFRecordMissingError = _RealPDFRecordMissingError
    _workflow_stub.download_and_store_pdf = AsyncMock(side_effect=fake_download_and_store)
    svc.embedder = MagicMock()
    svc.verifier = MagicMock()
    return _summ_stub


@pytest.mark.asyncio
async def test_process_library_mixed_fixture_and_idempotent_rerun(tmp_path, monkeypatch):
    """The acceptance fixture: five papers exercising every stage-machine branch,
    honest ``partial`` status, per-paper progress, then an idempotent rerun that
    re-attempts ONLY the previously-failed paper (no completed stage repeats)."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    user_id = 1
    for name in ("1", "2", "3", "4"):
        (tmp_path / f"{name}.pdf").write_bytes(b"%PDF-1.4 stub")
    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))

    process_calls: list[int] = []
    fail_ids = {4}

    async def fake_run_process_pdf(paper_id, *args, **kwargs):
        process_calls.append(paper_id)
        if paper_id in fail_ids:
            raise RuntimeError("process boom")
        return {"status": "processed", "chunk_count": 3}

    download_pdf = AsyncMock(return_value=tmp_path / "1.pdf")
    _install_library_service_stubs(
        monkeypatch, run_process_pdf=fake_run_process_pdf, download_pdf=download_pdf
    )

    rows = [
        _lib_row(1, source_type="arxiv", pdf_url="https://ex/1.pdf", pdf_downloaded=False),
        _lib_row(2, pdf_local_path=str(tmp_path / "2.pdf")),
        _lib_row(
            3, pdf_local_path=str(tmp_path / "3.pdf"), needs_process=False, needs_summary=False
        ),
        _lib_row(4, pdf_local_path=str(tmp_path / "4.pdf")),
        _lib_row(5, source_type="arxiv", pdf_url=None, pdf_downloaded=False),
    ]
    pool = _make_library_pool(
        rows, update_rows=[{"pdf_local_path": str(tmp_path / "1.pdf")}], user_id=user_id
    )
    ctx = _make_ctx()

    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": user_id, "summarize": False},
        ctx=ctx,
    )

    assert result["status"] == "partial"
    assert result["total"] == 5
    assert result["downloaded"] == 1
    assert result["processed"] == 2  # papers 1 and 2; paper 4 failed
    assert result["summarized"] == 0
    assert result["blocked"] == [{"paper_id": 5, "reason": "no_pdf_source"}]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["paper_id"] == 4
    assert result["errors"][0]["stage"] == "process"
    # Stage machine: complete paper 3 triggered no work; download only for paper 1.
    assert process_calls == [1, 2, 4]
    download_pdf.assert_awaited_once_with("https://ex/1.pdf", 1)
    # Per-paper progress emitted for every selected paper.
    msgs = [c.args[1] for c in ctx.update_progress.await_args_list if len(c.args) > 1]
    for pid in (1, 2, 3, 4, 5):
        assert any(f"Paper {pid} (" in (m or "") for m in msgs), f"no progress for paper {pid}"

    # ---- Second run: paper 4's failure cause removed → only paper 4 re-attempted ----
    fail_ids.clear()
    rerun_rows = [_lib_row(4, pdf_local_path=str(tmp_path / "4.pdf"))]
    pool2 = _make_library_pool(rerun_rows, update_rows=[], user_id=user_id)
    result2 = await _papers_process_library_job(
        pool=pool2,
        http_client=MagicMock(),
        payload={"user_id": user_id, "summarize": False},
        ctx=_make_ctx(),
    )
    assert result2["status"] == "ok"
    assert result2["processed"] == 1
    # Completed papers 1 and 2 are NOT re-processed; only paper 4 runs again.
    assert process_calls == [1, 2, 4, 4]


@pytest.mark.asyncio
async def test_process_library_all_blocked_is_partial_not_success(tmp_path, monkeypatch):
    """A selection where every paper lacks a PDF source: status must be
    ``partial`` (never an unqualified success) with all entries in ``blocked``
    and an empty ``errors``."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    run_process_pdf = AsyncMock()
    stubs = _install_library_service_stubs(monkeypatch, run_process_pdf=run_process_pdf)
    _ = stubs

    from paper_ingestion._state import svc  # noqa: PLC0415

    user_id = 1
    rows = [
        _lib_row(10, source_type="arxiv", pdf_url=None, pdf_downloaded=False),
        _lib_row(11, source_type="arxiv", pdf_url=None, pdf_downloaded=False),
    ]
    pool = _make_library_pool(rows, update_rows=[], user_id=user_id)

    ctx = _make_ctx()
    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": user_id, "summarize": False},
        ctx=ctx,
    )

    assert result["status"] == "partial"
    assert result["errors"] == []
    assert result["blocked"] == [
        {"paper_id": 10, "reason": "no_pdf_source"},
        {"paper_id": 11, "reason": "no_pdf_source"},
    ]
    assert result["processed"] == 0
    assert result["downloaded"] == 0
    assert ctx.update_progress.await_args_list[-1].args[1].startswith("Partial:")
    svc.pdf_processor.download_pdf.assert_not_awaited()
    run_process_pdf.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_library_cancelled_mid_run_is_not_unqualified_success(tmp_path, monkeypatch):
    """A run stopped by cancellation must report ``cancelled`` — never ``ok`` —
    so the counts it did not reach cannot read as a clean full completion."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    for name in ("40", "41", "42"):
        (tmp_path / f"{name}.pdf").write_bytes(b"%PDF-1.4 stub")
    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    _install_library_service_stubs(
        monkeypatch,
        run_process_pdf=AsyncMock(return_value={"status": "processed", "chunk_count": 1}),
    )

    user_id = 1
    rows = [_lib_row(pid, pdf_local_path=str(tmp_path / f"{pid}.pdf")) for pid in (40, 41, 42)]
    pool = _make_library_pool(rows, update_rows=[], user_id=user_id)
    ctx = _make_ctx()
    ctx.is_cancelled = AsyncMock(side_effect=[False, False, True])

    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": user_id, "summarize": False},
        ctx=ctx,
    )

    assert result["status"] == "cancelled"
    assert result["total"] == 3
    assert result["processed"] == 2
    assert result["errors"] == []
    assert result["blocked"] == []


@pytest.mark.asyncio
async def test_process_library_summary_only_paper_without_pdf_source_is_summarized(
    tmp_path, monkeypatch
):
    """A chunked paper selected only for its missing summary still gets that
    summary when it has no PDF source; a paper that genuinely needs the PDF to be
    processed stays blocked and is not summarized."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    run_process_pdf = AsyncMock()
    summ = _install_library_service_stubs(monkeypatch, run_process_pdf=run_process_pdf)

    from paper_ingestion._state import svc  # noqa: PLC0415

    user_id = 3
    rows = [
        _lib_row(
            30,
            source_type="arxiv",
            pdf_url=None,
            pdf_downloaded=False,
            needs_process=False,
            needs_summary=True,
        ),
        _lib_row(
            31,
            source_type="arxiv",
            pdf_url=None,
            pdf_downloaded=False,
            needs_process=True,
            needs_summary=True,
        ),
    ]
    pool = _make_library_pool(rows, update_rows=[], user_id=user_id)

    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": user_id, "summarize": True},
        ctx=_make_ctx(),
    )

    assert result["summarized"] == 1
    assert result["processed"] == 0
    # Only paper 31 needed the PDF it cannot fetch; paper 30 lost no stage it needed.
    assert result["status"] == "partial"
    assert result["blocked"] == [{"paper_id": 31, "reason": "no_pdf_source"}]
    summ.generate_paper_summary.assert_awaited_once()
    assert summ.generate_paper_summary.await_args.args[0] == 30
    svc.pdf_processor.download_pdf.assert_not_awaited()
    run_process_pdf.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_library_summarize_stage_forwards_user_id(tmp_path, monkeypatch):
    """With ``summarize=True`` a paper needing a summary runs the summarize stage,
    forwarding the caller's user_id (per-user summaries)."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    (tmp_path / "20.pdf").write_bytes(b"%PDF-1.4 stub")
    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    summ = _install_library_service_stubs(
        monkeypatch,
        run_process_pdf=AsyncMock(return_value={"status": "processed", "chunk_count": 1}),
    )

    user_id = 7
    rows = [
        _lib_row(
            20, pdf_local_path=str(tmp_path / "20.pdf"), needs_process=True, needs_summary=True
        )
    ]
    pool = _make_library_pool(rows, update_rows=[], user_id=user_id)

    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": user_id, "summarize": True},
        ctx=_make_ctx(),
    )

    assert result["status"] == "ok"
    assert result["processed"] == 1
    assert result["summarized"] == 1
    summ.generate_paper_summary.assert_awaited_once()
    assert summ.generate_paper_summary.await_args.kwargs.get("user_id") == user_id


@pytest.mark.asyncio
async def test_process_library_empty_selection_is_ok_zero(monkeypatch):
    """An empty selection completes cleanly as ``ok`` with zero counts."""
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    pool = _make_library_pool([], update_rows=[], user_id=1)
    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": 1, "summarize": False},
        ctx=_make_ctx(),
    )
    assert result == {
        "status": "ok",
        "total": 0,
        "examined": 0,
        "remaining": 0,
        "downloaded": 0,
        "processed": 0,
        "summarized": 0,
        "blocked": [],
        "errors": [],
    }


@pytest.mark.asyncio
async def test_process_library_reconciles_completed_paper_without_pdf_file(tmp_path, monkeypatch):
    """Completed candidates probe persisted vectors without requiring PDF extraction."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    run_process_pdf = AsyncMock()
    reconcile = AsyncMock(return_value={"paper_id": 71, "chunk_count": 2, "status": "healthy"})
    _install_library_service_stubs(
        monkeypatch,
        run_process_pdf=run_process_pdf,
        reconcile_embeddings=reconcile,
    )
    rows = [
        _lib_row(
            71,
            pdf_local_path=None,
            needs_process=False,
            needs_reconcile=True,
        )
    ]
    pool = _make_library_pool(rows, update_rows=[], user_id=4)

    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": 4, "summarize": False},
        ctx=_make_ctx(),
    )

    assert result["status"] == "ok"
    assert result["processed"] == 0
    reconcile.assert_awaited_once_with(71, db_pool=pool, embedder=ANY)
    run_process_pdf.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_library_reports_reconciliation_probe_failure_as_retryable_partial(
    tmp_path, monkeypatch
):
    """A failed vector probe is visible in the job result and leaves the paper retryable."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    reconcile = AsyncMock(side_effect=RuntimeError("qdrant unavailable"))
    _install_library_service_stubs(
        monkeypatch,
        run_process_pdf=AsyncMock(),
        reconcile_embeddings=reconcile,
    )
    rows = [_lib_row(72, needs_process=False, needs_reconcile=True)]
    pool = _make_library_pool(rows, update_rows=[], user_id=4)

    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": 4, "summarize": False},
        ctx=_make_ctx(),
    )

    assert result["status"] == "partial"
    # The raw exception message ("qdrant unavailable") must never reach the
    # caller — only the classified code crosses the job boundary.
    assert result["errors"] == [{"paper_id": 72, "stage": "reconcile", "error": "unknown_error"}]


@pytest.mark.asyncio
async def test_process_library_classifies_asyncpg_errors_not_raw_message(tmp_path, monkeypatch):
    """A DB constraint violation during a paper stage is reported as its classified
    code (sanitization applies to library-stage errors, not just bulk actions)."""
    import asyncpg  # noqa: PLC0415
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    reconcile = AsyncMock(
        side_effect=asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
    )
    _install_library_service_stubs(
        monkeypatch,
        run_process_pdf=AsyncMock(),
        reconcile_embeddings=reconcile,
    )
    rows = [_lib_row(73, needs_process=False, needs_reconcile=True)]
    pool = _make_library_pool(rows, update_rows=[], user_id=4)

    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": 4, "summarize": False},
        ctx=_make_ctx(),
    )

    assert result["status"] == "partial"
    assert result["errors"] == [{"paper_id": 73, "stage": "reconcile", "error": "already_in_state"}]


@pytest.mark.asyncio
async def test_process_library_continues_until_more_than_one_page_is_examined(
    tmp_path, monkeypatch
):
    """A whole-library job automatically visits every paper beyond the 100-row page."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    reconcile = AsyncMock(return_value={"paper_id": 0, "chunk_count": 1, "status": "healthy"})
    _install_library_service_stubs(
        monkeypatch,
        run_process_pdf=AsyncMock(),
        reconcile_embeddings=reconcile,
    )
    user_id = 9
    rows = [
        _lib_row(
            paper_id,
            needs_process=False,
            needs_reconcile=True,
        )
        for paper_id in range(1, 206)
    ]
    conn = MagicMock()

    async def _fetch(sql, *params):
        if "AS is_visible" in sql:
            return [{"id": paper_id, "is_visible": True} for paper_id in params[0]]
        if "FROM papers p" in sql:
            cursor = int(params[3])
            return [row for row in rows if row["id"] > cursor][:100]
        raise AssertionError(f"Unexpected query: {sql}")

    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.fetchval = AsyncMock(return_value=len(rows))
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)

    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": user_id, "summarize": False},
        ctx=_make_ctx(),
    )

    assert result["status"] == "ok"
    assert result["total"] == 205
    assert result["examined"] == 205
    assert result["remaining"] == 0
    assert reconcile.await_count == 205


@pytest.mark.asyncio
async def test_process_library_rejects_null_user():
    """A NULL-user invocation is rejected — user_id is the tenancy boundary."""
    from jarvis_common.jobs import JobError  # noqa: PLC0415
    from paper_ingestion.paper_jobs import _papers_process_library_job  # noqa: PLC0415

    with pytest.raises(JobError):
        await _papers_process_library_job(
            pool=MagicMock(),
            http_client=MagicMock(),
            payload={"summarize": False},  # no user_id
            ctx=_make_ctx(),
        )


# ---------------------------------------------------------------------------
# POST /api/jobs — a force key the payload schema does not declare
#
# PaperAnalyzePayload and PapersBatchProcessPayload leave pydantic's default
# extra="ignore" in place, and the endpoint validates a merged copy of the
# payload while forwarding the raw one, so an undeclared force reaches the
# worker. These tests drive the endpoint and run the handler on exactly the
# payload it deferred, against the real run_process_pdf.
# ---------------------------------------------------------------------------

_REBUILD_PAPER_ID = 42
_NON_HOLDER_ID = 7
_REBUILD_SOURCE_URL = "https://example.test/rebuild-source.pdf"
_REBUILD_VISIBILITY = VectorVisibility(
    source_type="arxiv",
    visibility_scope="public",
    visibility_generation="a" * 32,
)


def _rebuild_row(pdf_file) -> dict:
    """Row answering every ``fetchrow`` the rebuild path makes.

    ``is_visible`` makes the paper public — the state a force run must not be
    allowed to rely on. ``acquired`` answers the per-paper advisory-lock probe.
    """
    return {
        "id": _REBUILD_PAPER_ID,
        "source_type": "local",
        "pdf_url": None,
        "pdf_downloaded": True,
        "pdf_local_path": str(pdf_file),
        "is_visible": True,
        "acquired": True,
    }


def _rebuild_fetchval():
    """Answer the workflow's ``fetchval`` reads for a paper with two persisted chunks.

    Order: source URL, download premise, chunk count, ``chunked_at``. The commit
    fence re-reads the source URL, which the trailing default supplies.
    """
    answers = iter((_REBUILD_SOURCE_URL, True, 2, None))
    return lambda *_args, **_kwargs: next(answers, _REBUILD_SOURCE_URL)


def _rebuild_pool(tmp_path, *, fetch_results: list):
    """Return a ``(pool, conn)`` pair on which a permitted force rebuild completes.

    ``fetch_results`` answers the ``fetch`` reads in call order: any ownership
    batch read, then the holdership probe, then the stale-vector read a refused
    run never reaches.
    """
    pdf_file = tmp_path / f"{_REBUILD_PAPER_ID}.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_rebuild_row(pdf_file))
    conn.fetchval = AsyncMock(side_effect=_rebuild_fetchval())
    conn.fetch = AsyncMock(side_effect=fetch_results)
    return make_pool_and_conn(conn=conn)


def _use_real_workflow(monkeypatch, tmp_path, pj) -> MagicMock:
    """Point the handlers at the real ``run_process_pdf``; return its PDF processor.

    The gate under test lives inside the workflow, so a mocked workflow would
    prove nothing about whether a non-holder's force run is refused.
    """
    from paper_ingestion._state import svc  # noqa: PLC0415

    _workflow_stub.run_process_pdf = _real_pdf_workflow.run_process_pdf
    monkeypatch.setattr(pj, "PDF_STORAGE_PATH", str(tmp_path))
    # The generation resolver is read through two module namespaces: the PDF
    # run (pdf_workflow) and reconciliation (embedding_reconcile). Patch both
    # so either entry point sees the stubbed generation.
    resolve_generation = AsyncMock(return_value=_REBUILD_VISIBILITY.visibility_generation)
    monkeypatch.setattr(
        _real_pdf_workflow,
        "_resolve_visibility_generation",
        resolve_generation,
    )
    monkeypatch.setattr(
        _real_embedding_reconcile,
        "_resolve_visibility_generation",
        resolve_generation,
    )
    monkeypatch.setattr(
        _real_pdf_workflow,
        "_load_paper_embedding_context",
        AsyncMock(return_value=(_REBUILD_VISIBILITY, 17)),
    )
    chunks = [SimpleNamespace(chunk_index=0, content="A", page_number=1, start_char=0, end_char=1)]
    pdf_processor = MagicMock()
    pdf_processor.process = AsyncMock(return_value=("full text", chunks, ["vec-0"]))
    svc.pdf_processor = pdf_processor
    svc.embedder = MagicMock()
    svc.verifier = MagicMock()
    summarization = MagicMock()
    summarization.generate_paper_summary = AsyncMock()
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", summarization)
    return pdf_processor


def _worker_task(handler, pool, outcome: list) -> MagicMock:
    """Return a task stand-in whose ``defer_async`` runs *handler* on what it deferred.

    Mirrors the worker: the handler receives exactly the keyword payload the
    enqueue call was given, so a key the endpoint forwards arrives verbatim.
    """

    async def _defer(job_id, **payload):
        try:
            outcome.append(await handler(pool, MagicMock(), payload, _make_ctx()))
        except Exception as exc:  # noqa: BLE001 - recorded so the test can assert on it
            outcome.append(exc)

    task = MagicMock()
    task.defer_async = AsyncMock(side_effect=_defer)
    return task


def _jobs_app(pool, *, user_id: int):
    """Return a FastAPI app serving the jobs router against mocked dependencies."""
    from fastapi import FastAPI  # noqa: PLC0415
    from jarvis_common.auth import current_user_id_strict  # noqa: PLC0415

    from paper_ingestion.deps import get_db_pool, limiter  # noqa: PLC0415

    # The limiter is a process-wide singleton and every app-level test keys to
    # the same bucket, so reset it rather than spend another test's quota.
    limiter.reset()

    app = FastAPI()
    app.include_router(_jobs_router_module.router)
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[current_user_id_strict] = lambda: user_id
    return app


def _jobs_client(app):
    """Return an in-process httpx client bound to *app*."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _assert_no_rebuild(conn, pdf_processor) -> None:
    """Fail if the run derived fresh content or discarded the persisted chunks."""
    pdf_processor.process.assert_not_awaited()
    discard_chunks = call("DELETE FROM paper_chunks WHERE paper_id = $1", _REBUILD_PAPER_ID)
    assert discard_chunks not in conn.execute.await_args_list


@pytest.mark.asyncio
async def test_jobs_endpoint_analyze_force_wins_no_rebuild_for_non_holder(tmp_path, monkeypatch):
    """An undeclared force on paper.analyze reaches the worker but rebuilds nothing."""
    import jarvis_common.task_registry as task_registry  # noqa: PLC0415
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from jarvis_common.jobs import JobError  # noqa: PLC0415

    # No user_library row for this caller; the same stub answers the stale-vector
    # read a run that got past the gate would make.
    pool, conn = _rebuild_pool(tmp_path, fetch_results=[[], []])
    pdf_processor = _use_real_workflow(monkeypatch, tmp_path, pj)

    outcome: list = []
    task = _worker_task(pj._paper_analyze_job, pool, outcome)
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": task}):
        async with _jobs_client(_jobs_app(pool, user_id=_NON_HOLDER_ID)) as client:
            resp = await client.post(
                "/api/jobs",
                json={
                    "kind": "paper.analyze",
                    "payload": {"paper_id": _REBUILD_PAPER_ID, "force": True},
                },
            )

    assert resp.status_code == 202, f"expected 202, got {resp.status_code}: {resp.text}"
    assert task.defer_async.await_args.kwargs["force"] is True, (
        "the undeclared force key must survive the endpoint, or this proves nothing"
    )
    assert isinstance(outcome[0], JobError), f"expected a refusal, got {outcome[0]!r}"
    assert "library" in str(outcome[0]).lower()
    _assert_no_rebuild(conn, pdf_processor)


@pytest.mark.asyncio
async def test_jobs_endpoint_batch_force_wins_no_rebuild_for_non_holder(tmp_path, monkeypatch):
    """An undeclared force on papers.batch_process reaches the worker but rebuilds nothing."""
    import jarvis_common.task_registry as task_registry  # noqa: PLC0415
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415

    # The endpoint and the handler each run one batch ownership read; the
    # holdership probe that follows finds no user_library row.
    visible = [{"id": _REBUILD_PAPER_ID, "is_visible": True}]
    pool, conn = _rebuild_pool(tmp_path, fetch_results=[visible, visible, [], []])
    pdf_processor = _use_real_workflow(monkeypatch, tmp_path, pj)

    outcome: list = []
    task = _worker_task(pj._papers_batch_process_job, pool, outcome)
    with patch.dict(task_registry._TASK_MAP, {"papers.batch_process": task}):
        async with _jobs_client(_jobs_app(pool, user_id=_NON_HOLDER_ID)) as client:
            resp = await client.post(
                "/api/jobs",
                json={
                    "kind": "papers.batch_process",
                    "payload": {"paper_ids": [_REBUILD_PAPER_ID], "force": True},
                },
            )

    assert resp.status_code == 202, f"expected 202, got {resp.status_code}: {resp.text}"
    assert task.defer_async.await_args.kwargs["force"] is True, (
        "the undeclared force key must survive the endpoint, or this proves nothing"
    )
    assert outcome[0]["processed"] == 0, f"a non-holder rebuilt a paper: {outcome[0]}"
    assert len(outcome[0]["errors"]) == 1
    _assert_no_rebuild(conn, pdf_processor)


@pytest.mark.asyncio
async def test_paper_process_job_reports_a_gate_refusal_as_a_job_error(tmp_path, monkeypatch):
    """A force run the workflow refuses is reported as a job failure, not a raw error.

    No requester is named here, so the handler's own membership probe is skipped
    and the refusal comes from the rebuild gate inside the workflow instead. It
    has to reach the worker as a job failure like every other refusal this
    handler reports.
    """
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from jarvis_common.jobs import JobError  # noqa: PLC0415

    # No fetch reads are expected: an unnamed requester is refused before the
    # holdership probe would run.
    pool, conn = _rebuild_pool(tmp_path, fetch_results=[])
    pdf_processor = _use_real_workflow(monkeypatch, tmp_path, pj)

    with pytest.raises(JobError, match="library"):
        await pj._paper_process_job(
            pool,
            MagicMock(),
            {"paper_id": _REBUILD_PAPER_ID, "force": True},
            _make_ctx(),
        )

    _assert_no_rebuild(conn, pdf_processor)


@pytest.mark.asyncio
async def test_papers_batch_process_job_cancelled_mid_run_is_not_unqualified_success(
    monkeypatch,
):
    """The fourth batch handler reports a cancelled run the same way its siblings do.

    It shares a file with one of them and had the identical shape: break out of
    the loop on cancellation, then report success. A user who stops it after two
    of five hundred papers must not be told the run finished.
    """
    from paper_ingestion import paper_jobs as pj  # noqa: PLC0415

    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()

    # No stored PDF, so the first paper is skipped without reaching the
    # workflow and the run stops on the next cancellation check.
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    pool, _ = make_pool_and_conn(conn=conn)

    ctx = _make_ctx()
    ctx.is_cancelled = AsyncMock(side_effect=[False, True])

    result = await pj._papers_batch_process_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"paper_ids": [1, 2, 3]},
        ctx=ctx,
    )

    assert result["status"] == "cancelled", (
        f"a cancelled batch must not read as a clean completion; got {result}"
    )
    assert result["total"] == 3
    assert result["remaining"] == 2, (
        "the one skipped paper was examined; only the two untouched papers remain"
    )
    terminal_message = ctx.update_progress.await_args_list[-1].args[1]
    assert "Done" not in terminal_message, (
        f"the terminal message must not say Done for a cancelled run; got {terminal_message!r}"
    )


@pytest.mark.asyncio
async def test_papers_batch_process_skipped_only_is_partial():
    """An examined paper without usable PDF bytes is incomplete, not successful."""
    from paper_ingestion import paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    pool, _ = make_pool_and_conn(conn=conn)
    ctx = _make_ctx()
    _workflow_stub.run_process_pdf = AsyncMock()

    result = await pj._papers_batch_process_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"paper_ids": [1]},
        ctx=ctx,
    )

    assert result == {
        "processed": 0,
        "skipped": 1,
        "errors": [],
        "failed": 0,
        "total": 1,
        "remaining": 0,
        "status": "partial",
    }
    assert ctx.update_progress.await_args_list[-1].args[1].startswith("Partial:")
    _workflow_stub.run_process_pdf.assert_not_awaited()


@pytest.mark.asyncio
async def test_papers_batch_process_failures_are_examined_not_remaining(tmp_path):
    """Processed, skipped, failed, and untouched counts form one exact partition."""
    import paper_ingestion.paper_jobs as pj  # noqa: PLC0415
    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.embedder = MagicMock()

    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 stub")
    available = {"pdf_downloaded": True, "pdf_local_path": str(pdf_file)}
    conn = AsyncMock()
    conn.fetchrow.side_effect = [available, available, None]
    pool, _ = make_pool_and_conn(conn=conn)
    _workflow_stub.run_process_pdf = AsyncMock(
        side_effect=[RuntimeError("broken PDF"), {"status": "processed"}]
    )

    original_storage = pj.PDF_STORAGE_PATH
    pj.PDF_STORAGE_PATH = str(tmp_path)
    try:
        result = await pj._papers_batch_process_job(
            pool=pool,
            http_client=MagicMock(),
            payload={"paper_ids": [1, 2, 3]},
            ctx=_make_ctx(),
        )
    finally:
        pj.PDF_STORAGE_PATH = original_storage

    assert result["status"] == "partial"
    assert result["processed"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 1
    assert result["remaining"] == 0
    assert (
        result["processed"] + result["skipped"] + result["failed"] + result["remaining"]
        == result["total"]
    ), f"batch outcome counts must partition the input exactly: {result}"
