"""Scheduler side-effects: cron rescheduling with DB rollback, and interval updates."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

__all__ = [
    "apply_pulse_cron",
    "apply_fetch_interval",
]

logger = logging.getLogger(__name__)


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
