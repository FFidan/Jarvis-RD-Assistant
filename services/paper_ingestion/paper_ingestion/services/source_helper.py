"""Source plugin helper — resolves a PaperSource instance for a given type."""

import asyncpg
import httpx
from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Request
from jarvis_common.crypto import decrypt_secret
from pydantic import ValidationError

from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.sources.base import PaperSource
from paper_ingestion.sources.registry import get_source_class

_SOURCE_BOOTSTRAP_EXCEPTIONS = (TypeError, ValueError, ValidationError)


def _decrypt_config_api_key(config: dict) -> dict:
    """Return *config* with api_key decrypted (Fernet ciphertext → plaintext).

    Handles legacy plaintext values transparently: if decrypt_secret raises
    InvalidToken the raw value is kept as-is so pre-migration rows continue to
    work without a data migration.
    """
    raw = config.get("api_key")
    if not raw:
        return config
    try:
        plaintext = decrypt_secret(raw)
    except (InvalidToken, RuntimeError):
        # Legacy plaintext row (InvalidToken) or JARVIS_CONFIG_KEY not configured
        # (RuntimeError) — keep the raw value so pre-migration rows keep working.
        return config
    return {**config, "api_key": plaintext}


async def get_source_for_type(
    source_type: SourceType,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    request: "Request | None" = None,
) -> PaperSource:
    """Return a PaperSource for the given type.

    C-8: Returns the pre-initialized singleton from paper_ingestion.state.sources when
    available so the per-instance rate limiter persists across requests.
    Falls back to creating a new instance (e.g. for sources not pre-loaded).

    Note: Cached singletons preserve rate limiter state across requests.
    Config fields (api_key, etc.) are read at startup -- changes via Settings
    UI require a service restart to take effect.
    """
    # Return cached singleton if available
    if request is not None:
        cached = getattr(request.app.state, "sources", {}).get(source_type.value)
        if cached is not None:
            # Still validate enabled status from DB before returning
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT enabled FROM paper_sources WHERE source_type = $1",
                    source_type.value,
                )
            if not row:
                raise HTTPException(
                    status_code=404, detail=f"Source '{source_type}' not in database"
                )
            if not row["enabled"]:
                raise HTTPException(status_code=400, detail=f"Source '{source_type}' is disabled")
            return cached

    # Fallback: create on demand (for sources not pre-initialized)
    source_cls = get_source_class(source_type.value)
    if not source_cls:
        raise HTTPException(status_code=404, detail=f"Source type '{source_type}' not registered")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, source_type, enabled, config FROM paper_sources WHERE source_type = $1",
            source_type.value,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"Source '{source_type}' not in database")
    if not row["enabled"]:
        raise HTTPException(status_code=400, detail=f"Source '{source_type}' is disabled")

    config = PaperSourceConfig(
        id=row["id"],
        source_type=row["source_type"],
        enabled=row["enabled"],
        config=_decrypt_config_api_key(row["config"] or {}),
    )
    return source_cls(config, http_client)


async def get_sources_for_types(
    source_types: list[SourceType],
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    request: "Request | None" = None,
) -> tuple[dict[SourceType, object], dict[SourceType, Exception]]:
    """Return PaperSource instances for several source types with one DB read.

    Cached singleton sources are still reused when available; the batched query
    only validates database presence/enabled state and provides fallback config.
    """
    if not source_types:
        return {}, {}

    unique_types = list(dict.fromkeys(source_types))
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, source_type, enabled, config"
            " FROM paper_sources WHERE source_type = ANY($1)",
            [source_type.value for source_type in unique_types],
        )
    rows_by_type = {row["source_type"]: row for row in rows}
    cached_sources = getattr(getattr(request, "app", None), "state", None)
    cached_map = getattr(cached_sources, "sources", {}) if cached_sources is not None else {}

    plugins: dict[SourceType, object] = {}
    errors: dict[SourceType, Exception] = {}
    for source_type in unique_types:
        row = rows_by_type.get(source_type.value)
        if not row:
            errors[source_type] = HTTPException(
                status_code=404, detail=f"Source '{source_type}' not in database"
            )
            continue
        if not row["enabled"]:
            errors[source_type] = HTTPException(
                status_code=400, detail=f"Source '{source_type}' is disabled"
            )
            continue

        cached = cached_map.get(source_type.value)
        if cached is not None:
            plugins[source_type] = cached
            continue

        source_cls = get_source_class(source_type.value)
        if not source_cls:
            errors[source_type] = HTTPException(
                status_code=404, detail=f"Source type '{source_type}' not registered"
            )
            continue
        try:
            config = PaperSourceConfig(
                id=row["id"],
                source_type=row["source_type"],
                enabled=row["enabled"],
                config=_decrypt_config_api_key(row["config"] or {}),
            )
            plugins[source_type] = source_cls(config, http_client)
        except _SOURCE_BOOTSTRAP_EXCEPTIONS as exc:
            errors[source_type] = exc

    return plugins, errors
