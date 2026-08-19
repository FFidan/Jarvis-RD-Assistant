"""JARVIS Telegram Bot.

Stateful Telegram bot handling daily briefings, flashcard review sessions,
task management, and paper interactions via inline keyboards.
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from typing import Any

from jarvis_common.config import get_jarvis_common_settings
from jarvis_common.logging_config import configure_logging
from jarvis_common.maintenance import (
    ensure_outbound_egress_allowed,
    maintenance_active,
    outbound_quarantine_active,
    secrets_rotated_since,
    skip_for_maintenance,
)
from jarvis_common.pinned_transport import JARVIS_SERVICE_POLICY, pinned_async_client
from jarvis_common.sentry import maybe_init_sentry
from jarvis_common.settings import get_core_settings
from jarvis_common.telemetry import configure_telemetry, flush_telemetry
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from telegram_bot.command_catalog import menu_command_specs
from telegram_bot.config import BotConfig, service_headers
from telegram_bot.handlers import (
    get_review_conversation_handler,
    register_callback_handlers,
    register_command_handlers,
)
from telegram_bot.internal_api import start_internal_server
from telegram_bot.scheduler import JarvisScheduler
from telegram_bot.service_auth import TelegramBackendAuth

configure_logging("telegram_bot", log_level=get_core_settings().log_level)
maybe_init_sentry("telegram_bot")
logger = logging.getLogger(__name__)


class _QuarantineAwareHTTPXRequest(HTTPXRequest):
    """Telegram transport that enforces quarantine at the network boundary."""

    async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        """Send a Telegram request only when outbound egress is permitted.

        Parameters
        ----------
        *args : Any
            Positional arguments accepted by :meth:`HTTPXRequest.do_request`.
        **kwargs : Any
            Keyword arguments accepted by :meth:`HTTPXRequest.do_request`.

        Returns
        -------
        tuple[int, bytes]
            HTTP status code and response body from Telegram.

        Raises
        ------
        OutboundEgressBlockedError
            If restored credentials await review before the request.
        """
        ensure_outbound_egress_allowed("Telegram Bot API request")
        return await super().do_request(*args, **kwargs)


async def _secrets_rotation_watcher(started_at: float, poll_interval_s: float = 5.0) -> None:
    """Restart the bot after a completed restore refreshes mounted secrets.

    The watcher waits until maintenance ends before requesting a clean shutdown.
    Container restart policy then starts the bot with the refreshed files.

    Parameters
    ----------
    started_at : float
        Bot start time expressed as a Unix epoch.
    poll_interval_s : float
        Seconds between marker checks.
    """
    while True:
        try:
            await asyncio.sleep(poll_interval_s)
            if not maintenance_active() and secrets_rotated_since(started_at):
                logger.warning("secrets rotated; restarting to reload updated secrets")
                os.kill(os.getpid(), signal.SIGINT)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("secrets-rotation watcher tick failed", exc_info=True)


async def post_init(application: Application) -> None:
    """Initialize shared resources after the Application is built.

    Creates scoped Platform and backend HTTP clients and starts the scheduler.

    Parameters
    ----------
    application : Application
        The python-telegram-bot Application instance.

    Raises
    ------
    OutboundEgressBlockedError
        If restored credentials await review before Telegram resources start.
    """
    ensure_outbound_egress_allowed("Telegram bot startup")
    settings = get_jarvis_common_settings()
    configure_telemetry(
        service="telegram_bot",
        enabled=settings.observability_enabled,
        otlp_endpoint=getattr(settings, "otel_exporter_otlp_traces_endpoint", None),
        timeout_ms=getattr(settings, "otel_export_timeout_ms", 5_000),
    )
    config: BotConfig = application.bot_data["config"]
    platform_client = pinned_async_client(
        JARVIS_SERVICE_POLICY,
        timeout=10.0,
        headers=service_headers(config),
    )
    application.bot_data["platform_client"] = platform_client
    application.bot_data["http_client"] = pinned_async_client(
        JARVIS_SERVICE_POLICY,
        timeout=30.0,
        auth=TelegramBackendAuth(config, platform_client),
    )

    # Start scheduler
    scheduler = JarvisScheduler(
        platform_client=platform_client,
        http_client=application.bot_data["http_client"],
        bot=application.bot,
        config=config,
    )
    await scheduler.load_and_start()
    application.bot_data["scheduler"] = scheduler

    # Start the private liveness API in the background.
    _internal_api_task = asyncio.get_running_loop().create_task(
        start_internal_server(),
        name="internal_api",
    )
    application.bot_data["internal_api_task"] = _internal_api_task

    def _log_internal_api_exception(task: "asyncio.Task[None]") -> None:
        if task.cancelled():
            return
        if (exc := task.exception()) is not None:
            logger.error("internal_api task raised: %s", exc, exc_info=exc)

    _internal_api_task.add_done_callback(_log_internal_api_exception)

    # Restart the bot when an off-host restore replaces its configured secrets.
    _secrets_watcher_task = asyncio.get_running_loop().create_task(
        _secrets_rotation_watcher(time.time()),
        name="secrets_rotation_watcher",
    )
    application.bot_data["secrets_rotation_watcher_task"] = _secrets_watcher_task

    # Register bot commands for the Telegram "/" autocomplete menu
    await application.bot.set_my_commands(
        [BotCommand(spec.name, spec.description) for spec in menu_command_specs()]
    )

    logger.info("Bot initialized: scoped HTTP clients, scheduler, and liveness API ready")


async def post_shutdown(application: Application) -> None:
    """Clean up shared resources on shutdown.

    Parameters
    ----------
    application : Application
        The python-telegram-bot Application instance.
    """
    try:
        import telegram_bot.internal_api as _iapi  # local import to avoid circular refs

        # Gracefully stop the internal uvicorn server
        if _iapi._server_state.server is not None:
            _iapi._server_state.server.should_exit = True
        if _iapi._server_state.task is not None:
            try:
                await asyncio.wait_for(_iapi._server_state.task, timeout=5.0)
            except TimeoutError:
                logger.warning(
                    "Internal API server task did not stop within 5 s — continuing shutdown"
                )
            except asyncio.CancelledError:
                logger.warning("Internal API server task was cancelled during shutdown")

        watcher_task = application.bot_data.get("secrets_rotation_watcher_task")
        if watcher_task is not None:
            watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher_task

        scheduler = application.bot_data.get("scheduler")
        if scheduler:
            await scheduler.stop()

        http_client = application.bot_data.get("http_client")
        if http_client:
            await http_client.aclose()

        platform_client = application.bot_data.get("platform_client")
        if platform_client:
            await platform_client.aclose()
    finally:
        # The global provider survives bot restarts; only flush this lifecycle.
        flush_telemetry()

    logger.info("Bot shutdown: resources released")


async def _maintenance_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Short-circuit updates during restore maintenance or outbound quarantine.

    The bot bypasses the HTTP :class:`MaintenanceMiddleware`, so this handler is
    registered in group ``-1`` to run before every command/callback handler:
    during a restore it replies once and raises ``ApplicationHandlerStop`` so no
    downstream handler writes to the database. Quarantine stops silently because
    even a denial reply would use the restored Telegram token.
    """
    if outbound_quarantine_active():
        raise ApplicationHandlerStop
    if not maintenance_active():
        return
    notice = "Restore in progress — please try again shortly."
    if update.callback_query is not None:
        with contextlib.suppress(Exception):
            await update.callback_query.answer(notice, show_alert=True)
    elif update.effective_message is not None:
        with contextlib.suppress(Exception):
            await update.effective_message.reply_text(notice)
    raise ApplicationHandlerStop


