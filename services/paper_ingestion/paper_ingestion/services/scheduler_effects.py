"""Scheduler side-effects: cron rescheduling with DB rollback, and interval updates."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

__all__ = [
    "apply_pulse_cron",
    "apply_zotero_cron",
    "apply_fetch_interval",
]

logger = logging.getLogger(__name__)


async def _apply_cron_reschedule(
    *,
    scheduler: Any,
    job_id: str,
    new_cron: str,
    old_cron: str | None,
    rollback_sql_factory: Any,
    db_pool: asyncpg.Pool,
) -> None:
    """Reschedule *job_id* and roll back the DB write on failure.

    *rollback_sql_factory* is called with ``(conn, old_cron)`` to issue the
    DB rollback when the reschedule raises.  It must handle the ``old_cron is
    None`` (delete) case internally.
    """
    try:
        scheduler.reschedule_job(
            job_id,
            trigger=CronTrigger.from_crontab(new_cron),
        )
        logger.info("%s rescheduled live (cron=%s)", job_id, new_cron)
    except Exception:
        async with db_pool.acquire() as conn:
            await rollback_sql_factory(conn, old_cron)
        logger.error(
            "%s reschedule failed; DB write rolled back (cron=%s)",
            job_id,
            new_cron,
            exc_info=True,
        )
        raise


async def apply_pulse_cron(
    *,
    db_pool: asyncpg.Pool,
    scheduler: Any,
    new_cron: str,
    old_cron: str | None,
) -> None:
    """Reschedule the pulse_overnight job and validate next_run_time.

    Rolls back the DB write if the job produces an invalid next_run_time, then
    raises ``fastapi.HTTPException(400)``.  Any other scheduler exception
    propagates unchanged.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    if scheduler is None:
        return
    scheduler.reschedule_job(
        "pulse_overnight",
        trigger=CronTrigger.from_crontab(new_cron),
    )
    logger.info("pulse_overnight rescheduled live (cron=%s)", new_cron)

    job = scheduler.get_job("pulse_overnight")
    now = datetime.now(UTC)
    next_run = job.next_run_time if job is not None else None
    if next_run is None or not (now <= next_run <= now + timedelta(days=366)):
        logger.error(
            "pulse_overnight reschedule produced invalid next_run_time=%s for cron=%s; reverting",
            next_run,
            new_cron,
        )
        _rollback_sql = (
            "INSERT INTO user_config (user_id, key, value)"
            " VALUES (NULL, 'pulse.cron', $1::jsonb)"
            " ON CONFLICT (user_id, key) DO UPDATE"
            " SET value = $1::jsonb, updated_at = NOW()"
        )
        async with db_pool.acquire() as conn:
            if old_cron is not None:
                await conn.execute(_rollback_sql, old_cron)
            else:
                await conn.execute(
                    "DELETE FROM user_config WHERE key = 'pulse.cron' AND user_id IS NULL"
                )
        try:
            if old_cron is not None:
                scheduler.reschedule_job(
                    "pulse_overnight",
                    trigger=CronTrigger.from_crontab(old_cron),
                )
        except Exception:
            logger.warning("pulse_overnight scheduler revert also failed", exc_info=True)
        raise HTTPException(
            status_code=400,
            detail="Cron expression produced an invalid next run time"
            " (must be within the next 366 days)",
        )


async def apply_zotero_cron(
    *,
    db_pool: asyncpg.Pool,
    scheduler: Any,
    new_cron: str,
    old_cron: str | None,
    row_user_id: int | None,
) -> None:
    """Legacy global Zotero cron updater kept for compatibility exports."""
    if scheduler is None:
        return

    async def _rollback(conn: Any, old: str | None) -> None:
        _sql = (
            "INSERT INTO user_config (user_id, key, value)"
            " VALUES ($1, 'zotero.poll_cron', $2::jsonb)"
            " ON CONFLICT (user_id, key) DO UPDATE"
            " SET value = $2::jsonb, updated_at = NOW()"
        )
        if old is not None:
            await conn.execute(_sql, row_user_id, old)
        else:
            await conn.execute(
                "DELETE FROM user_config"
                " WHERE key = 'zotero.poll_cron'"
                " AND user_id IS NOT DISTINCT FROM $1",
                row_user_id,
            )

    await _apply_cron_reschedule(
        scheduler=scheduler,
        job_id="zotero_library_sync",
        new_cron=new_cron,
        old_cron=old_cron,
        rollback_sql_factory=_rollback,
        db_pool=db_pool,
    )


def apply_fetch_interval(
    *,
    scheduler: Any,
    hours: int,
) -> None:
    """Reschedule the auto_pipeline job to the new interval (best-effort)."""
    if scheduler is None:
        return
    job = scheduler.get_job("auto_pipeline")
    if job is not None:
        try:
            scheduler.reschedule_job(
                "auto_pipeline",
                trigger=IntervalTrigger(hours=hours),
            )
            logger.info("auto_pipeline rescheduled live (interval=%dh)", hours)
        except Exception:
            logger.warning(
                "auto_pipeline reschedule failed (interval=%dh); persisted value still saved",
                hours,
                exc_info=True,
            )
    else:
        logger.warning(
            "auto_pipeline job not found in scheduler; persisted value will take effect on restart"
        )
