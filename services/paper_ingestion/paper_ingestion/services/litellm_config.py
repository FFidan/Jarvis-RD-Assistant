"""Deliver model-role changes to LiteLLM via its admin database.

When a user selects a different model for a role (smart/fast) in the Settings
UI — or the boot reconciler re-applies stored choices — the change is delivered
with ``POST /model/new`` (create the replacement deployment) followed by
``POST /model/delete`` (remove the superseded DB deployments). LiteLLM
persists DB deployments in ``LiteLLM_ProxyModelTable`` (loaded at boot and
reconciled by LiteLLM's own background job), so deliveries survive LiteLLM
restarts.

Why not LiteLLM's legacy config-update endpoint: the pinned image silently
DROPS the request's ``model_list`` there, so every such delivery was a 200
no-op — the worst kind of phantom. Why the YAML carries no smart/fast aliases:
YAML-seeded deployments can never be removed at runtime (``/model/delete``
only deletes DB rows), so a DB ``smart`` would STACK with a YAML ``smart`` and
latency-based routing could keep preferring the stale model. The switchable
aliases therefore live ONLY in the admin DB; ``litellm/config.yaml`` keeps the
dimension-locked ``embed`` aliases plus router/general settings.

Cloud-provider keys (anthropic/openai/gemini) are Fernet-decrypted from
``user_config`` in memory and carried in the ``/model/new`` payload; LiteLLM
stores them encrypted under the pinned ``LITELLM_SALT_KEY`` in its admin DB.
They are never written to the YAML or any other file.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from jarvis_common.crypto import resolve_secret_row
from jarvis_common.db_helpers import invalidate_effective_num_ctx_cache

from paper_ingestion.services.litellm_api import (
    _HTTP_TIMEOUT,  # noqa: F401
    LiteLLMDeployment,
    LiteLLMModelInfo,  # noqa: F401
    _key_fingerprint,
    _parse_deployment,  # noqa: F401
    _post_model_delete,
    _post_model_new,
    _redact_secret,  # noqa: F401
    get_litellm_deployments,
)
from paper_ingestion.services.model_prefixes import is_local_ollama

logger = logging.getLogger(__name__)

# Serializes concurrent LiteLLM delivery sequences — the settings router imports
# this to guard PUT /api/config/{llm.*} against interleaved /model/new +
# /model/delete pairs (an interleave could delete a deployment another request
# just created).
_config_lock = asyncio.Lock()  # pyright: ignore[reportUnusedVariable]  # imported from routers/settings.py


# Map from LiteLLM model prefix → canonical provider name used in user_config keys.
# gemini/ is the LiteLLM prefix for Google models; the config key uses "google".
_CLOUD_PREFIX_TO_PROVIDER: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "google",
}

ROLE_TO_ALIAS: dict[str, str] = {
    "llm.smart_model": "smart",
    "llm.fast_model": "fast",
    "llm.embed_model": "embed",
}

# Tuned defaults for deployments the app creates when an alias has no existing
# deployment to inherit from (first bootstrap after the YAML de-seed). These
# mirror the values formerly seeded in litellm/config.yaml.
#
# Placement invariant (verified against the pinned LiteLLM image): for ollama/
# and ollama_chat/ models, num_ctx and think must be TOP-LEVEL litellm_params.
# Non-OpenAI providers pass unknown top-level params straight into Ollama's
# ``options``
# (and the ollama transforms pop top-level ``think`` into the request body),
# while a nested ``extra_body`` is forwarded verbatim under ``options`` and
# silently ignored by Ollama.
ALIAS_BOOTSTRAP_PARAMS: dict[str, dict[str, Any]] = {
    "smart": {
        "temperature": 0.2,
        "num_ctx": 8192,
        "think": False,
        "timeout": 300,
        "num_retries": 2,
    },
    "fast": {
        "temperature": 0.1,
        "num_ctx": 4096,
        "think": False,
    },
    # smart's resilience fallback: the fast-tier model with a longer timeout.
    "smart-fallback": {
        "temperature": 0.1,
        "num_ctx": 4096,
        "think": False,
        "timeout": 120,
        "num_retries": 2,
    },
}

# litellm_params keys the app manages / carries forward when replacing a
# deployment. Anything else returned by /v1/model/info (internal flags,
# defaults) is dropped on purpose — explicit over accidental carriage.
_CARRIED_PARAM_KEYS = (
    "model",
    "api_base",
    "temperature",
    "num_ctx",
    "think",
    "timeout",
    "num_retries",
    "keep_alive",
    "dimensions",
)

# Cloud no-op fingerprinting. The pinned LiteLLM image's /v1/model/info POPS
# litellm_params.api_key from every deployment it returns
# (remove_sensitive_info_from_deployment in the proxy), so the deployed key can
# never be compared directly. Without a no-op check the ~30 s reconciler would
# re-deliver every cloud alias forever: deployment-id churn, router
# cooldown/latency state resets, an INFO line every pass, and the plaintext key
# re-transmitted 2,880x/day. Instead we remember, per alias, the
# (model, think, key-fingerprint) of the LAST successful delivery made BY THIS
# PROCESS and no-op only when both the live routing state (model/think from
# /v1/model/info) and the fingerprint match. Process-local on purpose: the
# cache clears on restart (one harmless redelivery per boot, which
# self-corrects), and a genuine key rotation changes the fingerprint so the
# very next delivery call (configure_cloud_llm_keys re-push, Settings PUT, or
# a reconciler pass) carries the fresh key.
_CLOUD_DELIVERED_FINGERPRINTS: dict[str, tuple[str, Any, str]] = {}

# The always-pulled OLLAMA_MODELS default for the fast tier (matches the
# static fallback in main.py's _LITELLM_ROLE_FALLBACKS). Used to pin
# smart-fallback when a cloud fast model has no provider key.
_STATIC_FALLBACK_MODEL = "qwen3:4b"

# ensure_smart_fallback runs every reconciler pass; a missing cloud key must
# warn once per distinct model, not every 30 s.
_FALLBACK_KEYLESS_WARNED: set[str] = set()


async def get_provider_api_key(provider: str, db_pool: Any) -> str | None:
    """Fetch and decrypt the LLM provider API key from user_config.

    Returns plaintext key, or None if not configured.

    Parameters
    ----------
    provider:
        One of ``"anthropic"``, ``"openai"``, ``"google"``.
    db_pool:
        asyncpg Pool instance.

    Raises
    ------
    ValueError
        If *provider* is not in the allowed set.
    """
    from paper_ingestion.services.config_metadata import CLOUD_PROVIDERS  # noqa: PLC0415

    if provider not in CLOUD_PROVIDERS:
        raise ValueError(
            f"Unsupported provider {provider!r}. Allowed values: {sorted(CLOUD_PROVIDERS)}"
        )

    config_key = f"llm.{provider}.api_key"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config WHERE key = $1 AND user_id IS NULL",
            config_key,
        )

    if row is None:
        return None
    return resolve_secret_row(row)


def _validate_model_name(ollama_model_name: str) -> None:
    """Reject model names that contain path traversal or shell metacharacters.

    The model name is sent to LiteLLM's admin API; a value like
    ``../../etc/passwd`` or ``; rm -rf /`` must never reach a config surface.
    Permit only ``[a-zA-Z0-9._:-]`` characters (covers all real Ollama IDs).
    """
    if not re.fullmatch(r"[a-zA-Z0-9._:\-]+", ollama_model_name):
        raise ValueError(
            f"Model name {ollama_model_name!r} contains disallowed characters. "
            "Only alphanumerics and . _ : - are permitted."
        )


async def _get_thinking_disabled(
    model_name: str,
    machine_id: str,
    db_pool: Any,
) -> bool:
    """Return True if the user has disabled thinking mode for *model_name* on *machine_id*.

    Reads ``llm.{machine_id}.thinking_disabled.{model_name}`` from user_config.
    Returns False if the key is absent, db_pool is None, or any error occurs.
    """
    if not machine_id or db_pool is None:
        return False
    config_key = f"llm.{machine_id}.thinking_disabled.{model_name}"
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
                config_key,
            )
        if row is None:
            return False
        val = row["value"]
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)
    except Exception:
        logger.warning(
            "Could not read thinking_disabled key for %r on machine %r",
            model_name,
            machine_id,
            exc_info=True,
        )
        return False


async def _get_num_ctx(
    alias: str,
    machine_id: str,
    db_pool: Any,
) -> int | None:
    """Return the per-machine num_ctx override for *alias*, if configured."""
    if not machine_id or db_pool is None:
        return None
    config_key = f"llm.{machine_id}.{alias}_num_ctx"
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL",
                config_key,
            )
        if row is None:
            return None
        val = row["value"]
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
        return None
    except Exception:
        logger.warning(
            "Could not read num_ctx key for alias %r on machine %r",
            alias,
            machine_id,
            exc_info=True,
        )
        return None


def _deployments_for_alias(
    deployments: list[LiteLLMDeployment], alias: str
) -> tuple[list[LiteLLMDeployment], list[LiteLLMDeployment]]:
    """Split *alias*'s deployments into (db_deployments, yaml_deployments)."""
    db_entries: list[LiteLLMDeployment] = []
    yaml_entries: list[LiteLLMDeployment] = []
    for entry in deployments:
        if entry.model_name != alias:
            continue
        if entry.model_info.db_model:
            db_entries.append(entry)
        else:
            yaml_entries.append(entry)
    return db_entries, yaml_entries


