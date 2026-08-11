"""Settings, nudges, and source management endpoints.

HTTP transport layer only — business logic lives in concern-specific service modules.

Route handlers are thin:  parse → auth check → delegate → return.

Sub-routers:
- ``settings_sources.router`` — /api/nudges/* and /api/sources/*
"""

import logging
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from jarvis_common import log_audit
from jarvis_common.auth import current_user_id_strict, require_admin, verify_api_key
from jarvis_common.crypto import resolve_secret_row
from jarvis_common.event_log import log_event as _log_event
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, get_scheduler, limiter
from paper_ingestion.models import (
    ConfigEntry,
    PapersBySourceItem,
    PapersByStatusItem,
)
from paper_ingestion.routers.settings_sources import sources_router
from paper_ingestion.services.analytics_queries import (
    fetch_papers_by_source,
    fetch_papers_by_status,
)
from paper_ingestion.services.config_db import (
    _fetch_effective_config_row,
    _resolve_config_value,
)
from paper_ingestion.services.config_metadata import (
    _ENCRYPTED_KEYS,
    PERSONAL_KEYS,
    _classify_config_key,
    _is_allowed_config_key,
)
from paper_ingestion.services.config_write import write_config
from paper_ingestion.services.data_export import build_export_zip
from paper_ingestion.services.litellm_config import (
    get_provider_base_url,
    update_litellm_model,
)
from paper_ingestion.services.llm_provider_registry import (
    PROVIDER_REGISTRY,
    provider_for_id,
)
from paper_ingestion.services.model_assignment import cloud_provider_key_present
from paper_ingestion.services.provider_account import fetch_provider_account
from paper_ingestion.services.provider_test import (
    _SUPPORTED_PROVIDERS,
    ProviderTestResult,
    test_provider_connectivity,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["settings"])

# Include sub-routers (no prefix — sub-files already define full paths under /api)
router.include_router(sources_router)


# ---------------------------------------------------------------------------
# Response models (router-local; not part of the service contract)
# ---------------------------------------------------------------------------


class ProviderTestResponse(BaseModel):
    ok: bool
    error: str | None = None


class ProviderMetadataResponse(BaseModel):
    """Public, non-secret metadata for one supported cloud provider."""

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
    account_capability: Literal["current_key", "balance", "unavailable"]


class ProviderAccountResponse(BaseModel):
    """Capability-gated, sanitized account data for one registered provider."""

    provider: str
    capability: Literal["current_key", "balance", "unavailable"]
    data: dict[str, bool | int | float | str | None]
    error_code: str | None = None


# ---------------------------------------------------------------------------
# Private router helpers
# ---------------------------------------------------------------------------


def _has_browser_session(request: Request) -> bool:
    return getattr(request.state, "user_role", None) is not None


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------


@router.get("/config", response_model=list[ConfigEntry])
@limiter.limit("60/minute")
async def list_config(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    caller_user_id: int = Depends(current_user_id_strict),
) -> list[ConfigEntry]:
    """Return all config entries.

    Browser users only receive personal settings unless they are admins.
    API-key-only callers preserve the legacy single-tenant view.
    """
    browser_session = _has_browser_session(request)
    role = getattr(request.state, "user_role", None)
    personal_keys = sorted(PERSONAL_KEYS)
    async with db_pool.acquire() as conn:
        if browser_session and role != "admin":
            rows = await conn.fetch(
                """SELECT DISTINCT ON (key) key, value, encrypted_value, user_id
                   FROM user_config
                   WHERE key = ANY($1::text[])
                     AND (user_id = $2 OR user_id IS NULL)
                   ORDER BY key, user_id IS NULL""",
                personal_keys,
                caller_user_id,
            )
        elif browser_session and caller_user_id is not None:
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
    return [ConfigEntry(key=r["key"], value=_resolve_config_value(r["key"], r)) for r in rows]


