"""Typed pydantic-settings configuration for JARVIS infrastructure env vars.

Bucket H — consolidates the ~52 ``os.getenv`` call sites into typed
Settings classes per service. Each class is a **1:1 mapping** of existing env
vars — no renames, no drops.  Awkward names are preserved and documented with
a follow-up comment.

Architecture
------------
* ``JarvisCommonSettings`` — shared infra keys: database pool, CORS, proxy,
  LiteLLM, Langfuse, core paths. Extends nothing; used as a base class by
  both service settings classes.
* ``PaperIngestionSettings`` (in ``paper_ingestion.config``) — PI-specific:
  Qdrant, Ollama, embedding, storage paths, scheduler interval, multi-tenant
  flag, BBT.
* ``LearningEngineSettings`` (in ``learning_engine.config``) — LE-specific:
  snapshot path, multi-tenant flag, scheduler tune-ables.

Factory pattern
---------------
All classes expose a ``get_<class_name_snake>()`` factory following the same
uncached-by-default convention as ``jarvis_common.settings``.  Tests can set
env vars before constructing instances; no ``lru_cache`` wrapping is used so
``monkeypatch.setenv`` takes effect immediately.

1:1 env-var table (shared infra layer)
---------------------------------------
Env var                         Field                       Service layer
---                             ---                         ---
DATABASE_URL                    database_url                shared
POSTGRES_USER                   postgres_user               shared
POSTGRES_DB                     postgres_db                 shared
DB_POOL_MIN                     db_pool_min                 shared
DB_POOL_MAX                     db_pool_max                 shared
CORS_ORIGINS                    cors_origins                shared
LITELLM_BASE_URL                litellm_base_url            shared
OBSERVABILITY_ENABLED           observability_enabled       shared
LANGFUSE_HOST                   langfuse_host               shared
TRUSTED_PROXY_CIDRS             trusted_proxy_cidrs         shared
JARVIS_TRUST_CF_CONNECTING_IP   trust_cf_connecting_ip      shared
JARVIS_MIGRATION_LOCK_CONTENDED_OK  migration_lock_contended_ok  shared
JARVIS_IDENTITY_ASSERTIONS_REQUIRED identity_assertions_required  shared
JARVIS_IDENTITY_ISSUER          identity_issuer             shared
JARVIS_IDENTITY_CURRENT_PUBLIC_KEY_FILE identity_current_public_key_file shared
JARVIS_IDENTITY_PREVIOUS_PUBLIC_KEY_FILE identity_previous_public_key_file shared
JARVIS_IDENTITY_PREVIOUS_KEY_ACCEPT_UNTIL identity_previous_key_accept_until shared
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "POSTGRES_PASSWORD_SECRET_PATH",
    "JarvisCommonSettings",
    "get_jarvis_common_settings",
]

#: Docker Secret mount path for the PostgreSQL password.  Preferred over the
#: ``DATABASE_URL`` env var because it avoids leaking the password via
#: ``/proc/<pid>/environ`` or ``docker inspect``.  Shared by app_factory (DSN
#: construction) and auth (production secret-strength gate).
POSTGRES_PASSWORD_SECRET_PATH = "/run/secrets/postgres_password"

# Read from real process env only — services run in Docker and receive config
# via docker-compose env blocks.  No .env file loading.
_CONFIG = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)


class JarvisCommonSettings(BaseSettings):
    """Typed settings for shared infrastructure env vars.

    Covers database connectivity, connection pool sizing, CORS, LiteLLM proxy,
    Langfuse observability, trusted-proxy CIDRs, and migration behaviour.
    These fields are consumed in ``jarvis_common.app_factory``,
    ``jarvis_common.llm_client``, ``jarvis_common.http_rate_limiter``, and
    ``jarvis_common.migrations``.

    Notes
    -----
    * ``postgres_user`` and ``postgres_password_file`` are the explicit runtime
      credential settings. ``database_url`` remains a direct test/local fallback.
    * ``db_pool_min`` / ``db_pool_max`` are ``int | None`` because
      ``app_factory._resolve_db_pool_kwargs`` applies them only when set.
    * ``cors_origins`` preserves the comma-separated string form; use
      ``cors_origins_list`` to split.
    * Langfuse keypair (``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``) live
      on :class:`jarvis_common.settings.SecretsSettings` where they gain
      ``_FILE`` indirection support.  Only ``OBSERVABILITY_ENABLED`` (bool gate)
      and ``LANGFUSE_HOST`` (plain URL) are stored here.

    """

    model_config = _CONFIG

    # --- Database -------------------------------------------------------
    database_url: str = Field(
        default="",
        description=(
            "Full PostgreSQL DSN.  Fallback used when the Docker Secret "
            "/run/secrets/postgres_password is absent (tests, local dev)."
        ),
    )
    postgres_user: str = Field(
        default="",
        description="PostgreSQL runtime login role (POSTGRES_USER).",
    )
    postgres_password_file: Path | None = Field(
        default=None,
        alias="POSTGRES_PASSWORD_FILE",
        description="Mounted PostgreSQL runtime password file.",
    )
    postgres_host: str = Field(
        default="postgres",
        description="PostgreSQL host (POSTGRES_HOST).",
    )
    postgres_port: int = Field(
        default=5432,
        description="PostgreSQL TCP port (POSTGRES_PORT).",
    )
    postgres_db: str = Field(
        default="jarvis",
        description="PostgreSQL database name; matches docker-compose.yml default.",
    )

    # --- Connection pool -------------------------------------------------
    db_pool_min: int | None = Field(
        default=None,
        description=(
            "asyncpg pool minimum connections (DB_POOL_MIN).  None = use app_factory default of 2."
        ),
    )
    db_pool_max: int | None = Field(
        default=None,
        description=(
            "asyncpg pool maximum connections (DB_POOL_MAX).  None = use app_factory default of 10."
        ),
    )

    # --- CORS -----------------------------------------------------------
    cors_origins: str = Field(
        default="https://localhost:3001",
        description=(
            "Comma-separated list of allowed CORS origins.  Split with "
            "``cors_origins_list`` property."
        ),
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Return CORS origins split on commas, whitespace-stripped, empties removed."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # --- LiteLLM proxy --------------------------------------------------
    litellm_base_url: str = Field(
        default="http://litellm:4000",
        description="Base URL for the LiteLLM proxy (LITELLM_BASE_URL).",
    )

    # --- Langfuse observability -----------------------------------------
    observability_enabled: bool = Field(
        default=False,
        description=(
            "Master gate for Langfuse tracing (OBSERVABILITY_ENABLED).  When "
            "false, the Langfuse SDK is not initialised even if host+keys are "
            "present.  Set true only when the observability stack "
            "(``--profile observability``) is running."
        ),
    )
    langfuse_host: str | None = Field(
        default=None,
        description=(
            "Langfuse server URL (LANGFUSE_HOST).  When absent, the Langfuse "
            "SDK and Instructor patching are skipped."
        ),
    )
    otel_exporter_otlp_traces_endpoint: str | None = Field(
        default=None,
        description="Optional OTLP trace endpoint (OTEL_EXPORTER_OTLP_TRACES_ENDPOINT).",
    )
    otel_export_timeout_ms: int = Field(
        default=5_000,
        gt=0,
        le=60_000,
        description="Bounded telemetry export timeout in milliseconds (OTEL_EXPORT_TIMEOUT_MS).",
    )
    log_forward_address: str | None = Field(
        default=None,
        description="Optional Vector UDP destination as host:port (LOG_FORWARD_ADDRESS).",
    )

    @field_validator("otel_exporter_otlp_traces_endpoint", "log_forward_address", mode="before")
    @classmethod
    def _empty_observability_address_is_none(cls, value: object) -> object:
        """Normalize optional observability addresses without accepting whitespace."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("log_forward_address")
    @classmethod
    def _validate_log_forward_address(cls, value: str | None) -> str | None:
        """Require a bounded UDP destination in ``host:port`` form."""
        if value is None:
            return None
        host, separator, port_text = value.rpartition(":")
        if not separator or not host or not port_text.isdecimal():
            raise ValueError("LOG_FORWARD_ADDRESS must use host:port form")
        port = int(port_text)
        if not 1 <= port <= 65_535:
            raise ValueError("LOG_FORWARD_ADDRESS port must be between 1 and 65535")
        return value

    # --- Trusted-proxy CIDRs --------------------------------------------
    trusted_proxy_cidrs: str = Field(
        default="",
        description=(
            "Comma-separated list of trusted proxy CIDRs (TRUSTED_PROXY_CIDRS). "
            "When non-empty, this REPLACES the built-in default (loopback only) "
            "inside ``http_rate_limiter``; when empty, the default is used."
        ),
    )

    @property
    def trusted_proxy_cidrs_list(self) -> list[str]:
        """Return additional proxy CIDRs split on commas, empties removed."""
        return [c.strip() for c in self.trusted_proxy_cidrs.split(",") if c.strip()]

    # --- Cloudflare IP passthrough --------------------------------------
    # Follow-up: consider renaming to TRUST_CF_CONNECTING_IP (drop JARVIS_ prefix).
    jarvis_trust_cf_connecting_ip: bool = Field(
        default=False,
        alias="JARVIS_TRUST_CF_CONNECTING_IP",
        description=(
            "When true, treat CF-Connecting-IP as the canonical real IP.  "
            "Only enable behind Cloudflare."
        ),
    )

    @property
    def trust_cf_connecting_ip(self) -> bool:
        """Alias for ``jarvis_trust_cf_connecting_ip`` for cleaner call-site reads."""
        return self.jarvis_trust_cf_connecting_ip

    # --- Migration behaviour --------------------------------------------
    jarvis_migration_lock_contended_ok: bool = Field(
        default=False,
        alias="JARVIS_MIGRATION_LOCK_CONTENDED_OK",
        description=(
            "When true, treat advisory-lock contention on migration runs as "
            "non-fatal (another instance already holds the lock, i.e. is "
            "already migrating).  Safe for multi-replica deploys."
        ),
    )

    @property
    def migration_lock_contended_ok(self) -> bool:
        """Alias for ``jarvis_migration_lock_contended_ok`` for cleaner reads."""
        return self.jarvis_migration_lock_contended_ok

    # --- Internal identity assertions -----------------------------------
    identity_assertions_required: bool = Field(
        default=True,
        alias="JARVIS_IDENTITY_ASSERTIONS_REQUIRED",
        description=(
            "Require Platform-signed identity assertions on protected Research "
            "and Learning routes. Disable only in isolated test harnesses."
        ),
    )
    identity_issuer: str = Field(
        default="jarvis-platform",
        alias="JARVIS_IDENTITY_ISSUER",
        min_length=1,
        description="Exact issuer accepted for internal identity assertions.",
    )
    identity_current_public_key_file: Path = Field(
        default=Path("/run/secrets/platform_identity_public_key"),
        alias="JARVIS_IDENTITY_CURRENT_PUBLIC_KEY_FILE",
        description="Mounted current Ed25519 public-key file.",
    )
    identity_previous_public_key_file: Path | None = Field(
        default=None,
        alias="JARVIS_IDENTITY_PREVIOUS_PUBLIC_KEY_FILE",
        description="Optional mounted previous Ed25519 public-key file during rotation.",
    )
    identity_previous_key_accept_until: datetime | None = Field(
        default=None,
        alias="JARVIS_IDENTITY_PREVIOUS_KEY_ACCEPT_UNTIL",
        description="Timezone-aware overlap deadline for the optional previous key.",
    )

    @property
    def identity_public_key_files(self) -> tuple[Path, ...]:
        """Return the current and optional previous identity public-key paths.

        Returns
        -------
        tuple[Path, ...]
            The current key path first, followed by the previous key path when
            one is configured.
        """
        if self.identity_previous_public_key_file is None:
            return (self.identity_current_public_key_file,)
        return (
            self.identity_current_public_key_file,
            self.identity_previous_public_key_file,
        )


def get_jarvis_common_settings() -> JarvisCommonSettings:
    """Return a fresh ``JarvisCommonSettings`` snapshot.

    Intentionally uncached so that tests can ``monkeypatch.setenv`` values.
    """
    return JarvisCommonSettings()