def _deployment_id(entry: LiteLLMDeployment) -> str | None:
    if entry.model_info.id:
        return entry.model_info.id
    return None


def _carry_base_params(entry: LiteLLMDeployment | None) -> dict[str, Any]:
    """Extract the managed litellm_params from an existing deployment.

    Lifts legacy ``extra_body`` values (num_ctx/think) to top level — the only
    placement Ollama actually honours (see module docstring) — and drops
    everything outside the managed whitelist.
    """
    if entry is None:
        return {}
    raw = entry.litellm_params
    carried = {k: raw[k] for k in _CARRIED_PARAM_KEYS if raw.get(k) is not None}
    extra = raw.get("extra_body")
    if isinstance(extra, dict):
        if "num_ctx" not in carried and extra.get("num_ctx") is not None:
            carried["num_ctx"] = extra["num_ctx"]
        if "think" not in carried and extra.get("think") is not None:
            carried["think"] = extra["think"]
    return carried


def _routing_signature(params: dict[str, Any]) -> tuple[Any, Any, Any]:
    """(model, num_ctx, think) — the routing-relevant identity of a deployment."""
    extra = params.get("extra_body") if isinstance(params.get("extra_body"), dict) else {}
    return (
        params.get("model"),
        params.get("num_ctx", extra.get("num_ctx") if isinstance(extra, dict) else None),
        params.get("think", extra.get("think") if isinstance(extra, dict) else None),
    )


