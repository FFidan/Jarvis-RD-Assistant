"""Service layer for paper lifecycle business logic.

C3 extraction (design doc 2026-05-19-c3-extraction-design.md §3 step 3): the
state-mutation / data-aggregation logic that previously lived inline in
``routers/papers.py`` moves here so the router retains only the HTTP boundary
(decorators, DI, request/response shaping, ``HTTPException``).

The two seam collaborators ``delete_paper_vectors`` and
``assert_paper_ownership`` are imported at *module level here* on purpose: the
Phase-2 tests patch ``paper_ingestion.papers_service.delete_paper_vectors`` /
``paper_ingestion.papers_service.assert_paper_ownership`` and call through the
router. The patched name must be the one actually invoked, so every call site
that the router used to own is dispatched from this module instead.

Behaviour is preserved verbatim from the pre-extraction router — same SQL, same
ordering, same error handling, same return shapes. In particular the
load-bearing DELETE→Qdrant ordering (WS-AH2 NEW-H2) is unchanged: the DB DELETE
commits inside the transaction, then ``delete_paper_vectors`` runs OUTSIDE the
transaction.
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
    "get_feed_counts",
    "hard_delete_paper",
]

logger = logging.getLogger(__name__)


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

    ``router_logger`` is the ``paper_ingestion.routers.papers`` logger passed
    in by the route handler so the orphan-vector ``logger.exception`` retains
    its original logger name (caplog / patch.object targets that module).
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
        await conn.execute("DELETE FROM papers WHERE id = $1", paper_id)
        # Collect the ID for Qdrant cleanup OUTSIDE the transaction.
        # Callers that do not pass _hard_deleted_ids get the legacy inline
        # behaviour as a fallback (e.g. tests that call this directly).
        if _hard_deleted_ids is not None:
            _hard_deleted_ids.append(paper_id)
        else:
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
    user_id: int,
) -> FeedCountsResponse:
    """Return per-bucket paper counts for the current user (10 named views)."""
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

    # Sprint B: scope feed counts via the caller's user_library; single-user
    # mode (user_id=None) falls back to the canonical corpus.
    if user_id is not None:
        sql = f"""
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
              FROM papers p
              JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
              LEFT JOIN paper_user_state pus ON pus.paper_id = p.id
                AND pus.user_id = $1
        """
    else:
        sql = f"""
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
              FROM papers p
              LEFT JOIN paper_user_state pus ON pus.paper_id = p.id
                AND pus.user_id IS NULL
        """
    async with db_pool.acquire() as conn:
        if user_id is not None:
            row = await conn.fetchrow(sql, user_id)
        else:
            row = await conn.fetchrow(sql)
        assert row is not None  # aggregate query always returns one row

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

    Order rationale (WS-AH2 NEW-H2 — load-bearing): if SQL ``DELETE`` fails,
    the txn rolls back and Qdrant is untouched (user retries cleanly). If
    SQL succeeds and Qdrant fails, vectors are orphaned (recoverable). The
    reverse order is data-loss-prone — do not collapse the inside-txn
    DELETE and outside-txn Qdrant cleanup into a single try/except.

    ``router_logger`` is the ``paper_ingestion.routers.papers`` logger passed
    in by the route handler so the orphan-vector ``logger.exception`` retains
    its original logger name (caplog / patch.object targets that module).
    """
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        await _assert_paper_in_states(conn, paper_id, user_id, allowed=("trash",))
        async with conn.transaction():
            await conn.execute("DELETE FROM papers WHERE id = $1", paper_id)
        # Qdrant cleanup OUTSIDE the transaction — Qdrant is non-transactional;
        # we prefer the row to commit first so a Qdrant failure leaves orphan
        # vectors (recoverable) rather than a missing-vectors row (data loss).
        try:
            await delete_paper_vectors(paper_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            router_logger.exception(
                "Qdrant cleanup failed for paper %d after DB delete; vectors are now orphans",
                paper_id,
            )
    return {"deleted": paper_id}
