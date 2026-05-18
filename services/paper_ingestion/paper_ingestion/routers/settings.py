"""Settings, nudges, and source management endpoints.

HTTP transport layer only — all business logic lives in
``paper_ingestion.services.settings_service``.

Route handlers are thin:  parse → auth check → delegate → return.

Symbols that tests patch via ``paper_ingestion.routers.settings.*`` are
re-exported here so existing patch-paths remain stable after the extraction.
"""

import logging
from typing import Any

import asyncpg
import httpx  # noqa: F401 — in namespace so tests can patch routers.settings.httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from jarvis_common import dynamic_update, log_audit
from jarvis_common.auth import current_user_id_strict, require_admin, verify_api_key
from jarvis_common.crypto import (
    decrypt_secret,  # noqa: F401 — in namespace for test patch-path compat
    encrypt_secret,  # noqa: F401 — tests patch routers.settings.encrypt_secret
    mask_secret,  # noqa: F401 — imported by downstream (service uses it; keep for compat)
    resolve_secret_row,
)
from jarvis_common.event_log import log_event as _log_event  # noqa: F401 — tests patch this name
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, get_scheduler, limiter
from paper_ingestion.models import (
    ConfigEntry,
    NudgeResponse,
    NudgeUpdate,
    PapersBySourceItem,
    PapersByStatusItem,
    SourceResponse,
    SourceUpdate,
)

# update_litellm_model is passed to write_config so the router-module symbol is
# what tests monkeypatch.  ROLE_TO_ALIAS / reload_litellm are re-exported for
# any caller that previously imported them from this module.
from paper_ingestion.services.litellm_config import (
    ROLE_TO_ALIAS,  # noqa: F401
    reload_litellm,  # noqa: F401
    update_litellm_model,
)

# --- Symbols used by handler code ---
# --- Re-exports: symbols that existing tests import from this module ---
# Tests that do `from paper_ingestion.routers.settings import X` or
# `patch("paper_ingestion.routers.settings.X")` must still find X here.
from paper_ingestion.services.settings_service import (  # noqa: F401
    _ALLOWED_CONFIG_KEYS,
    _CONFIG_VALIDATORS,
    _ENCRYPTED_KEYS,
    _NUDGE_ALLOWED_COLUMNS,
    _NUDGE_JSONB_COLUMNS,
    _NUM_CTX_PATTERN,
    _SECRET_KEYS,
    _SOURCE_ALLOWED_COLUMNS,
    _SOURCE_JSONB_COLUMNS,
    _SUPPORTED_PROVIDERS,
    _THINKING_DISABLED_PATTERN,
    _ZOTERO_LIBRARY_SCOPE_KEYS,
    PERSONAL_KEYS,
    SYSTEM_KEYS,
    _classify_config_key,
    _fetch_effective_config_row,
    _is_allowed_config_key,
    _resolve_config_value,
    _validate_bool,
    _validate_cron,
    _validate_fsrs_learning_steps,
    _validate_fsrs_retention,
    _validate_group_id,
    _validate_l2_lambda,
    _validate_langfuse_dashboard_url,
    _validate_library_type,
    _validate_lookback_days,
    _validate_nonempty_str,
    _validate_optional_int,
    _validate_positive_int,
    _validate_pulse_weights,
    _validate_startup_grace_seconds,
    _validate_zotero_cron,
    _write_config_row,
    apply_fetch_interval,
    apply_pulse_cron,
    apply_zotero_cron,
    build_export_zip,
    cloud_provider_key_present,
    fetch_papers_by_source,
    fetch_papers_by_status,
    migrate_plaintext_secrets,
    reload_telegram_nudges,
    test_provider_connectivity,
    validate_model_assignment,
    write_config,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["settings"])


# ---------------------------------------------------------------------------
# Response models (router-local; not part of the service contract)
# ---------------------------------------------------------------------------


