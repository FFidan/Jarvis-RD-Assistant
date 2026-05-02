"""Shared DB helper functions for paper lifecycle and recommendation feedback.

These helpers are used by both ``routers/papers.py`` and ``routers/pulse.py``
and are intentionally kept here (rather than inlined) to maintain a single
source of truth and avoid cross-router circular imports.
"""

import asyncpg
from fastapi import HTTPException


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

    **Idempotent on re-trash**: when the row is already in ``'trash'``, the
    CASE expression keeps the existing ``state_before_trash`` value unchanged,
    avoiding a CHECK-constraint violation (``state_before_trash`` cannot be
    ``'trash'`` per the schema).
    """
    await conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, state_before_trash)
           VALUES ($1, $2, 'trash', 'inbox')
           ON CONFLICT (paper_id, user_id) DO UPDATE
             SET state_before_trash = CASE
                     WHEN paper_user_state.state = 'trash' THEN paper_user_state.state_before_trash
                     ELSE paper_user_state.state
                 END,
                 state = 'trash'""",
        paper_id,
        user_id,
    )


async def _assert_paper_in_states(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    *,
    allowed: tuple[str, ...],
) -> None:
    """Raise 409 if the current state is not in ``allowed``. Treats missing rows as 'inbox'."""
    current = (
        await conn.fetchval(
            """SELECT COALESCE(state, 'inbox') FROM paper_user_state
           WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2""",
            paper_id,
            user_id,
        )
        or "inbox"
    )
    if current not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Paper must be in one of {sorted(allowed)}; currently '{current}'",
        )


async def _upsert_recommendation_feedback(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    signal: str,
    source: str,
    reason: str | None = None,
    topic_id: int | None = None,
) -> None:
    """INSERT/UPSERT a ``recommendation_feedback`` row for the given source.

    If topic_id is None, looks up the paper's primary topic (highest
    relevance_score) from paper_topics. NULL is acceptable when the paper
    has no topic associations — the row is still written.
    """
    if topic_id is None:
        topic_id = await conn.fetchval(
            """SELECT topic_id FROM paper_topics
                WHERE paper_id = $1
                ORDER BY relevance_score DESC NULLS LAST
                LIMIT 1""",
            paper_id,
        )
    await conn.execute(
        """INSERT INTO recommendation_feedback
               (paper_id, user_id, signal, source, reason, topic_id)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (paper_id, user_id, source) DO UPDATE
             SET signal = EXCLUDED.signal,
                 reason = EXCLUDED.reason,
                 topic_id = EXCLUDED.topic_id,
                 created_at = NOW()""",
        paper_id,
        user_id,
        signal,
        source,
        reason,
        topic_id,
    )
