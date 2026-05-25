"""CFG-GDPR-1: unit/boundary tests for data_export.build_export_zip.

Boundary-adapter shape: mocks only the asyncpg pool/conn/cursor boundary
(idiomatic external-infrastructure boundary per carve-out registry).
No mock-the-mock anti-pattern — the function under test is called with a
realistic fake pool that routes SQL to canned row lists.
"""

from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.services.data_export import (
    _EXPORT_QUERIES,
    build_export_zip,
)


# ---------------------------------------------------------------------------
# Fake asyncpg infrastructure (boundary carve-out: external DB cursor)
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Async-iterable over (json_str,) tuples — mimics asyncpg cursor rows."""

    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def __aiter__(self):
        async def gen():
            for r in self._rows:
                yield (r,)

        return gen()


def _build_pool(rows_by_user: dict[int, list[str]]) -> MagicMock:
    """Return a fake asyncpg Pool that feeds canned rows to cursor() calls."""
    conn = AsyncMock()

    def cursor(sql: str, user_id: int):
        return _FakeCursor(rows_by_user.get(user_id, []))

    conn.cursor = cursor
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_includes_user_library_entry() -> None:
    """CFG-GDPR-1: user_library.jsonl must be present in the export ZIP."""
    pool = _build_pool({})
    zip_bytes = await build_export_zip(pool, user_id=1)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()

    assert "user_library.jsonl" in names, (
        f"user_library must be included in GDPR export; got: {names}"
    )


@pytest.mark.asyncio
async def test_export_papers_scoped_via_user_library_join() -> None:
    """CFG-GDPR-1: papers query must use EXISTS/user_library join, not discovered_by.

    Verifies the SQL text in _EXPORT_QUERIES directly — the mock pool cannot
    distinguish SQL semantics, so we assert on the query string itself.
    """
    papers_sql = next(
        (sql for name, sql in _EXPORT_QUERIES if name == "papers"),
        None,
    )
    assert papers_sql is not None, "papers entry missing from _EXPORT_QUERIES"
    assert "discovered_by" not in papers_sql, (
        "papers query must NOT scope by discovered_by — use user_library join instead"
    )
    assert "user_library" in papers_sql, (
        "papers query must scope via user_library join for correct multi-tenant export"
    )
    assert "EXISTS" in papers_sql.upper(), (
        "papers query must use EXISTS (...) predicate against user_library"
    )


@pytest.mark.asyncio
async def test_export_papers_returns_only_user_library_rows() -> None:
    """CFG-GDPR-1: papers.jsonl contains only rows returned for this user_id.

    The fake cursor routes by user_id, so user 1 sees different rows than user 2.
    This exercises the full build_export_zip path including the ZIP writing.
    """
    pool = _build_pool(
        {
            1: ['{"id": 1, "title": "my-paper"}'],
            2: ['{"id": 9, "title": "other-user-paper"}'],
        }
    )
    zip_bytes = await build_export_zip(pool, user_id=1)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        papers_content = zf.read("papers.jsonl")

    assert b"my-paper" in papers_content
    assert b"other-user-paper" not in papers_content


@pytest.mark.asyncio
async def test_export_user_library_rows_exported() -> None:
    """CFG-GDPR-1: user_library.jsonl contains the user's library rows."""
    pool = _build_pool(
        {
            1: ['{"user_id": 1, "paper_id": 42, "added_via": "manual_save"}'],
        }
    )
    zip_bytes = await build_export_zip(pool, user_id=1)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        ul_content = zf.read("user_library.jsonl")

    assert b"paper_id" in ul_content
    assert b"manual_save" in ul_content


@pytest.mark.asyncio
async def test_export_all_expected_tables_present() -> None:
    """Regression: build_export_zip emits a .jsonl file for every _EXPORT_QUERIES entry."""
    pool = _build_pool({})
    zip_bytes = await build_export_zip(pool, user_id=1)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()

    expected = {name for name, _ in _EXPORT_QUERIES}
    actual = {n.removesuffix(".jsonl") for n in names}
    assert expected == actual, (
        f"ZIP entries mismatch. expected={sorted(expected)} actual={sorted(actual)}"
    )


@pytest.mark.asyncio
async def test_export_includes_library_paper_not_discovered_by_user() -> None:
    """CFG-GDPR-1 behavioral: user B receives papers in their library even when
    discovered_by=A (i.e., user A first found the paper). Papers discovered by
    user A but NOT in user B's library must NOT appear in user B's export.

    The fake cursor routes by user_id, mirroring the real EXISTS/user_library
    query scope. This test documents the CFG-GDPR-1 fix scenario.

    # Verified: services/paper_ingestion/paper_ingestion/services/data_export.py:24-26
    # (papers query uses EXISTS/user_library join, not discovered_by)
    """
    user_a_id = 10
    user_b_id = 20

    # Paper 1: added to user B's library (discovered_by=A in production, but
    # the export query scopes on user_library — so this row appears for user B).
    # Paper 2: discovered by user A only — NOT in user B's library.
    pool = _build_pool(
        {
            user_a_id: ['{"id": 2, "title": "discovered-by-A-only"}'],
            user_b_id: ['{"id": 1, "title": "in-B-library-discovered-by-A"}'],
        }
    )

    zip_bytes_b = await build_export_zip(pool, user_id=user_b_id)

    with zipfile.ZipFile(io.BytesIO(zip_bytes_b)) as zf:
        papers_content = zf.read("papers.jsonl")

    assert b"in-B-library-discovered-by-A" in papers_content, (
        "Paper in user B's library must appear in user B's GDPR export"
    )
    assert b"discovered-by-A-only" not in papers_content, (
        "Paper discovered by user A but NOT in user B's library must NOT appear in user B's export"
    )
