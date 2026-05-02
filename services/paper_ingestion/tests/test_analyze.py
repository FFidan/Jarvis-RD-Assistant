"""Tests for the analyze SSE endpoint (download -> process -> summarize).

These tests avoid importing ``app.main`` (which triggers fitz/numpy) by
injecting a fake module into ``sys.modules`` before the deferred imports
inside ``_analyze_stream`` run.
"""

import json
import os
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.routers._sse import SSE_DONE, sse_event
from paper_ingestion.routers.analyze import _analyze_stream

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse_events(raw_events: list[str]) -> list[Any]:
    """Parse raw SSE frame strings into dicts (or raw strings for [DONE])."""
    results: list[dict | str] = []
    for raw in raw_events:
        if not raw.startswith("data: "):
            continue
        payload = raw.replace("data: ", "").strip()
        if payload == "[DONE]":
            results.append("[DONE]")
        else:
            results.append(json.loads(payload))
    return results


def _make_mock_request(paper_row, *, update_row=None):
    """Build a MagicMock Request with a mock db_pool returning paper_row.

    Returns a tuple of (mock_request, mock_pool, deps) — ``deps`` is a dict
    containing the http_client / pdf_processor / embedder / verifier stubs
    that callers pass explicitly to ``_analyze_stream``.
    """
    mock_request = MagicMock()

    mock_conn = AsyncMock()
    if update_row is not None:
        mock_conn.fetchrow = AsyncMock(side_effect=[paper_row, update_row])
    else:
        mock_conn.fetchrow = AsyncMock(return_value=paper_row)
    mock_conn.transaction = MagicMock(return_value=AsyncMock())

    mock_pool = AsyncMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock())
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    pdf_processor = MagicMock()
    pdf_processor.download_pdf = AsyncMock(return_value="/data/pdfs/1.pdf")
    deps = {
        "http_client": AsyncMock(),
        "pdf_processor": pdf_processor,
        "embedder": MagicMock(),
        "verifier": MagicMock(),
    }

    return mock_request, mock_pool, deps


async def _collect_events(
    monkeypatch,
    request,
    paper_id,
    db_pool,
    deps,
    *,
    mock_process=None,
    mock_summarize=None,
):
    """Run _analyze_stream with fake service modules to avoid heavy imports.

    Uses monkeypatch.setitem so stubs are scoped to the current test and
    automatically reversed on teardown — no manual save/restore needed.
    """
    _mock_process = mock_process or AsyncMock(
        return_value={"paper_id": paper_id, "chunk_count": 42, "status": "processed"}
    )
    _mock_summarize = mock_summarize or AsyncMock(return_value=MagicMock())

    # Inject fake modules so deferred imports in _analyze_stream resolve
    # without pulling in qdrant_client/numpy/fitz.
    fake_pdf_workflow = types.ModuleType("paper_ingestion.services.pdf_workflow")
    fake_pdf_workflow.run_process_pdf = _mock_process  # type: ignore[attr-defined]

    fake_summarization = types.ModuleType("paper_ingestion.services.summarization")
    fake_summarization.generate_paper_summary = _mock_summarize  # type: ignore[attr-defined]

    # Ensure the parent packages exist in sys.modules
    if "paper_ingestion.services" not in sys.modules:
        fake_services = types.ModuleType("paper_ingestion.services")
        fake_services.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "paper_ingestion.services", fake_services)

    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", fake_pdf_workflow)
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", fake_summarization)

    _real_env_get = os.environ.get

    def _selective_env_get(key, default=None):
        if key == "PDF_STORAGE_PATH":
            return "/data/pdfs"
        return _real_env_get(key, default)

    with (
        patch("paper_ingestion.routers.analyze.Path") as MockPath,  # noqa: N806
        patch("paper_ingestion.routers.analyze.os.environ.get", side_effect=_selective_env_get),
    ):
        mock_path_instance = MagicMock()
        mock_path_instance.resolve.return_value.is_relative_to.return_value = True
        mock_path_instance.exists.return_value = True
        MockPath.return_value = mock_path_instance

        events_raw = []
        async for event in _analyze_stream(
            request,
            paper_id,
            db_pool,
            deps["http_client"],
            deps["pdf_processor"],
            deps["embedder"],
            deps["verifier"],
        ):
            events_raw.append(event)

    return _parse_sse_events(events_raw), _mock_process