# ---------------------------------------------------------------------------
# Delivery orchestration
# ---------------------------------------------------------------------------


async def _replace_alias_deployment(
    alias: str,
    new_params: dict[str, Any],
    stale_db_entries: list[LiteLLMDeployment],
) -> bool:
    """Create the replacement deployment, then delete the superseded DB rows.

    Create-first ordering keeps the old routing intact if the create fails.
    If a delete fails afterwards the new and old deployments would STACK
    (latency routing may keep preferring the old model), so the just-created
    deployment is rolled back best-effort and the error is raised.
    """
    new_id = await _post_model_new(alias, new_params)
    delete_errors: list[str] = []
    for entry in stale_db_entries:
        dep_id = _deployment_id(entry)
        if dep_id is None or dep_id == new_id:
            continue
        try:
            await _post_model_delete(dep_id)
        except RuntimeError as exc:
            delete_errors.append(str(exc))
    if delete_errors:
        if new_id is not None:
            try:
                await _post_model_delete(new_id)
            except RuntimeError:
                logger.warning(
                    "Rollback delete of just-created deployment %r for alias %r failed",
                    new_id,
                    alias,
                    exc_info=True,
                )
        raise RuntimeError(
            f"LiteLLM stale-deployment cleanup failed for alias {alias!r} "
            f"(delivery rolled back): {'; '.join(delete_errors)}"
        )
    logger.info(
        "LiteLLM alias %r now routes %s (deployment %s; %d stale deployment(s) removed)",
        alias,
        new_params.get("model"),
        new_id or "<unknown id>",
        len(stale_db_entries),
    )
    return True


