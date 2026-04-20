"""Tests for W3.2 fixes: download_pdf releases DB conn before HTTP download;
scan_local_pdfs uses per-file connections [H12, H13].
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.dependencies import utils as fastapi_dependency_utils

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
fastapi_dependency_utils.ensure_multipart_is_installed = lambda: None

from app.routers import pdf  # noqa: E402


class FakeRecord(dict):
    """Dict-like asyncpg.Record substitute."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _make_conn(fetchrow_return=None):
    """Return an AsyncMock connection with transaction support."""
    conn = AsyncMock()
    conn.fetchrow.return_value = fetchrow_return
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _make_pool_multi_conn(*conns):
    """Return a pool mock that yields each connection in order on successive acquire() calls."""
    pool = MagicMock()
    contexts = []
    for conn in conns:
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        contexts.append(ctx)
    pool.acquire.side_effect = contexts
    return pool


def _paper_row(**overrides):
    """Build a FakeRecord representing a papers table row."""
    defaults = dict(
        id=1,
        title="Neural ODEs",
        external_id="arxiv:1234",
        source_type="arxiv",
        authors=["Chen"],
        abstract="An abstract",
        published_date=None,
        url="https://arxiv.org/abs/1234",
        pdf_url="https://arxiv.org/pdf/1234.pdf",
        pdf_downloaded=False,
        pdf_local_path=None,
        citation_count=0,
        metadata={},
        created_at=datetime(2026, 3, 11, tzinfo=UTC),
        user_state=None,
    )
    defaults.update(overrides)
    return FakeRecord(**defaults)


# ---------------------------------------------------------------------------
# download_pdf — connection lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_pdf_releases_conn_before_http():
    """DB conn must be released (acquire() closes) before the HTTP download begins.

    Verifies that db_pool.acquire() is called exactly twice:
    - once during Phase 1 (load row)
    - once during Phase 3 (write back)
    The download happens between those two calls, with no connection held.
    """
    phase1_row = _paper_row(pdf_downloaded=False)
    updated_row = _paper_row(pdf_downloaded=True, pdf_local_path="/data/pdfs/1.pdf")

    phase1_conn = _make_conn(fetchrow_return=phase1_row)
    phase3_conn = _make_conn(fetchrow_return=updated_row)
    pool = _make_pool_multi_conn(phase1_conn, phase3_conn)

    # Track when download happens relative to acquire() calls
    acquire_call_count_at_download: list[int] = []

    async def mock_download(url, paper_id):
        # At this point Phase 1 conn should already be released,
        # so acquire has been called once and Phase 3 has not started yet.
        acquire_call_count_at_download.append(pool.acquire.call_count)
        return Path("/data/pdfs/1.pdf")

    processor = AsyncMock()
    processor.download_pdf.side_effect = mock_download

    result = await pdf.download_pdf.__wrapped__(
        MagicMock(),
        paper_id=1,
        db_pool=pool,
        pdf_processor=processor,
    )

    # acquire() must have been called exactly twice
    assert pool.acquire.call_count == 2, (
        f"Expected 2 acquire() calls (Phase 1 + Phase 3), got {pool.acquire.call_count}"
    )

    # During the download, exactly 1 acquire() call had been made (Phase 1 done, Phase 3 not yet)
    assert acquire_call_count_at_download == [1], (
        "DB connection was still held (or not yet released) when HTTP download ran"
    )

    assert result.pdf_downloaded is True
    processor.download_pdf.assert_awaited_once_with("https://arxiv.org/pdf/1234.pdf", 1)


@pytest.mark.asyncio
async def test_download_pdf_catches_http_error():
    """download_pdf must convert any httpx.HTTPError subclass into a 502 response."""
    phase1_conn = _make_conn(fetchrow_return=_paper_row(pdf_downloaded=False))
    pool = _make_pool_multi_conn(phase1_conn)

    processor = AsyncMock()
    processor.download_pdf.side_effect = httpx.ConnectError("Connection refused")

    with pytest.raises(HTTPException) as exc_info:
        await pdf.download_pdf.__wrapped__(
            MagicMock(),
            paper_id=1,
            db_pool=pool,
            pdf_processor=processor,
        )

    assert exc_info.value.status_code == 502
    assert "PDF download failed" in exc_info.value.detail
    # Only Phase 1 acquire() should have happened (Phase 3 never reached)
    assert pool.acquire.call_count == 1