# ---------------------------------------------------------------------------
# Test sse_event helper (from paper_ingestion.routers._sse)
# ---------------------------------------------------------------------------


def test_sse_event_dict():
    result = sse_event({"type": "step", "step": "downloading", "status": "started"})
    assert result.startswith("data: ")
    assert result.endswith("\n\n")
    parsed = json.loads(result[6:])
    assert parsed["type"] == "step"
    assert parsed["step"] == "downloading"


def test_sse_done_constant():
    assert SSE_DONE == "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Test _analyze_stream — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_stream_happy_path(monkeypatch):
    """Full chain yields download->process->summarize step events + complete + [DONE]."""
    paper_row = {
        "id": 1,
        "source_type": "arxiv",
        "pdf_url": "https://arxiv.org/pdf/test.pdf",
        "pdf_downloaded": False,
        "pdf_local_path": None,
    }
    updated_row = {
        **paper_row,
        "pdf_downloaded": True,
        "pdf_local_path": "/data/pdfs/1.pdf",
    }
    mock_request, mock_pool, deps = _make_mock_request(paper_row, update_row=updated_row)

    events, _ = await _collect_events(monkeypatch, mock_request, 1, mock_pool, deps)

    assert len(events) == 8
    assert events[0] == {"type": "step", "step": "downloading", "status": "started"}
    assert events[1] == {"type": "step", "step": "downloading", "status": "completed"}
    assert events[2] == {"type": "step", "step": "processing", "status": "started"}
    assert events[3]["type"] == "step"
    assert events[3]["step"] == "processing"
    assert events[3]["status"] == "completed"
    assert events[3]["chunk_count"] == 42
    assert events[4] == {"type": "step", "step": "summarizing", "status": "started"}
    assert events[5] == {"type": "step", "step": "summarizing", "status": "completed"}
    assert events[6] == {"type": "complete", "paper_id": 1}
    assert events[7] == "[DONE]"


# ---------------------------------------------------------------------------
# Test _analyze_stream — paper not found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_stream_paper_not_found():
    """Stream emits error when paper ID doesn't exist."""
    mock_request, mock_pool, deps = _make_mock_request(None)

    events_raw = []
    async for event in _analyze_stream(
        mock_request,
        999,
        mock_pool,
        deps["http_client"],
        deps["pdf_processor"],
        deps["embedder"],
        deps["verifier"],
    ):
        events_raw.append(event)
    events = _parse_sse_events(events_raw)

    assert len(events) == 3
    assert events[0] == {"type": "step", "step": "downloading", "status": "started"}
    assert events[1]["type"] == "error"
    assert events[1]["step"] == "downloading"
    assert "not found" in events[1]["message"]
    assert events[2] == "[DONE]"


# ---------------------------------------------------------------------------
# Test _analyze_stream — no pdf_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_stream_no_pdf_url():
    """Stream emits error when a non-local paper has no PDF URL."""
    paper_row = {
        "id": 2,
        "source_type": "arxiv",
        "pdf_url": None,
        "pdf_downloaded": False,
        "pdf_local_path": None,
    }
    mock_request, mock_pool, deps = _make_mock_request(paper_row)

    events_raw = []
    async for event in _analyze_stream(
        mock_request,
        2,
        mock_pool,
        deps["http_client"],
        deps["pdf_processor"],
        deps["embedder"],
        deps["verifier"],
    ):
        events_raw.append(event)
    events = _parse_sse_events(events_raw)

    assert len(events) == 3
    assert events[1]["type"] == "error"
    assert "no PDF URL" in events[1]["message"]