@router.get("/config/{key}")
@limiter.limit("60/minute")
async def get_config(
    request: Request,
    key: str,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    caller_user_id: int = Depends(current_user_id_strict),
) -> ConfigEntry:
    if not _is_allowed_config_key(key):
        raise HTTPException(404, f"Config key '{key}' not found")
    if _classify_config_key(key) == "system" and _has_browser_session(request):
        await require_admin(request)
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        row = await _fetch_effective_config_row(conn, key, caller_user_id, is_admin=is_admin)
    if not row:
        raise HTTPException(404, f"Config key '{key}' not found")
    value = _resolve_config_value(key, row)
    return ConfigEntry(key=row["key"], value=value)


@router.put("/config/{key}")
@limiter.limit("30/minute")
async def set_config(
    request: Request,
    key: str,
    body: ConfigEntry,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    scheduler=Depends(get_scheduler),
    caller_user_id: int = Depends(current_user_id_strict),
) -> ConfigEntry:
    if not _is_allowed_config_key(key):
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")

    # System-scope keys require an admin browser session. API-key-only callers
    # (Telegram/cron/lifespan) never reach this endpoint for system keys — they
    # write directly to user_config via SQL, bypassing this gate.
    if _classify_config_key(key) == "system":
        await require_admin(request)

    # Delegate the full write + side-effects to the service layer.
    # require_admin and audit logging stay here so patch paths on this module
    # remain stable (tests patch paper_ingestion.routers.settings.require_admin
    # and paper_ingestion.routers.settings._log_event).
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    ollama_url = get_paper_ingestion_settings().ollama_base_url
    http_client = request.app.state.http_client

    # Pass update_litellm_model from the router namespace so monkeypatching
    # ``paper_ingestion.routers.settings.update_litellm_model`` in tests still works.
    result = await write_config(
        db_pool=db_pool,
        scheduler=scheduler,
        http_client=http_client,
        ollama_url=ollama_url,
        key=key,
        value=body.value,
        caller_user_id=caller_user_id,
        update_litellm_model_fn=update_litellm_model,
        app=request.app,
    )
    display_value = result.display_value

    route_role = {"llm.fast_model": "fast", "llm.smart_model": "smart"}.get(key)
    if route_role is not None:
        await log_audit(
            db_pool,
            action="llm.route.change",
            resource=key,
            user_id=str(caller_user_id) if caller_user_id is not None else None,
        )
    elif key in _ENCRYPTED_KEYS:
        await log_audit(
            db_pool,
            action="secret.rotate",
            resource=key,
            user_id=str(caller_user_id) if caller_user_id is not None else None,
        )

    # Emit a config-change event for audit trail. Best-effort.
    try:
        await _log_event(
            pool=db_pool,
            level="info",
            category="config",
            source="settings",
            message="llm/route_changed" if route_role is not None else "setting_changed",
            context=(
                {"key": key, "role": route_role}
                if route_role is not None
                else {
                    "key": key,
                    "new_value": str(display_value),
                    **(
                        {"schedule_apply_warnings": result.schedule_apply_warnings}
                        if result.schedule_apply_warnings
                        else {}
                    ),
                }
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug("config event log_event failed (non-fatal)", exc_info=True)

    return ConfigEntry(key=key, value=display_value)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@router.get("/analytics/papers-by-source", response_model=list[PapersBySourceItem])
@limiter.limit("60/minute")
async def papers_by_source(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[dict]:
    """Return paper counts grouped by source type."""
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        return await fetch_papers_by_source(conn, user_id, is_admin=is_admin)


@router.get("/analytics/papers-by-status", response_model=list[PapersByStatusItem])
@limiter.limit("60/minute")
async def papers_by_status(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[dict]:
    """Return paper counts grouped by user-state status."""
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        return await fetch_papers_by_status(conn, user_id, is_admin=is_admin)


# ---------------------------------------------------------------------------
# Cloud LLM Providers
# ---------------------------------------------------------------------------


@router.get("/providers", response_model=list[ProviderMetadataResponse])
@limiter.limit("30/minute")
async def list_providers(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
    _admin: None = Depends(require_admin),
    caller_user_id: int = Depends(current_user_id_strict),
) -> list[ProviderMetadataResponse]:
    """Return supported provider metadata without exposing stored secrets."""
    rows: list[ProviderMetadataResponse] = []
    for provider in PROVIDER_REGISTRY:
        configured = await cloud_provider_key_present(provider.id, db_pool)
        base_url_configured = False
        if provider.base_url_config_key is not None:
            base_url_configured = await get_provider_base_url(provider.id, db_pool) is not None
        rows.append(
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
                configured=configured,
                base_url_configured=base_url_configured,
                supports_assignment=provider.supports_assignment,
                dashboard_url=provider.dashboard_url,
                account_capability=provider.account_capability,
            )
        )
    return rows


@router.get("/providers/{provider}/account", response_model=ProviderAccountResponse)
@limiter.limit("5/minute")
async def get_provider_account(
    request: Request,
    provider: str,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
    _admin: None = Depends(require_admin),
) -> ProviderAccountResponse:
    """Return only the account fields that this provider's API key can safely expose."""
    try:
        snapshot = await fetch_provider_account(provider, db_pool=db_pool)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="unsupported provider") from exc
    return ProviderAccountResponse(
        provider=snapshot.provider,
        capability=snapshot.capability,
        data=snapshot.data,
        error_code=snapshot.error_code,
    )


@router.post("/providers/{provider}/test", response_model=ProviderTestResponse)
@limiter.limit("5/minute")
async def test_provider(
    request: Request,
    provider: str,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
    _admin: None = Depends(require_admin),
    caller_user_id: int = Depends(current_user_id_strict),
) -> ProviderTestResponse:
    """Probe a cloud LLM provider with its stored API key to verify connectivity."""
    if provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="unsupported provider")

    provider_definition = provider_for_id(provider)
    config_key = provider_definition.api_key_config_key
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        row = await _fetch_effective_config_row(conn, config_key, caller_user_id, is_admin=is_admin)

    api_key: str | None = None
    if row is not None:
        try:
            api_key = resolve_secret_row(row)
        except Exception:
            api_key = None

    if not api_key:
        result = ProviderTestResult(ok=False, error="no api key configured")
    else:
        base_url = None
        if provider_definition.base_url_config_key is not None:
            base_url = await get_provider_base_url(provider, db_pool)
        if provider_definition.base_url_config_key is not None and base_url is None:
            result = ProviderTestResult(ok=False, error="no base URL configured")
        else:
            result = await test_provider_connectivity(provider, api_key, base_url=base_url)
    try:
        await _log_event(
            pool=db_pool,
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
    except Exception:  # noqa: BLE001
        logger.debug("provider connection event log_event failed (non-fatal)", exc_info=True)
    return ProviderTestResponse(ok=result.ok, error=result.error)


def _provider_test_code(error: str | None, ok: bool) -> str:
    """Map provider-test outcomes to stable event codes without logging provider text."""
    if ok:
        return "ok"
    if error is None:
        return "connection_failed"
    return {
        "no api key configured": "api_key_unavailable",
        "no base URL configured": "base_url_unavailable",
        "unsupported provider": "unsupported_provider",
    }.get(error, "connection_failed")


# ---------------------------------------------------------------------------
# GDPR data export
# ---------------------------------------------------------------------------


@router.get("/me/export")
@limiter.limit("5/minute")
async def export_my_data(
    request: Request,
    caller_user_id: int = Depends(current_user_id_strict),
) -> Any:
    """Stream a ZIP of the calling user's structured data (GDPR export).

    JSON dumps only — no PDF binaries, no embeddings. Scoped to
    ``current_user_id_strict`` so a caller can never read another user's data.
    """
    pool = request.app.state.db_pool

    data = await build_export_zip(pool, caller_user_id)
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="jarvis-data-export.zip"'},
    )
