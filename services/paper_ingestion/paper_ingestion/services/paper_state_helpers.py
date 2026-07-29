"""Service-layer helpers for paper state and recommendation feedback mutations."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from jarvis_common.paper_state import (
    upsert_paper_user_state as _upsert_paper_user_state,
)

from paper_ingestion.db_types import ConnLike
from paper_ingestion.exceptions import SourceGenerationChangedError


@asynccontextmanager
async def guard_current_source_generation(
    conn: ConnLike,
    paper_id: int,
    captured_generation: int,
) -> AsyncIterator[None]:
    """Hold the paper source stable while a derived artifact is persisted.

    The shared row lock conflicts with source replacement and remains held
    through the caller's writes because this context owns their transaction.
    A run that captured an older source version fails before it can mutate
    PostgreSQL or an external vector store.
    """
    async with conn.transaction():
        current_generation = await conn.fetchval(
            "SELECT content_generation FROM papers WHERE id = $1 FOR SHARE",
            paper_id,
        )
        if current_generation is None or int(current_generation) != captured_generation:
            raise SourceGenerationChangedError(
                "The paper changed while derived content was being generated. Please retry."
            )
        yield


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