# ---------------------------------------------------------------------------
# Test _analyze_stream — already downloaded, skips download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_stream_already_downloaded(monkeypatch):
    """If paper already has PDF downloaded (pdf_local_path set), skips download.

    B6 note: when pdf_local_path is already set, is_local=True, so the step
    yields 'skipped' rather than 'completed'. Both skip the actual HTTP download.
    """
    paper_row = {
        "id": 2,
        "source_type": "arxiv",
        "pdf_url": "https://arxiv.org/pdf/test.pdf",
        "pdf_downloaded": True,
        "pdf_local_path": "/data/pdfs/2.pdf",
    }
    mock_request, mock_pool, deps = _make_mock_request(paper_row)

    events, _ = await _collect_events(monkeypatch, mock_request, 2, mock_pool, deps)

    assert len(events) == 8
    assert events[0] == {"type": "step", "step": "downloading", "status": "started"}
    # pdf_local_path is set → treated as local → skipped (not an error)
    assert events[1]["type"] == "step"
    assert events[1]["step"] == "downloading"
    assert events[1]["status"] == "skipped"

    # download_pdf should NOT have been called
    deps["pdf_processor"].download_pdf.assert_not_called()


# ---------------------------------------------------------------------------
# Test _analyze_stream — process failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_stream_process_failure(monkeypatch):
    """Stream emits error event when processing fails."""
    paper_row = {
        "id": 3,
        "source_type": "arxiv",
        "pdf_url": "https://arxiv.org/pdf/test.pdf",
        "pdf_downloaded": True,
        "pdf_local_path": "/data/pdfs/3.pdf",
    }
    mock_request, mock_pool, deps = _make_mock_request(paper_row)

    mock_process = AsyncMock(side_effect=RuntimeError("Embedding service error"))
    events, _ = await _collect_events(
        monkeypatch, mock_request, 3, mock_pool, deps, mock_process=mock_process
    )

    # download started, download completed, process started, error, [DONE]
    assert len(events) == 5
    assert events[3]["type"] == "error"
    # C1: processing errors now use stage/error_type/error_detail (not step/message)
    assert events[3]["stage"] == "process_pdf"
    assert events[4] == "[DONE]"


# ---------------------------------------------------------------------------
# Test _analyze_stream — summarize failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_stream_summarize_failure(monkeypatch):
    """Stream emits error event when summarization fails."""
    paper_row = {
        "id": 4,
        "source_type": "arxiv",
        "pdf_url": "https://arxiv.org/pdf/test.pdf",
        "pdf_downloaded": True,
        "pdf_local_path": "/data/pdfs/4.pdf",
    }
    mock_request, mock_pool, deps = _make_mock_request(paper_row)

    mock_summarize = AsyncMock(side_effect=RuntimeError("LLM timeout"))
    events, _ = await _collect_events(
        monkeypatch, mock_request, 4, mock_pool, deps, mock_summarize=mock_summarize
    )

    # download started/completed, process started/completed, summarize started, error, [DONE]
    assert len(events) == 7
    assert events[5]["type"] == "error"
    assert events[5]["step"] == "summarizing"
    assert events[6] == "[DONE]"


# ---------------------------------------------------------------------------
# Test _analyze_stream — B6: local paper skips download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_stream_local_paper_skips_download(monkeypatch):
    """B6: local paper (source_type='local') skips download step, processes to completion."""
    paper_row = {
        "id": 5,
        "source_type": "local",
        "pdf_url": None,  # local papers have no pdf_url — was the original bug
        "pdf_downloaded": True,
        "pdf_local_path": "/data/pdfs/5.pdf",
    }
    mock_request, mock_pool, deps = _make_mock_request(paper_row)

    events, _ = await _collect_events(monkeypatch, mock_request, 5, mock_pool, deps)

    # Expect: download started, download skipped, process started, process completed,
    #         summarize started, summarize completed, complete, [DONE] = 8 events
    assert len(events) == 8, f"Expected 8 events, got {len(events)}: {events}"
    assert events[0] == {"type": "step", "step": "downloading", "status": "started"}
    # skipped event (not an error)
    assert events[1]["type"] == "step"
    assert events[1]["step"] == "downloading"
    assert events[1]["status"] == "skipped"
    assert events[1].get("reason") == "local paper"
    # Processing proceeds normally
    assert events[2] == {"type": "step", "step": "processing", "status": "started"}
    assert events[3]["type"] == "step"
    assert events[3]["step"] == "processing"
    assert events[3]["status"] == "completed"
    assert events[4] == {"type": "step", "step": "summarizing", "status": "started"}
    assert events[5] == {"type": "step", "step": "summarizing", "status": "completed"}
    assert events[6] == {"type": "complete", "paper_id": 5}
    assert events[7] == "[DONE]"

    # download_pdf must NOT have been called for a local paper
    deps["pdf_processor"].download_pdf.assert_not_called()


