"""Unit tests for papers.batch_summarize job handler.

Validates that the handler iterates through paper_ids, delegates to
generate_paper_summary, tracks successes/failures, and reports progress.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# We build the embedder fake here but scope its installation to each test via the
# autouse fixture below — this prevents polluting test_embedder.py which needs its
# own precise qdrant_client.models stubs.
_fake_embedder_mod = types.ModuleType("paper_ingestion.embedder")
_fake_embedder_mod.Embedder = MagicMock()  # type: ignore[attr-defined]
_fake_embedder_mod.COLLECTION_NAME = "paper_chunks"  # type: ignore[attr-defined]
_fake_embedder_mod.EMBEDDING_MODEL_NAME = "embed-model"  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch):
    """Scope the paper_ingestion.embedder fake to each test only."""
    monkeypatch.setitem(sys.modules, "paper_ingestion.embedder", _fake_embedder_mod)
    # Force paper_jobs to re-import with the scoped embedder stub.
    monkeypatch.delitem(sys.modules, "paper_ingestion.paper_jobs", raising=False)
    # Reset svc after each test to avoid cross-test pollution.
    yield
    import paper_ingestion._state as _state_mod  # noqa: PLC0415

    _state_mod.svc.verifier = None
    _state_mod.svc.embedder = None


class _FakeCtx:
    """Minimal JobContext substitute capturing progress calls."""

    def __init__(self, cancelled: bool = False) -> None:
        self.progress_calls: list[tuple[float, str | None]] = []
        self._cancelled = cancelled

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        self.progress_calls.append((progress, message))

    async def is_cancelled(self) -> bool:
        return self._cancelled


def _install_fake_app(monkeypatch, *, verifier, embedder) -> None:
    """Populate svc with stub objects so job handlers can resolve verifier/embedder."""
    import paper_ingestion._state as _state_mod  # noqa: PLC0415

    _state_mod.svc.verifier = verifier
    _state_mod.svc.embedder = embedder


def _install_fake_summarization(monkeypatch, mock_fn) -> None:
    """Install a stub app.services.summarization module with generate_paper_summary."""
    fake_mod = types.ModuleType("paper_ingestion.services.summarization")
    fake_mod.generate_paper_summary = mock_fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.summarization", fake_mod)


@pytest.mark.asyncio
async def test_batch_summarize_happy_path(monkeypatch):
    """All papers succeed: summarized count matches paper_ids length."""
    from paper_ingestion.paper_jobs import _papers_batch_summarize_job

    verifier = MagicMock()
    embedder = MagicMock()
    _install_fake_app(monkeypatch, verifier=verifier, embedder=embedder)
    mock_gen = AsyncMock(return_value=None)
    _install_fake_summarization(monkeypatch, mock_gen)

    pool = MagicMock()
    http_client = MagicMock()
    ctx = _FakeCtx()

    result = await _papers_batch_summarize_job(pool, http_client, {"paper_ids": [1, 2, 3]}, ctx)

    assert result["summarized"] == 3
    assert result["failed"] == 0
    assert result["errors"] == []
    assert mock_gen.await_count == 3
    # First call, positional args: (paper_id, pool, http_client, verifier, embedder)
    first_args = mock_gen.await_args_list[0].args
    assert first_args[0] == 1
    assert first_args[1] is pool
    assert first_args[2] is http_client
    assert first_args[3] is verifier
    assert first_args[4] is embedder
    # Progress should start at 0 and end at 1.0
    assert ctx.progress_calls[0][0] == 0.0
    assert ctx.progress_calls[-1][0] == 1.0


@pytest.mark.asyncio
async def test_batch_summarize_partial_failure(monkeypatch):
    """If one paper raises, it is counted as failed; others succeed."""
    from paper_ingestion.paper_jobs import _papers_batch_summarize_job

    _install_fake_app(monkeypatch, verifier=MagicMock(), embedder=MagicMock())

    async def _gen(paper_id, *_args, **_kwargs):
        if paper_id == 2:
            raise RuntimeError("boom")
        return None

    mock_gen = AsyncMock(side_effect=_gen)
    _install_fake_summarization(monkeypatch, mock_gen)

    result = await _papers_batch_summarize_job(
        MagicMock(), MagicMock(), {"paper_ids": [1, 2, 3]}, _FakeCtx()
    )

    assert result["summarized"] == 2
    assert result["failed"] == 1
    assert len(result["errors"]) == 1
    assert "Paper 2" in result["errors"][0]
    assert "boom" in result["errors"][0]


@pytest.mark.asyncio
async def test_batch_summarize_respects_cancellation(monkeypatch):
    """If ctx.is_cancelled() is true, loop exits without processing any paper."""
    from paper_ingestion.paper_jobs import _papers_batch_summarize_job

    _install_fake_app(monkeypatch, verifier=MagicMock(), embedder=MagicMock())
    mock_gen = AsyncMock()
    _install_fake_summarization(monkeypatch, mock_gen)

    ctx = _FakeCtx(cancelled=True)
    result = await _papers_batch_summarize_job(
        MagicMock(), MagicMock(), {"paper_ids": [1, 2, 3]}, ctx
    )

    assert result["summarized"] == 0
    assert result["failed"] == 0
    mock_gen.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_summarize_empty_payload(monkeypatch):
    """Empty paper_ids list: handler completes cleanly with zero counts."""
    from paper_ingestion.paper_jobs import _papers_batch_summarize_job

    _install_fake_app(monkeypatch, verifier=MagicMock(), embedder=MagicMock())
    mock_gen = AsyncMock()
    _install_fake_summarization(monkeypatch, mock_gen)

    ctx = _FakeCtx()
    result = await _papers_batch_summarize_job(MagicMock(), MagicMock(), {"paper_ids": []}, ctx)

    assert result == {"summarized": 0, "failed": 0, "errors": []}
    mock_gen.assert_not_awaited()
    # Should still emit start (0.0) + end (1.0) progress
    assert ctx.progress_calls[0][0] == 0.0
    assert ctx.progress_calls[-1][0] == 1.0
