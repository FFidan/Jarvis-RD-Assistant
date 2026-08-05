"""auto_fetch gather isolation — defense-in-depth (PR2-T1 fix #3).

The download/process inner coroutines already wrap their bodies in try/except,
and run_auto_pipeline has an outer guard, so a raising task is already
contained. These tests lock the ``asyncio.gather(..., return_exceptions=True)``
contract directly: even if a gathered task raises *outside* the inner guard
(e.g. a future removal of that guard), sibling tasks still complete and
run_auto_pipeline does NOT propagate the exception.

We exercise the gather seam by patching the module's ``asyncio.create_task`` to
substitute a directly-raising coroutine for one task — this bypasses the inner
try/except entirely, so the only thing that can contain it is the gather call.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jarvis_common.task_registry as task_registry
import pytest
from paper_ingestion.pipelines import auto_fetch as af
from tests.conftest import FakeRecord, _make_pool_and_conn


def test_resolve_topic_pairs_defaults_and_coerces_ids():
    from paper_ingestion.pipelines.auto_fetch import _resolve_topic_pairs

    assert _resolve_topic_pairs([]) == [(None, "machine learning", [])]
    assert _resolve_topic_pairs([{"id": 7, "name": "graphs"}]) == [(7, "graphs", [])]
    # nameless rows are dropped; non-int id coerces to None
    assert _resolve_topic_pairs([{"id": None, "name": "nlp"}, {"id": 3, "name": ""}]) == [
        (None, "nlp", [])
    ]
    # a NULL query_terms (partial-schema row) coerces to [], same as a missing key
    assert _resolve_topic_pairs([{"id": 5, "name": "vision", "query_terms": None}]) == [
        (5, "vision", [])
    ]


def test_resolve_topic_pairs_carries_configured_query_terms():
    from paper_ingestion.pipelines.auto_fetch import _resolve_topic_pairs

    rows = [{"id": 9, "name": "RL", "query_terms": ["reinforcement learning"]}]
    assert _resolve_topic_pairs(rows) == [(9, "RL", ["reinforcement learning"])]


def _make_app(conn) -> SimpleNamespace:
    pool = _make_pool_and_conn(conn=conn, with_transaction=False)[0]
    return SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            pdf_processor=MagicMock(),
            embedder=MagicMock(),
        )
    )


@pytest.mark.asyncio
async def test_gather_isolates_a_raising_download_task(monkeypatch):
    """One download task raising outside the inner guard must not abort siblings
    nor propagate out of run_auto_pipeline. Two papers are queued for download;
    the first raises, the second must still run to completion."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # sources query -> no sources (skip discovery)
            [],  # topics query
            [  # to_download: two papers
                {"id": 1, "pdf_url": "https://example.org/1.pdf"},
                {"id": 2, "pdf_url": "https://example.org/2.pdf"},
            ],
            [],  # to_process
        ]
    )

    sibling_ran = asyncio.Event()

    async def _raising_coro():
        raise RuntimeError("escaped the inner guard")

    async def _sibling_coro():
        sibling_ran.set()

    real_create_task = asyncio.create_task
    coro_index = 0

    def _patched_create_task(coro, *args, **kwargs):
        nonlocal coro_index
        # Substitute the first download coroutine with one that raises outside
        # the inner guard; keep the second as a sibling that records completion.
        coro.close()  # avoid "coroutine was never awaited" warnings
        replacement = _raising_coro() if coro_index == 0 else _sibling_coro()
        coro_index += 1
        return real_create_task(replacement, *args, **kwargs)

    app = _make_app(conn)
    with patch.object(af.asyncio, "create_task", _patched_create_task):
        # The gather itself must contain the raising task — it must never reach
        # run_auto_pipeline's outer guard (which logs at ERROR), and siblings run.
        with patch.object(af.logger, "error") as mock_error:
            await af.run_auto_pipeline(app)

    assert sibling_ran.is_set(), "sibling download task must still complete"
    assert not any("unhandled error" in str(c.args) for c in mock_error.call_args_list), (
        "gather must contain the task exception; it must not surface to the outer guard"
    )