# ---------------------------------------------------------------------------
# update_litellm_model stages: resolve -> parse -> overrides -> routing -> deliver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ModelTarget:
    """Normalized routing target for a requested model name.

    ``new_name`` is the model string after the implicit ``:latest`` tag is
    stripped (the value carried forward into the new deployment); ``suffix`` is
    the part after an optional ``provider/`` prefix (validated);
    ``cloud_provider`` is the canonical provider name for a known cloud prefix,
    else None for local/Ollama models.
    """

    new_name: str
    suffix: str
    cloud_provider: str | None


def _resolve_alias(config_key: str) -> str | None:
    """Resolve a Settings ``config_key`` (or a bare alias) to the LiteLLM alias.

    Accepts either the ``llm.*`` config-key format or an already-resolved alias
    for caller convenience; returns None when neither matches.
    """
    return ROLE_TO_ALIAS.get(config_key) or (
        config_key if config_key in ROLE_TO_ALIAS.values() else None
    )


def _parse_model_target(model_name: str) -> _ModelTarget:
    """Parse a requested model name into a typed routing target.

    Strips the implicit ``:latest`` tag (Ollama's default tag is never stored,
    so ``mistral-nemo:latest`` and ``mistral-nemo`` must be treated as equal),
    splits an optional ``provider/`` prefix mapping ``gemini/`` → ``google``,
    and validates the model-name portion (for ``provider/model`` only
    the suffix is validated). Raises ``ValueError`` on a disallowed suffix.
    """
    # Normalize: strip :latest -- Ollama's default implicit tag is never stored
    # anywhere, so "mistral-nemo:latest" and "mistral-nemo" must be treated as equal.
    if model_name.endswith(":latest"):
        model_name = model_name[:-7]
    model_suffix = model_name  # the part after provider/ (or full name for Ollama)

    cloud_provider: str | None = None
    if "/" in model_name:
        prefix, model_suffix = model_name.split("/", 1)
        cloud_provider = _CLOUD_PREFIX_TO_PROVIDER.get(prefix)

    # Validate the model-name portion (no path traversal / shell chars).
    # For "provider/model-name" strings we validate only the model-name suffix.
    _validate_model_name(model_suffix)
    return _ModelTarget(new_name=model_name, suffix=model_suffix, cloud_provider=cloud_provider)


