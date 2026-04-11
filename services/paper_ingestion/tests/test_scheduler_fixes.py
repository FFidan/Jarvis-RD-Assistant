"""Tests for scheduler.py bug fixes (C-1: http_client arg, C-2: path traversal guard)."""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module-level stubs for packages unavailable outside Docker.
# Must happen before ``import app.scheduler``.
# ---------------------------------------------------------------------------
_STUBS: dict[str, MagicMock] = {}


def _ensure_stub(name: str) -> MagicMock:
    if name not in sys.modules:
        mock = MagicMock()
        sys.modules[name] = mock
        _STUBS[name] = mock
    return sys.modules[name]


# apscheduler (top-level import in scheduler.py)
for _mod in (
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers",
    "apscheduler.triggers.interval",
    "apscheduler.triggers.cron",
):
    _ensure_stub(_mod)

# pdf_processor needs fitz, litellm, etc.  We only need PDF_STORAGE_PATH.
_pdf_proc_stub = _ensure_stub("app.pdf_processor")
_pdf_proc_stub.PDF_STORAGE_PATH = "/data/pdfs"

# app.main – heavy; stub it to avoid importing FastAPI app initialization.
_main_stub = _ensure_stub("app.main")

# app.services.pdf_workflow – provides _upsert_paper and _run_process_pdf to the scheduler.
_ensure_stub("app.services")
_workflow_stub = _ensure_stub("app.services.pdf_workflow")
_workflow_stub._upsert_paper = AsyncMock()
_workflow_stub._run_process_pdf = AsyncMock()

# Keep a reference under _main_stub for backward-compat with assertion helpers below.
_main_stub._upsert_paper = _workflow_stub._upsert_paper
_main_stub._run_process_pdf = _workflow_stub._run_process_pdf

# Now safe to import
from app.scheduler import run_auto_pipeline  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_state(
    *,
    sources_rows: list | None = None,
    topics_rows: list | None = None,
    to_download: list | None = None,
    to_process: list | None = None,
) -> SimpleNamespace:
    """Build a fake ``app`` object that mirrors FastAPI app.state.

    Parameters
    ----------
    sources_rows : list | None
        Rows returned for the ``paper_sources`` query.
    topics_rows : list | None
        Rows returned for the ``topics`` query.
    to_download : list | None
        Rows returned for the download query.
    to_process : list | None
        Rows returned for the process query.

    Returns
    -------
    SimpleNamespace
        A minimal ``app`` stand-in with ``app.state.*`` attributes.
    """
    if sources_rows is None:
        sources_rows = []
    if topics_rows is None:
        topics_rows = []
    if to_download is None:
        to_download = []
    if to_process is None:
        to_process = []

    # Each call to conn.fetch returns the next result set in order.
    fetch_results = iter([sources_rows, topics_rows, to_download, to_process])

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=lambda *_a, **_kw: next(fetch_results))

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    http_client = MagicMock(name="http_client")
    pdf_processor = MagicMock(name="pdf_processor")
    embedder = MagicMock(name="embedder")

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=http_client,
            pdf_processor=pdf_processor,
            embedder=embedder,
        ),
    )
    return app


# ---------------------------------------------------------------------------
# C-1: source is instantiated with http_client
# ---------------------------------------------------------------------------


async def test_source_instantiated_with_http_client() -> None:
    """Source class must be called with (config, http_client), not just (config)."""
    fake_source_instance = AsyncMock()
    fake_source_instance.search = AsyncMock(return_value=[])

    fake_source_class = MagicMock(return_value=fake_source_instance)

    source_row = {
        "id": 1,
        "source_type": "arxiv",
        "enabled": True,
        "config": {},
    }
    topic_row = {"name": "transformers"}

    app = _make_app_state(
        sources_rows=[source_row],
        topics_rows=[topic_row],
    )

    with patch(
        "app.sources.registry.get_source_class",
        return_value=fake_source_class,
    ):
        await run_auto_pipeline(app)

    # The source class must have been called with two positional args
    fake_source_class.assert_called_once()
    call_args = fake_source_class.call_args
    assert len(call_args.args) == 2, (
        f"Expected 2 positional args (config, http_client), got {len(call_args.args)}"
    )
    assert call_args.args[1] is app.state.http_client


# ---------------------------------------------------------------------------
# C-2: path traversal is rejected
# ---------------------------------------------------------------------------


async def test_path_traversal_pdf_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Papers whose pdf_local_path escapes the storage dir must be skipped."""
    traversal_row = {
        "id": 42,
        "pdf_local_path": "/data/pdfs/../../etc/passwd",
    }

    app = _make_app_state(to_process=[traversal_row])

    # Reset the shared mock so we can assert it was NOT called
    _main_stub._run_process_pdf.reset_mock()

    with caplog.at_level(logging.WARNING, logger="app.scheduler"):
        await run_auto_pipeline(app)

    # _run_process_pdf must NOT have been called for the traversal path
    _main_stub._run_process_pdf.assert_not_called()

    # The warning must have been logged
    assert any(
        "Skipping paper 42" in msg and "outside storage dir" in msg for msg in caplog.messages
    ), f"Expected traversal warning, got: {caplog.messages}"


async def test_valid_pdf_path_is_processed() -> None:
    """Papers with a pdf_local_path inside the storage dir should be processed."""
    valid_row = {
        "id": 7,
        "pdf_local_path": "/data/pdfs/paper_7.pdf",
    }

    app = _make_app_state(to_process=[valid_row])

    # Reset the shared mock
    _main_stub._run_process_pdf.reset_mock()

    await run_auto_pipeline(app)

    # _run_process_pdf should have been called for the valid path
    _main_stub._run_process_pdf.assert_called_once()
    call_args = _main_stub._run_process_pdf.call_args
    assert call_args.args[0] == 7
    assert call_args.args[1] == Path("/data/pdfs/paper_7.pdf")
