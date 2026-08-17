"""Typed runtime configuration for the Platform API."""

from __future__ import annotations

import ipaddress
from functools import cached_property, lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GatewayNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class PlatformSettings(BaseSettings):
    """Configuration for identity signing and trusted gateway access.

    Environment variables use the ``JARVIS_`` prefix. The private key path is
    intentionally a file reference so key material never enters the process
    environment.

    Parameters
    ----------
    identity_issuer : str
        Stable issuer expected by Research and Learning.
    identity_private_key_file : Path
        PEM secret containing the Platform-only Ed25519 private key.
    gateway_auth_allowed_cidrs : str
        Comma-separated transport-peer networks allowed to request assertions.
    telegram_service_token_file : Path
        Docker-secret path for Telegram's scoped service credential.
    research_service_token_file : Path
        Docker-secret path for Research's scoped service credential.
    learning_service_token_file : Path
        Docker-secret path for Learning's scoped service credential.
    qdrant_url : str
        Internal Qdrant origin used by the setup dependency probe.
    ollama_base_url : str
        Internal Ollama origin used by the setup dependency probe.
    litellm_base_url : str
        Internal LiteLLM origin used by the setup dependency probe.
    research_api_url : str
        Internal Research API origin used for owner-local setup diagnostics.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="JARVIS_",
        extra="ignore",
        case_sensitive=False,
    )

    identity_issuer: str = "jarvis-platform"
    identity_private_key_file: Path = Path("/run/secrets/platform_identity_private_key")
    gateway_auth_allowed_cidrs: str = "127.0.0.0/8,::1/128"
    telegram_service_token_file: Path = Path("/run/secrets/telegram_service_token")
    research_service_token_file: Path = Path("/run/secrets/research_service_token")
    learning_service_token_file: Path = Path("/run/secrets/learning_service_token")
    app_base_url: str | None = Field(default=None, validation_alias="APP_BASE_URL")
    qdrant_url: str = Field(default="http://qdrant:6333", validation_alias="QDRANT_URL")
    ollama_base_url: str = Field(
        default="http://ollama:11434",
        validation_alias="OLLAMA_BASE_URL",
    )
    litellm_base_url: str = Field(
        default="http://litellm:4000",
        validation_alias="LITELLM_BASE_URL",
    )
    research_api_url: str = Field(
        default="http://paper_ingestion:8000",
        validation_alias="PAPER_INGESTION_URL",
    )
    learning_api_url: str = Field(
        default="http://learning_engine:8001",
        validation_alias="LEARNING_ENGINE_URL",
    )

    @field_validator("identity_issuer")
    @classmethod
    def _validate_identity_issuer(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized != value:
            raise ValueError("identity issuer must be non-empty and have no surrounding whitespace")
        return normalized

    @field_validator(
        "qdrant_url",
        "ollama_base_url",
        "litellm_base_url",
        "research_api_url",
        "learning_api_url",
    )
    @classmethod
    def _validate_service_origin(cls, value: str) -> str:
        """Require a credential-free internal HTTP or HTTPS origin.

        Parameters
        ----------
        value : str
            Configured service origin.

        Returns
        -------
        str
            Normalized origin without a trailing slash.

        Raises
        ------
        ValueError
            If the value is not an absolute credential-free HTTP(S) origin.
        """
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("service origins must be absolute HTTP(S) URLs")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("service origins must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("service origins must not contain query or fragment text")
        return value.rstrip("/")

    @cached_property
    def gateway_auth_networks(self) -> tuple[GatewayNetwork, ...]:
        """Return validated gateway transport-peer networks.

        Returns
        -------
        tuple[IPv4Network or IPv6Network, ...]
            Non-empty set of exact configured networks.

        Raises
        ------
        ValueError
            If a configured CIDR is malformed or the list is empty.
        """
        parts = tuple(part.strip() for part in self.gateway_auth_allowed_cidrs.split(","))
        if not parts or any(not part for part in parts):
            raise ValueError("gateway auth CIDRs must be a non-empty comma-separated list")
        return tuple(ipaddress.ip_network(part, strict=False) for part in parts)


@lru_cache(maxsize=1)
def get_platform_settings() -> PlatformSettings:
    """Return the process-wide Platform configuration snapshot.

    Returns
    -------
    PlatformSettings
        Cached settings read from the process environment.
    """
    settings = PlatformSettings()
    settings.gateway_auth_networks
    return settings


__all__ = ["GatewayNetwork", "PlatformSettings", "get_platform_settings"]
