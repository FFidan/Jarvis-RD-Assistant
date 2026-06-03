"""Centralized configuration for the Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import asyncpg
from jarvis_common import init_pg_connection
from jarvis_common.app_factory import build_database_url
from jarvis_common.config import JarvisCommonSettings
from jarvis_common.crypto import resolve_secret_row
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

logger = logging.getLogger(__name__)


async def _read_db_bot_token(database_url: str) -> str | None:
    """Return the wizard-saved Telegram bot token from ``user_config``.

    The first-run wizard persists ``telegram.bot_token`` (Fernet-encrypted,
    ``user_id IS NULL``). Reading it here makes a UI-saved token the source of
    truth — no .env edit, just a container restart. Returns ``None`` (and never
    raises) when the row is absent or the DB/key is unavailable, so the env /
    Docker-secret value remains the fallback.
    """
    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(database_url, server_settings={"jit": "off"}),
            timeout=5.0,
        )
        assert conn is not None  # asyncpg.connect() never returns None; guard for type checker
        await init_pg_connection(conn)
        row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config "
            "WHERE key = 'telegram.bot_token' AND user_id IS NULL",
        )
        if row is None:
            return None
        token = resolve_secret_row(row)
        return token or None
    except Exception:  # noqa: BLE001 — best-effort; env/secret is the fallback
        logger.debug("telegram.bot_token DB lookup failed; using env", exc_info=True)
        return None
    finally:
        if conn is not None:
            await conn.close()


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

    # --- Public base URL ------------------------------------------------
    jarvis_base_url: str | None = Field(
        default=None,
        alias="JARVIS_BASE_URL",
        description=(
            "Absolute public base URL of the JARVIS dashboard (JARVIS_BASE_URL). "
            "Used to build working deep-links in Telegram digests; None omits the link."
        ),
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

        # Resolve database URL first (Docker-Secret-aware) — we need it both
        # for the DB-stored token lookup and for the running bot.
        try:
            resolved_url = build_database_url()
        except RuntimeError as exc:
            logger.critical("Cannot build DATABASE_URL: %s", exc)
            raise SystemExit(1) from exc

        # DB-first token: a token saved via the first-run wizard
        # (user_config.telegram.bot_token) wins over the env/Docker-secret
        # value, so changing it is a UI save + container restart, never an
        # .env edit. Falls back to the env token when the DB has none.
        token = cfg.telegram_token
        if not token:
            # BotConfig (JarvisCommonSettings) does not apply the `_FILE` secret
            # indirection, so the bare TELEGRAM_BOT_TOKEN env is empty when only
            # the Docker secret (TELEGRAM_BOT_TOKEN_FILE) is mounted — which is the
            # documented convention. Read that secret file directly so a preserved
            # .env/secret token works without requiring the first-run wizard.
            token_file = os.environ.get("TELEGRAM_BOT_TOKEN_FILE", "")
            if token_file:
                try:
                    token = Path(token_file).read_text().strip()
                except OSError:
                    logger.warning("TELEGRAM_BOT_TOKEN_FILE=%r could not be read", token_file)
        db_token = asyncio.run(_read_db_bot_token(resolved_url))
        if db_token:
            token = db_token
            logger.info("Telegram bot token loaded from user_config (DB)")

        if not token:
            logger.critical("TELEGRAM_BOT_TOKEN is not set (no env value and no DB row)")
            raise SystemExit(1)

        if cfg.telegram_chat_id is None:
            logger.info(
                "TELEGRAM_CHAT_ID is not set — bot will use DB pairing flow for outbound messages"
            )

        if not cfg.jarvis_api_key:
            logger.warning("JARVIS_API_KEY not set — all API calls will be unauthenticated")

        # Return a new instance with the resolved database_url + effective token.
        return cfg.model_copy(update={"database_url": resolved_url, "telegram_token": token})


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
