"""Service layer for paper lifecycle business logic.

State-mutation and data-aggregation logic extracted from ``routers/papers.py``
so the router retains only the HTTP boundary (decorators, DI, request/response
shaping, ``HTTPException``).

The two seam collaborators ``delete_paper_vectors`` and
``assert_paper_ownership`` are imported at *module level here* on purpose: tests
patch ``paper_ingestion.papers_service.delete_paper_vectors`` /
``paper_ingestion.papers_service.assert_paper_ownership`` and call through the
router. The patched name must be the one actually invoked, so every call site
that the router used to own is dispatched from this module instead.

Behaviour is preserved verbatim from the pre-extraction router — same SQL, same
ordering, same error handling, same return shapes. In particular the
load-bearing DELETE→Qdrant ordering is unchanged: the DB DELETE commits inside
the transaction, then ``delete_paper_vectors`` runs OUTSIDE the transaction.
"""

import logging

import asyncpg
from fastapi import HTTPException
from jarvis_common import assert_paper_ownership
from jarvis_common.paper_state import (  # noqa: I001
    assert_paper_in_states as _assert_paper_in_states,
)
from jarvis_common.paper_state import (
    restore_paper as _restore_paper,
)
from jarvis_common.paper_state import (
    trash_paper as _trash_paper,
)
from jarvis_common.paper_visibility import paper_visibility_sql

from paper_ingestion.ingestion.embedder import delete_paper_vectors
from paper_ingestion.models import (
    FeedCountsResponse,
    TopicFacetCount,
)
from paper_ingestion.queries.predicates import VIEW_PREDICATES
from paper_ingestion.services.feed_query import fetch_feed_facet_counts
from paper_ingestion.services.paper_state_helpers import (
    _upsert_recommendation_feedback,
    _upsert_state_and_starred,
)

__all__ = [
    "assert_paper_ownership",
    "delete_paper_vectors",
    "_apply_bulk_action",
    "find_papers_needing_analysis",
    "get_feed_counts",
    "hard_delete_paper",
]

logger = logging.getLogger(__name__)

_CALLER_PRIVATE_PAPER_DELETES = (
    "DELETE FROM author_alert_log WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM cards WHERE paper_id = $1 AND user_id = $2",
    """DELETE FROM paper_contradictions
       WHERE (paper_a_id = $1 OR paper_b_id = $1) AND user_id = $2""",
    """WITH deleted AS (
           DELETE FROM paper_entities
           WHERE paper_id = $1 AND user_id = $2
           RETURNING entity_id
       )
       UPDATE entities AS entity
       SET paper_count = (
           SELECT count(*)
           FROM paper_entities AS remaining
           WHERE remaining.entity_id = entity.id
             AND NOT (remaining.paper_id = $1 AND remaining.user_id = $2)
       )
       WHERE entity.id IN (SELECT entity_id FROM deleted)""",
    "DELETE FROM paper_extractions WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_highlights WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_notes WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_recommendations WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_summaries WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM paper_user_zotero_links WHERE paper_id = $1 AND user_id = $2",
    """WITH deleted AS (
           DELETE FROM pulse_cards
           WHERE paper_id = $1 AND user_id = $2
           RETURNING deck_id
       )
       UPDATE pulse_decks AS deck
       SET card_count = (
           SELECT count(*)
           FROM pulse_cards AS remaining
           WHERE remaining.deck_id = deck.id
             AND NOT (remaining.paper_id = $1 AND remaining.user_id = $2)
       )
       WHERE deck.id IN (SELECT deck_id FROM deleted)""",
    "DELETE FROM recommendation_feedback WHERE paper_id = $1 AND user_id = $2",
    """DELETE FROM task_paper_links AS link
       USING tasks AS owner
       WHERE link.task_id = owner.id
         AND link.paper_id = $1
         AND owner.user_id = $2""",
    """DELETE FROM project_papers AS link
       USING projects AS owner
       WHERE link.project_id = owner.id
         AND link.paper_id = $1
         AND owner.user_id = $2""",
    "DELETE FROM paper_user_state WHERE paper_id = $1 AND user_id = $2",
    "DELETE FROM user_library WHERE paper_id = $1 AND user_id = $2",
)


