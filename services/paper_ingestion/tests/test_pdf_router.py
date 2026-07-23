"""Tests for download_pdf releasing DB conn before HTTP download;
scan_local_pdfs using per-file connections.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.dependencies import utils as fastapi_dependency_utils

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
fastapi_dependency_utils.ensure_multipart_is_installed = lambda: None

from paper_ingestion.routers import pdf  # noqa: E402
from tests.conftest import FakeRecord  # noqa: E402


def _make_conn(fetchrow_return=None):
    """Return an AsyncMock connection with transaction support."""
    conn = AsyncMock()
    conn.fetchrow.return_value = fetchrow_return
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


# Keep local: multi-conn side_effect semantics (successive acquire() yields different
# connections) are not covered by jarvis_common.make_pool_and_conn.
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
        is_visible=True,
    )
    defaults.update(overrides)
    return FakeRecord(**defaults)


# ---------------------------------------------------------------------------
# download_pdf — connection lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_pdf_releases_conn_before_http(tmp_path: Path):
    """DB conn must be released (acquire() closes) before the HTTP download begins.

    Verifies that db_pool.acquire() is called exactly twice:
    - once to load the row
    - once to write back
    The download happens between those two calls, with no connection held.
    """
    load_row = _paper_row(pdf_downloaded=False)
    final_path = tmp_path / "1.pdf"
    staged_path = tmp_path / "_download_1.pdf"
    updated_row = _paper_row(pdf_downloaded=True, pdf_local_path=str(final_path))

    load_conn = _make_conn(fetchrow_return=load_row)
    writeback_conn = _make_conn(fetchrow_return=updated_row)
    pool = _make_pool_multi_conn(load_conn, writeback_conn)

    # Track when download happens relative to acquire() calls
    acquire_call_count_at_download: list[int] = []

    async def mock_download(url, paper_id):
        # At this point the load conn should already be released,
        # so acquire has been called once and the write-back has not started yet.
        acquire_call_count_at_download.append(pool.acquire.call_count)
        staged_path.write_bytes(b"%PDF-1.7\ncontent")
        return staged_path, final_path

    processor = MagicMock()
    processor.stage_pdf_download = AsyncMock(side_effect=mock_download)

    result = await pdf.download_pdf.__wrapped__(
        MagicMock(),
        paper_id=1,
        db_pool=pool,
        pdf_processor=processor,
        user_id=1,
    )

    # acquire() must have been called exactly twice
    assert pool.acquire.call_count == 2, (
        f"Expected 2 acquire() calls (load + write-back), got {pool.acquire.call_count}"
    )

    # During the download, exactly 1 acquire() call had been made (load done, write-back not yet)
    assert acquire_call_count_at_download == [1], (
        "DB connection was still held (or not yet released) when HTTP download ran"
    )

    assert result.pdf_downloaded is True
    processor.stage_pdf_download.assert_awaited_once_with(
        "https://arxiv.org/pdf/1234.pdf", 1
    )


@pytest.mark.asyncio
async def test_download_pdf_catches_http_error():
    """download_pdf must convert any httpx.HTTPError subclass into a 502 response."""
    phase1_conn = _make_conn(fetchrow_return=_paper_row(pdf_downloaded=False))
    pool = _make_pool_multi_conn(phase1_conn)

    processor = MagicMock()
    processor.stage_pdf_download = AsyncMock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    with pytest.raises(HTTPException) as exc_info:
        await pdf.download_pdf.__wrapped__(
            MagicMock(),
            paper_id=1,
            db_pool=pool,
            pdf_processor=processor,
            user_id=1,
        )

    assert exc_info.value.status_code == 502
    assert "PDF download failed" in exc_info.value.detail
    # Only the load acquire() should have happened (write-back never reached)
    assert pool.acquire.call_count == 1
