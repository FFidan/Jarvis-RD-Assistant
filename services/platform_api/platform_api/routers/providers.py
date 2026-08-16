"""Platform-owned cloud provider administration routes."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Annotated, Final, Literal

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jarvis_common import log_audit
from jarvis_common.auth import current_user_id_strict, require_admin, verify_api_key
from jarvis_common.crypto import resolve_secret_row
from jarvis_common.event_log import log_event
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.llm_provider_registry import (
    PROVIDER_CONFIG_KEYS,
    PROVIDER_REGISTRY,
    ProviderDefinition,
    provider_for_id,
    provider_for_prefix,
)
from jarvis_common.logging_config import request_id_ctx
from jarvis_common.maintenance import OutboundEgressBlockedError, ensure_outbound_egress_allowed
from jarvis_common.provider_account import fetch_provider_account
from jarvis_common.provider_test import (
    _SUPPORTED_PROVIDERS,
    ProviderTestResult,
    test_provider_connectivity,
)
from pydantic import BaseModel

from platform_api.config import get_platform_settings
from platform_api.deps import get_db_pool, get_identity_signer, limiter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/providers",
    tags=["providers"],
    dependencies=[Depends(verify_api_key), Depends(require_admin)],
)

_RESEARCH_CACHE_PATH: Final = "/internal/platform/providers/{provider}/cache/invalidate"
_RESEARCH_CACHE_SCOPE: Final = ("research:providers:write",)
_MODEL_ROUTE_LABELS: Final[dict[str, str]] = {
    "llm.smart_model": "Main",
    "llm.fast_model": "Quick",
    "llm.embed_model": "Embedding",
}


class ProviderTestResponse(BaseModel):
    """Connectivity result returned to the administrator UI.

    Parameters
    ----------
    ok : bool
        Whether the provider accepted the configured credential.
    error : str or None, optional
        Stable, bounded failure detail suitable for display.
    """

    ok: bool
    error: str | None = None


class ProviderMetadataResponse(BaseModel):
    """Public, non-secret metadata for one supported cloud provider.

    Parameters
    ----------
    id : str
        Stable provider identifier.
    display_name : str
        Human-readable provider name.
    kind : str
        Provider transport family.
    api_key_config_key : str
        Platform configuration key holding the credential.
    base_url_config_key : str or None, optional
        Platform configuration key holding a custom endpoint.
    assignment_prefix : str
        Prefix stored in model assignments.
    litellm_prefix : str
        Prefix delivered to LiteLLM.
    privacy_boundary : str
        Provider privacy classification shown to administrators.
    best_for : str
        Concise provider-selection guidance.
    data_note : str
        Concise data-handling guidance.
    configured : bool
        Whether a non-empty credential row exists.
    base_url_configured : bool, optional
        Whether a non-empty custom endpoint row exists.
    supports_assignment : bool
        Whether the provider can back a model route.
    dashboard_url : str or None, optional
        Provider-owned credential dashboard URL.
    account_capability : {"current_key", "balance", "unavailable", "no_provider_api"}
        Account information that can be retrieved safely.
    """

    id: str
    display_name: str
    kind: str
    api_key_config_key: str
    base_url_config_key: str | None = None
    assignment_prefix: str
    litellm_prefix: str
    privacy_boundary: str
    best_for: str
    data_note: str
    configured: bool
    base_url_configured: bool = False
    supports_assignment: bool
    dashboard_url: str | None = None
    account_capability: Literal["current_key", "balance", "unavailable", "no_provider_api"]


class ProviderAccountResponse(BaseModel):
    """Capability-gated account data for one registered provider.

    Parameters
    ----------
    provider : str
        Stable provider identifier.
    capability : {"current_key", "balance", "unavailable", "no_provider_api"}
        Account lookup capability declared by the provider registry.
    data : dict[str, bool or int or float or str or None]
        Allowlisted non-identifying account fields.
    error_code : str or None, optional
        Stable sanitized failure code.
    """

    provider: str
    capability: Literal["current_key", "balance", "unavailable", "no_provider_api"]
    data: dict[str, bool | int | float | str | None]
    error_code: str | None = None


def _row_has_value(row: Mapping[str, object]) -> bool:
    """Return whether a configuration row contains a non-empty value.

    Parameters
    ----------
    row : Mapping[str, object]
        Mapping-like asyncpg row with plaintext and encrypted columns.

    Returns
    -------
    bool
        ``True`` when either storage column is non-empty.
    """
    return bool(row.get("value")) or bool(row.get("encrypted_value"))


async def _read_system_secret(pool: asyncpg.Pool, key: str) -> str | None:
    """Read one system-scoped secret without exposing storage failures.

    Parameters
    ----------
    pool : asyncpg.Pool
        Platform database pool.
    key : str
        Exact configuration key.

    Returns
    -------
    str or None
        Decrypted non-empty value, or ``None`` when absent or unreadable.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config WHERE key = $1 AND user_id IS NULL",
            key,
        )
    if row is None:
        return None
    try:
        value = resolve_secret_row(row)
    except Exception:  # noqa: BLE001 - secret failures stay behind the Platform boundary
        logger.warning("Provider secret could not be resolved", exc_info=True)
        return None
    return value or None


