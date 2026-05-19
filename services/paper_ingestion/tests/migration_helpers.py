"""Shared helpers for per-migration live-PG tests.

D4-02: ``_apply_fresh_init`` was copy-pasted verbatim into 10 per-migration
test files.  Import from here instead.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SQL = _REPO_ROOT / "db" / "init.sql"


async def apply_fresh_init(pool: asyncpg.Pool) -> None:
    """Apply db/init.sql to *pool* — sets up the baseline schema for live-PG tests."""
    async with pool.acquire() as conn:
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))
