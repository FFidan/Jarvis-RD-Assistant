"""Top-level write_config orchestration and LiteLLM runtime update helper."""

import logging
from typing import Any

import asyncpg
import httpx
from jarvis_common.crypto import encrypt_secret, mask_secret

from paper_ingestion.services.config_db import _write_config_row
from paper_ingestion.services.config_metadata import (
    _ENCRYPTED_KEYS,
    _ZOTERO_LIBRARY_SCOPE_KEYS,
    _classify_config_key,
    _classify_litellm_runtime_key,
    _is_cloud_model_assignment,
)
from paper_ingestion.services.config_validators import (
    _CONFIG_VALIDATORS,
    _validate_bool,
    _validate_positive_int,
)
from paper_ingestion.services.model_assignment import (
    reload_telegram_nudges,
    validate_model_assignment,
)
from paper_ingestion.services.scheduler_effects import (
    apply_fetch_interval,
    apply_pulse_cron,
    apply_zotero_cron,
)

__all__ = [
    "_fetch_system_config_values",
    "_apply_litellm_runtime_update",
    "write_config",
]

logger = logging.getLogger(__name__)


async def _fetch_system_config_values(
    db_pool: asyncpg.Pool,
    keys: list[str],
) -> dict[str, Any]:
    """Return NULL-user config values for *keys*, keyed by config key."""
    if not keys:
        return {}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value FROM user_config WHERE key = ANY($1::text[]) AND user_id IS NULL",
            keys,
        )
    return {str(row["key"]): row["value"] for row in rows}


