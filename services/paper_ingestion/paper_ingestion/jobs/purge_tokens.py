"""Daily purge of expired ``magic_link_tokens`` rows.

BE-09: ``magic_link_tokens`` rows whose ``expires_at`` is more than 1 day
in the past are deleted to prevent unbounded table growth and to limit the
window during which a compromised token hash could be replayed (the DB
constraint already prevents use after ``expires_at``, but old rows should
not accumulate indefinitely).

Retention policy: keep tokens for 1 day beyond ``expires_at`` to allow
server-clock skew and diagnostic inspection; delete anything older.
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from jarvis_common.maintenance import skip_for_maintenance

logger = logging.getLogger(__name__)

# Daily at 03:20 — after classifier training (03:30) but before Pulse deck (04:00)
_PURGE_TOKENS_CRON = "20 3 * * *"


async def purge_expired_magic_link_tokens(pool: Any) -> None:
    """Delete ``magic_link_tokens`` rows that expired more than 1 day ago.

    Calls ``pool.execute`` directly (no ``acquire`` needed for a single
    statement — consistent with ``purge_system_events_task`` pattern).

    Logs the number of deleted rows at INFO level and swallows any
    exception so a transient DB failure does not crash the scheduler.
    """
    try:
        deleted = int(
            await pool.fetchval(
                "SELECT platform.purge_identity_retention_v1($1)", "magic_link_tokens"
            )
        )
        logger.info("purge_tokens: deleted %d expired magic_link_token(s)", deleted)
    except Exception:
        logger.exception("purge_tokens: failed to purge expired magic_link_tokens")


async def purge_expired_magic_link_tokens_task(app: Any) -> None:
    """APScheduler entrypoint — extracts pool from ``app.state`` and delegates."""
    if skip_for_maintenance("purge magic_link_tokens"):
        return
    pool = app.state.db_pool
    await purge_expired_magic_link_tokens(pool)


def register_purge_tokens(scheduler: Any, app: Any) -> None:
    """Register :func:`purge_expired_magic_link_tokens_task` on *scheduler* (daily cron)."""
    try:
        scheduler.add_job(
            purge_expired_magic_link_tokens_task,
            trigger=CronTrigger.from_crontab(_PURGE_TOKENS_CRON),
            args=[app],
            id="purge_magic_link_tokens",
            name="Daily expired magic_link_tokens purge",
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        logger.info("purge_magic_link_tokens scheduler registered (cron=%s)", _PURGE_TOKENS_CRON)
    except Exception:
        logger.exception("Failed to register purge_magic_link_tokens job")
