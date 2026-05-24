"""Contract test asserting vector_writer role is absent after baseline (BUG-DBINIT-1).

# Verified: db/init.sql lines 1240-1247 removed in this commit (role + 2 GRANTs).
"""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_vector_writer_role_does_not_exist(
    contract_conn: asyncpg.Connection,
) -> None:
    """vector_writer role must not be created by the schema baseline."""
    row = await contract_conn.fetchrow("SELECT 1 FROM pg_roles WHERE rolname = 'vector_writer'")
    assert row is None, "vector_writer role must not exist after baseline applied"