async def _resolve_effective_overrides(
    target: _ModelTarget,
    alias: str,
    machine_id: str,
    db_pool: Any,
    num_ctx: int | None,
    thinking_disabled: bool | None,
) -> tuple[int | None, bool | None]:
    """Resolve the effective per-machine num_ctx + thinking-disabled overrides.

    num_ctx is an Ollama runtime option (None for cloud aliases); thinking is
    honoured only for thinking-capable catalog entries. Explicit keyword values
    are pending settings writes and win over persisted DB state.
    """
    model_name = target.new_name
    effective_num_ctx: int | None = None
    if target.cloud_provider is None:
        effective_num_ctx = num_ctx
        if effective_num_ctx is None:
            effective_num_ctx = await _get_num_ctx(alias, machine_id, db_pool)

    # Import here to avoid circular import at module level
    from jarvis_common.model_catalog import ModelCatalogEntry, load_model_catalog  # noqa: PLC0415

    catalog = load_model_catalog()
    catalog_entry: ModelCatalogEntry | None = None
    bare_model = model_name.split("/", 1)[-1] if "/" in model_name else model_name
    for _entry in catalog:
        entry_bare = (_entry.ollama_tag or _entry.id).split("/")[-1]
        if entry_bare == bare_model or _entry.id == model_name:
            catalog_entry = _entry
            break
    effective_thinking_disabled: bool | None = None
    if catalog_entry is not None and catalog_entry.supports_thinking:
        effective_thinking_disabled = thinking_disabled
        if effective_thinking_disabled is None:
            effective_thinking_disabled = await _get_thinking_disabled(
                model_name,
                machine_id,
                db_pool,
            )
        if effective_thinking_disabled:
            logger.info(
                "Thinking mode disabled for model %r on machine %r (alias %r)",
                model_name,
                machine_id,
                alias,
            )
    return effective_num_ctx, effective_thinking_disabled


def _resolve_new_model(alias: str, target: _ModelTarget, base_params: dict[str, Any]) -> str:
    """Compute the new model string for the replacement deployment.

    Caller-supplied provider prefix wins; otherwise inherit the existing entry's
    non-local prefix; otherwise default to the alias's local prefix. Chat aliases
    default to ``ollama_chat/`` (honours grammar-constrained decoding); the
    dimension-locked embed alias stays on ``ollama/`` (different endpoint).
    """
    model_name = target.new_name
    existing_model = str(base_params.get("model", ""))
    local_default_prefix = "ollama/" if alias == "embed" else "ollama_chat/"
    if "/" in model_name:
        return model_name
    if "/" in existing_model and not is_local_ollama(existing_model):
        existing_prefix = existing_model.split("/")[0]
        return f"{existing_prefix}/{model_name}"
    return f"{local_default_prefix}{model_name}"


def _deliver_embed(
    new_model: str,
    db_entries: list[LiteLLMDeployment],
    yaml_entries: list[LiteLLMDeployment],
) -> bool:
    """Embed alias is dimension-locked + YAML-seeded.

    Re-selecting the model it already routes is a no-op (False); routing it
    anywhere else is refused (a DB embed deployment would stack with the YAML
    one and latency routing would mix embedders).
    """
    routed = {str(e.litellm_params.get("model", "")) for e in (*db_entries, *yaml_entries)}
    if new_model in routed:
        return False
    raise RuntimeError(
        f"The embed alias is dimension-locked to the Qdrant collection and cannot "
        f"be re-routed at runtime (requested {new_model!r}). Switching embedders is "
        "a deliberate operation: edit litellm/config.yaml and re-embed the corpus."
    )


