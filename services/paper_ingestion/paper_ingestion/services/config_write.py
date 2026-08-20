"""Top-level write_config orchestration and LiteLLM runtime update helper."""

import asyncio
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
from jarvis_common.auth import (
    API_KEY_LOGIN_CONFIG_KEY,
    invalidate_api_key_login_cache,
)
from jarvis_common.config_metadata import (
    _ENCRYPTED_KEYS,
    _ZOTERO_LIBRARY_SCOPE_KEYS,
    ROLE_TO_ALIAS,
    _classify_config_key,
    _classify_litellm_runtime_key,
    _is_cloud_model_assignment,
)
from jarvis_common.config_store import _write_config_row
from jarvis_common.config_validators import (
    _CONFIG_VALIDATORS,
    _validate_bool,
    _validate_positive_int,
)
from jarvis_common.crypto import encrypt_secret, mask_secret
from jarvis_common.db_helpers import invalidate_effective_num_ctx_cache
from pydantic import BaseModel, model_serializer

from paper_ingestion.ingestion.embedding_config import EMBEDDING_MODEL_NAME
from paper_ingestion.integrations.zotero_client import (
    BBT_ALLOWED_PRIVATE_HOSTS_CONFIG_KEY,
    refresh_configured_private_hosts,
)
from paper_ingestion.services.model_assignment import validate_model_assignment
from paper_ingestion.services.model_lifecycle import (
    catalog_entry_for_model,
    detect_hardware,
    safe_num_ctx,
)
from paper_ingestion.services.scheduler_effects import (
    apply_fetch_interval,
    apply_pulse_cron,
)

__all__ = [
    "_fetch_system_config_values",
    "_apply_litellm_runtime_update",
    "ConfigWriteResult",
    "write_config",
]

logger = logging.getLogger(__name__)

# System row tracking model roles whose committed config LiteLLM has not
# accepted yet (litellm running without its admin DB, e.g. prisma migrate
# failed and the proxy degraded to DB-less mode). GET /api/system/models
# surfaces these roles as delivery="pending_restart" while the boot reconciler
# keeps retrying.
_DELIVERY_PENDING_KEY = "llm.delivery_pending"
_ZOTERO_POLL_RECONCILE_KEYS = frozenset(
    {
        "zotero.poll_cron",
        "zotero.poll_enabled",
        "zotero.api_key",
        "zotero.user_id",
        "zotero.library_type",
        "zotero.group_id",
    }
)

# Must stay at or above the largest caller's reserved response allowance.
_NUM_CTX_MIN = 2048
_NUM_CTX_HARD_CEILING = 262144


async def _validate_num_ctx_bounds(value: int, *, model_id: str | None) -> None:
    """Bound a context-window write to the assigned model and current hardware.

    Parameters
    ----------
    value : int
        Requested context-window size.
    model_id : str or None
        Assigned model identifier, when one is configured.

    Raises
    ------
    ValueError
        If ``value`` is below the usable floor or above the resolved safe bound.
    """
    if value < _NUM_CTX_MIN:
        raise ValueError(f"num_ctx must be at least {_NUM_CTX_MIN}")

    entry = catalog_entry_for_model(model_id) if model_id else None
    if entry is None:
        upper = _NUM_CTX_HARD_CEILING
    else:
        upper = entry.max_num_ctx if entry.max_num_ctx is not None else entry.context_tokens
        if entry.provider == "ollama":
            hardware = await asyncio.to_thread(detect_hardware)
            if hardware.vram_gb > 0.0:
                embed_entry = catalog_entry_for_model(EMBEDDING_MODEL_NAME)
                embed_reserve_gb = embed_entry.vram_gb if embed_entry is not None else 0.0
                upper = min(upper, safe_num_ctx(entry, hardware, embed_reserve_gb))
    if value > upper:
        raise ValueError(f"num_ctx must be at most {upper} for the assigned model on this hardware")


@dataclass(frozen=True)
class _ScheduleRuntime:
    """Explicit runtime context for scheduler side effects."""

    scheduler: Any
    app: Any | None = None


def _schedule_runtime(scheduler: Any, app: Any | None = None) -> _ScheduleRuntime:
    """Return scheduler side-effect context without widening helper signatures."""
    if isinstance(scheduler, _ScheduleRuntime):
        return scheduler
    return _ScheduleRuntime(scheduler=scheduler, app=app)


# A masked secret echo from GET /api/config: mask_secret() emits exactly '****'
# (secret shorter than 4 chars) or '****' + the last 4 chars. Re-submitting that
# value must be a no-op, never a write — otherwise the literal mask overwrites the
# stored secret (defense-in-depth; the in-product forms also guard via draft!=current).
_MASK_SENTINEL_RE = re.compile(r"^\*{4}(.{4})?$")