def _require_provider_egress(operation: str) -> None:
    """Reject a credential-bearing provider action during quarantine.

    Parameters
    ----------
    operation : str
        Non-secret operation label used by the quarantine log.

    Raises
    ------
    HTTPException
        With status 503 while restored credentials await review.
    """
    try:
        ensure_outbound_egress_allowed(operation)
    except OutboundEgressBlockedError:
        raise HTTPException(
            status_code=503,
            detail=(
                "This restored deployment is read-only until outbound credentials are reviewed"
            ),
        ) from None


@router.get("", response_model=list[ProviderMetadataResponse])
@limiter.limit("30/minute")
async def list_providers(
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
) -> list[ProviderMetadataResponse]:
    """Return provider metadata and deployment-wide configuration state.

    Parameters
    ----------
    request : Request
        Authenticated administrator request used by the rate limiter.
    pool : asyncpg.Pool
        Platform database pool.

    Returns
    -------
    list[ProviderMetadataResponse]
        Registry entries without stored secret values.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value, encrypted_value FROM user_config "
            "WHERE key = ANY($1::text[]) AND user_id IS NULL",
            sorted(PROVIDER_CONFIG_KEYS),
        )
    configured_keys = {str(row["key"]) for row in rows if _row_has_value(row)}
    return [
        ProviderMetadataResponse(
            id=provider.id,
            display_name=provider.display_name,
            kind=provider.kind,
            api_key_config_key=provider.api_key_config_key,
            base_url_config_key=provider.base_url_config_key,
            assignment_prefix=provider.assignment_prefix,
            litellm_prefix=provider.provider_model_prefix,
            privacy_boundary=provider.privacy_boundary,
            best_for=provider.best_for,
            data_note=provider.data_note,
            configured=provider.api_key_config_key in configured_keys,
            base_url_configured=(
                provider.base_url_config_key is not None
                and provider.base_url_config_key in configured_keys
            ),
            supports_assignment=provider.supports_assignment,
            dashboard_url=provider.dashboard_url,
            account_capability=provider.account_capability,
        )
        for provider in PROVIDER_REGISTRY
    ]


@router.get("/{provider}/account", response_model=ProviderAccountResponse)
@limiter.limit("5/minute")
async def get_provider_account(
    request: Request,
    provider: str,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
) -> ProviderAccountResponse:
    """Return sanitized account data available to the stored credential.

    Parameters
    ----------
    request : Request
        Authenticated administrator request used by the rate limiter.
    provider : str
        Registered provider identifier.
    pool : asyncpg.Pool
        Platform database pool.

    Returns
    -------
    ProviderAccountResponse
        Capability, allowlisted data, and a stable error code.

    Raises
    ------
    HTTPException
        With status 400 for an unknown provider.
    """
    try:
        definition = provider_for_id(provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unsupported provider") from exc
    _require_provider_egress("cloud provider account snapshot")
    api_key = await _read_system_secret(pool, definition.api_key_config_key)
    snapshot = await fetch_provider_account(provider, api_key=api_key)
    return ProviderAccountResponse(
        provider=snapshot.provider,
        capability=snapshot.capability,
        data=snapshot.data,
        error_code=snapshot.error_code,
    )


@router.post("/{provider}/test", response_model=ProviderTestResponse)
@limiter.limit("5/minute")
async def test_provider(
    request: Request,
    provider: str,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
) -> ProviderTestResponse:
    """Probe one provider using its Platform-owned connection settings.

    Parameters
    ----------
    request : Request
        Authenticated administrator request used by the rate limiter.
    provider : str
        Registered provider identifier.
    pool : asyncpg.Pool
        Platform database pool.

    Returns
    -------
    ProviderTestResponse
        Non-raising provider connectivity outcome.

    Raises
    ------
    HTTPException
        With status 400 for a provider outside the probe allowlist.
    """
    if provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="unsupported provider")
    _require_provider_egress("cloud provider connectivity probe")
    definition = provider_for_id(provider)
    api_key = await _read_system_secret(pool, definition.api_key_config_key)
    if not api_key:
        result = ProviderTestResult(ok=False, error="no api key configured")
    else:
        base_url = None
        if definition.base_url_config_key is not None:
            base_url = await _read_system_secret(pool, definition.base_url_config_key)
        if definition.base_url_config_key is not None and base_url is None:
            result = ProviderTestResult(ok=False, error="no base URL configured")
        else:
            result = await test_provider_connectivity(provider, api_key, base_url=base_url)
    await log_event(
        pool=pool,
        level="info" if result.ok else "warning",
        category="config",
        source="settings",
        message="llm/provider_connection_checked",
        context={
            "provider": provider,
            "success": result.ok,
            "code": _provider_test_code(result.error, result.ok),
        },
    )
    return ProviderTestResponse(ok=result.ok, error=result.error)


def _provider_test_code(error: str | None, ok: bool) -> str:
    """Map a provider-test outcome to one stable event code.

    Parameters
    ----------
    error : str or None
        Sanitized probe error.
    ok : bool
        Whether the probe succeeded.

    Returns
    -------
    str
        Low-cardinality event code.
    """
    if ok:
        return "ok"
    if error is None:
        return "connection_failed"
    return {
        "no api key configured": "api_key_unavailable",
        "no base URL configured": "base_url_unavailable",
        "unsupported provider": "unsupported_provider",
    }.get(error, "connection_failed")


async def _model_route_using(
    provider: ProviderDefinition,
    pool: asyncpg.Pool,
) -> str | None:
    """Return the display label of a model route using ``provider``.

    Parameters
    ----------
    provider : ProviderDefinition
        Provider whose credential or endpoint would be removed.
    pool : asyncpg.Pool
        Platform database pool.

    Returns
    -------
    str or None
        Assigned route label, or ``None`` when no route depends on the provider.

    Raises
    ------
    HTTPException
        With status 503 when assignments cannot be read safely.
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM user_config "
                "WHERE key = ANY($1::text[]) AND user_id IS NULL",
                list(_MODEL_ROUTE_LABELS),
            )
    except Exception as exc:
        logger.warning("Provider removal could not read model assignments", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail=(
                "The stored model routes could not be read just now, so this setting "
                "was left in place. Please try again in a moment."
            ),
        ) from exc

    for row in rows:
        model_id = row["value"]
        if not isinstance(model_id, str):
            continue
        normalized = model_id.strip('"')
        assigned_provider = provider_for_prefix(normalized.partition("/")[0])
        if assigned_provider == provider:
            return _MODEL_ROUTE_LABELS[str(row["key"])]
    return None


