"""Tests for B-UPLOAD: dedupe hit adds existing paper to the caller's library.

Before fix: a global-dedupe hit raised 409 and never touched user_library.
After fix: the canonical paper is added to the caller's library (idempotent)
and the handler returns 200 with the existing paper record.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.dependencies import utils as fastapi_dependency_utils

fastapi_dependency_utils.ensure_multipart_is_installed = lambda: None

from paper_ingestion.routers import pdf  # noqa: E402
from tests.conftest import FakeRecord  # noqa: E402


def _make_pool(conn):
    """Return a pool mock whose acquire() always yields the given connection."""
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _existing_paper_row(**overrides) -> FakeRecord:
    """Build a FakeRecord that looks like a canonical local-PDF paper row."""
    defaults = dict(
        id=73,
        external_id="local:435299cc42b75f9a",
        source_type="local",
        title="Neural ODEs",
        authors=["Chen", "Rubanova"],
        abstract="An ODE-based approach to neural networks.",
        published_date=None,
        url="local://435299cc42b75f9a",
        pdf_url=None,
        pdf_local_path="/data/pdfs/73.pdf",
        pdf_downloaded=True,
        citation_count=0,
        metadata={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        discovery_origin="user_initiated",
    )
    defaults.update(overrides)
    return FakeRecord(**defaults)


def _make_upload_file() -> object:
    from fastapi import UploadFile

    pdf_content = b"%PDF-1.7\n" + b"x" * 100
    return UploadFile(filename="paper.pdf", file=io.BytesIO(pdf_content))


# ---------------------------------------------------------------------------
# Characterisation: CURRENT behaviour raises 409 (red before fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_pdf_dedupe_old_behaviour_raises_409(monkeypatch):
    """Documents the pre-fix behaviour: dedupe → 409, library untouched.

    This test is EXPECTED TO FAIL once the fix is applied; it is kept as
    a living record of the original bug for reviewers.  Skip it after the
    fix lands if it causes noise — the two post-fix tests below are the
    authoritative spec.
    """
    # Not run as part of the green suite; kept for documentation purposes.
    pytest.skip("characterisation test — superseded by post-fix assertions below")


# ---------------------------------------------------------------------------
# Post-fix: dedupe adds canonical paper to caller's library (user_id set)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_pdf_dedupe_adds_existing_paper_to_callers_library(tmp_path, monkeypatch):
    """User B re-uploading a PDF already in the corpus gets 200, not 409.

    The canonical paper (id=73, external_id local:435299cc42b75f9a) was
    previously uploaded by user A.  User B uploads the same bytes.  After
    the fix:
      - add_to_library is called for user B (user_id=99) on paper_id=73
      - the handler returns 200 with the existing paper record (not 409)
    """
    storage_dir = tmp_path / "pdfs"
    storage_dir.mkdir()
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(pdf, "current_user_id_strict", AsyncMock(return_value=99))

    add_to_library_mock = AsyncMock()
    monkeypatch.setattr(pdf, "add_to_library", add_to_library_mock, raising=False)

    existing = _existing_paper_row()  # paper owned by user A in the global corpus
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=existing)  # dedupe hit on first call
    pool = _make_pool(conn)

    upload_file = _make_upload_file()

    response = await pdf.upload_pdf.__wrapped__(
        MagicMock(),
        file=upload_file,
        title="Neural ODEs",
        authors="Chen, Rubanova",
        abstract="",
        db_pool=pool,
    )

    # Must NOT raise 409 — returns the existing paper with status 200
    assert response.id == 73
    assert response.external_id == "local:435299cc42b75f9a"

    # add_to_library must be called for the caller (user B) on the existing paper
    add_to_library_mock.assert_awaited_once_with(
        conn,
        user_id=99,
        paper_id=73,
        added_via="manual_save",
    )


@pytest.mark.asyncio
async def test_upload_pdf_dedupe_idempotent_same_user_reraises_no_error(tmp_path, monkeypatch):
    """Re-uploading the same PDF as the user who already owns it: 200, no error.

    add_to_library uses ON CONFLICT DO NOTHING, so this is safe and yields
    the paper back without creating duplicate rows.
    """
    storage_dir = tmp_path / "pdfs"
    storage_dir.mkdir()
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(pdf, "current_user_id_strict", AsyncMock(return_value=42))

    add_to_library_mock = AsyncMock()
    monkeypatch.setattr(pdf, "add_to_library", add_to_library_mock, raising=False)

    existing = _existing_paper_row()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=existing)
    pool = _make_pool(conn)

    upload_file = _make_upload_file()

    response = await pdf.upload_pdf.__wrapped__(
        MagicMock(),
        file=upload_file,
        title="Neural ODEs",
        authors="",
        abstract="",
        db_pool=pool,
    )

    assert response.id == 73
    # add_to_library still called — the helper itself is idempotent
    add_to_library_mock.assert_awaited_once_with(
        conn,
        user_id=42,
        paper_id=73,
        added_via="manual_save",
    )


@pytest.mark.asyncio
async def test_upload_pdf_dedupe_no_library_write_when_unauthenticated(tmp_path, monkeypatch):
    """Single-user / unauthenticated mode: dedupe returns 200 but skips library write."""
    storage_dir = tmp_path / "pdfs"
    storage_dir.mkdir()
    monkeypatch.setattr(pdf, "PDF_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(pdf, "current_user_id_strict", AsyncMock(return_value=None))

    add_to_library_mock = AsyncMock()
    monkeypatch.setattr(pdf, "add_to_library", add_to_library_mock, raising=False)

    existing = _existing_paper_row()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=existing)
    pool = _make_pool(conn)

    upload_file = _make_upload_file()

    response = await pdf.upload_pdf.__wrapped__(
        MagicMock(),
        file=upload_file,
        title="Neural ODEs",
        authors="",
        abstract="",
        db_pool=pool,
    )

    assert response.id == 73
    add_to_library_mock.assert_not_awaited()