@pytest.mark.asyncio
async def test_gather_isolates_a_raising_process_task(monkeypatch):
    """Same contract for the process (extract/embed) gather at the 3b stage."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    # to_process paths must live under PDF storage to pass the traversal guard.
    storage = af.PDF_STORAGE_PATH
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # sources
            [],  # topics
            [],  # to_download
            [  # to_process: two papers with in-storage paths
                {"id": 1, "pdf_local_path": f"{storage}/1.pdf"},
                {"id": 2, "pdf_local_path": f"{storage}/2.pdf"},
            ],
        ]
    )

    sibling_ran = asyncio.Event()

    async def _raising_coro():
        raise RuntimeError("escaped the inner guard")

    async def _sibling_coro():
        sibling_ran.set()

    real_create_task = asyncio.create_task
    coro_index = 0

    def _patched_create_task(coro, *args, **kwargs):
        nonlocal coro_index
        coro.close()
        replacement = _raising_coro() if coro_index == 0 else _sibling_coro()
        coro_index += 1
        return real_create_task(replacement, *args, **kwargs)

    app = _make_app(conn)
    with patch.object(af.asyncio, "create_task", _patched_create_task):
        with patch.object(af.logger, "error") as mock_error:
            await af.run_auto_pipeline(app)

    assert sibling_ran.is_set(), "sibling process task must still complete"
    assert not any("unhandled error" in str(c.args) for c in mock_error.call_args_list), (
        "gather must contain the task exception; it must not surface to the outer guard"
    )


@pytest.mark.asyncio
async def test_escaped_download_exception_is_logged_at_warning(monkeypatch, caplog):
    """A gathered download exception (escaping the inner guard) must be LOGGED at
    WARNING — not silently discarded — while the sibling task still completes."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # sources
            [],  # topics
            [  # to_download: two papers
                {"id": 1, "pdf_url": "https://example.org/1.pdf"},
                {"id": 2, "pdf_url": "https://example.org/2.pdf"},
            ],
            [],  # to_process
        ]
    )

    sibling_ran = asyncio.Event()

    async def _raising_coro():
        raise RuntimeError("escaped the inner guard")

    async def _sibling_coro():
        sibling_ran.set()

    real_create_task = asyncio.create_task
    coro_index = 0

    def _patched_create_task(coro, *args, **kwargs):
        nonlocal coro_index
        coro.close()
        replacement = _raising_coro() if coro_index == 0 else _sibling_coro()
        coro_index += 1
        return real_create_task(replacement, *args, **kwargs)

    app = _make_app(conn)
    with patch.object(af.asyncio, "create_task", _patched_create_task):
        with caplog.at_level("WARNING", logger=af.logger.name):
            await af.run_auto_pipeline(app)

    assert sibling_ran.is_set(), "sibling download task must still complete"
    download_warnings = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING" and "download task failed" in rec.getMessage()
    ]
    assert len(download_warnings) == 1, (
        "the escaped download exception must be logged exactly once at WARNING; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_escaped_process_exception_is_logged_at_warning(monkeypatch, caplog):
    """A gathered process exception (escaping the inner guard) must be LOGGED at
    WARNING — not silently discarded — while the sibling task still completes."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    storage = af.PDF_STORAGE_PATH
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # sources
            [],  # topics
            [],  # to_download
            [  # to_process: two papers with in-storage paths
                {"id": 1, "pdf_local_path": f"{storage}/1.pdf"},
                {"id": 2, "pdf_local_path": f"{storage}/2.pdf"},
            ],
        ]
    )

    sibling_ran = asyncio.Event()

    async def _raising_coro():
        raise RuntimeError("escaped the inner guard")

    async def _sibling_coro():
        sibling_ran.set()

    real_create_task = asyncio.create_task
    coro_index = 0

    def _patched_create_task(coro, *args, **kwargs):
        nonlocal coro_index
        coro.close()
        replacement = _raising_coro() if coro_index == 0 else _sibling_coro()
        coro_index += 1
        return real_create_task(replacement, *args, **kwargs)

    app = _make_app(conn)
    with patch.object(af.asyncio, "create_task", _patched_create_task):
        with caplog.at_level("WARNING", logger=af.logger.name):
            await af.run_auto_pipeline(app)

    assert sibling_ran.is_set(), "sibling process task must still complete"
    process_warnings = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING" and "process task failed" in rec.getMessage()
    ]
    assert len(process_warnings) == 1, (
        "the escaped process exception must be logged exactly once at WARNING; "
        f"got records: {[r.getMessage() for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# automation.auto_summarize_discovered toggle (default OFF) — best-effort
# defer of paper.summarize for a newly-chunked, unsummarized paper.
# ---------------------------------------------------------------------------


def _to_process_app(conn) -> SimpleNamespace:
    """Like _make_app, but for the process-step (one paper pending chunk)."""
    return _make_app(conn)


def _configure_conn_for_process_one_paper(
    conn, *, auto_summarize_value, holders=({"user_id": 3},)
) -> None:
    """Wire a single to-process paper (id=7) plus the reads used by the
    auto-summarize toggle. ``.fetch()`` drives the five positional queries
    (sources/topics/to_download/to_process/unsummarized library holders);
    ``.fetchrow`` backs the toggle read.
    """
    storage = af.PDF_STORAGE_PATH
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # sources
            [],  # topics
            [],  # to_download
            [{"id": 7, "pdf_local_path": f"{storage}/7.pdf"}],  # to_process
            list(holders),  # library holders lacking a summary for paper 7
        ]
    )
    conn.fetchrow = AsyncMock(return_value={"value": auto_summarize_value})


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("yes", True),
        ("1", True),
        ("false", False),
        ("no", False),
        ("null", False),
        ("", False),
        (None, False),
        # Fail-closed: an unrecognised string must NOT enable auto-summarization.
        # bool("maybe") is True, so a truthiness fallback would turn on unattended
        # LLM spend for a value we cannot interpret.
        ("maybe", False),
        ("enabled", False),
        ("TRUE ", False),
    ],
)
@pytest.mark.asyncio
async def test_is_auto_summarize_enabled_coerces_stored_values(stored, expected):
    """Pins the shared flag reader's coercion for the auto-summarize flag,
    including its fail-closed handling of unrecognised strings."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": stored})
    assert await af._is_auto_summarize_enabled(pool) is expected


