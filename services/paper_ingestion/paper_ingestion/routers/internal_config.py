"""Signed Platform-to-Research configuration write boundary."""

from __future__ import annotations

from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jarvis_common.auth import current_user_id_strict, require_admin
from jarvis_common.config_metadata import (
    _classify_config_key,
    _is_allowed_config_key,
)
from jarvis_common.llm_provider_registry import PROVIDER_REGISTRY, provider_for_id
from pydantic import BaseModel, Field

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import get_db_pool, get_scheduler
from paper_ingestion.services.config_write import write_config
from paper_ingestion.services.litellm_config import update_litellm_model
from paper_ingestion.services.provider_models import invalidate_provider_model_cache

router = APIRouter(prefix="/internal/platform", tags=["internal"])


class ConfigWriteRequest(BaseModel):
    """Configuration value forwarded by Platform.

    Parameters
    ----------
    value : Any
        JSON-compatible value from the public configuration contract.
    """

    value: Any


class ConfigWriteResponse(BaseModel):
    """Result of applying a Research-coupled configuration write.

    Parameters
    ----------
    key : str
        Configuration key that was written.
    value : Any
        Display-safe stored value, with secrets masked.
    schedule_apply_warnings : list[str]
        Scheduler effects that could not be applied immediately.
    """

    key: str
    value: Any
    schedule_apply_warnings: list[str] = Field(default_factory=list)


@router.put("/config/{key}", response_model=ConfigWriteResponse)
async def write_platform_config(  # noqa: PLR0913 - FastAPI command boundary dependencies
    request: Request,
    key: str,
    body: ConfigWriteRequest,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
    scheduler: Annotated[Any, Depends(get_scheduler)],
    caller_user_id: Annotated[int, Depends(current_user_id_strict)],
) -> ConfigWriteResponse:
    """Apply one configuration write authorized and signed by Platform.

    Platform owns the public contract and audit trail. Research temporarily
    retains this private command because model delivery and scheduler updates
    are still Research runtime responsibilities in v1.2.6.

    Parameters
    ----------
    request : Request
        Verified request whose state carries the Platform-issued identity.
    key : str
        Configuration key from the exact request path.
    body : ConfigWriteRequest
        Requested JSON-compatible value.
    pool : asyncpg.Pool
        Research database pool.
    scheduler : Any
        Research scheduler used for immediate schedule reconciliation.
    caller_user_id : int
        Positive user identity from the verified assertion.

    Returns
    -------
    ConfigWriteResponse
        Display-safe value and any non-fatal scheduler warnings.

    Raises
    ------
    HTTPException
        With status 400 for an unknown key or 403 when a non-administrator
        attempts to write a system-scoped key.
    """
    if not _is_allowed_config_key(key):
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")
    if _classify_config_key(key) == "system":
        await require_admin(request)

    settings = get_paper_ingestion_settings()
    result = await write_config(
        db_pool=pool,
        scheduler=scheduler,
        http_client=request.app.state.http_client,
        ollama_url=settings.ollama_base_url,
        key=key,
        value=body.value,
        caller_user_id=caller_user_id,
        update_litellm_model_fn=update_litellm_model,
        app=request.app,
    )

    changed_provider = next(
        (
            provider
            for provider in PROVIDER_REGISTRY
            if key in {provider.api_key_config_key, provider.base_url_config_key}
        ),
        None,
    )
    if changed_provider is not None:
        await invalidate_provider_model_cache(changed_provider.id)

    return ConfigWriteResponse(
        key=key,
        value=result.display_value,
        schedule_apply_warnings=result.schedule_apply_warnings,
    )


@router.post(
    "/providers/{provider}/cache/invalidate",
    status_code=204,
    response_class=Response,
)
async def invalidate_platform_provider_cache(
    request: Request,
    provider: str,
    caller_user_id: Annotated[int, Depends(current_user_id_strict)],
) -> Response:
    """Invalidate Research's cached model listing for one provider.

    Platform calls this exact command before removing provider connection
    settings. The verified administrator identity prevents another service or
    an API-key-only caller from turning the command into a generic cache flush.

    Parameters
    ----------
    request : Request
        Verified request carrying the Platform-issued administrator identity.
    provider : str
        Registered provider identifier.
    caller_user_id : int
        Positive administrator identifier from the verified assertion.

    Returns
    -------
    Response
        Empty 204 response after the cache is invalidated.

    Raises
    ------
    HTTPException
        With status 400 for an unknown provider or 403 for a non-administrator.
    """
    del caller_user_id
    await require_admin(request)
    try:
        definition = provider_for_id(provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unsupported provider") from exc
    await invalidate_provider_model_cache(definition.id)
    return Response(status_code=204)


__all__ = [
    "ConfigWriteRequest",
    "ConfigWriteResponse",
    "invalidate_platform_provider_cache",
    "router",
    "write_platform_config",
]
