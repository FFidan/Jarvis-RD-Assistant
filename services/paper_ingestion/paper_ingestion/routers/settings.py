"""Settings, nudges, and source management endpoints."""

import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import dynamic_update
from jarvis_common.auth import verify_api_key
from jarvis_common.crypto import decrypt_secret, encrypt_secret, mask_secret
from jarvis_common.settings import get_core_settings, get_telegram_settings
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
from paper_ingestion.services.litellm_config import (
    ROLE_TO_ALIAS,
    reload_litellm,
    update_litellm_model,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["settings"])


class ProviderTestResponse(BaseModel):
    ok: bool
    error: str | None = None


_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "llm.smart_model",
        "llm.fast_model",
        "llm.embed_model",
        "ui.page_size",
        "ingestion.max_papers_per_run",
        "ingestion.chunk_size",
        "paper.max_daily",
        "paper.auto_generate_cards",
        # FSRS
        "fsrs.desired_retention",
        "fsrs.learning_steps",
        # User preferences
        "user.timezone",
        # Recommendation engine
        "recommendation.liked_weight",
        "recommendation.project_weight",
        "recommendation.enabled",
        # Pulse (overnight deck subsystem)
        "pulse.enabled",
        "pulse.cron",
        "pulse.deck_size",
        "pulse.stage2_top_k",
        "pulse.weights",
        # Setup wizard
        "setup.completed",
        "telegram.owner_chat_id",
        # Zotero integration
        "zotero.api_key",
        "zotero.user_id",
        "zotero.library_type",
        "zotero.poll_enabled",
        "zotero.poll_cron",
        "zotero.auto_push_on_star",
        # Cloud LLM provider keys
        "llm.anthropic.api_key",
        "llm.openai.api_key",
        "llm.google.api_key",
    }
)

_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "zotero.api_key",
        "llm.anthropic.api_key",
        "llm.openai.api_key",
        "llm.google.api_key",
    }
)

_ENCRYPTED_KEYS: frozenset[str] = frozenset(
    {
        "llm.anthropic.api_key",
        "llm.openai.api_key",
        "llm.google.api_key",
        "zotero.api_key",
    }
)

_NUDGE_ALLOWED_COLUMNS: set[str] = {"cron_expression", "enabled"}
_NUDGE_JSONB_COLUMNS: frozenset[str] = frozenset()

_SOURCE_ALLOWED_COLUMNS: set[str] = {"enabled", "priority", "config", "display_order"}
_SOURCE_JSONB_COLUMNS: frozenset[str] = frozenset({"config"})


# --- Config key validators ---

_PULSE_WEIGHT_KEYS = frozenset(
    {
        "embedding",
        "topic",
        "llm_relevance",
        "llm_novelty",
        "author_bonus",
        "recency",
        "citation_pagerank",
        "citation_count",
        "citation_adamic_adar",
        "classifier",
    }
)
_PULSE_REQUIRED_WEIGHT_KEYS = frozenset(
    {"embedding", "topic", "llm_relevance", "llm_novelty", "author_bonus", "recency"}
)


def _validate_cron(v: Any) -> None:
    if not isinstance(v, str):
        raise ValueError("pulse.cron must be a string")
    try:
        CronTrigger.from_crontab(v)
    except Exception as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc


def _validate_pulse_weights(v: Any) -> None:
    if not isinstance(v, dict):
        raise ValueError("pulse.weights must be a dict")
    keys = set(v.keys())
    if not _PULSE_REQUIRED_WEIGHT_KEYS.issubset(keys) or not keys.issubset(_PULSE_WEIGHT_KEYS):
        raise ValueError(
            "pulse.weights must include the core keys and only known optional keys: "
            f"{sorted(_PULSE_WEIGHT_KEYS)}"
        )
    for k, val in v.items():
        if not isinstance(val, int | float) or isinstance(val, bool) or not (0 <= val <= 1):
            raise ValueError(f"pulse.weights.{k} must be a float between 0 and 1")


def _validate_positive_int(v: Any) -> None:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ValueError("value must be a positive integer")


def _validate_bool(v: Any) -> None:
    if not isinstance(v, bool):
        raise ValueError("value must be a boolean")


def _validate_optional_int(v: Any) -> None:
    if v is None:
        return
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("value must be an integer or null")


def _validate_nonempty_str(v: Any) -> None:
    if not isinstance(v, str) or not v.strip():
        raise ValueError("value must be a non-empty string")


def _validate_library_type(v: Any) -> None:
    if v not in ("user", "group"):
        raise ValueError("zotero.library_type must be 'user' or 'group'")


def _validate_zotero_cron(v: Any) -> None:
    if not isinstance(v, str):
        raise ValueError("zotero.poll_cron must be a string")
    try:
        CronTrigger.from_crontab(v)
    except Exception as exc:
        raise ValueError(f"invalid cron expression: {exc}") from exc


