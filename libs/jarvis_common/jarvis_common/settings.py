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
* ``DEV_MODE``, ``ENVIRONMENT``, and ``OWNER_OVERRIDE_ALLOWED_CIDRS`` now flow
  through :class:`CoreSettings`.  ``JARVIS_API_KEY`` flows through
  :class:`SecretsSettings` (cached in ``auth.py`` via ``_CACHED_API_KEY`` +
  ``refresh_api_key_cache()`` so the per-request path never re-reads env).
* Risky-to-cache values (e.g. ``JARVIS_ENABLE_TEST_JOBS`` which tests flip at
  runtime) are **not** wrapped in the lru_cache'd factory — the call site
  builds a fresh ``JobsSettings()`` per invocation so runtime env changes are
  honoured.

"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "CoreSettings",
    "JobsSettings",
    "RerankerSettings",
    "SecretsSettings",
    "TelegramSettings",
    "get_core_settings",
    "get_reranker_settings",
    "get_secrets_settings",
    "get_telegram_settings",
    "get_jobs_settings",
]


def _resolve_env_file_indirection(values: Any, fields: Any) -> Any:
    """Resolve ``<FIELD>_FILE`` env-var indirection for any settings class.

    For each field name in *fields*, checks ``os.environ`` for a corresponding
    ``<FIELD_NAME_UPPER>_FILE`` variable.  When found, reads and strips that
    file's content and injects it into *values* under the field name.  An empty
    file resolves to ``None`` so that ``Optional[SecretStr]`` fields remain
    unset rather than receiving an empty string.

    Raised:
        RuntimeError: when the nominated file cannot be opened.
    """
    if not isinstance(values, dict):
        return values
    for field_name in fields:
        env_name = field_name.upper()
        file_var = os.environ.get(f"{env_name}_FILE", "")
        if file_var:
            try:
                # An empty secret file must resolve to None, not "",
                # so downstream Optional[SecretStr] fields stay unset.
                values[field_name] = Path(file_var).read_text().strip() or None
            except OSError as exc:
                raise RuntimeError(f"Failed to read secret from {file_var!r}") from exc
    return values


# Read from the real process environment only — no .env files. Services are
# containerised and receive config through docker-compose env blocks.
_COMMON_CONFIG = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)


