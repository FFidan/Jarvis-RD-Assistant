"""Unit tests for app.extraction_jobs — extraction.batch job handler.

Happy path: valid payload is processed and result counts are returned.
Edge case: missing template_id raises KeyError before batch_extract is called.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeCtx:
    """Minimal JobContext substitute."""

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        pass

    async def is_cancelled(self) -> bool:
        return False


def _install_fake_batch_extract(monkeypatch, result) -> None:
    """Stub app.extraction so batch_extract returns a controlled result."""
    fake_mod = types.ModuleType("paper_ingestion.extraction")
    fake_mod.batch_extract = AsyncMock(return_value=result)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paper_ingestion.extraction", fake_mod)


def _install_fake_app(monkeypatch, *, embedder=None, verifier=None) -> None:
    """Populate svc so job handlers can resolve embedder/verifier."""
    from paper_ingestion.extraction import jobs as jobs_mod  # noqa: PLC0415

    services = jobs_mod.get_services()
    services.embedder = embedder
    services.verifier = verifier


@pytest.mark.asyncio
async def test_extraction_batch_happy_path(monkeypatch):
    """Handler returns extracted/failed/skipped/total from batch_extract result."""
    from paper_ingestion.extraction.jobs import _extraction_batch_job

    fake_result = SimpleNamespace(extracted=5, failed=1, skipped=2)
    _install_fake_batch_extract(monkeypatch, fake_result)
    embedder = MagicMock()
    verifier = MagicMock()
    _install_fake_app(monkeypatch, embedder=embedder, verifier=verifier)

    pool = MagicMock()
    http_client = MagicMock()
    ctx = _FakeCtx()
    payload = {"paper_ids": [10, 20, 30, 40, 50, 60, 70, 80], "template_id": 3}

    result = await _extraction_batch_job(pool, http_client, payload, ctx)

    assert result["extracted"] == 5
    assert result["failed"] == 1
    assert result["skipped"] == 2
    assert result["total"] == 8

    # batch_extract should have been called once with pool, http_client, ids, template_id
    # Use sys.modules directly: `import pkg.mod` after the real module was already loaded
    # returns the real module via package attribute lookup, bypassing the sys.modules stub.
    import sys as _sys

    extraction_mod = _sys.modules["paper_ingestion.extraction"]  # always the stub

    extraction_mod.batch_extract.assert_awaited_once()
    call_args = extraction_mod.batch_extract.await_args
    assert call_args.args[2] == [10, 20, 30, 40, 50, 60, 70, 80]
    assert call_args.args[3] == 3
    assert call_args.kwargs["embedder"] is embedder
    assert call_args.kwargs["verifier"] is verifier


@pytest.mark.asyncio
async def test_extraction_batch_missing_template_id(monkeypatch):
    """Handler raises KeyError immediately when template_id is absent from payload."""
    from paper_ingestion.extraction.jobs import _extraction_batch_job

    # batch_extract should never be called
    fake_mod = types.ModuleType("paper_ingestion.extraction")
    fake_mod.batch_extract = AsyncMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "paper_ingestion.extraction", fake_mod)
    _install_fake_app(monkeypatch)

    with pytest.raises(KeyError):
        await _extraction_batch_job(MagicMock(), MagicMock(), {"paper_ids": [1, 2]}, _FakeCtx())

    fake_mod.batch_extract.assert_not_awaited()