async def _deliver_cloud(
    alias: str,
    cloud_provider: str,
    new_model: str,
    base_params: dict[str, Any],
    db_entries: list[LiteLLMDeployment],
    yaml_entries: list[LiteLLMDeployment],
    db_pool: Any,
    effective_thinking_disabled: bool | None,
) -> bool:
    """Deliver a cloud-provider model + its Fernet-decrypted key.

    The no-op mirrors the ollama shape (single DB entry, no YAML stack, signature
    match) with the cloud signature = (model, think, key-fingerprint) — num_ctx /
    api_base don't apply. /v1/model/info pops api_key, so the key leg is the
    process-local fingerprint of the last delivery; key rotation re-delivers
    because the fresh key's fingerprint differs from the cached one.
    """
    api_key: str | None = None
    if db_pool is None:
        logger.warning(
            "Cannot inject cloud API key for alias %r -- db_pool not provided; "
            "delivering the deployment without a key",
            alias,
        )
    else:
        api_key = await get_provider_api_key(cloud_provider, db_pool)
        if api_key is None:
            logger.warning(
                "No API key configured for provider %r (alias %r) -- "
                "delivering the deployment without a key; requests will fail "
                "until the key is saved",
                cloud_provider,
                alias,
            )
    new_params: dict[str, Any] = {
        k: v
        for k, v in base_params.items()
        # Ollama-only / local-transport params must not leak onto a cloud deployment.
        if k not in ("api_base", "num_ctx", "keep_alive", "dimensions", "think", "model")
    }
    new_params["model"] = new_model
    if api_key is not None:
        new_params["api_key"] = api_key
    if effective_thinking_disabled:
        new_params["think"] = False

    desired_cloud = (new_model, new_params.get("think"), _key_fingerprint(api_key))
    if len(db_entries) == 1 and not yaml_entries:
        deployed = db_entries[0].litellm_params
        if (
            deployed.get("model") == new_model
            and deployed.get("think") == new_params.get("think")
            and _CLOUD_DELIVERED_FINGERPRINTS.get(alias) == desired_cloud
        ):
            return False
    delivered = await _replace_alias_deployment(alias, new_params, db_entries)
    _CLOUD_DELIVERED_FINGERPRINTS[alias] = desired_cloud
    return delivered


async def _deliver_local(
    alias: str,
    new_model: str,
    base_params: dict[str, Any],
    base_entry: LiteLLMDeployment | None,
    db_entries: list[LiteLLMDeployment],
    yaml_entries: list[LiteLLMDeployment],
    db_pool: Any,
    effective_num_ctx: int | None,
    effective_thinking_disabled: bool | None,
    thinking_disabled: bool | None,
) -> bool:
    """Deliver a local/Ollama deployment.

    Seeds the tuned defaults on fresh creation (post-de-seed bootstrap), applies
    per-machine num_ctx / think, no-ops on a routing-signature match (warning on
    a divergent YAML-seeded stack), then mirrors a delivered num_ctx into the
    system prompt-budget row to keep budget readers in lock-step.
    """
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    new_params = {k: v for k, v in base_params.items() if k != "model"}
    if base_entry is None:
        # Fresh creation (post-de-seed bootstrap): seed the tuned defaults.
        new_params = {**ALIAS_BOOTSTRAP_PARAMS.get(alias, {}), **new_params}
    new_params["model"] = new_model
    if is_local_ollama(new_model):
        new_params["api_base"] = get_paper_ingestion_settings().ollama_base_url
    # else (inherited non-cloud prefix, e.g. the vLLM openai/ spike): keep the
    # carried api_base — forcing the Ollama URL would break that transport.
    if effective_num_ctx is not None and is_local_ollama(new_model):
        new_params["num_ctx"] = effective_num_ctx
    if effective_thinking_disabled:
        new_params["think"] = False
    elif thinking_disabled is False:
        # Explicit re-enable: remove the think flag, preserving everything else.
        new_params.pop("think", None)

    desired_signature = _routing_signature(new_params)
    if len(db_entries) == 1 and not yaml_entries:
        if _routing_signature(db_entries[0].litellm_params) == desired_signature:
            return False
    elif not db_entries and yaml_entries:
        if any(_routing_signature(e.litellm_params) == desired_signature for e in yaml_entries):
            # The YAML already routes exactly this; a DB copy would stack.
            return False
        logger.warning(
            "Alias %r is YAML-seeded with different routing than requested. The YAML "
            "deployment cannot be removed at runtime and will STACK with the new DB "
            "deployment — remove the %r entry from litellm/config.yaml.",
            alias,
            alias,
        )

    delivered = await _replace_alias_deployment(alias, new_params, db_entries)

    # Keep the prompt-budget source of truth in lock-step with the value just
    # delivered to the (deployment-global) proxy. The Settings PUT path writes
    # this same system row on success; without mirroring it here, a reconciler
    # or model-change delivery of a per-machine num_ctx would route the new
    # window while the budget readers kept using a stale one (silent overflow
    # across a fleet). Only when a num_ctx actually rode the delivery.
    if (
        delivered
        and db_pool is not None
        and effective_num_ctx is not None
        and is_local_ollama(new_model)
    ):
        from paper_ingestion.services.config_db import _upsert_system_num_ctx  # noqa: PLC0415

        async with db_pool.acquire() as conn:
            await _upsert_system_num_ctx(conn, alias, effective_num_ctx)
        invalidate_effective_num_ctx_cache()

    return delivered


