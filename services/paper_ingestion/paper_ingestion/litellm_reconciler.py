"""LiteLLM model reconciler.

The persistent background pass that keeps LiteLLM's admin DB routing the
settings-chosen smart/fast deployments. It is separate from application startup
so its state machine can be imported and tested at its owning boundary.
"""

import asyncio
import contextlib
import logging
import os
import socket
from typing import Any

from fastapi import FastAPI
from jarvis_common.config_metadata import ROLE_TO_ALIAS
from jarvis_common.db_helpers import _ALIAS_MODELS
from jarvis_common.maintenance import skip_for_maintenance

from paper_ingestion.constants import FAST_MODEL_DEFAULT, SMART_MODEL_DEFAULT
from paper_ingestion.ingestion.embedding_config import EMBEDDING_MODEL_NAME

# Log continuity: keep emitting under the historical "paper_ingestion.main"
# logger name (not __name__) so operators' log filters and the caplog tests
# (test_main_lifespan.py, at_level(logger="paper_ingestion.main")) stay valid.
logger = logging.getLogger("paper_ingestion.main")


# ---------------------------------------------------------------------------
# LiteLLM model reconciler — replaces the old boot-time "_rehydrate" pass.
#
# litellm/config.yaml deliberately carries NO smart/fast/smart-fallback aliases
# (a YAML alias can never be deleted at runtime and would STACK with its DB
# replacement). The reconciler therefore guarantees those deployments exist in
# LiteLLM's admin DB: a persistent background loop runs one pass ~every 30 s
# (matching LiteLLM's own DB-reconciler cadence), marking the affected roles in
# ``llm.delivery_pending`` while undelivered so the UI shows an honest
# "pending — applying automatically" pill instead of a phantom "applied".
# A LiteLLM stuck DB-less (prisma migrate failed) stays visibly pending while
# the loop keeps retrying — a loud, honest degradation instead of a silently
# LLM-dead deployment.
# ---------------------------------------------------------------------------

_LITELLM_RECONCILE_INTERVAL_SECONDS = 30.0

# Desired-model resolution, one-way precedence: user_config (Settings choice)
# wins; else the setup-chosen .env value when the env var is present in this
# process; else the static always-pulled default (OLLAMA_MODELS coherence).
_LITELLM_ROLE_FALLBACKS: dict[str, tuple[str, str]] = {
    "llm.smart_model": ("JARVIS_SMART_MODEL", SMART_MODEL_DEFAULT),
    "llm.fast_model": ("JARVIS_FAST_MODEL", FAST_MODEL_DEFAULT),
}

# Transition-aware failure logging for the persistent reconciler. A degraded
# LiteLLM would otherwise emit a full WARNING+traceback per role every 30 s
# (~5,760 tracebacks/day). Policy: full traceback on the FIRST consecutive
# failure per delivery target, then one terse WARNING every
# _RECONCILE_TERSE_EVERY_N passes (~10 min at the 30 s cadence) while the
# failure persists; the streak resets on success so the next distinct outage
# logs a fresh traceback. The recovery transition stays the loop's
# "all deployments reconciled" INFO.
_RECONCILE_TERSE_EVERY_N = 20
_RECONCILE_FAILURE_STREAKS: dict[str, int] = {}

# One-shot anomaly logs: a stored bare-alias placeholder and a legacy
# embed-model mismatch repeat identically on every pass — log each distinct
# value once per process lifetime instead of every 30 s.
_ALIAS_PLACEHOLDER_LOGGED: set[tuple[str, str]] = set()
_EMBED_MISMATCH_WARNED: set[str] = set()