# ---------------------------------------------------------------------------
# Test _analyze_stream — C1: structured PDF error event
# ---------------------------------------------------------------------------


class _FakePdfReadError(Exception):
    """Fake exception mimicking pypdf.errors.PdfReadError for test isolation."""


@pytest.mark.asyncio
async def test_analyze_stream_process_failure_structured_error(monkeypatch):
    """C1: When run_process_pdf raises, the SSE error event has structured fields.

    Asserts schema: {"type": "error", "stage": "process_pdf",
                     "error_type": <class name>, "error_detail": <str(exc)[:200]>}
    """
    paper_row = {
        "id": 7,
        "source_type": "arxiv",
        "pdf_url": "https://arxiv.org/pdf/test.pdf",
        "pdf_downloaded": True,
        "pdf_local_path": "/data/pdfs/7.pdf",
    }
    mock_request, mock_pool, deps = _make_mock_request(paper_row)

    error_msg = "Stream end was reached early"
    mock_process = AsyncMock(side_effect=_FakePdfReadError(error_msg))
    events, _ = await _collect_events(
        monkeypatch, mock_request, 7, mock_pool, deps, mock_process=mock_process
    )

    # download started/skipped (pdf_local_path set → skipped), process started, error, [DONE]
    error_events = [e for e in events if isinstance(e, dict) and e.get("type") == "error"]
    assert len(error_events) == 1, f"Expected exactly 1 error event; got: {error_events}"

    err = error_events[0]
    assert err["type"] == "error"
    assert err["stage"] == "process_pdf"
    assert err["error_type"] == "_FakePdfReadError"
    assert err["error_detail"] == error_msg
    # Old "step" / "message" keys ALSO present so the existing FE step-tracker +
    # per-stage Retry button (W1.6-F) keep working — backend emits both shapes.
    assert err["step"] == "processing"
    assert err["message"] == "PDF processing failed"
    # Last event is [DONE]
    assert events[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_analyze_stream_process_failure_error_detail_truncated(monkeypatch):
    """C1: error_detail is capped at 200 characters."""
    paper_row = {
        "id": 8,
        "source_type": "arxiv",
        "pdf_url": "https://arxiv.org/pdf/test.pdf",
        "pdf_downloaded": True,
        "pdf_local_path": "/data/pdfs/8.pdf",
    }
    mock_request, mock_pool, deps = _make_mock_request(paper_row)

    long_msg = "x" * 300
    mock_process = AsyncMock(side_effect=RuntimeError(long_msg))
    events, _ = await _collect_events(
        monkeypatch, mock_request, 8, mock_pool, deps, mock_process=mock_process
    )

    error_events = [e for e in events if isinstance(e, dict) and e.get("type") == "error"]
    assert len(error_events) == 1
    assert len(error_events[0]["error_detail"]) == 200


@pytest.mark.asyncio
async def test_analyze_stream_local_paper_with_pdf_local_path(monkeypatch):
    """B6: paper with pdf_local_path already set (any source_type) skips download."""
    paper_row = {
        "id": 6,
        "source_type": "arxiv",  # not 'local' source_type, but has pdf_local_path
        "pdf_url": "https://arxiv.org/pdf/6.pdf",
        "pdf_downloaded": True,
        "pdf_local_path": "/data/pdfs/6.pdf",  # already set → treated as local
    }
    mock_request, mock_pool, deps = _make_mock_request(paper_row)

    events, _ = await _collect_events(monkeypatch, mock_request, 6, mock_pool, deps)

    # download step should be skipped (pdf_local_path is already set)
    assert events[1]["type"] == "step"
    assert events[1]["step"] == "downloading"
    assert events[1]["status"] == "skipped"

    deps["pdf_processor"].download_pdf.assert_not_called()
