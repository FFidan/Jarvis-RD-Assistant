"""Daily purge of stale ``sessions`` rows and expired WebAuthn challenges.

A session row is dead once it can no longer authenticate anyone: either its
``expires_at`` is well past the offline grace window, or it has been revoked.
``session_middleware`` already rejects both, so such rows are pure table bloat
and a needless retention surface. Delete sessions expired more than 30 days ago,
and revoked ones more than 7 days ago — a short window is kept beyond each event
for server-clock skew and diagnostic inspection.

``webauthn_challenges`` rows are single-use nonces for in-flight passkey
ceremonies; each carries its own hard ``expires_at``. Any row past that instant
is spent and safe to delete outright — no grace window applies.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from apscheduler.triggers.cron import CronTrigger
from jarvis_common.maintenance import skip_for_maintenance

logger = logging.getLogger(__name__)

# Daily at 03:35 — after the magic_link_tokens purge (03:20) and classifier
# training (03:30), before the Pulse deck (04:00).
_PURGE_SESSIONS_CRON = "35 3 * * *"


async def _run_purge(
    pool: Any, operation: Literal["sessions", "webauthn_challenges"], noun: str
) -> None:
    """Run one purge DELETE, log the deleted-row count, and swallow any DB error.

    Calls ``pool.execute`` directly (single statement, no ``acquire`` needed —
    consistent with ``purge_expired_magic_link_tokens``). A transient DB failure
    is logged and swallowed so it never crashes the scheduler.
    """
    try:
        deleted = int(
            await pool.fetchval("SELECT platform.purge_identity_retention_v1($1)", operation)
        )
        logger.info("purge_sessions: deleted %d stale %s", deleted, noun)
    except Exception:
        logger.exception("purge_sessions: failed to purge stale %s", noun)


async def purge_stale_sessions(pool: Any) -> None:
    """Delete stale ``sessions`` rows and expired ``webauthn_challenges`` rows.

    Each DELETE is issued and error-handled independently so a failure of one
    does not skip the other.
    """
    await _run_purge(pool, "sessions", "session(s)")
    await _run_purge(pool, "webauthn_challenges", "webauthn challenge(s)")


async def purge_stale_sessions_task(app: Any) -> None:
    """APScheduler entrypoint — extracts pool from ``app.state`` and delegates."""
    if skip_for_maintenance("purge sessions"):
        return
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
            misfire_grace_time=3600,
        )
        logger.info("purge_sessions scheduler registered (cron=%s)", _PURGE_SESSIONS_CRON)
    except Exception:
        logger.exception("Failed to register purge_sessions job")