def _litellm_reconciler_enabled() -> bool:
    """Return whether the persistent LiteLLM reconciler should start.

    The reconciler is enabled by default because it is the product path that
    keeps switchable aliases aligned with stored Settings choices. Operators can
    disable only this background task during controlled maintenance or
    benchmark runs that need a temporary admin-DB route.
    """
    value = os.environ.get("JARVIS_LITELLM_RECONCILER_ENABLED", "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _log_reconcile_failure(target: str) -> None:
    """Streak-aware delivery-failure logging (call from an ``except`` block)."""
    streak = _RECONCILE_FAILURE_STREAKS.get(target, 0)
    if streak == 0:
        logger.warning(
            "LiteLLM reconcile: could not deliver %s; will retry",
            target,
            exc_info=True,
        )
    elif streak % _RECONCILE_TERSE_EVERY_N == 0:
        logger.warning(
            "LiteLLM reconcile: still cannot deliver %s (%d consecutive failures); will retry",
            target,
            streak + 1,
        )
    _RECONCILE_FAILURE_STREAKS[target] = streak + 1


async def _mark_role_pending(pool: Any, role: str, pending: bool) -> None:
    """Best-effort llm.delivery_pending bookkeeping — never raises."""
    from paper_ingestion.services.config_write import (  # noqa: PLC0415
        _update_delivery_pending_roles,
    )

    try:
        await _update_delivery_pending_roles(pool, roles={role}, pending=pending)
    except Exception:
        logger.warning(
            "Could not update llm.delivery_pending for role %s during reconcile",
            role,
            exc_info=True,
        )


async def _desired_model_for_role(pool: Any, config_key: str) -> str:
    """Resolve the model the *config_key* role should route (see precedence above)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL", config_key
        )
    if row is not None:
        model_id = str(row["value"])
        if model_id in _ALIAS_MODELS:
            # Defense-in-depth: a stray re-seed could store the bare alias
            # ("smart"); forwarding it would create ollama/smart → 404.
            # Logged once per distinct value — this resolves every pass.
            if (config_key, model_id) not in _ALIAS_PLACEHOLDER_LOGGED:
                _ALIAS_PLACEHOLDER_LOGGED.add((config_key, model_id))
                logger.info(
                    "Ignoring stored value for %s: %r is an alias placeholder, not a model",
                    config_key,
                    model_id,
                )
        elif model_id:
            return model_id
    env_name, static_default = _LITELLM_ROLE_FALLBACKS[config_key]
    return os.environ.get(env_name, "").strip() or static_default


async def _reconcile_litellm_models_once(pool: Any) -> bool:
    """One reconcile pass. Returns True when every delivery is verified.

    Per-role failures are caught, logged, and marked pending — a pass never
    raises for delivery errors, so the surrounding loop (and the lifespan)
    survives LiteLLM still warming up or running DB-less.
    """
    from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
        _config_lock,
        ensure_smart_fallback,
        update_litellm_model,
    )

    machine_id = socket.gethostname()
    all_ok = True
    desired_by_key: dict[str, str] = {}
    for config_key in _LITELLM_ROLE_FALLBACKS:
        role = ROLE_TO_ALIAS[config_key]
        try:
            # _config_lock serializes against Settings PUT deliveries: an
            # interleaved /model/new + /model/delete pair could delete a
            # deployment a concurrent request just created. The row read sits
            # INSIDE the lock so a racing PUT and this pass converge on the
            # same committed value regardless of order.
            async with _config_lock:
                desired = await _desired_model_for_role(pool, config_key)
                desired_by_key[config_key] = desired
                await update_litellm_model(config_key, desired, db_pool=pool, machine_id=machine_id)
        except Exception:
            _log_reconcile_failure(config_key)
            await _mark_role_pending(pool, role, True)
            all_ok = False
            continue
        _RECONCILE_FAILURE_STREAKS.pop(config_key, None)
        # Delivered (True) or already routing the committed model (False) —
        # either way LiteLLM routes the desired model: clear any stale marker.
        await _mark_role_pending(pool, role, False)

    # smart-fallback: the real deployment group behind router_settings'
    # smart → ["smart-fallback"] mapping (fast-tier model, timeout 120). Not a
    # settings role, so it has no pending marker — just retry until it exists.
    if all_ok:
        try:
            async with _config_lock:
                await ensure_smart_fallback(
                    desired_by_key["llm.fast_model"], db_pool=pool, machine_id=machine_id
                )
        except Exception:
            _log_reconcile_failure("smart-fallback")
            all_ok = False
        else:
            _RECONCILE_FAILURE_STREAKS.pop("smart-fallback", None)

    # embed is dimension-locked and YAML-seeded — never delivered here. A
    # stored llm.embed_model that differs from the static default needs a
    # deliberate YAML edit + re-embed, so only warn (no pending: no automatic
    # delivery is coming for it).
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key = 'llm.embed_model' AND user_id IS NULL"
        )
    if row is not None:
        embed_model = str(row["value"])
        if (
            embed_model not in _ALIAS_MODELS
            and embed_model != EMBEDDING_MODEL_NAME
            # Legacy rows trip this on EVERY pass — warn once per distinct value.
            and embed_model not in _EMBED_MISMATCH_WARNED
        ):
            _EMBED_MISMATCH_WARNED.add(embed_model)
            logger.warning(
                "llm.embed_model is %r but the embed alias is YAML-seeded with %r; "
                "switching embedders requires editing litellm/config.yaml and re-embedding",
                embed_model,
                EMBEDDING_MODEL_NAME,
            )

    return all_ok


async def _litellm_model_reconciler_loop(pool: Any) -> None:
    """Persistent reconcile loop: one pass ~every 30 s for the process lifetime.

    WHY persistent (not stop-on-first-success): a LiteLLM that later restarts
    against an unreachable admin DB (prisma migrate WARN-and-continue) comes
    back DB-less with ONLY the YAML models — i.e. with NO smart/fast
    deployments at all, because the YAML is de-seeded — and a stopped loop
    would leave the deployment LLM-dead until an operator restarts this
    service. The ~30 s cadence matches LiteLLM's own DB reconciler; a
    steady-state pass costs three cheap GETs that no-op on comparison. This
    loop is also the re-convergence path for a delivered-but-uncommitted
    settings write (PUT failed after /model/new succeeded): the next pass
    routes LiteLLM back to the still-stored row.
    """
    reconciled_logged = False
    while True:
        try:
            # A restore rewrites this DB out from under the loop (its own asyncio
            # task, not the procrastinate worker the watcher pauses). Skip the pass
            # so it performs no user_config/model writes against the being-restored
            # DB; the next post-restore tick reconciles the forward-migrated rows.
            if skip_for_maintenance("litellm reconciler"):
                reconciled_logged = False
            elif await _reconcile_litellm_models_once(pool):
                if not reconciled_logged:
                    logger.info("LiteLLM model reconciler: all deployments reconciled")
                    reconciled_logged = True
            else:
                reconciled_logged = False
        except asyncio.CancelledError:
            raise
        except Exception:
            # Belt-and-braces: per-role errors are handled inside the pass;
            # this catches infrastructure failures (e.g. DB pool teardown).
            logger.warning(
                "LiteLLM model reconciler pass failed unexpectedly; retrying",
                exc_info=True,
            )
            reconciled_logged = False
        await asyncio.sleep(_LITELLM_RECONCILE_INTERVAL_SECONDS)


async def _start_litellm_reconciler(app: FastAPI) -> None:
    """Start the persistent LiteLLM model reconciler as a background task."""
    if not _litellm_reconciler_enabled():
        app.state.litellm_reconciler_task = None
        logger.info("LiteLLM model reconciler disabled by JARVIS_LITELLM_RECONCILER_ENABLED")
        return
    app.state.litellm_reconciler_task = asyncio.create_task(
        _litellm_model_reconciler_loop(app.state.db_pool),
        name="litellm_model_reconciler",
    )


async def _shutdown_litellm_reconciler(app: FastAPI) -> None:
    """Cancel the reconciler task and await its termination (clean teardown)."""
    task = getattr(app.state, "litellm_reconciler_task", None)
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