async def update_litellm_model(
    config_key: str,
    model_name: str,
    db_pool: Any = None,
    machine_id: str = "",
    num_ctx: int | None = None,
    thinking_disabled: bool | None = None,
) -> bool:
    """Route an alias to a new model via LiteLLM's admin DB.

    Resolves the alias's current deployments (``GET /v1/model/info``), creates
    the replacement (``POST /model/new`` — carrying merged num_ctx / think /
    temperature / api_base, and the Fernet-decrypted provider key for cloud
    models), then deletes the superseded DB deployments (``POST
    /model/delete``). Deployments are deployment-global: the last writer wins
    across machines; per-machine num_ctx / thinking preferences are read from
    user_config for the *delivering* machine.

    ``num_ctx`` and ``thinking_disabled`` are pending per-machine settings
    overrides and win over persisted DB state. ``num_ctx`` is local/Ollama-only.

    Returns True when a delivery happened, False when nothing needed changing
    (the alias already routes the requested model with the same effective
    params). Raises ``RuntimeError`` when delivery fails — including the
    "No DB Connected" degraded state, which callers map to the
    ``llm.delivery_pending`` bookkeeping.
    """
    # Stage 1 — resolve config_key -> alias (accept either format for convenience).
    alias = _resolve_alias(config_key)
    if not alias:
        return False

    # Stage 2 — parse the target: strip :latest, split provider/suffix, validate.
    target = _parse_model_target(model_name)

    # Stage 3 — effective per-machine num_ctx / thinking overrides (pending
    # settings writes win over persisted DB state; num_ctx is local/Ollama-only).
    effective_num_ctx, effective_thinking_disabled = await _resolve_effective_overrides(
        target, alias, machine_id, db_pool, num_ctx, thinking_disabled
    )

    # Stage 4 — resolve current routing state + the new model string.
    deployments = await get_litellm_deployments()
    db_entries, yaml_entries = _deployments_for_alias(deployments, alias)
    base_entry = db_entries[0] if db_entries else (yaml_entries[0] if yaml_entries else None)
    base_params = _carry_base_params(base_entry)
    new_model = _resolve_new_model(alias, target, base_params)

    # Stage 5 — dispatch to the terminal delivery handler. Embed is checked
    # before cloud (the dimension-locked alias is refused / no-op regardless of
    # any provider prefix), and cloud before local.
    if alias == "embed":
        return _deliver_embed(new_model, db_entries, yaml_entries)
    if target.cloud_provider is not None:
        return await _deliver_cloud(
            alias,
            target.cloud_provider,
            new_model,
            base_params,
            db_entries,
            yaml_entries,
            db_pool,
            effective_thinking_disabled,
        )
    return await _deliver_local(
        alias,
        new_model,
        base_params,
        base_entry,
        db_entries,
        yaml_entries,
        db_pool,
        effective_num_ctx,
        effective_thinking_disabled,
        thinking_disabled,
    )