async def _invalidate_research_provider_cache(
    *,
    request: Request,
    signer: IdentityAssertionSigner,
    provider: str,
    caller_user_id: int,
) -> None:
    """Require Research to invalidate one provider model-list cache.

    Parameters
    ----------
    request : Request
        Platform request carrying administrator session state and HTTP client.
    signer : IdentityAssertionSigner
        Platform-only identity assertion signer.
    provider : str
        Validated provider identifier.
    caller_user_id : int
        Authenticated administrator identifier.

    Raises
    ------
    HTTPException
        With status 503 when Research cannot acknowledge the exact command.
    """
    path = _RESEARCH_CACHE_PATH.format(provider=provider)
    request_id = request_id_ctx.get() or str(uuid.uuid4())
    assertion = signer.issue(
        audience="research",
        subject=f"user:{caller_user_id}",
        principal="browser",
        user_id=caller_user_id,
        user_role=getattr(request.state, "user_role", None),
        session_id=getattr(request.state, "session_id", None),
        request_id=request_id,
        request_method="POST",
        request_path=path,
        scopes=_RESEARCH_CACHE_SCOPE,
    )
    client: httpx.AsyncClient = request.app.state.http_client
    try:
        response = await client.post(
            f"{get_platform_settings().research_api_url}{path}",
            headers={"X-Jarvis-Identity": assertion, "X-Request-Id": request_id},
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Research provider cache invalidation is unavailable", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Provider settings are temporarily unavailable",
        ) from None


