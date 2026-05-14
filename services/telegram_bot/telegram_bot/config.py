"""Centralized configuration for the Telegram bot."""

import logging
import os
from dataclasses import dataclass

import asyncpg
from jarvis_common import build_database_url, init_pg_connection
from pydantic import SecretStr

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotConfig:
    """Immutable configuration loaded from environment variables."""

    telegram_token: str
    telegram_chat_id: int | None
    database_url: str
    paper_ingestion_url: str
    learning_engine_url: str
    jarvis_api_key: SecretStr | None

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Build config from environment variables.

        Raises
        ------
        SystemExit
            If required variables are missing.
        """
        from jarvis_common.settings import SecretsSettings  # noqa: PLC0415

        secrets = SecretsSettings()
        token_secret = secrets.telegram_bot_token
        token = token_secret.get_secret_value() if token_secret else ""
        if not token:
            logger.critical("TELEGRAM_BOT_TOKEN is not set")
            raise SystemExit(1)

        chat_id_str = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        chat_id: int | None
        if not chat_id_str:
            logger.info(
                "TELEGRAM_CHAT_ID is not set — bot will use DB pairing flow for outbound messages"
            )
            chat_id = None
        else:
            try:
                chat_id = int(chat_id_str)
            except ValueError:
                logger.warning(
                    "TELEGRAM_CHAT_ID=%r not parseable as integer; treating as unset",
                    chat_id_str,
                )
                chat_id = None

        try:
            database_url = build_database_url()
        except RuntimeError as exc:
            logger.critical("Cannot build DATABASE_URL: %s", exc)
            raise SystemExit(1) from exc

        api_key_secret = secrets.jarvis_api_key
        _raw_api_key = api_key_secret.get_secret_value() if api_key_secret else ""
        if not _raw_api_key:
            logger.warning("JARVIS_API_KEY not set — all API calls will be unauthenticated")
        jarvis_api_key: SecretStr | None = SecretStr(_raw_api_key) if _raw_api_key else None

        return cls(
            telegram_token=token,
            telegram_chat_id=chat_id,
            database_url=database_url,
            paper_ingestion_url=os.environ.get(
                "PAPER_INGESTION_URL", "http://paper_ingestion:8000"
            ),
            learning_engine_url=os.environ.get(
                "LEARNING_ENGINE_URL", "http://learning_engine:8001"
            ),
            jarvis_api_key=jarvis_api_key,
        )


async def create_db_pool(database_url: str) -> asyncpg.Pool:
    """Create an asyncpg connection pool with JSON codec support.

    Parameters
    ----------
    database_url : str
        PostgreSQL connection string.

    Returns
    -------
    asyncpg.Pool
        Ready-to-use connection pool.
    """
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=5, init=init_pg_connection)
    logger.info("Database pool created: %s", database_url.split("@")[-1])
    return pool
