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

# Import the real path-traversal guard so the stubbed pdf_processor module
# exposes genuine traversal semantics (a bare MagicMock attribute would always
# be truthy and silently disable the guard).
from paper_ingestion.pdf_processor import check_pdf_path_safe as _real_check_pdf_path_safe

_pdf_proc_stub = MagicMock()
_pdf_proc_stub.PDF_STORAGE_PATH = "/data/pdfs"
_pdf_proc_stub.check_pdf_path_safe = _real_check_pdf_path_safe

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
    _pdf_proc_stub.check_pdf_path_safe = _real_check_pdf_path_safe
    _main_stub.reset_mock()
    _workflow_stub.reset_mock()

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


# Keep local: pool.acquire returns conn directly (no aenter/aexit ctx wrapper) — paper_jobs uses async-with on conn itself.
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
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=side_effects)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool


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

    # Mock pool: assert_paper_ownership does `fetchrow("SELECT discovered_by ...")`;
    # discovered_by == caller → ownership granted without a second query.
    pool = _make_pool({"discovered_by": user_id})

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
        "needs_summary": needs_summary,
    }


def _make_library_pool(select_rows: list[dict], update_rows: list[dict], user_id: int) -> MagicMock:
    """Pool mock for _papers_process_library_job.

    ``conn.fetch`` yields [selection rows, ownership rows]; ownership rows carry
    ``discovered_by == user_id`` so assert_papers_ownership fast-grants without a
    second user_library lookup. ``conn.fetchrow`` yields the post-download
    UPDATE ... RETURNING rows in order (one per downloaded paper).
    """
    ownership_rows = [{"id": r["id"], "discovered_by": user_id} for r in select_rows]
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[list(select_rows), ownership_rows])
    conn.fetchrow = AsyncMock(side_effect=list(update_rows))
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool


def _install_library_service_stubs(monkeypatch, *, run_process_pdf, download_pdf=None):
    """Wire the pdf_workflow/summarization submodule stubs + svc for a library job.

    Returns the summarization stub so callers can assert on generate_paper_summary.
    """
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", _workflow_stub)
    _workflow_stub.run_process_pdf = run_process_pdf
    _summ_stub = MagicMock()
    _summ_stub.generate_paper_summary = AsyncMock()
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", _summ_stub)

    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = MagicMock()
    svc.pdf_processor.download_pdf = download_pdf if download_pdf is not None else AsyncMock()
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

    result = await _papers_process_library_job(
        pool=pool,
        http_client=MagicMock(),
        payload={"user_id": user_id, "summarize": False},
        ctx=_make_ctx(),
    )

    assert result["status"] == "partial"
    assert result["errors"] == []
    assert result["blocked"] == [
        {"paper_id": 10, "reason": "no_pdf_source"},
        {"paper_id": 11, "reason": "no_pdf_source"},
    ]
    assert result["processed"] == 0
    assert result["downloaded"] == 0
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
        "downloaded": 0,
        "processed": 0,
        "summarized": 0,
        "blocked": [],
        "errors": [],
    }


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