@pytest.mark.asyncio
async def test_auto_summarize_toggle_on_defers_paper_summarize_once(monkeypatch):
    """Toggle ON: a newly-chunked, unsummarized paper defers paper.summarize once."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    _configure_conn_for_process_one_paper(conn, auto_summarize_value=True)
    app = _to_process_app(conn)

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with (
        patch.object(
            af,
            "run_process_pdf",
            AsyncMock(return_value={"paper_id": 7, "chunk_count": 3, "status": "processed"}),
        ),
        patch.dict(task_registry._TASK_MAP, {"paper.summarize": mock_task}),
    ):
        await af.run_auto_pipeline(app)

    mock_task.defer_async.assert_awaited_once()
    call_kwargs = mock_task.defer_async.await_args.kwargs
    assert call_kwargs.get("paper_id") == 7
    assert "job_id" in call_kwargs
    # The summary must be owned by the library holder — a NULL owner is
    # invisible to every reader (they bind a strict integer user id).
    assert call_kwargs.get("user_id") == 3


@pytest.mark.asyncio
async def test_auto_summarize_toggle_off_defers_nothing(monkeypatch):
    """Toggle OFF (default): no paper.summarize defer, even for an unsummarized paper."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    _configure_conn_for_process_one_paper(conn, auto_summarize_value=False)
    app = _to_process_app(conn)

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with (
        patch.object(
            af,
            "run_process_pdf",
            AsyncMock(return_value={"paper_id": 7, "chunk_count": 3, "status": "processed"}),
        ),
        patch.dict(task_registry._TASK_MAP, {"paper.summarize": mock_task}),
    ):
        await af.run_auto_pipeline(app)

    mock_task.defer_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_summarize_defers_once_per_library_holder(monkeypatch):
    """A paper held by N users defers N summarize jobs, one owned by each holder."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    _configure_conn_for_process_one_paper(
        conn, auto_summarize_value=True, holders=({"user_id": 3}, {"user_id": 4})
    )
    app = _to_process_app(conn)

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with (
        patch.object(
            af,
            "run_process_pdf",
            AsyncMock(return_value={"paper_id": 7, "chunk_count": 3, "status": "processed"}),
        ),
        patch.dict(task_registry._TASK_MAP, {"paper.summarize": mock_task}),
    ):
        await af.run_auto_pipeline(app)

    assert mock_task.defer_async.await_count == 2
    owners = sorted(call.kwargs["user_id"] for call in mock_task.defer_async.await_args_list)
    assert owners == [3, 4]
    assert all(call.kwargs["paper_id"] == 7 for call in mock_task.defer_async.await_args_list)


@pytest.mark.asyncio
async def test_auto_summarize_defers_nothing_when_no_user_holds_the_paper(monkeypatch):
    """A paper in nobody's library defers ZERO jobs — summarizing it would be
    LLM spend on content no reader can ever see."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    _configure_conn_for_process_one_paper(conn, auto_summarize_value=True, holders=())
    app = _to_process_app(conn)

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with (
        patch.object(
            af,
            "run_process_pdf",
            AsyncMock(return_value={"paper_id": 7, "chunk_count": 3, "status": "processed"}),
        ),
        patch.dict(task_registry._TASK_MAP, {"paper.summarize": mock_task}),
    ):
        await af.run_auto_pipeline(app)

    mock_task.defer_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_summarize_defer_failure_is_swallowed(monkeypatch, caplog):
    """A raising paper.summarize defer is logged via logger.exception and does not
    escape to run_auto_pipeline's outer 'unhandled error' guard."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    _configure_conn_for_process_one_paper(conn, auto_summarize_value=True)
    app = _to_process_app(conn)

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(side_effect=RuntimeError("broker down"))

    # NOTE: do not mock af.logger.error directly — Logger.exception() delegates
    # to self.error() internally, so a mocked error() would silently swallow
    # the record caplog is meant to observe. caplog attaches its own handler
    # instead, leaving the real logger methods (and their delegation) intact.
    with (
        patch.object(
            af,
            "run_process_pdf",
            AsyncMock(return_value={"paper_id": 7, "chunk_count": 3, "status": "processed"}),
        ),
        patch.dict(task_registry._TASK_MAP, {"paper.summarize": mock_task}),
        caplog.at_level("ERROR", logger=af.logger.name),
    ):
        await af.run_auto_pipeline(app)

    mock_task.defer_async.assert_awaited_once()  # the attempt was made
    assert not any("unhandled error" in rec.getMessage() for rec in caplog.records), (
        "defer failure must be swallowed inside the guarded-defer block, "
        "never surfaced to the outer pipeline error handler"
    )
    enqueue_failed = [
        rec
        for rec in caplog.records
        if rec.levelname == "ERROR"
        and "paper.summarize enqueue failed for paper 7" in rec.getMessage()
    ]
    assert len(enqueue_failed) == 1, (
        f"expected exactly one enqueue-failed ERROR record; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Last-run stamp — the scheduler's catch-up reads it, so it must record only
# runs that actually completed.
# ---------------------------------------------------------------------------


def _last_run_writes(conn) -> list:
    """Every conn.execute call that persists the last-run stamp."""
    return [
        call for call in conn.execute.await_args_list if af.AUTO_PIPELINE_LAST_RUN_KEY in call.args
    ]


@pytest.mark.asyncio
async def test_completed_run_records_the_last_run_stamp(monkeypatch):
    """A run that reaches the end persists exactly one stamp, carrying its time."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[[], [], [], []])

    await af.run_auto_pipeline(_make_app(conn))

    writes = _last_run_writes(conn)
    assert len(writes) == 1, f"expected exactly one last-run write; got {len(writes)}"
    stamp = writes[0].args[2]
    assert datetime.fromisoformat(stamp).tzinfo is not None, (
        f"the stamp must be an unambiguous timestamp; got {stamp!r}"
    )


@pytest.mark.asyncio
async def test_failed_run_leaves_the_last_run_stamp_unmoved(monkeypatch):
    """A run whose discovery raises must NOT record a stamp — otherwise the next
    boot believes the pipeline succeeded and skips the catch-up it needs."""
    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "1")

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[[], []])

    with patch.object(
        af, "_discover_and_save", AsyncMock(side_effect=RuntimeError("discovery unavailable"))
    ):
        await af.run_auto_pipeline(_make_app(conn))

    assert _last_run_writes(conn) == [], "a failed run must not move the last-run stamp"