async def find_papers_needing_analysis(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_ids: list[int],
) -> set[int]:
    """Return unprocessed papers that have a usable local or remote PDF source.

    Saving bibliographic metadata is valid even when a source does not expose a
    PDF. The analysis worker, however, can only make progress from a nonblank
    ``pdf_url`` or ``pdf_local_path``. Keeping that eligibility rule here gives
    per-paper, batch, and Pulse saves the same scheduling contract.
    """
    if not paper_ids:
        return set()
    rows = await conn.fetch(
        """
        SELECT p.id
          FROM papers AS p
         WHERE p.id = ANY($1::int[])
           AND (
               NULLIF(BTRIM(p.pdf_url), '') IS NOT NULL
               OR NULLIF(BTRIM(p.pdf_local_path), '') IS NOT NULL
           )
           AND NOT EXISTS (
               SELECT 1 FROM paper_chunks AS pc WHERE pc.paper_id = p.id
           )
        """,
        paper_ids,
    )
    return {int(row["id"]) for row in rows}


async def _hard_delete_scoped(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
) -> bool:
    """Remove the caller's claim on *paper_id* without deleting shared data.

    Single-user mode (``user_id is None``) keeps the legacy full delete.

    Multi-user mode removes the caller's private data and links while preserving
    the canonical row and shared processing artifacts. The row may still be used
    by another person even when no other ``user_library`` membership currently
    exists, so a session-scoped deletion must never cascade through it.

    Returns ``True`` only for the legacy unscoped delete, so the caller runs shared
    Qdrant cleanup only when the canonical row was deliberately removed.
    """
    if user_id is None:
        await conn.execute("DELETE FROM papers WHERE id = $1", paper_id)
        return True

    for statement in _CALLER_PRIVATE_PAPER_DELETES:
        await conn.execute(statement, paper_id, user_id)

    return False


async def _apply_bulk_action(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    action: str,
    *,
    _hard_deleted_ids: list[int] | None = None,
    router_logger: logging.Logger | None = None,
) -> None:
    """Dispatch a single bulk action to the appropriate state mutation.

    ``router_logger`` is the calling router's own logger. Passing it keeps the
    orphan-vector ``exception`` record attributed to the router that requested
    the action; the bulk path passes nothing and the record falls back to this
    module's logger.
    """
    if action == "save":
        await _upsert_state_and_starred(conn, paper_id, user_id, state="to_read")
    elif action == "skip":
        await _upsert_state_and_starred(conn, paper_id, user_id, state="done")
    elif action == "trash":
        await _trash_paper(conn, paper_id, user_id)
    elif action == "mark_reading":
        await _upsert_state_and_starred(conn, paper_id, user_id, state="reading")
    elif action == "mark_done":
        await _upsert_state_and_starred(conn, paper_id, user_id, state="done")
    elif action == "restore":
        await _assert_paper_in_states(conn, paper_id, user_id, allowed=("trash",))
        await _restore_paper(conn, paper_id, user_id)
    elif action == "star":
        await _upsert_state_and_starred(conn, paper_id, user_id, starred=True)
    elif action == "unstar":
        await _upsert_state_and_starred(conn, paper_id, user_id, starred=False)
    elif action == "feedback_positive":
        await _upsert_recommendation_feedback(conn, paper_id, user_id, "positive", "feed_thumbs")
    elif action == "feedback_negative":
        await _upsert_recommendation_feedback(conn, paper_id, user_id, "negative", "feed_thumbs")
    elif action == "hard_delete":
        await _assert_paper_in_states(conn, paper_id, user_id, allowed=("trash",))
        # Caller (bulk_action_papers) already wraps each paper in a per-paper
        # SAVEPOINT (async with conn.transaction()), so no inner txn is needed.
        row_deleted = await _hard_delete_scoped(conn, paper_id, user_id)
        # Qdrant vectors are shared (keyed by paper_id), so only clean them when
        # the canonical row was physically removed — never on a membership-only
        # removal that leaves the row for other tenants.
        if row_deleted and _hard_deleted_ids is not None:
            # Collect the ID for Qdrant cleanup OUTSIDE the transaction.
            _hard_deleted_ids.append(paper_id)
        elif row_deleted:
            # Callers that do not pass _hard_deleted_ids get the legacy inline
            # behaviour as a fallback (e.g. tests that call this directly).
            try:
                await delete_paper_vectors(paper_id)
            except Exception:  # noqa: BLE001 — best-effort cleanup; orphan vectors are harmless
                (router_logger or logger).exception(
                    "Qdrant cleanup failed for paper %d in bulk hard_delete; "
                    "vectors are now orphans",
                    paper_id,
                )
    else:
        raise ValueError(f"Unknown bulk action: {action}")


