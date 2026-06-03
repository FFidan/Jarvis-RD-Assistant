"""JARVIS Telegram Bot.

Stateful Telegram bot handling daily briefings, flashcard review sessions,
task management, and paper interactions via inline keyboards.
"""

import asyncio
import logging
import sys

import httpx
from jarvis_common.crypto import reload_fernet_on_sighup
from jarvis_common.logging_config import configure_logging
from jarvis_common.settings import get_core_settings
from telegram import BotCommand
from telegram.ext import Application

from telegram_bot.config import BotConfig, create_db_pool
from telegram_bot.handlers import (
    get_review_conversation_handler,
    register_callback_handlers,
    register_command_handlers,
)
from telegram_bot.internal_api import start_internal_server
from telegram_bot.scheduler import JarvisScheduler

configure_logging("telegram_bot", log_level=get_core_settings().log_level)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Initialize shared resources after the Application is built.

    Creates database pool and HTTP client, stores them in bot_data.

    Parameters
    ----------
    application : Application
        The python-telegram-bot Application instance.
    """
    config: BotConfig = application.bot_data["config"]
    application.bot_data["db_pool"] = await create_db_pool(config.database_url)
    application.bot_data["http_client"] = httpx.AsyncClient(
        timeout=30.0,
        headers=(
            {"X-API-Key": config.jarvis_api_key.get_secret_value()} if config.jarvis_api_key else {}
        ),
    )

    # Start scheduler
    scheduler = JarvisScheduler(
        db_pool=application.bot_data["db_pool"],
        http_client=application.bot_data["http_client"],
        bot=application.bot,
        config=config,
    )
    await scheduler.load_and_start()
    application.bot_data["scheduler"] = scheduler

    # Start internal HTTP API in the background (for reload-nudges endpoint)
    _internal_api_task = asyncio.get_running_loop().create_task(
        start_internal_server(scheduler),
        name="internal_api",
    )
    application.bot_data["internal_api_task"] = _internal_api_task

    def _log_internal_api_exception(task: "asyncio.Task[None]") -> None:
        if task.cancelled():
            return
        if (exc := task.exception()) is not None:
            logger.error("internal_api task raised: %s", exc, exc_info=exc)

    _internal_api_task.add_done_callback(_log_internal_api_exception)

    # Register bot commands for the Telegram "/" autocomplete menu
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Show help"),
            BotCommand("papers", "List recent papers"),
            BotCommand("briefing", "Daily briefing"),
            BotCommand("next", "Next paper recommendation"),
            BotCommand("inbox", "Show your unread papers"),
            BotCommand("pulse_now", "Run Pulse discovery now"),
            BotCommand("review", "Start flashcard review"),
            BotCommand("stats", "Learning statistics"),
            BotCommand("projects", "List active projects"),
            BotCommand("newproject", "Create a new project"),
            BotCommand("tasks", "List in-progress tasks"),
            BotCommand("done", "Mark task complete"),
            BotCommand("focus", "Start a focus session"),
            BotCommand("pair", "Pair this chat to your JARVIS account"),
            BotCommand("unpair", "Unlink this chat from your account"),
            BotCommand("whoami", "Show which account this chat is paired to"),
        ]
    )

    logger.info("Bot initialized: db_pool, http_client, scheduler, and internal API ready")


async def post_shutdown(application: Application) -> None:
    """Clean up shared resources on shutdown.

    Parameters
    ----------
    application : Application
        The python-telegram-bot Application instance.
    """
    import telegram_bot.internal_api as _iapi  # local import to avoid circular refs

    # Gracefully stop the internal uvicorn server (D-01 / H-02)
    if _iapi._server_state.server is not None:
        _iapi._server_state.server.should_exit = True
    if _iapi._server_state.task is not None:
        try:
            await asyncio.wait_for(_iapi._server_state.task, timeout=5.0)
        except TimeoutError:
            logger.warning("Internal API server task did not stop within 5 s — continuing shutdown")
        except asyncio.CancelledError:
            logger.warning("Internal API server task was cancelled during shutdown")

    scheduler = application.bot_data.get("scheduler")
    if scheduler:
        await scheduler.stop()

    http_client = application.bot_data.get("http_client")
    if http_client:
        await http_client.aclose()

    db_pool = application.bot_data.get("db_pool")
    if db_pool:
        await db_pool.close()

    logger.info("Bot shutdown: resources released")


def main() -> None:
    """Build the bot application and start polling for updates.

    Reads configuration via :func:`BotConfig.from_env`, constructs the
    ``python-telegram-bot`` ``Application``, registers all command and
    callback handlers, and runs the event loop with ``run_polling``.
    Exits with code 1 when configuration is invalid.
    """
    reload_fernet_on_sighup()

    try:
        config = BotConfig.from_env()
    except SystemExit:
        sys.exit(1)

    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.bot_data["config"] = config

    # Register handlers
    register_command_handlers(application)
    application.add_handler(get_review_conversation_handler())
    register_callback_handlers(application)

    logger.info("JARVIS Telegram Bot starting")
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
