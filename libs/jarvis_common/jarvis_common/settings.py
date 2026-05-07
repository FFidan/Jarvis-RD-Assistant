"""Typed environment-settings classes shared across JARVIS services.

Consolidates the unambiguously shared env vars (core auth/log config, jobs
test toggle, reranker gate, telegram URL) into ``pydantic-settings`` BaseSettings
classes. Each group exposes an ``lru_cache``-wrapped factory that mirrors the
``get_litellm_config()`` pattern in :mod:`jarvis_common.llm_client` so call
sites can cheaply retrieve a frozen snapshot.

Notes
-----
* Pydantic-settings reads env vars case-insensitively by default, so the
  snake_case field names match the SHOUTING_CASE env vars (e.g.
  ``dev_mode`` <-> ``DEV_MODE``).
* The ``auth.py`` module intentionally keeps reading ``JARVIS_API_KEY`` and
  ``DEV_MODE`` directly from ``os.environ`` — it owns a mutable cache plus a
  ``refresh_api_key_cache()`` hook that test monkeypatching depends on. Do
  not move those reads here.
* Risky-to-cache values (e.g. ``JARVIS_ENABLE_TEST_JOBS`` which tests flip at
  runtime) are **not** wrapped in the lru_cache'd factory — the call site
  builds a fresh ``JobsSettings()`` per invocation so runtime env changes are
  honoured.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "CoreSettings",
    "JobsSettings",
    "RerankerSettings",
    "TelegramSettings",
    "get_core_settings",
    "get_reranker_settings",
    "get_telegram_settings",
    "get_jobs_settings",
]


# Read from the real process environment only — no .env files. Services are
# containerised and receive config through docker-compose env blocks.
_COMMON_CONFIG = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)


class CoreSettings(BaseSettings):
    """Process-wide core auth / logging / environment settings."""

    model_config = _COMMON_CONFIG

    dev_mode: bool = False
    # Wrapped in SecretStr so accidental ``repr(settings)`` / structured-log
    # serialisations print ``SecretStr('**********')`` instead of the raw
    # value. Call sites must use ``.get_secret_value()`` to obtain the
    # plaintext for HTTP headers / Fernet keys.
    jarvis_api_key: SecretStr | None = None
    jarvis_config_key: SecretStr | None = None
    log_level: str = "INFO"
    environment: str = "development"
    # Comma-separated list of trusted proxy hostnames for ProxyHeadersMiddleware.
    # Parsed at the use site — pydantic-settings would otherwise try to JSON-decode
    # a list-typed field and reject plain "dashboard,foo" strings.
    trusted_proxy_hosts: str = "dashboard"

    @property
    def trusted_proxy_hosts_list(self) -> list[str]:
        """Return ``trusted_proxy_hosts`` split on commas, ignoring empties."""
        return [h.strip() for h in self.trusted_proxy_hosts.split(",") if h.strip()]


class RerankerSettings(BaseSettings):
    """Cross-encoder reranker gate."""

    model_config = _COMMON_CONFIG

    reranker_enabled: bool = False
    reranker_backend: Literal["cross-encoder", "qwen3"] = "cross-encoder"


class JobsSettings(BaseSettings):
    """Background-jobs test toggle.

    ``jarvis_enable_test_jobs`` is typed as ``str | None`` (not ``bool``) to
    preserve the existing ``== "1"`` semantics — any other value (including
    empty/unset) disables the noop.test kind.
    """

    model_config = _COMMON_CONFIG

    jarvis_enable_test_jobs: str | None = None

    @property
    def test_jobs_enabled(self) -> bool:
        """True when ``JARVIS_ENABLE_TEST_JOBS`` is exactly ``"1"``."""
        return self.jarvis_enable_test_jobs == "1"


class TelegramSettings(BaseSettings):
    """Telegram bot inter-service URL (used by settings router to push reloads)."""

    model_config = _COMMON_CONFIG

    telegram_bot_url: str = ""

    @property
    def url_or_none(self) -> str | None:
        """Return the stripped URL or ``None`` when unset."""
        stripped = self.telegram_bot_url.strip()
        return stripped or None


def get_core_settings() -> CoreSettings:
    """Return a fresh ``CoreSettings`` snapshot.

    Mirrors the ``get_litellm_config()`` factory style — intentionally uncached
    so that tests can ``monkeypatch.setenv`` values at runtime. Each call just
    re-reads the process environment via pydantic-settings; the cost is
    negligible compared to the surrounding I/O.
    """
    return CoreSettings()


def get_reranker_settings() -> RerankerSettings:
    """Return a fresh ``RerankerSettings`` snapshot."""
    return RerankerSettings()


def get_telegram_settings() -> TelegramSettings:
    """Return a fresh ``TelegramSettings`` snapshot."""
    return TelegramSettings()


def get_jobs_settings() -> JobsSettings:
    """Return a fresh ``JobsSettings`` instance.

    Integration tests flip ``JARVIS_ENABLE_TEST_JOBS`` at runtime to enable
    ``noop.test``; each call reads the current env so the toggle is honoured.
    """
    return JobsSettings()
