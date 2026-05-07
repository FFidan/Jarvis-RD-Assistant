"""Shared DB helper functions for paper lifecycle and recommendation feedback.

These helpers are used by both ``routers/papers.py`` and ``routers/pulse.py``
and are intentionally kept here (rather than inlined) to maintain a single
source of truth and avoid cross-router circular imports.

The three paper_user_state helpers have been promoted to
``jarvis_common.paper_state``; the private names below are compatibility
re-exports so existing call sites keep working without changes.
"""

import asyncpg
from jarvis_common.paper_state import (
    upsert_paper_user_state as _upsert_paper_user_state,
)


async def _upsert_state_and_starred(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    *,
    state: str | None = None,
    starred: bool | None = None,
) -> None:
    """Upsert paper_user_state, writing only the fields explicitly supplied.

    Delegates to :func:`jarvis_common.paper_state.upsert_paper_user_state`
    with ``on_conflict="update_dynamic"``.
    """
    await _upsert_paper_user_state(
        conn, paper_id, user_id, state=state, starred=starred, on_conflict="update_dynamic"
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