class CoreSettings(BaseSettings):
    """Process-wide core auth / logging / environment settings."""

    model_config = _COMMON_CONFIG

    dev_mode: bool = False
    # Granular dev flags — each defaults False and can be set independently.
    # When dev_mode=True and an individual flag was NOT explicitly set in the
    # environment, the model_validator below promotes it to True so that
    # DEV_MODE=true alone preserves v0.3 behaviour (all five effectively true).
    dev_auth_bypass: bool = False
    dev_error_detail: bool = False
    dev_cors_open: bool = False
    dev_smtp_log_only: bool = False
    dev_crypto_relaxed: bool = False
    log_level: str = "INFO"
    environment: str = "development"
    # Comma-separated list of trusted proxy hostnames for ProxyHeadersMiddleware.
    # Parsed at the use site — pydantic-settings would otherwise try to JSON-decode
    # a list-typed field and reject plain "dashboard,foo" strings.
    trusted_proxy_hosts: str = "dashboard"
    # Comma-separated CIDRs allowed to use X-Owner-User-Id override.
    # Deny-by-default: loopback only. The compose deploy injects the docker
    # bridge range (OWNER_OVERRIDE_ALLOWED_CIDRS tracks JARVIS_NET_SUBNET), so a
    # containerized bot is trusted there; any other non-loopback caller must
    # widen this explicitly.
    owner_override_allowed_cidrs: str = "127.0.0.0/8"
    # Comma-separated CIDRs allowed to POST to the infra-ingest endpoint.
    # Default-deny: empty means infra-ingest is unprovisioned and _check_auth
    # returns 503. Operator must explicitly opt in by setting the env var,
    # e.g. INFRA_INGEST_ALLOWED_CIDRS="127.0.0.1/8,::1/128".
    infra_ingest_allowed_cidrs: str = ""
    # Explicit owner user for the API-key→session mint.
    # When set, that user id is bound to the minted session. When unset, the
    # endpoint resolves the lowest-id non-deleted admin user. Never synthesises
    # or auto-creates a user.
    owner_user_id: int | None = None
    # Single-tenant gate opt-in. The endpoint mints only
    # when exactly one non-deleted user exists OR this flag is explicitly true
    # (operator opt-in for a small multi-user-but-single-owner deployment).
    api_key_login_enabled: bool = False
    # Written by setup.sh into .env. Drives the first-run wizard SMTP step.
    jarvis_setup_mode: Literal["single", "multi"] = "single"
    # SSRF guard escape-hatch: set ALLOW_PRIVATE_SMTP_HOST=true only when the
    # relay is an internal/corporate host on a known-trusted network.
    allow_private_smtp_host: bool = False
    llm_smart_num_ctx: int = 8192
    """Default/boot context window for the `smart` alias (matches the LiteLLM
    bootstrap params). Prompt budgets prefer the delivered system row
    (``llm.smart_num_ctx``, written when the Settings slider delivery
    succeeds) via :func:`jarvis_common.effective_num_ctx`; this env value is
    the fallback — and the operative value on vLLM stacks, where it must
    match the compose ``--max-model-len``."""

    llm_fast_num_ctx: int = 4096
    """Default/boot context window for the `fast` alias (matches the LiteLLM
    bootstrap params). Same row-then-fallback resolution as
    ``llm_smart_num_ctx`` — see :func:`jarvis_common.effective_num_ctx`."""

    @model_validator(mode="before")
    @classmethod
    def _resolve_file_indirection(cls, values):
        return _resolve_env_file_indirection(values, cls.model_fields)

    @model_validator(mode="after")
    def _validate_proxy_trust(self) -> CoreSettings:
        """Warn + ignore a literal ``"*"`` in ``trusted_proxy_hosts`` outside dev mode.

        Trusting all proxies in production is a security risk (IP spoofing via
        X-Forwarded-For). In dev mode it is a common convenience; outside dev
        a warning is emitted and the value is reset to the safe default.
        """
        import logging as _logging  # noqa: PLC0415

        _logger = _logging.getLogger(__name__)
        if self.trusted_proxy_hosts.strip() == "*" and not self.dev_mode:
            _logger.warning(
                "TRUSTED_PROXY_HOSTS='*' is not allowed outside dev mode "
                "(IP-spoofing risk); resetting to default 'dashboard'. "
                "Set DEV_MODE=true or list specific hostnames."
            )
            object.__setattr__(self, "trusted_proxy_hosts", "dashboard")
        return self

    @model_validator(mode="after")
    def _promote_dev_flags(self) -> CoreSettings:
        """When dev_mode=True, promote any granular flag that was NOT explicitly
        set in the environment to True.  An explicit env var always wins.
        """
        if not self.dev_mode:
            return self
        _flag_env_names = {
            "dev_auth_bypass": "DEV_AUTH_BYPASS",
            "dev_error_detail": "DEV_ERROR_DETAIL",
            "dev_cors_open": "DEV_CORS_OPEN",
            "dev_smtp_log_only": "DEV_SMTP_LOG_ONLY",
            "dev_crypto_relaxed": "DEV_CRYPTO_RELAXED",
        }
        env_keys_upper = {k.upper() for k in os.environ}
        for field_name, env_name in _flag_env_names.items():
            if env_name.upper() not in env_keys_upper:
                object.__setattr__(self, field_name, True)
        return self

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


class SecretsSettings(BaseSettings):
    """Typed access to all secrets, honouring the ``<NAME>_FILE`` convention.

    A ``model_validator(mode="before")`` resolves ``<FIELD>_FILE`` env vars by
    reading & stripping the file, then handing the value to the standard
    pydantic-settings env loader. Replaces the deleted ``jarvis_common.secrets`` module.
    """

    model_config = _COMMON_CONFIG

    jarvis_api_key: SecretStr | None = None
    jarvis_model_hmac_key: SecretStr | None = None
    jarvis_config_key: SecretStr | None = None
    jarvis_config_key_old: SecretStr | None = None
    jarvis_setup_token: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    litellm_master_key: SecretStr | None = None
    smtp_host: SecretStr | None = None
    smtp_port: SecretStr | None = None
    smtp_user: SecretStr | None = None
    smtp_pass: SecretStr | None = None
    smtp_from: SecretStr | None = None
    smtp_reply_to: SecretStr | None = None
    smtp_from_name: SecretStr | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_file_indirection(cls, values):
        return _resolve_env_file_indirection(values, cls.model_fields)

    @field_validator(
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_pass",
        "smtp_from",
        "smtp_reply_to",
        "smtp_from_name",
        mode="before",
    )
    @classmethod
    def _reject_empty_smtp_secret(cls, value):
        if value == "":
            raise ValueError("SMTP secret values must be unset or non-empty")
        return value


@lru_cache(maxsize=1)
def get_secrets_settings() -> SecretsSettings:
    """Return a cached ``SecretsSettings`` instance.

    Cached for process lifetime — call ``get_secrets_settings.cache_clear()``
    in tests after mutating secret-related env vars.
    """
    return SecretsSettings()
