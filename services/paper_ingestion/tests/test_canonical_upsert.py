"""``upsert_paper`` no longer accepts ``user_id``; canonical-only.

The legacy ``user_id`` keyword has been replaced with ``discovered_by`` (audit
only). Library membership lives in ``user_library`` and is added by callers
via ``jarvis_common.library.add_to_library``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from paper_ingestion.models import PaperCreate, SourceType
from paper_ingestion.services.pdf_workflow import upsert_paper

from tests.conftest import FakeRecord


# Keep local: upsert-specific paper (discovery_origin/citation_count/metadata kwargs) not in pulse_helpers.make_pulse_paper.
def _make_paper() -> PaperCreate:
    return PaperCreate(
        external_id="arxiv:9999.99999",
        source_type=SourceType.ARXIV,
        title="A canonical paper",
        authors=["Alice", "Bob"],
        abstract="Abstract.",
        published_date=datetime(2026, 1, 1, tzinfo=UTC).date(),
        url="https://example.org/p",
        pdf_url=None,
        citation_count=0,
        metadata={},
        discovery_origin="user_initiated",
    )


@pytest.mark.asyncio
async def test_upsert_paper_signature_no_user_id_kwarg():
    """``upsert_paper(conn, paper, user_id=...)`` is gone. The canonical-only
    refactor keeps a different keyword (``discovered_by``) for audit-trail attribution."""
    import inspect

    sig = inspect.signature(upsert_paper)
    assert "user_id" not in sig.parameters, "canonical refactor drops user_id from upsert_paper"
    assert "discovered_by" in sig.parameters


@pytest.mark.asyncio
async def test_upsert_paper_inserts_canonical_record_with_discovered_by():
    """The INSERT writes the discoverer to ``papers.discovered_by`` (audit)."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=FakeRecord(id=42, is_insert=True))

    paper = _make_paper()
    row = await upsert_paper(conn, paper, discovered_by=7)

    conn.fetchrow.assert_awaited_once()
    sql = conn.fetchrow.await_args.args[0]
    args = conn.fetchrow.await_args.args[1:]

    assert "INSERT INTO papers" in sql
    assert "discovered_by" in sql
    assert "ON CONFLICT (external_id)" in sql
    assert args[-2] == 7
    assert args[-1] == "private"
    assert row["id"] == 42


@pytest.mark.asyncio
async def test_upsert_paper_idempotent_same_paper_returns_same_row():
    """Calling twice with the same external_id returns the same paper id —
    ON CONFLICT DO UPDATE preserves identity."""
    conn = AsyncMock()
    # First call → insert (xmax=0 ⇒ is_insert=True). Second → update.
    conn.fetchrow = AsyncMock(
        side_effect=[
            FakeRecord(id=42, is_insert=True),
            FakeRecord(id=42, is_insert=False),
        ]
    )

    paper = _make_paper()
    row1 = await upsert_paper(conn, paper, discovered_by=7)
    row2 = await upsert_paper(conn, paper, discovered_by=99)  # different discoverer

    assert row1["id"] == row2["id"] == 42
    # Identity is preserved across both calls; on conflict, the original
    # discoverer is retained because the SQL only updates non-attribution
    # columns. (Captured in the SQL string — discovered_by is in INSERT VALUES
    # but NOT in the DO UPDATE SET list.)
    sql = conn.fetchrow.await_args.args[0]
    do_update_clause = sql.split("DO UPDATE SET", 1)[1]
    assert "discovered_by" not in do_update_clause


@pytest.mark.asyncio
async def test_upsert_paper_accepts_none_discovered_by_for_system_papers():
    """A missing discoverer remains an audit null without changing private scope."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=FakeRecord(id=42, is_insert=True))

    await upsert_paper(conn, _make_paper())  # default: discovered_by=None
    args = conn.fetchrow.await_args.args[1:]
    assert args[-2] is None
    assert args[-1] == "private"