class ProviderTestResponse(BaseModel):
    ok: bool
    error: str | None = None


class ReorderRequest(BaseModel):
    source_types: list[str]


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
) -> list[ConfigEntry]:
    """Return all config entries.

    Browser users only receive personal settings unless they are admins.
    API-key-only callers preserve the legacy single-tenant view.
    """
    caller_user_id = await current_user_id_strict(request)
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
) -> ConfigEntry:
    if not _is_allowed_config_key(key):
        raise HTTPException(404, f"Config key '{key}' not found")
    if _classify_config_key(key) == "system" and _has_browser_session(request):
        await require_admin(request)
    caller_user_id = await current_user_id_strict(request)
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
) -> ConfigEntry:
    if not _is_allowed_config_key(key):
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")

    # System-scope keys require an admin browser session. API-key-only callers
    # (Telegram/cron/lifespan) never reach this endpoint for system keys — they
    # write directly to user_config via SQL, bypassing this gate.
    if _classify_config_key(key) == "system":
        await require_admin(request)
    caller_user_id = await current_user_id_strict(request)

    # Delegate the full write + side-effects to the service layer.
    # require_admin and audit logging stay here so patch paths on this module
    # remain stable (tests patch paper_ingestion.routers.settings.require_admin
    # and paper_ingestion.routers.settings._log_event).
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    ollama_url = get_paper_ingestion_settings().ollama_base_url
    http_client = request.app.state.http_client

    # Pass update_litellm_model from the router namespace so monkeypatching
    # ``paper_ingestion.routers.settings.update_litellm_model`` in tests still works.
    display_value = await write_config(
        db_pool=db_pool,
        scheduler=scheduler,
        http_client=http_client,
        ollama_url=ollama_url,
        key=key,
        value=body.value,
        caller_user_id=caller_user_id,
        update_litellm_model_fn=update_litellm_model,
    )

    if key in _ENCRYPTED_KEYS:
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
            message="setting_changed",
            context={"key": key, "new_value": str(display_value)},
        )
    except Exception:  # noqa: BLE001
        logger.debug("config event log_event failed (non-fatal)", exc_info=True)

    return ConfigEntry(key=key, value=display_value)


# ---------------------------------------------------------------------------
# Scheduled Nudges
# ---------------------------------------------------------------------------


