"""Platform-owned public configuration contract."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import log_audit, require_admin
from jarvis_common.auth import current_user_id_strict
from jarvis_common.config_metadata import (
    _ENCRYPTED_KEYS,
    BROWSER_READABLE_SYSTEM_KEYS,
    PERSONAL_KEYS,
    _classify_config_key,
    _is_allowed_config_key,
)
from jarvis_common.config_store import _fetch_effective_config_row, _resolve_config_value
from jarvis_common.event_log import log_event
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.logging_config import request_id_ctx
from pydantic import BaseModel, Field, ValidationError

from platform_api.config import get_platform_settings
from platform_api.deps import get_db_pool, get_identity_signer, limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["configuration"])

_RESEARCH_CONFIG_SCOPE = ("research:config:write",)
_RESEARCH_CONFIG_PATH_PREFIX = "/internal/platform/config"


class ConfigEntry(BaseModel):
    """One public configuration entry.

    Parameters
    ----------
    key : str
        Stable configuration key.
    value : Any
        JSON-compatible value or a masked secret preview.
    """

    key: str
    value: Any


class ResearchConfigWriteResponse(BaseModel):
    """Validated response from the temporary Research write command.

    Parameters
    ----------
    key : str
        Configuration key that Research applied.
    value : Any
        Display-safe stored value.
    schedule_apply_warnings : list[str]
        Scheduler effects that could not be applied immediately.
    """

    key: str
    value: Any
    schedule_apply_warnings: list[str] = Field(default_factory=list)


def _has_browser_session(request: Request) -> bool:
    """Return whether Platform resolved a browser role for the request.

    Parameters
    ----------
    request : Request
        Incoming Platform request.

    Returns
    -------
    bool
        ``True`` when the request carries a resolved user role.
    """
    return getattr(request.state, "user_role", None) is not None


@router.get("", response_model=list[ConfigEntry])
@limiter.limit("60/minute")
async def list_config(
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
    caller_user_id: Annotated[int, Depends(current_user_id_strict)],
) -> list[ConfigEntry]:
    """Return the configuration entries visible to the current caller.

    Browser members receive personal settings and explicitly readable system
    flags. Administrators receive their personal rows plus system rows.

    Parameters
    ----------
    request : Request
        Authenticated request carrying the resolved browser role.
    pool : asyncpg.Pool
        Platform database pool.
    caller_user_id : int
        Authenticated user identifier.

    Returns
    -------
    list[ConfigEntry]
        Visible entries with secret values masked.
    """
    browser_session = _has_browser_session(request)
    role = getattr(request.state, "user_role", None)
    readable_keys = sorted(PERSONAL_KEYS | BROWSER_READABLE_SYSTEM_KEYS)
    async with pool.acquire() as conn:
        if browser_session and role != "admin":
            rows = await conn.fetch(
                """SELECT DISTINCT ON (key) key, value, encrypted_value, user_id
                   FROM user_config
                   WHERE key = ANY($1::text[])
                     AND (user_id = $2 OR user_id IS NULL)
                   ORDER BY key, user_id IS NULL""",
                readable_keys,
                caller_user_id,
            )
        elif browser_session:
            rows = await conn.fetch(
                """SELECT DISTINCT ON (key) key, value, encrypted_value, user_id
                   FROM user_config
                   WHERE user_id IS NULL OR user_id = $1
                   ORDER BY key, user_id IS NULL""",
                caller_user_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT key, value, encrypted_value, user_id
                   FROM user_config
                   WHERE user_id IS NULL
                   ORDER BY key"""
            )
    return [
        ConfigEntry(key=row["key"], value=_resolve_config_value(row["key"], row)) for row in rows
    ]


