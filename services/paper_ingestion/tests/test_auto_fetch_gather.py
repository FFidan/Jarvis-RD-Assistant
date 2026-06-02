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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.pipelines import auto_fetch as af


def _make_app(conn) -> SimpleNamespace:
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
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