async def _unrecognized_text(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer plain text so a message the bot cannot act on is never dropped silently.

    Registered last in the default group, so every command, conversation state,
    and callback claims its own updates first.
    """
    if update.message is None:
        return
    await update.message.reply_text("I only understand commands — try /help")


def main() -> None:
    """Build the bot application and start polling for updates.

    Reads configuration via :func:`BotConfig.from_env`, constructs the
    ``python-telegram-bot`` ``Application``, registers all command and
    callback handlers, and runs the event loop with ``run_polling``.
    Exits with code 1 when configuration is invalid.

    Notes
    -----
    Active restore maintenance or outbound quarantine causes a clean return
    before configuration is read or Telegram polling begins.
    """
    if skip_for_maintenance("Telegram polling"):
        return

    try:
        config = BotConfig.from_env()
    except SystemExit:
        sys.exit(1)

    builder = Application.builder()
    builder.request(_QuarantineAwareHTTPXRequest())
    builder.get_updates_request(_QuarantineAwareHTTPXRequest(connection_pool_size=1))
    application = (
        builder.token(config.telegram_token.get_secret_value())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.bot_data["config"] = config

    # Maintenance gate runs before every other handler (group -1): the bot
    # bypasses the HTTP MaintenanceMiddleware, so this is its restore guard.
    application.add_handler(TypeHandler(Update, _maintenance_gate), group=-1)

    # Register handlers
    register_command_handlers(application)
    application.add_handler(get_review_conversation_handler())
    register_callback_handlers(application)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _unrecognized_text))

    logger.info("JARVIS Telegram Bot starting")
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
