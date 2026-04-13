"""Job scheduler for JARVIS automated workflows.

Uses APScheduler's AsyncIOScheduler to run cron-based jobs that are
configured in the scheduled_nudges database table.
"""

import importlib
import logging
from datetime import UTC, datetime

import asyncpg
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot

from app.config import BotConfig

logger = logging.getLogger(__name__)

JOB_REGISTRY: dict[str, str] = {
    "daily_summary": "app.orchestration.daily_briefing:run_daily_briefing",
    "paper_digest": "app.orchestration.paper_digest:run_paper_digest",
    "review_reminder": "app.orchestration.review_reminder:run_review_reminder",
    "deadline_warning": "app.orchestration.deadline_warning:run_deadline_warning",
    "research_pulse": "app.orchestration.research_pulse:run_research_pulse",
    "author_alert": "app.orchestration.author_alerts:run_author_alerts",
}


class JarvisScheduler:
    """Manages cron-based scheduled jobs from the database.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Database connection pool.
    http_client : httpx.AsyncClient
        Shared HTTP client.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    """

    def __init__(
        self,
        db_pool: asyncpg.Pool,
        http_client: httpx.AsyncClient,
        bot: Bot,
        config: BotConfig,
    ) -> None:
        self.db_pool = db_pool
        self.http_client = http_client
        self.bot = bot
        self.config = config
        self.scheduler = AsyncIOScheduler()

    async def load_and_start(self) -> None:
        """Load enabled nudges from DB and start the scheduler."""
        rows = await self.db_pool.fetch(
            "SELECT id, nudge_type, cron_expression FROM scheduled_nudges WHERE enabled = TRUE"
        )

        for row in rows:
            nudge_type = row["nudge_type"]
            cron_expr = row["cron_expression"]
            nudge_id = row["id"]

            if nudge_type not in JOB_REGISTRY:
                logger.warning("Unknown nudge_type: %s (id=%d)", nudge_type, nudge_id)
                continue

            try:
                parts = cron_expr.split()
                if len(parts) != 5:
                    logger.warning("Invalid cron expression: %s (id=%d)", cron_expr, nudge_id)
                    continue

                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                    timezone=UTC,
                )

                self.scheduler.add_job(
                    self._run_job,
                    trigger=trigger,
                    args=[nudge_type, nudge_id],
                    id=f"nudge_{nudge_id}",
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )
                logger.info(
                    "Scheduled job: %s (id=%d, cron=%s)",
                    nudge_type,
                    nudge_id,
                    cron_expr,
                )
            except Exception:
                logger.exception("Failed to schedule nudge id=%d", nudge_id)

        self.scheduler.start()
        logger.info("Scheduler started with %d jobs", len(self.scheduler.get_jobs()))

    async def _run_job(self, nudge_type: str, nudge_id: int) -> None:
        """Execute a scheduled job and update last_fired_at.

        Parameters
        ----------
        nudge_type : str
            Type of nudge to run.
        nudge_id : int
            Database ID of the nudge.
        """
        logger.info("Running scheduled job: %s (id=%d)", nudge_type, nudge_id)
        try:
            # Import and run the appropriate orchestration function
            module_path, func_name = JOB_REGISTRY[nudge_type].rsplit(":", 1)
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)
            await func(self.http_client, self.db_pool, self.bot, self.config)

            # Update last_fired_at
            await self.db_pool.execute(
                "UPDATE scheduled_nudges SET last_fired_at = $1 WHERE id = $2",
                datetime.now(UTC),
                nudge_id,
            )
            logger.info("Job completed: %s (id=%d)", nudge_type, nudge_id)
        except Exception:
            logger.exception("Job failed: %s (id=%d)", nudge_type, nudge_id)
            alert = (
                "⚠️ <b>Scheduled job failed</b>\n"
                f"\n<b>Type:</b> {nudge_type}\n"
                f"<b>ID:</b> {nudge_id}\n"
                "Please check service logs for details."
            )
            try:
                from app.owner import resolve_owner_chat_id

                owner = await resolve_owner_chat_id(self.db_pool, self.config)
                if owner is None:
                    logger.info(
                        "Skipping failure alert for job %s (id=%d): no telegram owner paired",
                        nudge_type,
                        nudge_id,
                    )
                    return
                await self.bot.send_message(
                    chat_id=owner,
                    text=alert,
                    parse_mode="HTML",
                )
                logger.info(
                    "Sent failure alert for job %s (id=%d)",
                    nudge_type,
                    nudge_id,
                )
            except Exception:
                logger.exception(
                    "Failed to send Telegram failure alert for job %s (id=%d)",
                    nudge_type,
                    nudge_id,
                )

    async def stop(self) -> None:
        """Shut down the scheduler."""
        self.scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
