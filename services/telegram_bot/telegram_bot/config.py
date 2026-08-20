"""Typed configuration for the database-free Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

from jarvis_common.config import JarvisCommonSettings
from jarvis_common.pinned_transport import JARVIS_SERVICE_POLICY, pinned_async_client
from jarvis_common.secrets_files import read_secret_with_file_fallback
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

logger = logging.getLogger(__name__)


class BotConfig(JarvisCommonSettings):
    """Telegram runtime configuration without database credentials.

    Parameters
    ----------
    telegram_token : SecretStr
        Telegram Bot API token, resolved from Platform first and the mounted
        bootstrap secret second.
    telegram_service_token : SecretStr
        Dedicated credential accepted only by Platform's Telegram boundary.
    platform_api_url : str
        Internal Platform API origin.
    paper_ingestion_url : str
        Internal Research API origin.
    learning_engine_url : str
        Internal Learning API origin.
    jarvis_base_url : str or None
        Optional public dashboard origin used in Telegram deep links.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    telegram_token: SecretStr = Field(
        default=SecretStr(""),
        alias="TELEGRAM_BOT_TOKEN",
        description="Telegram Bot API token.",
    )
    telegram_service_token: SecretStr = Field(
        default=SecretStr(""),
        alias="JARVIS_TELEGRAM_SERVICE_TOKEN",
        description="Dedicated Telegram-to-Platform service credential.",
    )
    platform_api_url: str = Field(
        default="http://platform_api:8003",
        description="Platform API service URL.",
    )
    paper_ingestion_url: str = Field(
        default="http://paper_ingestion:8000",
        description="Research API service URL.",
    )
    learning_engine_url: str = Field(
        default="http://learning_engine:8001",
        description="Learning API service URL.",
    )
    jarvis_base_url: str | None = Field(
        default=None,
        alias="JARVIS_BASE_URL",
        description="Optional public dashboard base URL.",
    )

    @field_validator(
        "platform_api_url",
        "paper_ingestion_url",
        "learning_engine_url",
        "jarvis_base_url",
        mode="after",
    )
    @classmethod
    def _validate_http_url(cls, value: str | None) -> str | None:
        """Require a credential-free HTTP or HTTPS origin.

        Parameters
        ----------
        value : str or None
            Configured service or public URL.

        Returns
        -------
        str or None
            Normalized URL without a trailing slash.

        Raises
        ------
        ValueError
            If the URL is not an absolute credential-free HTTP(S) URL.
        """
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("service URLs must be absolute http:// or https:// URLs")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("service URLs must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("service URLs must not contain query or fragment text")
        return value.rstrip("/")

    @classmethod
    def from_env(cls) -> BotConfig:
        """Build the bot configuration from environment and secret files.

        Platform is queried for a wizard-saved bot token. The mounted Telegram
        token remains a bootstrap and outage fallback. PostgreSQL and the
        configuration-encryption key are intentionally not read.

        Returns
        -------
        BotConfig
            Validated runtime configuration.

        Raises
        ------
        SystemExit
            If the dedicated service credential or Telegram token is missing.
        """
        config = cls()
        service_token = read_secret_with_file_fallback(
            config.telegram_service_token.get_secret_value() or None,
            os.environ.get("JARVIS_TELEGRAM_SERVICE_TOKEN_FILE", ""),
        )
        if not service_token:
            logger.critical("JARVIS_TELEGRAM_SERVICE_TOKEN is not configured")
            raise SystemExit(1)

        fallback_token = read_secret_with_file_fallback(
            config.telegram_token.get_secret_value() or None,
            os.environ.get("TELEGRAM_BOT_TOKEN_FILE", ""),
        )
        platform_token = asyncio.run(
            _read_platform_bot_token(
                config.platform_api_url,
                service_token,
            )
        )
        token = platform_token or fallback_token
        if not token:
            logger.critical("TELEGRAM_BOT_TOKEN is not configured in Platform or its secret file")
            raise SystemExit(1)
        return config.model_copy(
            update={
                "telegram_service_token": SecretStr(service_token),
                "telegram_token": SecretStr(token),
            }
        )


async def _read_platform_bot_token(platform_url: str, service_token: str) -> str | None:
    """Return Platform's bot token or ``None`` when unavailable.

    Parameters
    ----------
    platform_url : str
        Fixed internal Platform origin.
    service_token : str
        Dedicated Telegram service credential.

    Returns
    -------
    str or None
        Decrypted bot token, or ``None`` on absence or Platform outage.
    """
    try:
        async with pinned_async_client(
            JARVIS_SERVICE_POLICY,
            timeout=5.0,
            headers=_service_headers(service_token),
        ) as client:
            response = await client.get(f"{platform_url}/internal/telegram/config")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            token = response.json().get("bot_token")
            return token if isinstance(token, str) and token else None
    except Exception:  # noqa: BLE001 - mounted secret is the intentional fallback
        logger.warning(
            "Platform Telegram token lookup failed; using mounted fallback", exc_info=True
        )
        return None


def service_headers(config: BotConfig) -> dict[str, str]:
    """Return Telegram's dedicated Platform authentication headers.

    Parameters
    ----------
    config : BotConfig
        Runtime configuration containing the service credential.

    Returns
    -------
    dict[str, str]
        Headers for the scoped Platform boundary.
    """
    return _service_headers(config.telegram_service_token.get_secret_value())


def _service_headers(token: str) -> dict[str, str]:
    return {
        "X-Jarvis-Service-Principal": "telegram",
        "X-Jarvis-Service-Token": token,
    }


def _owner_headers(config: BotConfig, user_id: int | None) -> dict[str, str]:
    """Return the local user-context marker consumed by Telegram's HTTP auth.

    Parameters
    ----------
    config : BotConfig
        Retained for stable call sites; no general API key is read.
    user_id : int or None
        Paired JARVIS user identifier.

    Returns
    -------
    dict[str, str]
        Internal client marker. :class:`TelegramBackendAuth` removes it before
        transport and replaces it with a signed Platform assertion.
    """
    del config
    return {"X-Jarvis-Paired-User-Id": str(user_id)} if user_id is not None else {}


__all__ = ["BotConfig", "service_headers"]
