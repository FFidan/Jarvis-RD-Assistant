"""Daily purge of stale ``sessions`` rows.

A session row is dead once it can no longer authenticate anyone: either its
``expires_at`` is well past the offline grace window, or it has been revoked.
``session_middleware`` already rejects both, so such rows are pure table bloat
and a needless retention surface. Delete sessions expired more than 30 days ago,
and revoked ones more than 7 days ago — a short window is kept beyond each event
for server-clock skew and diagnostic inspection.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Daily at 03:35 — after the magic_link_tokens purge (03:20) and classifier
# training (03:30), before the Pulse deck (04:00).
_PURGE_SESSIONS_CRON = "35 3 * * *"

_DELETE_STALE_SESSIONS = (
    "DELETE FROM sessions "
    "WHERE (expires_at < now() - INTERVAL '30 days') "
    "OR (revoked_at IS NOT NULL AND revoked_at < now() - INTERVAL '7 days')"
)


async def purge_stale_sessions(pool: Any) -> None:
    """Delete stale ``sessions`` rows (long-expired or long-revoked).

    Calls ``pool.execute`` directly (single statement, no ``acquire`` needed —
    consistent with ``purge_expired_magic_link_tokens``). Logs the number of
    deleted rows at INFO level and swallows any exception so a transient DB
    failure does not crash the scheduler.
    """
    try:
        result = await pool.execute(_DELETE_STALE_SESSIONS)
        try:
            deleted = int(result.split()[-1])
        except Exception:
            logger.debug(
                "purge_sessions: could not parse delete-count from %r", result, exc_info=True
            )
            deleted = -1
        logger.info("purge_sessions: deleted %d stale session(s)", deleted)
    except Exception:
        logger.exception("purge_sessions: failed to purge stale sessions")


async def purge_stale_sessions_task(app: Any) -> None:
    """APScheduler entrypoint — extracts pool from ``app.state`` and delegates."""
    pool = app.state.db_pool
    await purge_stale_sessions(pool)


def register_purge_sessions(scheduler: Any, app: Any) -> None:
    """Register :func:`purge_stale_sessions_task` on *scheduler* (daily cron)."""
    try:
        scheduler.add_job(
            purge_stale_sessions_task,
            trigger=CronTrigger.from_crontab(_PURGE_SESSIONS_CRON),
            args=[app],
            id="purge_sessions",
            name="Daily stale sessions purge",
            replace_existing=True,
            max_instances=1,
        )
        logger.info("purge_sessions scheduler registered (cron=%s)", _PURGE_SESSIONS_CRON)
    except Exception:
        logger.exception("Failed to register purge_sessions job")
