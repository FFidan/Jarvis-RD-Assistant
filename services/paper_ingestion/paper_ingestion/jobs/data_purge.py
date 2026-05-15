"""Daily hard-delete of soft-deleted users past the 30-day grace.

WS-USER-DELETION D2: ``DELETE /api/admin/users/{id}`` only sets
``users.deleted_at``. This job hard-deletes any user whose grace window has
elapsed; migration 080's ON DELETE CASCADE FKs then collapse every owned row
(papers.discovered_by stays ON DELETE SET NULL so discovered papers fall back
to the shared corpus instead of being destroyed).
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Daily at 03:10 — before the 04:00 Pulse deck, after classifier training.
_DATA_PURGE_CRON = "10 3 * * *"

_PURGE_SQL = (
    "DELETE FROM users WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days'"
)


async def data_purge_task(app: Any) -> None:
    """Hard-delete users whose 30-day soft-delete grace has elapsed."""
    from jarvis_common.event_log import log_event  # noqa: PLC0415

    pool = app.state.db_pool
    try:
        result = await pool.execute(_PURGE_SQL)
        try:
            deleted = int(result.split()[-1])
        except Exception:
            deleted = -1
        if deleted != 0:
            await log_event(
                pool=pool,
                level="info",
                category="config",
                source="data_purge",
                message=f"hard-deleted {deleted} expired soft-deleted user(s)",
            )
        logger.info("data_purge: hard-deleted %d expired user(s)", deleted)
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