class ConfigWriteResult(BaseModel):
    """Return value of ``write_config`` carrying the display value and any scheduler warnings.

    ``schedule_apply_warnings`` is populated with the names of schedulers that failed to
    apply the new value live (e.g. ``["pulse_cron"]``).  The DB commit always stands; the
    startup reconciler re-reads the DB on boot so any inconsistency is transient.

    Serialises as just ``display_value`` so that existing callers that embed it in a
    ``ConfigEntry(value=result)`` field see backward-compatible JSON output (a plain
    scalar/value, not a ``{"display_value": …, "schedule_apply_warnings": […]}`` dict).
    Callers that need the warnings read ``.schedule_apply_warnings`` directly.
    """

    model_config = {"arbitrary_types_allowed": True}

    display_value: Any
    schedule_apply_warnings: list[str] = []
    litellm_delivery_roles: list[str] = []
    litellm_delivery_pending: bool | None = None
    effective_num_ctx_role: str | None = None
    effective_num_ctx_value: int | None = None

    @model_serializer
    def _serialize(self) -> Any:  # noqa: PLR6301
        return self.display_value


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


async def _apply_schedules(
    *,
    db_pool: asyncpg.Pool,
    scheduler: Any,
    key: str,
    value: Any,
    cron_ctx: tuple[str | None, int | None] | tuple[str | None, int | None, bool],
) -> list[str]:
    """Apply scheduler side-effects for cron/interval keys; return names of any that failed.

    *cron_ctx* carries ``(old_cron, row_user_id)`` and, for non-persisting
    delivery calls, an optional false persistence flag.

    Each apply is wrapped in a broad try/except so a scheduler failure never blocks the
    response (the DB commit already stands).  The startup reconciler re-reads cron from DB
    on boot, so any live inconsistency is transient.
    """
    old_cron, row_user_id, *persistence = cron_ctx
    persist = persistence[0] if persistence else True
    runtime = _schedule_runtime(scheduler)
    failed: list[str] = []

    if key == "pulse.cron":
        try:
            await apply_pulse_cron(
                db_pool=db_pool,
                scheduler=runtime.scheduler,
                new_cron=value,
                old_cron=old_cron,
                rollback_persisted=persist,
            )
        except Exception as exc:
            logger.warning(
                "pulse_cron scheduler apply failed (value saved): %s", exc, exc_info=True
            )
            failed.append("pulse_cron")

    if key in _ZOTERO_POLL_RECONCILE_KEYS and row_user_id is not None and runtime.app is not None:
        try:
            from importlib import import_module  # noqa: PLC0415

            scheduler_module = import_module("paper_ingestion.scheduler")

            await scheduler_module.reconcile_zotero_poll_job(
                scheduler=runtime.scheduler,
                app=runtime.app,
                db_pool=db_pool,
                user_id=row_user_id,
            )
        except Exception as exc:
            logger.warning(
                "zotero_poll scheduler reconcile failed (value saved): %s", exc, exc_info=True
            )
            failed.append("zotero_poll")

    if key == "automation.fetch_interval_hours":
        try:
            apply_fetch_interval(scheduler=runtime.scheduler, hours=value)
        except Exception as exc:
            logger.warning(
                "fetch_interval scheduler apply failed (value saved): %s", exc, exc_info=True
            )
            failed.append("fetch_interval")

    return failed


