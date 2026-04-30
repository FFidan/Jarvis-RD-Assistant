"""Shared DB helper functions for paper lifecycle and recommendation feedback.

These helpers are used by both ``routers/papers.py`` and ``routers/pulse.py``
and are intentionally kept here (rather than inlined) to maintain a single
source of truth and avoid cross-router circular imports.
"""

import asyncpg


async def _upsert_state_and_starred(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    *,
    state: str | None = None,
    starred: bool | None = None,
) -> None:
    """Upsert paper_user_state, writing only the fields explicitly supplied.

    Fields left as ``None`` are preserved on conflict.
    """
    if state is None and starred is None:
        return
    cols = ["paper_id", "user_id"]
    placeholders = ["$1", "$2"]
    values: list[object] = [paper_id, user_id]
    updates: list[str] = []
    if state is not None:
        cols.append("state")
        placeholders.append(f"${len(values) + 1}")
        values.append(state)
        updates.append(f"state = ${len(values)}")
    if starred is not None:
        cols.append("starred")
        placeholders.append(f"${len(values) + 1}")
        values.append(starred)
        updates.append(f"starred = ${len(values)}")
    sql = (
        f"INSERT INTO paper_user_state ({', '.join(cols)}) "  # noqa: S608
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (paper_id, user_id) DO UPDATE SET {', '.join(updates)}"
    )
    await conn.execute(sql, *values)


async def _trash_paper(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
) -> None:
    """Atomic move to Trash: ``state_before_trash := state; state := 'trash'``.

    For a paper without a ``paper_user_state`` row, the INSERT branch
    initialises ``state_before_trash`` to ``'inbox'`` (the implicit default
    per spec §2.3). For an existing row, the UPDATE preserves the prior
    state into ``state_before_trash`` so :func:`_restore_paper` can return
    the paper to where it came from.
    """
    await conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, state_before_trash)
           VALUES ($1, $2, 'trash', 'inbox')
           ON CONFLICT (paper_id, user_id) DO UPDATE
             SET state_before_trash = paper_user_state.state,
                 state = 'trash'""",
        paper_id,
        user_id,
    )


async def _upsert_recommendation_feedback(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    signal: str,
    source: str,
    reason: str | None = None,
) -> None:
    """INSERT/UPSERT a ``recommendation_feedback`` row for the given source."""
    await conn.execute(
        """INSERT INTO recommendation_feedback
               (paper_id, user_id, signal, source, reason)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (paper_id, user_id, source) DO UPDATE
             SET signal = EXCLUDED.signal,
                 reason = EXCLUDED.reason,
                 created_at = NOW()""",
        paper_id,
        user_id,
        signal,
        source,
        reason,
    )
