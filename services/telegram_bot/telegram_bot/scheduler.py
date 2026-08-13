"""Job scheduler for JARVIS automated workflows.

Uses APScheduler's AsyncIOScheduler to run cron-based jobs that are
configured in the scheduled_nudges database table.
"""

import importlib
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from jarvis_common.maintenance import skip_for_maintenance
from telegram import Bot

from telegram_bot import formatters, services_client
from telegram_bot import owner as _owner
from telegram_bot.config import BotConfig
from telegram_bot.notification_policy import (
    SCHEDULED_NOTIFICATION_KINDS,
    ScheduledNotificationPolicy,
)

logger = logging.getLogger(__name__)

JOB_REGISTRY: dict[str, str] = {
    "daily_summary": "telegram_bot.orchestration.daily_briefing:run_daily_briefing",
    "paper_digest": "telegram_bot.orchestration.paper_digest:run_paper_digest",
    "review_reminder": "telegram_bot.orchestration.review_reminder:run_review_reminder",
    "deadline_warning": "telegram_bot.orchestration.deadline_warning:run_deadline_warning",
    "research_pulse": "telegram_bot.orchestration.research_pulse:run_research_pulse",
    "author_alert": "telegram_bot.orchestration.author_alerts:run_author_alerts",
}

if frozenset(JOB_REGISTRY) != SCHEDULED_NOTIFICATION_KINDS:
    raise RuntimeError("Every scheduled Telegram notification must have a focus delivery policy")


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
        self.delivery_policy = ScheduledNotificationPolicy(http_client, config)

    async def load_and_start(self) -> None:
        """Load enabled nudges from DB and start the scheduler."""
        await self.reload_nudges()
        await self._reconcile_focus_sessions()
        self.scheduler.add_job(
            self._reconcile_focus_sessions,
            "interval",
            seconds=10,
            id="focus_reconciliation",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        logger.info("Scheduler started with %d jobs", len(self.scheduler.get_jobs()))

    async def reload_nudges(self) -> None:
        """Re-read enabled nudges from DB and re-register all nudge_* jobs.

        Reads ``user.timezone`` from the personal row of the Telegram-paired
        owner user (resolved via ``telegram.owner_chat_id`` → ``telegram_user_pairings``),
        falling back to the operator-level ``user_id IS NULL`` seed row, then
        to ``"UTC"`` when neither is present.

        The reload is atomic: all DB rows are parsed into trigger objects first.
        Rows that fail (bad timezone or invalid cron expression) are WARN-logged
        and skipped. Only after the prepare pass succeeds are existing jobs
        removed and new ones registered — leaving the scheduler in a consistent
        state even if individual rows are malformed.
        """
        # Resolve timezone: owner's personal row wins; operator-level seed is fallback.
        tz_str: str = await self._resolve_owner_timezone()
        try:
            tz = ZoneInfo(tz_str)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown timezone %r, falling back to UTC", tz_str)
            tz = UTC  # type: ignore[assignment]
            tz_str = "UTC"

        # Re-read from DB
        rows = await self.db_pool.fetch(
            "SELECT id, nudge_type, cron_expression FROM scheduled_nudges WHERE enabled = TRUE"
        )

        # --- Prepare pass (no mutations yet) ---
        # Build a list of (nudge_id, nudge_type, cron_expr, trigger) for every
        # row that parses successfully.  Bad rows are WARN-logged and skipped.
        prepared: list[tuple[int, str, str, CronTrigger]] = []
        for row in rows:
            nudge_type: str = row["nudge_type"]
            cron_expr: str = row["cron_expression"]
            nudge_id: int = row["id"]

            if nudge_type not in JOB_REGISTRY:
                logger.warning("Unknown nudge_type: %s (id=%d)", nudge_type, nudge_id)
                continue

            parts = cron_expr.split()
            if len(parts) != 5:
                logger.warning("Invalid cron expression: %s (id=%d)", cron_expr, nudge_id)
                continue

            try:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                    timezone=tz,
                )
            except ValueError:
                logger.warning(
                    "Unparseable cron expression %r (id=%d), skipping",
                    cron_expr,
                    nudge_id,
                )
                continue

            prepared.append((nudge_id, nudge_type, cron_expr, trigger))

        # --- Commit pass (only after all rows are prepared) ---
        # Remove existing nudge_* jobs then register the freshly-built set.
        for job in list(self.scheduler.get_jobs()):
            if job.id.startswith("nudge_"):
                job.remove()
                logger.debug("Removed stale job: %s", job.id)

        registered = 0
        for nudge_id, nudge_type, cron_expr, trigger in prepared:
            self.scheduler.add_job(
                self._run_job,
                trigger=trigger,
                args=[nudge_type, nudge_id],
                id=f"nudge_{nudge_id}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            registered += 1
            logger.info(
                "Scheduled job: %s (id=%d, cron=%s, tz=%s)",
                nudge_type,
                nudge_id,
                cron_expr,
                tz_str,
            )

        logger.info("reload_nudges: registered %d jobs (tz=%s)", registered, tz_str)

    async def _resolve_owner_timezone(self) -> str:
        """Return the timezone string for the Telegram-paired owner user.

        Resolution order:
        1. Personal ``user.timezone`` row for the user whose chat_id matches
           ``telegram.owner_chat_id`` (the deployment's Telegram owner).
        2. Operator-level ``user.timezone`` seed row (``user_id IS NULL``).
        3. Hardcoded ``"UTC"`` when neither row exists.
        """
        owner_chat_id_row = await self.db_pool.fetchrow(
            "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL"
        )
        owner_chat_id = owner_chat_id_row["value"] if owner_chat_id_row else None
        owner_chat_id_int: int | None = None
        if owner_chat_id is not None:
            try:
                owner_chat_id_int = int(owner_chat_id)
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring non-numeric telegram.owner_chat_id %r for timezone resolution",
                    owner_chat_id,
                )
        if owner_chat_id_int is not None:
            pairing_row = await self.db_pool.fetchrow(
                "SELECT user_id FROM telegram_user_pairings WHERE chat_id = $1",
                owner_chat_id_int,
            )
            if pairing_row is not None:
                owner_user_id = pairing_row["user_id"]
                tz_row = await self.db_pool.fetchrow(
                    "SELECT value FROM user_config WHERE key = 'user.timezone' AND user_id = $1",
                    owner_user_id,
                )
                if tz_row and tz_row["value"]:
                    return str(tz_row["value"])
        fallback_row = await self.db_pool.fetchrow(
            "SELECT value FROM user_config WHERE key = 'user.timezone' AND user_id IS NULL"
        )
        return str(fallback_row["value"]) if fallback_row and fallback_row["value"] else "UTC"

    async def _resolve_owner_chat_id(self) -> int | None:
        """Return the numeric telegram.owner_chat_id, or None when unset/invalid."""
        row = await self.db_pool.fetchrow(
            "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL"
        )
        raw = row["value"] if row else None
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("Ignoring non-numeric telegram.owner_chat_id %r for failure alert", raw)
            return None

    async def _run_job(self, nudge_type: str, nudge_id: int) -> None:
        """Execute a scheduled job and update last_fired_at.

        Parameters
        ----------
        nudge_type : str
            Type of nudge to run.
        nudge_id : int
            Database ID of the nudge.
        """
        if skip_for_maintenance(f"telegram nudge {nudge_type}"):
            return
        logger.info("Running scheduled job: %s (id=%d)", nudge_type, nudge_id)
        try:
            # Import and run the appropriate orchestration function
            module_path, func_name = JOB_REGISTRY[nudge_type].rsplit(":", 1)
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)
            await func(
                self.http_client,
                self.db_pool,
                self.bot,
                self.config,
                delivery_policy=self.delivery_policy,
            )

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
                f"\n<b>Type:</b> {formatters.escape(nudge_type)}\n"
                f"<b>ID:</b> {formatters.escape(str(nudge_id))}\n"
                "Please check service logs for details."
            )
            try:
                owner_chat_id = await self._resolve_owner_chat_id()
                if owner_chat_id is None:
                    logger.warning(
                        "Skipping failure alert for job %s (id=%d): telegram.owner_chat_id unset",
                        nudge_type,
                        nudge_id,
                    )
                    return
                # Operator-level failure notice goes ONLY to the owner chat —
                # scheduled_nudges is deployment-global, so broadcasting to every
                # paired user would leak operator diagnostics to all tenants.
                await self.bot.send_message(
                    chat_id=owner_chat_id,
                    text=alert,
                    parse_mode="HTML",
                )
                logger.info(
                    "Sent failure alert for job %s (id=%d) to owner chat",
                    nudge_type,
                    nudge_id,
                )
            except Exception:
                logger.exception(
                    "Failed to send Telegram failure alert for job %s (id=%d)",
                    nudge_type,
                    nudge_id,
                )

    async def _reconcile_focus_sessions(self) -> None:
        """Deliver durable Telegram focus completions after timer or bot restarts."""
        pairings = await _owner.list_user_pairings(self.db_pool)
        for pairing in pairings:
            try:
                session = await services_client.fetch_pending_telegram_focus_completion(
                    self.http_client,
                    self.config,
                    pairing.user_id,
                )
            except Exception:
                logger.warning("Focus completion reconciliation failed", exc_info=True)
                continue
            if session is None:
                continue
            minutes = session.recorded_seconds / 60
            duration = f"{minutes:g}"
            try:
                await self.bot.send_message(
                    chat_id=pairing.chat_id,
                    text=(
                        f"Focus session complete ({duration} minutes). "
                        "Did you finish your task? Want to add any notes?"
                    ),
                )
                await services_client.acknowledge_telegram_focus_completion(
                    self.http_client,
                    self.config,
                    pairing.user_id,
                    session.id,
                )
            except Exception:
                logger.warning("Focus completion delivery failed", exc_info=True)

    async def stop(self) -> None:
        """Shut down the scheduler."""
        self.scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