async def ensure_smart_fallback(
    fast_model: str,
    db_pool: Any = None,
    machine_id: str = "",  # noqa: ARG001  (signature parity with update_litellm_model; reserved)
) -> bool:
    """Ensure the ``smart-fallback`` deployment group exists and routes *fast_model*.

    ``router_settings.fallbacks`` in the YAML maps ``smart → ["smart-fallback"]``;
    this function creates the real deployment behind that group (the fast-tier
    model with a longer timeout). Cloud fast models mirror
    ``update_litellm_model``'s cloud semantics (no api_base/num_ctx/think; the
    Fernet-decrypted provider key is carried); when the provider key is missing
    the fallback pins to the static pulled default instead of creating a
    deployment that cannot authenticate — i.e. one guaranteed to fail exactly
    when smart fails. Returns True when a delivery happened, False when the
    deployment already routes the target. Raises ``RuntimeError`` on delivery
    failure.
    """
    if fast_model.endswith(":latest"):
        fast_model = fast_model[:-7]
    _validate_model_name(fast_model.split("/", 1)[-1])

    cloud_provider: str | None = None
    if "/" in fast_model:
        cloud_provider = _CLOUD_PREFIX_TO_PROVIDER.get(fast_model.split("/", 1)[0])

    api_key: str | None = None
    if cloud_provider is not None:
        if db_pool is not None:
            api_key = await get_provider_api_key(cloud_provider, db_pool)
        if api_key is None:
            if fast_model not in _FALLBACK_KEYLESS_WARNED:
                _FALLBACK_KEYLESS_WARNED.add(fast_model)
                logger.warning(
                    "smart-fallback: no %r API key available for %r; pinning the "
                    "fallback to the static default %r until a key is saved",
                    cloud_provider,
                    fast_model,
                    _STATIC_FALLBACK_MODEL,
                )
            cloud_provider = None
            fast_model = _STATIC_FALLBACK_MODEL

    new_model = fast_model if "/" in fast_model else f"ollama_chat/{fast_model}"

    deployments = await get_litellm_deployments()
    db_entries, yaml_entries = _deployments_for_alias(deployments, "smart-fallback")

    if cloud_provider is not None:
        # Same no-op shape as update_litellm_model's cloud branch: live model
        # match + process-local key fingerprint, so a rotated key re-delivers
        # within one reconciler pass instead of riding the stale key forever.
        desired_cloud = (new_model, None, _key_fingerprint(api_key))
        if (
            len(db_entries) == 1
            and not yaml_entries
            and db_entries[0].litellm_params.get("model") == new_model
            and _CLOUD_DELIVERED_FINGERPRINTS.get("smart-fallback") == desired_cloud
        ):
            return False
        cloud_params: dict[str, Any] = {
            k: v
            for k, v in ALIAS_BOOTSTRAP_PARAMS["smart-fallback"].items()
            # Ollama-only params must not leak onto a cloud deployment.
            if k not in ("num_ctx", "think")
        }
        cloud_params["model"] = new_model
        cloud_params["api_key"] = api_key
        delivered = await _replace_alias_deployment("smart-fallback", cloud_params, db_entries)
        _CLOUD_DELIVERED_FINGERPRINTS["smart-fallback"] = desired_cloud
        return delivered

    matching_db = [e for e in db_entries if str(e.litellm_params.get("model", "")) == new_model]
    stale_db = [e for e in db_entries if e not in matching_db]

    if matching_db or any(
        str(e.litellm_params.get("model", "")) == new_model for e in yaml_entries
    ):
        # A matching deployment already exists. Delete stale sibling DB entries
        # so they cannot keep diverting traffic — mirrors the create-first/
        # delete-old ordering of _replace_alias_deployment (delete-only here
        # because the matching deployment is already correct).
        for entry in stale_db:
            dep_id = _deployment_id(entry)
            if dep_id is not None:
                try:
                    await _post_model_delete(dep_id)
                except RuntimeError as exc:
                    logger.warning(
                        "Could not delete stale smart-fallback sibling %s: %s", dep_id, exc
                    )
        return False

    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    new_params: dict[str, Any] = {
        **ALIAS_BOOTSTRAP_PARAMS["smart-fallback"],
        "model": new_model,
        "api_base": get_paper_ingestion_settings().ollama_base_url,
    }
    return await _replace_alias_deployment("smart-fallback", new_params, db_entries)