async def _apply_litellm_runtime_update(
    *,
    db_pool: asyncpg.Pool,
    key: str,
    value: Any,
    update_litellm_model_fn: Any,
) -> None:
    """Apply model and per-machine runtime settings to LiteLLM after the DB write."""
    from fastapi import HTTPException  # noqa: PLC0415

    from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
        ROLE_TO_ALIAS,
        _config_lock,
        reload_litellm,
        update_litellm_model,
    )

    runtime_key = _classify_litellm_runtime_key(key)
    if runtime_key is None:
        return

    _update_fn = (
        update_litellm_model_fn if update_litellm_model_fn is not None else update_litellm_model
    )

    try:
        async with _config_lock:
            updated = False
            kind = runtime_key["kind"]
            if kind == "model_role":
                updated = await _update_fn(key, str(value), db_pool=db_pool)
            elif kind == "num_ctx":
                role_key = runtime_key["role_key"]
                model_values = await _fetch_system_config_values(db_pool, [role_key])
                model_id = model_values.get(role_key)
                if model_id is None:
                    raise RuntimeError(f"No model is assigned for {role_key}")
                model_id_str = str(model_id)
                if _is_cloud_model_assignment(model_id_str):
                    return
                updated = await _update_fn(
                    role_key,
                    model_id_str,
                    db_pool=db_pool,
                    machine_id=runtime_key["machine_id"],
                    num_ctx=value,
                )
                if not updated:
                    raise RuntimeError(
                        f"LiteLLM alias {role_key} was not updated for {kind} on "
                        f"{runtime_key['machine_id']}"
                    )
            elif kind == "thinking_disabled":
                model_id = runtime_key["model_id"]
                assignments = await _fetch_system_config_values(db_pool, sorted(ROLE_TO_ALIAS))
                for role_key, assigned_model in assignments.items():
                    if str(assigned_model) != model_id:
                        continue
                    alias_updated = await _update_fn(
                        role_key,
                        str(assigned_model),
                        db_pool=db_pool,
                        machine_id=runtime_key["machine_id"],
                        thinking_disabled=value,
                    )
                    updated = bool(alias_updated) or updated

            if updated:
                reloaded = await reload_litellm()
                if not reloaded:
                    raise RuntimeError("LiteLLM accepted the alias update but reload failed")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def write_config(
    *,
    db_pool: asyncpg.Pool,
    scheduler: Any,
    http_client: httpx.AsyncClient,
    ollama_url: str,
    key: str,
    value: Any,
    caller_user_id: int | None,
    update_litellm_model_fn: Any = None,
) -> Any:
    """Persist a config value and apply all related side-effects.

    This is the core of what was previously the ``set_config`` handler body.
    Returns the display value (masked if the key is an encrypted secret).

    Raises ``fastapi.HTTPException`` on validation failure, model-assignment
    rejection, LiteLLM update failure, or scheduler rollback.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS  # noqa: PLC0415

    runtime_key = _classify_litellm_runtime_key(key)

    # Validate the value
    if runtime_key is not None and runtime_key["kind"] == "num_ctx":
        try:
            _validate_positive_int(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif runtime_key is not None and runtime_key["kind"] == "thinking_disabled":
        try:
            _validate_bool(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    validator = _CONFIG_VALIDATORS.get(key)
    if validator is not None:
        try:
            validator(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    row_user_id = caller_user_id if _classify_config_key(key) == "personal" else None

    # Model assignment check
    if key in ROLE_TO_ALIAS:
        await validate_model_assignment(
            http_client=http_client,
            ollama_url=ollama_url,
            key=key,
            model_id=str(value),
            db_pool=db_pool,
        )

    # Read old cron values before overwriting (for rollback)
    old_pulse_cron: str | None = None
    if key == "pulse.cron":
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = 'pulse.cron' AND user_id IS NULL"
            )
        if row is not None and isinstance(row["value"], str):
            old_pulse_cron = row["value"]

    old_zotero_poll_cron: str | None = None
    if key == "zotero.poll_cron":
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config"
                " WHERE key = 'zotero.poll_cron' AND user_id IS NOT DISTINCT FROM $1",
                row_user_id,
            )
        if row is not None and isinstance(row["value"], str):
            old_zotero_poll_cron = row["value"]

    # DB write first — DB is the source of truth.
    # Single acquire covers both encrypted and non-encrypted paths (acquire-collapse DRY).
    async with db_pool.acquire() as conn:
        if key in _ENCRYPTED_KEYS:
            ciphertext_bytes = encrypt_secret(str(value)).encode("ascii")
            await _write_config_row(
                conn,
                user_id=row_user_id,
                key=key,
                value=None,
                encrypted_value=ciphertext_bytes,
            )
        else:
            await _write_config_row(conn, user_id=row_user_id, key=key, value=value)

    # LiteLLM runtime update after DB commit; on failure the DB write stays
    # committed (LiteLLM reconciles from DB on reload).
    # The ``update_litellm_model_fn`` parameter lets callers (i.e. the router)
    # pass a monkeypatched reference so test patches remain effective.
    await _apply_litellm_runtime_update(
        db_pool=db_pool,
        key=key,
        value=value,
        update_litellm_model_fn=update_litellm_model_fn,
    )

    # Scheduler side-effects
    if key == "pulse.cron":
        await apply_pulse_cron(
            db_pool=db_pool,
            scheduler=scheduler,
            new_cron=value,
            old_cron=old_pulse_cron,
        )

    if key == "zotero.poll_cron":
        await apply_zotero_cron(
            db_pool=db_pool,
            scheduler=scheduler,
            new_cron=value,
            old_cron=old_zotero_poll_cron,
            row_user_id=row_user_id,
        )

    if key == "automation.fetch_interval_hours":
        hours = max(int(value), 1)
        apply_fetch_interval(scheduler=scheduler, hours=hours)

    # Zotero library-scope cache bust
    if key in _ZOTERO_LIBRARY_SCOPE_KEYS:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_config WHERE user_id IS NOT DISTINCT FROM $1"
                " AND key = 'zotero.last_library_version'",
                row_user_id,
            )

    # Telegram nudge reload on timezone change
    if key == "user.timezone":
        await reload_telegram_nudges()

    # Return display value (masked for secrets)
    return mask_secret(str(value)) if key in _ENCRYPTED_KEYS else value