_CONFIG_VALIDATORS: dict[str, Callable[[Any], None]] = {
    "pulse.cron": _validate_cron,
    "pulse.weights": _validate_pulse_weights,
    "pulse.deck_size": _validate_positive_int,
    "pulse.stage2_top_k": _validate_positive_int,
    "pulse.enabled": _validate_bool,
    "setup.completed": _validate_bool,
    "telegram.owner_chat_id": _validate_optional_int,
    # Zotero
    "zotero.api_key": _validate_nonempty_str,
    "zotero.user_id": _validate_nonempty_str,
    "zotero.library_type": _validate_library_type,
    "zotero.poll_enabled": _validate_bool,
    "zotero.poll_cron": _validate_zotero_cron,
    "zotero.auto_push_on_star": _validate_bool,
    # Cloud LLM provider keys
    "llm.anthropic.api_key": _validate_nonempty_str,
    "llm.openai.api_key": _validate_nonempty_str,
    "llm.google.api_key": _validate_nonempty_str,
}


# --- User Config ---


def _resolve_config_value(key: str, row: dict) -> Any:
    """Return the display value for a config row, applying masking / decryption."""
    if key in _ENCRYPTED_KEYS:
        enc = row.get("encrypted_value")
        if enc is not None:
            # Decrypt then mask — never expose plaintext over the API
            plaintext = decrypt_secret(enc.decode("ascii"))
            return mask_secret(plaintext)
        raw = row.get("value")
        if raw is not None:
            # Legacy plaintext row: mask without decrypting
            return mask_secret(str(raw))
        return None
    if key in _SECRET_KEYS:
        raw = row.get("value")
        return "****" if raw is not None else None
    return row.get("value")


@router.get("/config", response_model=list[ConfigEntry])
@limiter.limit("60/minute")
async def list_config(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[ConfigEntry]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value, encrypted_value FROM user_config ORDER BY key")
    return [ConfigEntry(key=r["key"], value=_resolve_config_value(r["key"], r)) for r in rows]


@router.get("/config/{key}")
@limiter.limit("60/minute")
async def get_config(
    request: Request,
    key: str,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> ConfigEntry:
    if key not in _ALLOWED_CONFIG_KEYS:
        raise HTTPException(404, f"Config key '{key}' not found")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT key, value, encrypted_value FROM user_config WHERE key = $1", key
        )
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
    if key not in _ALLOWED_CONFIG_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown config key: {key!r}")
    if key in ROLE_TO_ALIAS:
        if not isinstance(body.value, str):
            raise HTTPException(
                status_code=400,
                detail=f"Model name must be a string, got {type(body.value).__name__}",
            )
    validator = _CONFIG_VALIDATORS.get(key)
    if validator is not None:
        try:
            validator(body.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # For pulse.cron: read the current value before overwriting so we can roll back.
    old_pulse_cron: str | None = None
    if key == "pulse.cron":
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key = 'pulse.cron'")
        if row is not None and isinstance(row["value"], str):
            old_pulse_cron = row["value"]

    # Pass body.value directly — asyncpg's JSONB codec (registered via init_pg_connection)
    # handles JSON encoding. Wrapping with json.dumps() would double-encode the value,
    # storing e.g. '"0 4 * * *"' instead of '"0 4 * * *"' in JSONB. (WEB-C01)
    if key in _ENCRYPTED_KEYS:
        # Encrypt the secret and store in encrypted_value; clear plaintext value column.
        ciphertext_bytes = encrypt_secret(str(body.value)).encode("ascii")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_config (key, value, encrypted_value)
                VALUES ($1, NULL, $2)
                ON CONFLICT (key) DO UPDATE
                    SET value = NULL, encrypted_value = $2, updated_at = NOW()""",
                key,
                ciphertext_bytes,
            )
    else:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_config (key, value) VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE SET value = $2::jsonb, updated_at = NOW()""",
                key,
                body.value,
            )
    if key in ROLE_TO_ALIAS:
        from paper_ingestion.services.litellm_config import _config_lock

        try:
            async with _config_lock:
                updated = await update_litellm_model(key, body.value, db_pool=db_pool)
        except (ValueError, RuntimeError) as exc:
            # SEC-002: model name validation failure or read-only config mount
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated:
            await reload_litellm()
    if key == "pulse.cron":
        try:
            if scheduler is not None:
                scheduler.reschedule_job(
                    "pulse_overnight",
                    trigger=CronTrigger.from_crontab(body.value),
                )
                logger.info("pulse_overnight rescheduled live (cron=%s)", body.value)

                # Bounds check: next_run_time must be within [now, now+366d].
                # A malformed or adversarial cron could schedule a run in the past
                # or arbitrarily far in the future.
                job = scheduler.get_job("pulse_overnight")
                now = datetime.now(UTC)
                next_run = job.next_run_time if job is not None else None
                if next_run is None or not (now <= next_run <= now + timedelta(days=366)):
                    logger.error(
                        "pulse_overnight reschedule produced invalid next_run_time=%s"
                        " for cron=%s; reverting",
                        next_run,
                        body.value,
                    )
                    # Roll back DB to the old cron value.
                    _rollback_sql = (
                        "INSERT INTO user_config (key, value) VALUES ('pulse.cron', $1::jsonb)"
                        " ON CONFLICT (key) DO UPDATE SET value = $1::jsonb, updated_at = NOW()"
                    )
                    async with db_pool.acquire() as conn:
                        if old_pulse_cron is not None:
                            await conn.execute(_rollback_sql, old_pulse_cron)
                        else:
                            await conn.execute("DELETE FROM user_config WHERE key = 'pulse.cron'")
                    # Revert the live scheduler trigger.
                    with contextlib.suppress(Exception):
                        if old_pulse_cron is not None:
                            scheduler.reschedule_job(
                                "pulse_overnight",
                                trigger=CronTrigger.from_crontab(old_pulse_cron),
                            )
                    raise HTTPException(
                        status_code=400,
                        detail="Cron expression produced an invalid next run time"
                        " (must be within the next 366 days)",
                    )
        except HTTPException:
            raise
        except Exception:
            logger.warning(
                "pulse_overnight live reschedule failed (job may not exist yet)",
                exc_info=True,
            )
    if key == "zotero.poll_cron":
        try:
            if scheduler is not None:
                scheduler.reschedule_job(
                    "zotero_library_sync",
                    trigger=CronTrigger.from_crontab(body.value),
                )
                logger.info("zotero_library_sync rescheduled live (cron=%s)", body.value)
        except Exception:
            logger.warning(
                "zotero_library_sync live reschedule failed (job may not exist yet)",
                exc_info=True,
            )
    if key == "user.timezone":
        # Best-effort: notify telegram_bot to reload nudge jobs with the new timezone
        telegram_url = get_telegram_settings().url_or_none
        if not telegram_url:
            logger.debug("TELEGRAM_BOT_URL empty — skipping nudge reload")
        else:
            with contextlib.suppress(Exception):
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{telegram_url}/internal/reload-nudges",
                        headers={"X-API-Key": get_core_settings().jarvis_api_key or ""},
                        timeout=2.0,
                    )
    display_value = mask_secret(str(body.value)) if key in _ENCRYPTED_KEYS else body.value
    return ConfigEntry(key=key, value=display_value)


