"""Unit tests for papers.batch_summarize job handler.

Validates that the handler iterates through paper_ids, delegates to
generate_paper_summary, tracks successes/failures, and reports progress.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "libs" / "jarvis_common"))
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
    """Install a stub app.main module exposing app.state.{verifier, embedder}."""
    fake_main = types.ModuleType("app.main")
    fake_app = SimpleNamespace(state=SimpleNamespace(verifier=verifier, embedder=embedder))
    fake_main.app = fake_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.main", fake_main)


def _install_fake_summarization(monkeypatch, mock_fn) -> None:
    """Install a stub app.services.summarization module with generate_paper_summary."""
    fake_mod = types.ModuleType("app.services.summarization")
    fake_mod.generate_paper_summary = mock_fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.services.summarization", fake_mod)


@pytest.mark.asyncio
async def test_batch_summarize_happy_path(monkeypatch):
    """All papers succeed: summarized count matches paper_ids length."""
    from app.paper_jobs import _papers_batch_summarize_job

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
    from app.paper_jobs import _papers_batch_summarize_job

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
    from app.paper_jobs import _papers_batch_summarize_job

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
    from app.paper_jobs import _papers_batch_summarize_job

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