async def _remove_provider_setting(  # noqa: PLR0913 - shared safe-removal transaction inputs
    *,
    request: Request,
    provider: str,
    field: Literal["api_key", "base_url"],
    pool: asyncpg.Pool,
    signer: IdentityAssertionSigner,
    caller_user_id: int,
) -> Response:
    """Delete one provider setting after its dependent owners acknowledge it.

    Parameters
    ----------
    request : Request
        Authenticated Platform request.
    provider : str
        Provider identifier from the public path.
    field : {"api_key", "base_url"}
        Provider setting to remove.
    pool : asyncpg.Pool
        Platform database pool.
    signer : IdentityAssertionSigner
        Platform-only signer for the exact Research cache command.
    caller_user_id : int
        Authenticated administrator identifier.

    Returns
    -------
    Response
        Empty 204 response after the setting is removed.

    Raises
    ------
    HTTPException
        With status 400 for an unsupported setting, 409 for an assigned model
        route, or 503 when safe removal cannot be established.
    """
    try:
        definition = provider_for_id(provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unsupported provider") from exc
    config_key = (
        definition.api_key_config_key if field == "api_key" else definition.base_url_config_key
    )
    if config_key is None:
        stored = "an API key" if field == "api_key" else "an endpoint URL"
        raise HTTPException(
            status_code=400,
            detail=(
                f"{definition.display_name} does not store {stored}, so there is nothing to remove."
            ),
        )

    blocking_route = await _model_route_using(definition, pool)
    if blocking_route is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The {blocking_route} model route still uses {definition.display_name}. "
                "Point that route at another model first."
            ),
        )

    await _invalidate_research_provider_cache(
        request=request,
        signer=signer,
        provider=definition.id,
        caller_user_id=caller_user_id,
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_config WHERE key = $1 AND user_id IS NULL",
            config_key,
        )
    await log_audit(
        pool,
        action="secret.remove",
        resource=config_key,
        user_id=str(caller_user_id),
    )
    return Response(status_code=204)


@router.delete("/{provider}/key", status_code=204, response_class=Response)
@limiter.limit("5/minute")
async def remove_provider_key(
    request: Request,
    provider: str,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
    signer: Annotated[IdentityAssertionSigner, Depends(get_identity_signer)],
    caller_user_id: Annotated[int, Depends(current_user_id_strict)],
) -> Response:
    """Delete a stored provider credential that no model route uses.

    Parameters
    ----------
    request : Request
        Authenticated administrator request.
    provider : str
        Provider identifier from the public path.
    pool : asyncpg.Pool
        Platform database pool.
    signer : IdentityAssertionSigner
        Platform-only signer for the Research cache command.
    caller_user_id : int
        Authenticated administrator identifier.

    Returns
    -------
    Response
        Empty 204 response after safe removal.
    """
    return await _remove_provider_setting(
        request=request,
        provider=provider,
        field="api_key",
        pool=pool,
        signer=signer,
        caller_user_id=caller_user_id,
    )


@router.delete("/{provider}/base-url", status_code=204, response_class=Response)
@limiter.limit("5/minute")
async def remove_provider_base_url(
    request: Request,
    provider: str,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
    signer: Annotated[IdentityAssertionSigner, Depends(get_identity_signer)],
    caller_user_id: Annotated[int, Depends(current_user_id_strict)],
) -> Response:
    """Delete a custom endpoint URL that no model route uses.

    Parameters
    ----------
    request : Request
        Authenticated administrator request.
    provider : str
        Provider identifier from the public path.
    pool : asyncpg.Pool
        Platform database pool.
    signer : IdentityAssertionSigner
        Platform-only signer for the Research cache command.
    caller_user_id : int
        Authenticated administrator identifier.

    Returns
    -------
    Response
        Empty 204 response after safe removal.
    """
    return await _remove_provider_setting(
        request=request,
        provider=provider,
        field="base_url",
        pool=pool,
        signer=signer,
        caller_user_id=caller_user_id,
    )


__all__ = [
    "ProviderAccountResponse",
    "ProviderMetadataResponse",
    "ProviderTestResponse",
    "get_provider_account",
    "list_providers",
    "remove_provider_base_url",
    "remove_provider_key",
    "router",
    "test_provider",
]