@router.get("/nudges", response_model=list[NudgeResponse], dependencies=[Depends(require_admin)])
@limiter.limit("60/minute")
async def list_nudges(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[NudgeResponse]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM scheduled_nudges ORDER BY id")
    return [NudgeResponse(**dict(r)) for r in rows]


@router.put("/nudges/{nudge_id}", response_model=NudgeResponse)
@limiter.limit("30/minute")
async def update_nudge(
    request: Request,
    nudge_id: int,
    body: NudgeUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> NudgeResponse:
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM scheduled_nudges WHERE id = $1", nudge_id)
        if not existing:
            raise HTTPException(404, f"Nudge {nudge_id} not found")

        updates = body.model_dump(exclude_unset=True, include=_NUDGE_ALLOWED_COLUMNS)
        if not updates:
            return NudgeResponse(**dict(existing))

        if "cron_expression" in updates:
            try:
                _validate_zotero_cron(updates["cron_expression"])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        row = await dynamic_update(
            conn,
            "scheduled_nudges",
            nudge_id,
            updates,
            _NUDGE_ALLOWED_COLUMNS,
            jsonb_columns=_NUDGE_JSONB_COLUMNS,
        )

    # Best-effort: notify telegram_bot to reload its nudge jobs
    await reload_telegram_nudges()

    return NudgeResponse(**dict(row))


# ---------------------------------------------------------------------------
# Paper Sources
# ---------------------------------------------------------------------------


@router.get("/sources", response_model=list[SourceResponse])
@limiter.limit("60/minute")
async def list_sources(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[SourceResponse]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM paper_sources ORDER BY display_order ASC, id ASC")
    return [SourceResponse(**dict(r)) for r in rows]


@router.patch("/sources/reorder", response_model=list[SourceResponse])
@limiter.limit("10/minute")
async def reorder_sources(
    request: Request,
    body: ReorderRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> list[SourceResponse]:
    """Persist UI drag-and-drop order by assigning display_order = position index."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT source_type FROM paper_sources")
    existing = {r["source_type"] for r in rows}
    missing = set(body.source_types) - existing
    if missing:
        raise HTTPException(400, detail=f"Unknown sources: {sorted(missing)}")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for idx, stype in enumerate(body.source_types, start=1):
                await conn.execute(
                    "UPDATE paper_sources SET display_order = $1 WHERE source_type = $2",
                    idx,
                    stype,
                )
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM paper_sources ORDER BY display_order ASC, id ASC")
    return [SourceResponse(**dict(r)) for r in rows]


@router.put("/sources/{source_id}", response_model=SourceResponse)
@limiter.limit("30/minute")
async def update_source(
    request: Request,
    source_id: int,
    body: SourceUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _admin: None = Depends(require_admin),
) -> SourceResponse:
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM paper_sources WHERE id = $1", source_id)
        if not existing:
            raise HTTPException(404, f"Source {source_id} not found")

        updates = body.model_dump(exclude_unset=True, include=_SOURCE_ALLOWED_COLUMNS)
        if not updates:
            return SourceResponse(**dict(existing))

        row = await dynamic_update(
            conn,
            "paper_sources",
            source_id,
            updates,
            _SOURCE_ALLOWED_COLUMNS,
            jsonb_columns=_SOURCE_JSONB_COLUMNS,
        )
    return SourceResponse(**dict(row))


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@router.get("/analytics/papers-by-source", response_model=list[PapersBySourceItem])
@limiter.limit("60/minute")
async def papers_by_source(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """Return paper counts grouped by source type."""
    user_id = await current_user_id_strict(request)
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        return await fetch_papers_by_source(conn, user_id, is_admin=is_admin)


@router.get("/analytics/papers-by-status", response_model=list[PapersByStatusItem])
@limiter.limit("60/minute")
async def papers_by_status(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """Return paper counts grouped by user-state status."""
    user_id = await current_user_id_strict(request)
    is_admin = getattr(request.state, "user_role", None) == "admin"
    async with db_pool.acquire() as conn:
        return await fetch_papers_by_status(conn, user_id, is_admin=is_admin)


# ---------------------------------------------------------------------------
# Cloud LLM Provider Test
# ---------------------------------------------------------------------------


@router.post("/providers/{provider}/test", response_model=ProviderTestResponse)
@limiter.limit("5/minute")
async def test_provider(
    request: Request,
    provider: str,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    _: None = Depends(verify_api_key),
) -> ProviderTestResponse:
    """Probe a cloud LLM provider with its stored API key to verify connectivity."""
    if provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="unsupported provider")

    config_key = f"llm.{provider}.api_key"
    caller_user_id = await current_user_id_strict(request)
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
        return ProviderTestResponse(ok=False, error="no api key configured")

    result = await test_provider_connectivity(provider, api_key)
    return ProviderTestResponse(ok=result.ok, error=result.error)


# ---------------------------------------------------------------------------
# GDPR data export
# ---------------------------------------------------------------------------


@router.get("/me/export")
@limiter.limit("5/minute")
async def export_my_data(request: Request) -> Any:
    """Stream a ZIP of the calling user's structured data (GDPR export).

    JSON dumps only — no PDF binaries, no embeddings. Scoped to
    ``current_user_id_strict`` so a caller can never read another user's data.
    """
    caller_user_id = await current_user_id_strict(request)
    pool = request.app.state.db_pool

    data = await build_export_zip(pool, caller_user_id)
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="jarvis-export-user-{caller_user_id}.zip"'
            )
        },
    )