@router.get("/{key}", response_model=ConfigEntry)
@limiter.limit("60/minute")
async def get_config(
    request: Request,
    key: str,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
    caller_user_id: Annotated[int, Depends(current_user_id_strict)],
) -> ConfigEntry:
    """Return one caller-visible configuration entry.

    Parameters
    ----------
    request : Request
        Authenticated request carrying the resolved browser role.
    key : str
        Requested configuration key.
    pool : asyncpg.Pool
        Platform database pool.
    caller_user_id : int
        Authenticated user identifier.

    Returns
    -------
    ConfigEntry
        Requested entry with any secret value masked.

    Raises
    ------
    HTTPException
        With status 404 for an unknown or unavailable key, or 403 when a
        non-administrator browser requests a system-scoped key.
    """
    if not _is_allowed_config_key(key):
        raise HTTPException(status_code=404, detail=f"Config key '{key}' not found")
    if _classify_config_key(key) == "system" and _has_browser_session(request):
        await require_admin(request)
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with pool.acquire() as conn:
        row = await _fetch_effective_config_row(
            conn,
            key,
            caller_user_id,
            is_admin=is_admin,
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Config key '{key}' not found")
    return ConfigEntry(key=row["key"], value=_resolve_config_value(key, row))


@router.put("/{key}", response_model=ConfigEntry)
@limiter.limit("30/minute")
async def set_config(
    request: Request,
    key: str,
    body: ConfigEntry,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
    signer: Annotated[IdentityAssertionSigner, Depends(get_identity_signer)],
    caller_user_id: Annotated[int, Depends(current_user_id_strict)],
) -> ConfigEntry:
    """Validate, authorize, and apply one public configuration write.

    Platform owns the public contract and audit trail. The coupled model and
    scheduler side effects are forwarded through an exact signed Research
    command until their owning state is separated in a later workstream.

    Parameters
    ----------
    request : Request
        Authenticated Platform request and shared HTTP client owner.
    key : str
        Configuration key from the public request path.
    body : ConfigEntry
        Requested JSON-compatible value.
    pool : asyncpg.Pool
        Platform database pool used for the audit trail.
    signer : IdentityAssertionSigner
        Platform-only signer for the exact Research command.
    caller_user_id : int
        Authenticated user identifier.

    Returns
    -------
    ConfigEntry
        Display-safe value returned after Research applies the write.

    Raises
    ------
    HTTPException
        With status 400 for an unknown key, 403 for an unauthorized system
        write, or 503 when Research cannot confirm a valid result.
    """
    if not _is_allowed_config_key(key):
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")
    if _classify_config_key(key) == "system":
        await require_admin(request)

    result = await _write_research_config(
        request=request,
        signer=signer,
        key=key,
        value=body.value,
        caller_user_id=caller_user_id,
    )
    await _record_config_change(
        pool=pool,
        key=key,
        caller_user_id=caller_user_id,
        result=result,
    )
    return ConfigEntry(key=key, value=result.value)


async def _write_research_config(
    *,
    request: Request,
    signer: IdentityAssertionSigner,
    key: str,
    value: Any,
    caller_user_id: int,
) -> ResearchConfigWriteResponse:
    """Send one request-bound configuration command to Research.

    Parameters
    ----------
    request : Request
        Platform request carrying user role and session state.
    signer : IdentityAssertionSigner
        Platform assertion signer.
    key : str
        Validated configuration key.
    value : Any
        Requested configuration value.
    caller_user_id : int
        Authenticated user identifier.

    Returns
    -------
    ResearchConfigWriteResponse
        Validated downstream result.

    Raises
    ------
    HTTPException
        With a preserved safe 400 or 409 response, or status 503 when the
        downstream boundary is unavailable or malformed.
    """
    path = f"{_RESEARCH_CONFIG_PATH_PREFIX}/{key}"
    request_id = request_id_ctx.get() or str(uuid.uuid4())
    assertion = signer.issue(
        audience="research",
        subject=f"user:{caller_user_id}",
        principal="browser",
        user_id=caller_user_id,
        user_role=getattr(request.state, "user_role", None),
        session_id=getattr(request.state, "session_id", None),
        request_id=request_id,
        request_method="PUT",
        request_path=path,
        scopes=_RESEARCH_CONFIG_SCOPE,
    )
    client: httpx.AsyncClient = request.app.state.http_client
    settings = get_platform_settings()
    try:
        response = await client.put(
            f"{settings.research_api_url}{path}",
            json={"value": value},
            headers={
                "X-Jarvis-Identity": assertion,
                "X-Request-Id": request_id,
            },
            timeout=310.0,
        )
        if response.status_code in {400, 409}:
            raise HTTPException(
                status_code=response.status_code,
                detail=_safe_downstream_detail(response),
            )
        response.raise_for_status()
        result = ResearchConfigWriteResponse.model_validate(response.json())
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, ValidationError):
        logger.warning("Research configuration write is unavailable", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Configuration update is temporarily unavailable",
        ) from None
    if result.key != key:
        logger.warning("Research configuration response returned a mismatched key")
        raise HTTPException(
            status_code=503,
            detail="Configuration update is temporarily unavailable",
        )
    return result


def _safe_downstream_detail(response: httpx.Response) -> str:
    """Return a bounded user-facing detail from a safe client error.

    Parameters
    ----------
    response : httpx.Response
        Research response with status 400 or 409.

    Returns
    -------
    str
        Bounded detail string or a generic validation message.
    """
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and 0 < len(detail) <= 500:
        return detail
    return "Configuration value is invalid"


async def _record_config_change(
    *,
    pool: asyncpg.Pool,
    key: str,
    caller_user_id: int,
    result: ResearchConfigWriteResponse,
) -> None:
    """Record Platform-owned audit and event entries for a successful write.

    Parameters
    ----------
    pool : asyncpg.Pool
        Platform database pool.
    key : str
        Configuration key that changed.
    caller_user_id : int
        Authenticated user identifier.
    result : ResearchConfigWriteResponse
        Confirmed downstream result and scheduler warnings.
    """
    route_role = {"llm.fast_model": "fast", "llm.smart_model": "smart"}.get(key)
    if route_role is not None:
        await log_audit(
            pool,
            action="llm.route.change",
            resource=key,
            user_id=str(caller_user_id),
        )
    elif key in _ENCRYPTED_KEYS:
        await log_audit(
            pool,
            action="secret.rotate",
            resource=key,
            user_id=str(caller_user_id),
        )

    try:
        await log_event(
            pool=pool,
            level="info",
            category="config",
            source="settings",
            message="llm/route_changed" if route_role is not None else "setting_changed",
            context=(
                {"key": key, "role": route_role}
                if route_role is not None
                else {
                    "key": key,
                    "new_value": str(result.value),
                    **(
                        {"schedule_apply_warnings": result.schedule_apply_warnings}
                        if result.schedule_apply_warnings
                        else {}
                    ),
                }
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug("config event logging failed (non-fatal)", exc_info=True)


__all__ = [
    "ConfigEntry",
    "ResearchConfigWriteResponse",
    "get_config",
    "list_config",
    "router",
    "set_config",
]
