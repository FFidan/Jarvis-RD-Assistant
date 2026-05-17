"""Typed pydantic-settings configuration for JARVIS infrastructure env vars.

Bucket H — Wave 4: consolidates the ~52 ``os.getenv`` call sites into typed
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
LANGFUSE_PUBLIC_KEY             langfuse_public_key         shared (SecretStr)
LANGFUSE_SECRET_KEY             langfuse_secret_key         shared (SecretStr)
TRUSTED_PROXY_CIDRS             trusted_proxy_cidrs         shared
JARVIS_TRUST_CF_CONNECTING_IP   trust_cf_connecting_ip      shared
JARVIS_MIGRATION_LOCK_CONTENDED_OK  migration_lock_contended_ok  shared
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "JarvisCommonSettings",
    "get_jarvis_common_settings",
]

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
    * ``postgres_user`` / ``postgres_db`` default to ``"jarvis"`` matching
      docker-compose.yml; ``database_url`` is the fallback for test/local dev.
    * ``db_pool_min`` / ``db_pool_max`` are ``int | None`` because
      ``app_factory._resolve_db_pool_kwargs`` applies them only when set.
    * ``cors_origins`` preserves the comma-separated string form; use
      ``cors_origins_list`` to split.
    * ``langfuse_public_key`` / ``langfuse_secret_key`` are ``SecretStr`` so
      they are masked in ``repr`` / structured-log serialisations.
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
        default="jarvis",
        description="PostgreSQL username; matches docker-compose.yml default.",
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
    langfuse_public_key: SecretStr | None = Field(
        default=None,
        description="Langfuse public API key (LANGFUSE_PUBLIC_KEY).",
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None,
        description="Langfuse secret API key (LANGFUSE_SECRET_KEY).",
    )

    # --- Trusted-proxy CIDRs --------------------------------------------
    trusted_proxy_cidrs: str = Field(
        default="",
        description=(
            "Comma-separated list of additional trusted proxy CIDRs "
            "(TRUSTED_PROXY_CIDRS).  Appended to the built-in default list "
            "inside ``http_rate_limiter``."
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


def get_jarvis_common_settings() -> JarvisCommonSettings:
    """Return a fresh ``JarvisCommonSettings`` snapshot.

    Intentionally uncached so that tests can ``monkeypatch.setenv`` values.
    """
    return JarvisCommonSettings()
