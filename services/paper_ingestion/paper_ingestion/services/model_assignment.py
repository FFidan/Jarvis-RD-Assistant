"""Model assignment validation and Telegram nudge reload side-effect."""

import logging

import asyncpg
import httpx
from jarvis_common.settings import get_secrets_settings, get_telegram_settings

from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS
from paper_ingestion.services.model_lifecycle import catalog_entry_for_model, normalize_model_tag

__all__ = [
    "reload_telegram_nudges",
    "cloud_provider_key_present",
    "validate_model_assignment",
]

logger = logging.getLogger(__name__)


async def reload_telegram_nudges() -> None:
    """Best-effort POST to telegram_bot /internal/reload-nudges."""
    telegram_url = get_telegram_settings().url_or_none
    if not telegram_url:
        logger.debug("TELEGRAM_BOT_URL empty — skipping nudge reload")
        return
    api_key_secret = get_secrets_settings().jarvis_api_key
    api_key = api_key_secret.get_secret_value() if api_key_secret is not None else ""
    try:
        async with httpx.AsyncClient() as client:
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
    config_key = f"llm.{provider}.api_key"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config WHERE key = $1 AND user_id IS NULL",
            config_key,
        )
    return bool(
        row is not None and (row.get("encrypted_value") is not None or row.get("value") is not None)
    )


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
        raise HTTPException(
            status_code=400,
            detail=f"Model {model_id!r} is not in the model catalog",
        )
    if not entry.assignable:
        raise HTTPException(
            status_code=422,
            detail="This model is tracked for evaluation but is not assignable yet.",
        )
    if role not in entry.roles:
        raise HTTPException(
            status_code=400,
            detail=f"Model {model_id!r} cannot be assigned to the {role!r} role",
        )
    if entry.provider == "ollama":
        # Inline fetch_installed_ollama_names (sole caller — YAGNI delete 2)
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
        tag = normalize_model_tag(entry.ollama_tag or entry.id)
        if tag not in installed_names:
            raise HTTPException(status_code=422, detail="Model not pulled. Pull it first.")
        return
    if not await cloud_provider_key_present(entry.provider, db_pool):
        raise HTTPException(
            status_code=422,
            detail=f"Configure the {entry.provider} API key before assigning this model.",
        )