async def get_feed_counts(
    scope: str,
    db_pool: asyncpg.Pool,
    user_id: int | None,
) -> FeedCountsResponse:
    """Return feed and facet counts under the requested visibility scope.

    Library scope counts exact caller membership. Authenticated corpus scope
    counts persisted public rows plus the caller's private library rows. A
    `None` caller uses the trusted internal corpus path.
    """
    # Normalise sentinel: .__wrapped__ callers bypass FastAPI DI so `scope`
    # may arrive as the Query(…) FieldInfo object rather than a plain str.
    if not isinstance(scope, str):
        scope = "library"
    if scope not in {"library", "corpus"}:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown scope {scope!r}. Valid values: ['corpus', 'library']",
        )

    def _sum(view_key: str, alias: str) -> str:
        return (
            f"COALESCE(SUM(CASE WHEN {VIEW_PREDICATES[view_key]} "
            f"THEN 1 ELSE 0 END), 0)::int AS {alias}"
        )

    select_sql = f"""
        SELECT
            {_sum("inbox", "inbox")},
            {_sum("library", "library")},
            {_sum("reading_list", "reading_list")},
            {_sum("reading", "reading")},
            {_sum("done", "done")},
            {_sum("starred", "starred")},
            {_sum("trash", "trash")},
            {_sum("active", "active")},
            {_sum("kept", "kept")},
            {_sum("all_non_trash", "all_non_trash")}
    """
    if user_id is None:
        from_sql = """
          FROM papers p
          LEFT JOIN paper_user_state pus ON pus.paper_id = p.id
            AND pus.user_id IS NULL
        """
        query_args: tuple[object, ...] = ()
    elif scope == "library":
        from_sql = """
          FROM papers p
          JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
          LEFT JOIN paper_user_state pus ON pus.paper_id = p.id
            AND pus.user_id = $1
        """
        query_args = (user_id,)
    else:
        from_sql = f"""
          FROM papers p
          LEFT JOIN paper_user_state pus ON pus.paper_id = p.id
            AND pus.user_id = $1
         WHERE {paper_visibility_sql(1, alias="p")}
        """
        query_args = (user_id,)
    sql = select_sql + from_sql
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(sql, *query_args)
        if row is None:
            raise RuntimeError(
                f"get_feed_counts: aggregate SELECT returned no row (user_id={user_id!r})"
            )

        # UI v3 facet rail: by_source / by_topic / untagged — honour requested scope.
        by_source, by_topic_rows, untagged = await fetch_feed_facet_counts(
            conn, user_id, scope=scope
        )

    return FeedCountsResponse(
        inbox=row["inbox"],
        library=row["library"],
        reading_list=row["reading_list"],
        reading=row["reading"],
        done=row["done"],
        starred=row["starred"],
        trash=row["trash"],
        active=row["active"],
        kept=row["kept"],
        all_non_trash=row["all_non_trash"],
        by_source=by_source,
        by_topic=[TopicFacetCount(**t) for t in by_topic_rows],
        untagged=untagged,
    )


async def hard_delete_paper(
    paper_id: int,
    db_pool: asyncpg.Pool,
    user_id: int,
    *,
    router_logger: logging.Logger,
) -> dict[str, int]:
    """Permanently delete a trashed paper.

    Cascades through FK; Qdrant cleanup is best-effort.

    Order rationale (load-bearing — delete-first ordering): if SQL ``DELETE`` fails,
    the txn rolls back and Qdrant is untouched (user retries cleanly). If
    SQL succeeds and Qdrant fails, vectors are orphaned (recoverable). The
    reverse order is data-loss-prone — do not collapse the inside-txn
    DELETE and outside-txn Qdrant cleanup into a single try/except.

    ``router_logger`` is the calling router's own logger, so the orphan-vector
    ``exception`` record is attributed to the router that requested the delete
    rather than to this module. The sole caller is
    ``routers/papers_lifecycle.py``, which passes its own module logger.
    """
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        await _assert_paper_in_states(conn, paper_id, user_id, allowed=("trash",))
        async with conn.transaction():
            row_deleted = await _hard_delete_scoped(conn, paper_id, user_id)
        # Qdrant cleanup OUTSIDE the transaction — Qdrant is non-transactional;
        # we prefer the row to commit first so a Qdrant failure leaves orphan
        # vectors (recoverable) rather than a missing-vectors row (data loss).
        # Vectors are shared (keyed by paper_id) so only clean them when the
        # canonical row was physically removed, not on a membership-only removal.
        if row_deleted:
            try:
                await delete_paper_vectors(paper_id)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                router_logger.exception(
                    "Qdrant cleanup failed for paper %d after DB delete; vectors are now orphans",
                    paper_id,
                )
    return {"deleted": paper_id}
