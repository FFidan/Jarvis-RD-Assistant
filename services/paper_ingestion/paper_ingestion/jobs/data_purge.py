"""Daily hard-delete of soft-deleted users past the 30-day grace.

WS-USER-DELETION D2: ``DELETE /api/admin/users/{id}`` only sets
``users.deleted_at``. This job hard-deletes any user whose grace window has
elapsed; migration 080's ON DELETE CASCADE FKs then collapse every owned row
(papers.discovered_by stays ON DELETE SET NULL so discovered papers fall back
to the shared corpus instead of being destroyed).

GDPR compliance: Qdrant vectors (which carry ``user_id`` in their payload)
are purged per-user before the SQL DELETE so no personal data lingers in the
vector store after hard-delete.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from jarvis_common.audit import log_audit

logger = logging.getLogger(__name__)

# Daily at 03:10 — before the 04:00 Pulse deck, after classifier training.
_DATA_PURGE_CRON = "10 3 * * *"

_SELECT_EXPIRED_USERS = (
    "SELECT id FROM users WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days'"
)
_DELETE_EXPIRED_USERS = (
    "DELETE FROM users WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days'"
)


async def _purge_qdrant_for_user(qdrant: Any, uid: int) -> int:
    """Delete all Qdrant vectors whose payload ``user_id`` matches *uid*.

    Counts matching points before deleting so the audit metadata records the
    real per-uid vector count, not Qdrant's operation sequence number.

    Returns the pre-delete point count (0 if count or delete fails — caller
    logs the error and continues).
    """
    from qdrant_client.models import FieldCondition, Filter, MatchValue  # noqa: PLC0415

    from paper_ingestion.ingestion.embedder import COLLECTION_NAME  # noqa: PLC0415

    uid_filter = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=uid))])

    count_result = await qdrant.count(
        collection_name=COLLECTION_NAME,
        count_filter=uid_filter,
        exact=True,
    )
    point_count: int = count_result.count

    await qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=uid_filter,
        wait=True,
    )
    return point_count


async def data_purge_task(app: Any) -> None:
    """Hard-delete users whose 30-day soft-delete grace has elapsed.

    Order of operations:
    1. Identify expired user ids (SELECT before DELETE so we know who to purge).
    2. For each uid, delete Qdrant vectors filtered by user_id — resilient:
       a failure is logged and skipped so one bad uid never aborts the whole run.
    3. SQL DELETE FROM users (ON DELETE CASCADE collapses owned rows).
    4. Audit-log the destructive event via log_audit (best-effort, never raises).
    """
    pool = app.state.db_pool
    qdrant = getattr(app.state, "qdrant_client", None)

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SELECT_EXPIRED_USERS)
            expired_ids: list[int] = [r["id"] for r in rows]

            if not expired_ids:
                logger.info("data_purge: no expired users to purge")
                return

            qdrant_vectors: dict[int, int] = {}
            qdrant_errors: list[str] = []
            if qdrant is not None:
                for uid in expired_ids:
                    try:
                        qdrant_vectors[uid] = await _purge_qdrant_for_user(qdrant, uid)
                    except Exception as exc:
                        logger.warning("data_purge: Qdrant purge failed for user %d: %r", uid, exc)
                        qdrant_errors.append(f"uid={uid}: {exc!r}")
            else:
                logger.warning(
                    "data_purge: qdrant_client not on app.state"
                    " — vectors NOT purged for %d user(s)",
                    len(expired_ids),
                )
                qdrant_errors.append("qdrant_client missing from app.state")

            result = await conn.execute(_DELETE_EXPIRED_USERS)

        try:
            deleted: int | None = int(result.split()[-1])
        except Exception:
            # Parse failed — report unknown rather than an assumed count.
            deleted = None

        metadata: dict[str, Any] = {
            "user_ids": expired_ids,
            "users_deleted": deleted,
            "qdrant_vectors_deleted": qdrant_vectors,
        }
        if qdrant_errors:
            metadata["qdrant_errors"] = qdrant_errors

        await log_audit(
            pool=pool,
            action="user.hard_delete.purged",
            resource="users",
            metadata=metadata,
        )
        logger.info(
            "data_purge: hard-deleted %s expired user(s), qdrant_errors=%d",
            deleted,
            len(qdrant_errors),
        )
    except Exception:
        logger.exception("data_purge: failed to purge expired soft-deleted users")


def register_data_purge(scheduler: Any, app: Any) -> None:
    """Register :func:`data_purge_task` on *scheduler* (daily cron)."""
    try:
        scheduler.add_job(
            data_purge_task,
            trigger=CronTrigger.from_crontab(_DATA_PURGE_CRON),
            args=[app],
            id="data_purge",
            name="Daily expired-user hard purge",
            replace_existing=True,
            max_instances=1,
        )
        logger.info("data_purge scheduler registered (cron=%s)", _DATA_PURGE_CRON)
    except Exception:
        logger.exception("Failed to register data_purge job")
