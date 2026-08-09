"""Model assignment validation and Telegram nudge reload side-effect."""

import logging
from collections.abc import Sequence
from typing import Any

import asyncpg
import httpx
from jarvis_common.maintenance import outbound_quarantine_active
from jarvis_common.pinned_transport import JARVIS_SERVICE_POLICY, pinned_async_client
from jarvis_common.settings import get_secrets_settings, get_telegram_settings

from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS
from paper_ingestion.services.llm_provider_registry import ProviderDefinition, provider_for_id
from paper_ingestion.services.model_lifecycle import (
    catalog_entry_for_model,
    normalize_model_tag,
    provider_access_blocker,
)
from paper_ingestion.services.provider_models import live_entry_for_model

__all__ = [
    "reload_telegram_nudges",
    "cloud_provider_key_present",
    "provider_access_configured",
    "validate_model_assignment",
]

logger = logging.getLogger(__name__)


async def reload_telegram_nudges() -> None:
    """Ask the Telegram service to reload nudges when outbound use is allowed.

    The best-effort hook returns without loading credentials while quarantine is
    active, when no Telegram service URL is configured, or after an HTTP failure.
    It never propagates a network error to the settings-write caller.
    """
    if outbound_quarantine_active():
        logger.info("skip Telegram nudge reload: outbound quarantine awaiting review")
        return

    telegram_url = get_telegram_settings().url_or_none
    if not telegram_url:
        logger.debug("TELEGRAM_BOT_URL empty — skipping nudge reload")
        return
    api_key_secret = get_secrets_settings().jarvis_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret is not None else ""
    try:
        if outbound_quarantine_active():
            logger.info("skip Telegram nudge reload: outbound quarantine awaiting review")
            return
        async with pinned_async_client(JARVIS_SERVICE_POLICY) as client:
            resp = await client.post(
                f"{telegram_url}/internal/reload-nudges",
                headers={"X-API-Key": api_key},
                timeout=2.0,
            )
            resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Telegram nudge-reload failed (non-fatal)", exc_info=True)


async def cloud_provider_key_present(provider: str, db_pool: asyncpg.Pool) -> bool:
    """Return True if an API key for *provider* is stored in user_config."""
    config_key = provider_for_id(provider).api_key_config_key
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config WHERE key = $1 AND user_id IS NULL",
            config_key,
        )
    return bool(
        row is not None and (row.get("encrypted_value") is not None or row.get("value") is not None)
    )


def _config_row_present(row: Any) -> bool:
    """A user_config row counts as present when either value column holds content.

    A row cleared to the empty string is absent, matching what the readers do:
    ``get_provider_base_url`` returns the default for a falsy value, so counting
    ``""`` as configured would let a model save and then never deliver.
    """
    return bool(row.get("encrypted_value")) or bool(row.get("value"))


async def provider_access_configured(
    providers: Sequence[ProviderDefinition], db_pool: asyncpg.Pool
) -> dict[str, bool]:
    """Return, per provider, whether this deployment can reach it at all.

    Access means a stored API key OR — for a provider that has one — a stored
    base URL, because a self-hosted endpoint can serve requests without a key.
    This is the single rule behind both the picker's presence map and the
    assignment save gate; splitting them is what let a model render enabled and
    then be rejected on save.
    """
    config_keys = [provider.api_key_config_key for provider in providers]
    config_keys += [
        provider.base_url_config_key
        for provider in providers
        if provider.base_url_config_key is not None
    ]
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT key, value, encrypted_value FROM user_config
               WHERE key = ANY($1::text[]) AND user_id IS NULL""",
            config_keys,
        )
    present = {str(row["key"]) for row in rows if _config_row_present(row)}
    return {
        provider.id: provider.api_key_config_key in present
        or (provider.base_url_config_key is not None and provider.base_url_config_key in present)
        for provider in providers
    }


async def _require_ollama_model_pulled(
    http_client: httpx.AsyncClient, ollama_url: str, entry: Any
) -> None:
    """Raise unless the local Ollama daemon already holds *entry*'s tag."""
    from fastapi import HTTPException  # noqa: PLC0415

    try:
        resp = await http_client.get(f"{ollama_url}/api/tags", timeout=10.0)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Could not verify installed Ollama models",
        ) from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=503, detail="Could not verify installed Ollama models")
    data = resp.json()
    installed_names = {
        normalize_model_tag(str(item.get("name", ""))) for item in data.get("models", [])
    }
    if normalize_model_tag(entry.ollama_tag or entry.id) not in installed_names:
        raise HTTPException(status_code=422, detail="Model not pulled. Pull it first.")


async def validate_model_assignment(
    *,
    http_client: httpx.AsyncClient,
    ollama_url: str,
    key: str,
    model_id: str,
    db_pool: asyncpg.Pool,
) -> None:
    """Reject model assignments that are not usable in this deployment."""
    from fastapi import HTTPException  # noqa: PLC0415

    role = ROLE_TO_ALIAS.get(key)
    if role is None:
        return
    entry = catalog_entry_for_model(model_id)
    if entry is None:
        # Not bundled: it may still be a model the provider itself lists.
        entry = await live_entry_for_model(model_id, db_pool=db_pool, http_client=http_client)
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail=f"Model {model_id!r} is not in the model catalog",
        )
    if not entry.assignable:
        raise HTTPException(
            status_code=422,
            detail=(
                entry.notes or "This model is tracked for evaluation but is not assignable yet."
            ),
        )
    if role not in entry.roles:
        raise HTTPException(
            status_code=400,
            detail=f"Model {model_id!r} cannot be assigned to the {role!r} role",
        )
    if entry.provider == "ollama":
        await _require_ollama_model_pulled(http_client, ollama_url, entry)
        return
    provider = provider_for_id(entry.provider)
    access = await provider_access_configured([provider], db_pool)
    if not access.get(provider.id, False):
        raise HTTPException(status_code=422, detail=provider_access_blocker(provider.id))