# --- Scheduled Nudges ---


@router.get("/nudges", response_model=list[NudgeResponse])
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
                CronTrigger.from_crontab(updates["cron_expression"])
            except Exception as exc:
                raise HTTPException(status_code=422, detail="invalid cron expression") from exc

        row = await dynamic_update(
            conn,
            "scheduled_nudges",
            nudge_id,
            updates,
            _NUDGE_ALLOWED_COLUMNS,
            jsonb_columns=_NUDGE_JSONB_COLUMNS,
        )

    # Best-effort: notify telegram_bot to reload its nudge jobs
    telegram_url = get_telegram_settings().url_or_none
    if not telegram_url:
        logger.debug("TELEGRAM_BOT_URL empty — skipping nudge reload")
    else:
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{telegram_url}/internal/reload-nudges",
                    headers={"X-API-Key": get_core_settings().jarvis_api_key or ""},
                    timeout=2.0,
                )

    return NudgeResponse(**dict(row))


# --- Paper Sources ---


class ReorderRequest(BaseModel):
    source_types: list[str]


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


# --- Analytics ---


@router.get("/analytics/papers-by-source", response_model=list[PapersBySourceItem])
@limiter.limit("60/minute")
async def papers_by_source(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """Return paper counts grouped by source type."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_type, COUNT(*) AS count"
            " FROM papers GROUP BY source_type ORDER BY count DESC"
        )
    return [{"source_type": r["source_type"], "count": r["count"]} for r in rows]


@router.get("/analytics/papers-by-status", response_model=list[PapersByStatusItem])
@limiter.limit("60/minute")
async def papers_by_status(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """Return paper counts grouped by user-state status."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(pus.status, 'new') AS status, COUNT(*) AS count
            FROM papers p
            LEFT JOIN paper_user_state pus ON p.id = pus.paper_id
            GROUP BY COALESCE(pus.status, 'new')
            ORDER BY count DESC
            """
        )
    return [{"status": r["status"], "count": r["count"]} for r in rows]


# --- Cloud LLM Provider Test ---

_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai", "google"})


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
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config WHERE key = $1", config_key
        )

    api_key: str | None = None
    if row is not None:
        enc = row.get("encrypted_value")
        if enc is not None:
            try:
                api_key = decrypt_secret(enc.decode("ascii"))
            except Exception:
                api_key = None
        elif row.get("value") is not None:
            # Legacy plaintext row
            api_key = str(row["value"])

    if not api_key:
        return ProviderTestResponse(ok=False, error="no api key configured")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            if provider == "anthropic":
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages/count_tokens",
                    json={
                        "model": "claude-sonnet-4-5",
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                )
            elif provider == "openai":
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            else:  # google
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": api_key},
                )
    except httpx.HTTPError as exc:
        return ProviderTestResponse(ok=False, error=str(exc)[:200])

    if resp.is_success:
        return ProviderTestResponse(ok=True)
    return ProviderTestResponse(ok=False, error=f"provider returned HTTP {resp.status_code}")
