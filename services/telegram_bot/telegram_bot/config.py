"""Centralized configuration for the Telegram bot."""

from __future__ import annotations

import logging

import asyncpg
from jarvis_common import init_pg_connection
from jarvis_common.app_factory import build_database_url
from jarvis_common.config import JarvisCommonSettings
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

logger = logging.getLogger(__name__)


class BotConfig(JarvisCommonSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, populate_by_name=True
    )
    """Typed pydantic-settings configuration for the Telegram bot.

    Extends ``JarvisCommonSettings`` with bot-specific keys.  All fields map
    1:1 to the existing env vars — no drops, no renames.

    1:1 env-var table (telegram-bot layer)
    ----------------------------------------
    Env var                 Field                   Notes
    ---                     ---                     ---
    TELEGRAM_BOT_TOKEN      telegram_token          Required; bot token
    TELEGRAM_CHAT_ID        telegram_chat_id        Optional; int or None
    DATABASE_URL            database_url            Inherited; fallback DSN
    PAPER_INGESTION_URL     paper_ingestion_url     Service URL
    LEARNING_ENGINE_URL     learning_engine_url     Service URL
    JARVIS_API_KEY          jarvis_api_key          Optional; auth header
    """

    # --- Telegram bot token ---------------------------------------------
    telegram_token: str = Field(
        default="",
        alias="TELEGRAM_BOT_TOKEN",
        description="Telegram bot token (TELEGRAM_BOT_TOKEN).  Required at runtime.",
    )

    # --- Outbound chat ID -----------------------------------------------
    telegram_chat_id: int | None = Field(
        default=None,
        alias="TELEGRAM_CHAT_ID",
        description=(
            "Telegram chat ID for outbound messages (TELEGRAM_CHAT_ID).  "
            "None = use DB pairing flow."
        ),
    )

    # --- Backend service URLs -------------------------------------------
    paper_ingestion_url: str = Field(
        default="http://paper_ingestion:8000",
        description="Paper Ingestion service URL (PAPER_INGESTION_URL).",
    )
    learning_engine_url: str = Field(
        default="http://learning_engine:8001",
        description="Learning Engine service URL (LEARNING_ENGINE_URL).",
    )

    # --- API auth key ---------------------------------------------------
    jarvis_api_key: SecretStr | None = Field(  # type: ignore[assignment]
        default=None,
        description="JARVIS API key for authenticated backend calls (JARVIS_API_KEY).",
    )

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def _coerce_chat_id(cls, v: object) -> int | None:
        """Coerce TELEGRAM_CHAT_ID: non-integer strings become None."""
        if v is None or v == "":
            return None
        try:
            return int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning("TELEGRAM_CHAT_ID=%r not parseable as integer; treating as unset", v)
            return None

    @classmethod
    def from_env(cls) -> BotConfig:
        """Build and validate config from environment variables.

        Raises
        ------
        SystemExit
            If required variables are missing (TELEGRAM_BOT_TOKEN, DATABASE_URL).
        """
        cfg = cls()

        if not cfg.telegram_token:
            logger.critical("TELEGRAM_BOT_TOKEN is not set")
            raise SystemExit(1)

        if cfg.telegram_chat_id is None:
            logger.info(
                "TELEGRAM_CHAT_ID is not set — bot will use DB pairing flow for outbound messages"
            )

        # Resolve database URL via Docker-Secret-aware helper; overrides the
        # plain DATABASE_URL field when a secrets file is mounted.
        try:
            resolved_url = build_database_url()
        except RuntimeError as exc:
            logger.critical("Cannot build DATABASE_URL: %s", exc)
            raise SystemExit(1) from exc

        if not cfg.jarvis_api_key:
            logger.warning("JARVIS_API_KEY not set — all API calls will be unauthenticated")

        # Return a new instance with the resolved database_url applied.
        return cfg.model_copy(update={"database_url": resolved_url})


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
