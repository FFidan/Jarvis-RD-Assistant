"""Shared ownership guard helpers for learning_engine routers."""

import asyncpg
from fastapi import HTTPException


async def assert_project_owner(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    project_id: int,
    user_id: int,
) -> None:
    """Raise 404 unless *project_id* exists and belongs to *user_id*.

    Uses a 404 (not 403) to avoid leaking project existence to non-owners.
    """
    owned = await conn.fetchval(
        "SELECT id FROM projects WHERE id = $1 AND user_id = $2",
        project_id,
        user_id,
    )
    if not owned:
        raise HTTPException(status_code=404, detail="Project not found")
