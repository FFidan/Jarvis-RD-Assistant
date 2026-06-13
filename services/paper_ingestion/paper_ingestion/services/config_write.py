"""Top-level write_config orchestration and LiteLLM runtime update helper."""

import logging
import re
import socket
from typing import Any

import asyncpg
import httpx
from jarvis_common.crypto import encrypt_secret, mask_secret
from jarvis_common.db_helpers import invalidate_effective_num_ctx_cache

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
    _validate_num_ctx_bounds,
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

# System row tracking model roles whose committed config LiteLLM has not
# accepted yet (litellm running without its admin DB, e.g. prisma migrate
# failed and the proxy degraded to DB-less mode). GET /api/system/models
# surfaces these roles as delivery="pending_restart" while the boot reconciler
# keeps retrying.
_DELIVERY_PENDING_KEY = "llm.delivery_pending"


def _is_litellm_no_db_error(detail: str) -> bool:
    """Match LiteLLM's /model/new | /model/delete failure when no admin DB is attached.

    A DB-less proxy rejects every model-management call with a body containing
    "No DB Connected" (/model/new wraps it as HTTP 500, /config-era paths used
    HTTP 400 — match the substring, not the status). Mirrors the boot
    reconciler's matcher in ``main``.
    """
    return "No DB Connected" in detail


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
) -> str:
    """Deliver a model-role / per-machine runtime change to LiteLLM.

    Returns ``"applied"`` when LiteLLM accepted the update, ``"skipped"`` when
    no delivery applies (non-runtime key, num_ctx on a cloud model, or nothing
    needed updating). Raises ``HTTPException(400)`` when delivery failed.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
        ROLE_TO_ALIAS,
        _config_lock,
        update_litellm_model,
    )

    runtime_key = _classify_litellm_runtime_key(key)
    if runtime_key is None:
        return "skipped"

    _update_fn = (
        update_litellm_model_fn if update_litellm_model_fn is not None else update_litellm_model
    )

    try:
        async with _config_lock:
            updated = False
            kind = runtime_key["kind"]
            if kind == "model_role":
                # machine_id lets the delivery pick up THIS machine's stored
                # num_ctx / thinking preferences for the new model. Deployments
                # are deployment-global in LiteLLM: last writer wins.
                updated = await _update_fn(
                    key,
                    str(value),
                    db_pool=db_pool,
                    machine_id=socket.gethostname(),
                )
            elif kind == "num_ctx":
                role_key = runtime_key["role_key"]
                model_values = await _fetch_system_config_values(db_pool, [role_key])
                model_id = model_values.get(role_key)
                if model_id is None:
                    raise RuntimeError(f"No model is assigned for {role_key}")
                model_id_str = str(model_id)
                if _is_cloud_model_assignment(model_id_str):
                    return "skipped"
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

            # No reload step: /model/new registers the deployment with the
            # router immediately (and persists it in LiteLLM's admin DB).
            return "applied" if updated else "skipped"
    except (ValueError, RuntimeError) as exc:
        full_msg = str(exc)
        logger.error("LiteLLM delivery failure: %s", full_msg)
        m = re.match(r"^(.*?HTTP\s+\d+)\s+.+$", full_msg, re.DOTALL)
        safe_detail = m.group(1) if m else full_msg
        raise HTTPException(status_code=400, detail=safe_detail) from exc


async def _pending_roles_for_runtime_key(
    db_pool: asyncpg.Pool,
    runtime_key: dict[str, str],
) -> set[str]:
    """Return the model roles (smart/fast/embed) whose LiteLLM routing *key* affects."""
    from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS  # noqa: PLC0415

    if runtime_key["kind"] in ("model_role", "num_ctx"):
        return {ROLE_TO_ALIAS[runtime_key["role_key"]]}
    # thinking_disabled: affects every role currently routed to this model.
    assignments = await _fetch_system_config_values(db_pool, sorted(ROLE_TO_ALIAS))
    return {
        ROLE_TO_ALIAS[role_key]
        for role_key, assigned in assignments.items()
        if str(assigned) == runtime_key["model_id"]
    }


async def _update_delivery_pending_roles_on_conn(
    conn: Any,
    *,
    roles: set[str],
    pending: bool,
) -> None:
    """Add or clear *roles* in the ``llm.delivery_pending`` system row, on *conn*.

    A successful delivery clears its roles (the alias now routes the committed
    model); a "No DB Connected" failure adds them so the UI can show
    "pending — not yet delivered" instead of a phantom "applied".

    Caller must hold an open transaction on *conn*: the ``FOR UPDATE`` locks the
    pending row so concurrent read-modify-write cycles serialize instead of
    silently dropping each other's roles.
    """
    if not roles:
        return
    row = await conn.fetchrow(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL FOR UPDATE",
        _DELIVERY_PENDING_KEY,
    )
    raw = row["value"] if row is not None else None
    current: set[str] = {str(r) for r in raw} if isinstance(raw, list) else set()
    updated = current | roles if pending else current - roles
    if updated == current and isinstance(raw, list):
        return
    if raw is None and not updated:
        return
    await _write_config_row(conn, user_id=None, key=_DELIVERY_PENDING_KEY, value=sorted(updated))


async def _update_delivery_pending_roles(
    db_pool: asyncpg.Pool,
    *,
    roles: set[str],
    pending: bool,
) -> None:
    """Pool-level wrapper for :func:`_update_delivery_pending_roles_on_conn`.

    Runs the read-modify-write in its own short transaction. Used where no
    surrounding transaction exists (e.g. boot-time rehydrate in ``main.py``).
    """
    if not roles:
        return
    async with db_pool.acquire() as conn, conn.transaction():
        await _update_delivery_pending_roles_on_conn(conn, roles=roles, pending=pending)


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
    rejection, LiteLLM update failure, or scheduler rollback. For LiteLLM
    runtime keys a delivery failure means NO row is written (fail-closed),
    except the "No DB Connected" case (LiteLLM degraded to DB-less mode),
    where the row is committed and the role is recorded in
    ``llm.delivery_pending`` while the boot reconciler keeps retrying.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS  # noqa: PLC0415

    runtime_key = _classify_litellm_runtime_key(key)

    # Validate the value
    if runtime_key is not None and runtime_key["kind"] == "num_ctx":
        try:
            _validate_positive_int(value)
            role_key = runtime_key["role_key"]
            assigned = (await _fetch_system_config_values(db_pool, [role_key])).get(role_key)
            await _validate_num_ctx_bounds(
                value, model_id=str(assigned) if assigned is not None else None
            )
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

    # LiteLLM runtime keys: deliver FIRST, commit the row only on success
    # (fail-closed). The old order (commit, then deliver) produced phantom
    # state — GET /api/system/models reported the new model while LiteLLM kept
    # routing the old one. Carve-out: when LiteLLM rejects the update solely
    # because no admin DB is attached ("No DB Connected", e.g. prisma migrate
    # failed and the proxy degraded to DB-less mode), the row IS committed and
    # the affected roles are marked delivery-pending so the UI says "pending —
    # not yet delivered" instead of failing the model picker entirely; the
    # boot reconciler retries until LiteLLM recovers. Runtime keys have no
    # scheduler / encryption side-effects, so this branch returns early.
    if runtime_key is not None:
        try:
            outcome = await _apply_litellm_runtime_update(
                db_pool=db_pool,
                key=key,
                value=value,
                update_litellm_model_fn=update_litellm_model_fn,
            )
        except HTTPException as exc:
            if exc.status_code == 400 and _is_litellm_no_db_error(str(exc.detail)):
                outcome = "pending_restart"
            else:
                # Real delivery failure: surface the 400 with NO row written,
                # so the UI snap-back matches the persisted state.
                raise
        # "Skipped" nuance: for a MODEL-class key (llm.*_model) "skipped" means
        # the alias already routes this exact model — truthfully applied, so
        # the role's pending marker must be CLEARED (re-selecting the routed
        # model must not leave a permanent false pill). For a num_ctx /
        # thinking key, "skipped" (e.g. num_ctx on a cloud model) says NOTHING
        # about model delivery, so pending must stay untouched.
        roles: set[str]
        if outcome == "skipped" and runtime_key["kind"] != "model_role":
            roles = set()
        else:
            roles = await _pending_roles_for_runtime_key(db_pool, runtime_key)
        # Row commit + pending bookkeeping happen in ONE transaction on ONE
        # connection: a pending-write failure rolls back the row commit (no
        # committed-row-without-marker window), and the FOR UPDATE inside the
        # helper serializes concurrent PUTs over the pending row.
        async with db_pool.acquire() as conn, conn.transaction():
            await _write_config_row(conn, user_id=row_user_id, key=key, value=value)
            if runtime_key["kind"] == "num_ctx" and outcome == "applied":
                # System-scoped effective-context row consumed by the prompt
                # budget readers (jarvis_common.effective_num_ctx). LiteLLM
                # deployments are deployment-global, so the budget follows the
                # last delivered value regardless of which machine wrote it.
                # Deliberately NOT an allowed public /api/config key — written
                # only here, on delivery success.
                await _write_config_row(
                    conn,
                    user_id=None,
                    key=f"llm.{runtime_key['role']}_num_ctx",
                    value=value,
                )
            await _update_delivery_pending_roles_on_conn(
                conn, roles=roles, pending=(outcome == "pending_restart")
            )
        if runtime_key["kind"] == "num_ctx" and outcome == "applied":
            invalidate_effective_num_ctx_cache()
        return value

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
