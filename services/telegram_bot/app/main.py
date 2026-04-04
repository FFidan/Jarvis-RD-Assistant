"""JARVIS Telegram Bot.

Stateful Telegram bot handling daily briefings, flashcard review sessions,
task management, and paper interactions via inline keyboards.
"""

import logging
import os
import sys

import httpx
from telegram.ext import Application

from app.config import BotConfig, create_db_pool
from app.handlers import (
    get_review_conversation_handler,
    register_callback_handlers,
    register_command_handlers,
)
from app.scheduler import JarvisScheduler
from jarvis_common.logging_config import configure_logging

configure_logging("telegram_bot", log_level=os.environ.get("LOG_LEVEL", "INFO"))
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
        headers={"X-API-Key": config.jarvis_api_key} if config.jarvis_api_key else {},
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

    logger.info("Bot initialized: db_pool, http_client, and scheduler ready")


async def post_shutdown(application: Application) -> None:
    """Clean up shared resources on shutdown.

    Parameters
    ----------
    application : Application
        The python-telegram-bot Application instance.
    """
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
    """Start the Telegram bot."""
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

    logger.info(
        "JARVIS Telegram Bot starting (chat_id=%d)", config.telegram_chat_id
    )
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