async def _clear_zotero_library_cache(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    *,
    user_id: int,
) -> None:
    """Clear remote Zotero identifiers after this user's library identity changes.

    Link rows and their analysis scheduling history remain intact. Only values
    that identify objects in the previous remote library are invalidated.
    """
    await conn.execute(
        """UPDATE paper_user_zotero_links
              SET zotero_item_key = NULL,
                  zotero_citation_key = NULL,
                  zotero_attachment_key = NULL,
                  zotero_last_pushed_at = NULL,
                  updated_at = NOW()
            WHERE user_id = $1
              AND (zotero_item_key IS NOT NULL
                   OR zotero_citation_key IS NOT NULL
                   OR zotero_attachment_key IS NOT NULL
                   OR zotero_last_pushed_at IS NOT NULL)""",
        user_id,
    )
    await conn.execute(
        "SELECT learning.clear_zotero_collection_keys_v1($1)",
        user_id,
    )
    await conn.execute(
        "SELECT platform.set_research_config_v1($1, 'zotero.last_library_version', NULL, 'delete')",
        user_id,
    )


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
    app: Any | None = None,
    persist: bool = True,
    apply_effects: bool = True,
    zotero_scope_changed: bool = False,
) -> "ConfigWriteResult":
    """Validate a configuration command and apply Research-owned effects.

    Parameters
    ----------
    db_pool:
        Research database pool used by validators and local effects.
    scheduler:
        Scheduler receiving cron changes.
    http_client:
        Client used for model validation and provider effects.
    ollama_url:
        Base URL of the configured Ollama service.
    key, value:
        Validated configuration key and desired value.
    caller_user_id:
        User owning a personal value, or ``None`` for system values.
    update_litellm_model_fn:
        Optional LiteLLM delivery adapter.
    app:
        Application state used to resolve the active scheduler.
    persist:
        Persist through the Platform capability when ``True``. Platform
        delivery commands set this to ``False`` because Platform has already
        committed the authoritative value and durable delivery record.
    apply_effects:
        Apply provider and scheduler effects when ``True``. Validation-only
        commands set this to ``False``.
    zotero_scope_changed:
        Whether Platform observed a material Zotero library identity change
        before persisting the desired value.

    Returns
    -------
    ConfigWriteResult
        Display-safe value and any non-fatal scheduler warnings.

    Raises
    ------
    fastapi.HTTPException
        If validation, model assignment, or a required LiteLLM update fails.

    Notes
    -----
    Scheduler failures are returned as warnings. LiteLLM writes remain
    fail-closed except for its explicit database-unavailable state, which is
    recorded as pending for the existing boot reconciler.
    """
    from fastapi import HTTPException  # noqa: PLC0415

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

    # The multi-tenant API-key-login toggle is owned by jarvis_common.auth (the
    # read side); its bool validator is not in _CONFIG_VALIDATORS, so guard it here.
    if key == API_KEY_LOGIN_CONFIG_KEY:
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

    # Encrypted keys: a re-submitted mask sentinel ('****' / '****' + last4) is a
    # no-op — never encrypt the masked echo over the real secret.
    if key in _ENCRYPTED_KEYS and _MASK_SENTINEL_RE.fullmatch(str(value)):
        return ConfigWriteResult(display_value=mask_secret(str(value)))

    # Model assignment check
    if key in ROLE_TO_ALIAS:
        try:
            await validate_model_assignment(
                http_client=http_client,
                ollama_url=ollama_url,
                key=key,
                model_id=str(value),
                db_pool=db_pool,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not apply_effects:
        display_value = mask_secret(str(value)) if key in _ENCRYPTED_KEYS else value
        return ConfigWriteResult(display_value=display_value)

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
        pending_state = (outcome == "pending_restart") if roles else None
        effective_role = (
            runtime_key["role"]
            if runtime_key["kind"] == "num_ctx" and outcome == "applied"
            else None
        )
        if not persist:
            if effective_role is not None:
                invalidate_effective_num_ctx_cache()
            return ConfigWriteResult(
                display_value=value,
                litellm_delivery_roles=sorted(roles),
                litellm_delivery_pending=pending_state,
                effective_num_ctx_role=effective_role,
                effective_num_ctx_value=int(value) if effective_role is not None else None,
            )
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
        return ConfigWriteResult(display_value=value)

    # Read old cron values before overwriting (for rollback)
    old_pulse_cron: str | None = None
    if persist and key == "pulse.cron":
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = 'pulse.cron' AND user_id IS NULL"
            )
        if row is not None and isinstance(row["value"], str):
            old_pulse_cron = row["value"]

    # DB write first — DB is the source of truth. A material Zotero library
    # identity change invalidates its remote-object cache in the same
    # transaction, so readers can never observe new credentials with old keys.
    if persist:
        async with db_pool.acquire() as conn, conn.transaction():
            previous_scope_row = None
            if key in _ZOTERO_LIBRARY_SCOPE_KEYS and row_user_id is not None:
                previous_scope_row = await conn.fetchrow(
                    """SELECT value FROM user_config
                        WHERE user_id = $1 AND key = $2
                        FOR UPDATE""",
                    row_user_id,
                    key,
                )
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
            if (
                key in _ZOTERO_LIBRARY_SCOPE_KEYS
                and row_user_id is not None
                and (previous_scope_row is None or previous_scope_row["value"] != value)
            ):
                await _clear_zotero_library_cache(conn, user_id=row_user_id)
    elif key in _ZOTERO_LIBRARY_SCOPE_KEYS and row_user_id is not None and zotero_scope_changed:
        async with db_pool.acquire() as conn, conn.transaction():
            await _clear_zotero_library_cache(conn, user_id=row_user_id)

    # Drop the in-process API-key-login cache so the next mint sees the new
    # value immediately (the flag only widens access — a flip OFF must apply now).
    if key == API_KEY_LOGIN_CONFIG_KEY:
        invalidate_api_key_login_cache()

    # Scheduler side-effects — failures are caught, logged, and surfaced as warnings.
    _old_cron = old_pulse_cron if key == "pulse.cron" else None
    schedule_warnings = await _apply_schedules(
        db_pool=db_pool,
        scheduler=_schedule_runtime(scheduler, app),
        key=key,
        value=value,
        cron_ctx=(_old_cron, row_user_id, persist),
    )

    # Better BibTeX host allowlist refresh — the client caches it, so without
    # this the saved hosts would not take effect until the next restart.
    if key == BBT_ALLOWED_PRIVATE_HOSTS_CONFIG_KEY:
        await refresh_configured_private_hosts(db_pool)

    display_value = mask_secret(str(value)) if key in _ENCRYPTED_KEYS else value
    return ConfigWriteResult(display_value=display_value, schedule_apply_warnings=schedule_warnings)
