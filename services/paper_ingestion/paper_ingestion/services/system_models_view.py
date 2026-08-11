"""Model-state views for the system endpoints.

Setup-status model probes (Ollama readiness, per-role routing warnings) and
the installed/current/routing/recommendation snapshot served by
``GET /api/system/models``.
"""

import logging
import time
from collections.abc import Iterable
from typing import Any

import asyncpg
import httpx
from fastapi import Request
from jarvis_common.hardware_fit import recommend_models
from jarvis_common.pinned_transport import JARVIS_SERVICE_POLICY, pinned_async_client
from pydantic import Field

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.ingestion.embedder import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_NAME,
    validate_embedding_configuration,
)
from paper_ingestion.models import SystemModelsResponse
from paper_ingestion.services.llm_provider_registry import PROVIDER_REGISTRY
from paper_ingestion.services.model_assignment import provider_access_configured
from paper_ingestion.services.model_lifecycle import (
    HardwareInfo,
    async_get_cached_hardware,
    build_model_statuses,
    recommendations_for_role,
)
from paper_ingestion.services.model_prefixes import strip_ollama_prefix
from paper_ingestion.services.provider_models import ProviderModelList, fetch_all_provider_models

logger = logging.getLogger(__name__)


# Derive the embedder family prefix from EMBEDDING_MODEL_NAME at import time
# so setup-status always tracks whatever is actually configured.
# e.g. "qwen3-embedding:4b" → "qwen3-embedding"
_EMBEDDER_BASE: str = EMBEDDING_MODEL_NAME.split(":")[0]

_OLLAMA_PROBE_TTL = 10  # seconds


class _OllamaProbeCache:
    """TTL cache for Ollama /api/tags probe results.

    Replaces module-level ``global`` mutation with a contained state holder.
    """

    def __init__(self) -> None:
        self._ts: float = 0.0
        self._result: tuple[bool, list[str]] = (False, [])

    def get_cached(self, now: float) -> tuple[bool, list[str]] | None:
        if self._ts > 0 and now - self._ts < _OLLAMA_PROBE_TTL:
            return self._result
        return None

    def set(self, now: float, result: tuple[bool, list[str]]) -> None:
        self._ts = now
        self._result = result


_ollama_probe_cache = _OllamaProbeCache()

_MODEL_WARNINGS_TTL = 30  # seconds


class _ModelWarningsCache:
    """TTL cache for the setup-status model-warnings probe.

    ``_compute_model_warnings`` fires uncached litellm ``/v1/model/info`` +
    ollama ``/api/tags`` calls on every setup-status poll; this caches the
    computed warning list (~30 s) mirroring ``_OllamaProbeCache`` above.
    """

    def __init__(self) -> None:
        self._ts: float = 0.0
        self._result: list[str] = []

    def get_cached(self, now: float) -> list[str] | None:
        if self._ts > 0 and now - self._ts < _MODEL_WARNINGS_TTL:
            return self._result
        return None

    def set(self, now: float, result: list[str]) -> None:
        self._ts = now
        self._result = result


_model_warnings_cache = _ModelWarningsCache()


def _strip_latest(model: str) -> str:
    """Ollama's implicit default tag (``qwen3:8b`` vs ``qwen3:8b:latest``) must
    never cause a false divergence — applied to BOTH sides of every
    intent ↔ routing compare and to the installed-model set.
    """
    if model.endswith(":latest"):
        return model[:-7]
    return model


def _models_match(installed_names: list[str]) -> bool:
    """Return True iff a qwen3 chat model AND the embedder are both installed.

    Ready condition:
      - At least one model whose name starts with ``"qwen3:"`` (this prefix
        matches qwen3:4b / qwen3:8b / qwen3:14b and explicitly EXCLUDES the
        embedder whose name begins with ``"qwen3-embedding:"``).
      - At least one model whose name starts with ``_EMBEDDER_BASE`` (derived
        from EMBEDDING_MODEL_NAME — no hardcoded tag).
    """
    if not installed_names:
        return False
    has_chat = any(name.startswith("qwen3:") for name in installed_names)
    has_embed = any(name.startswith(_EMBEDDER_BASE) for name in installed_names)
    return has_chat and has_embed


async def _probe_ollama() -> tuple[bool, list[str]]:
    """Probe ``{OLLAMA_BASE_URL}/api/tags``; return (models_ready, downloading).

    Results are cached for 10 seconds to avoid hammering Ollama on every
    setup-status request. Any failure (network, timeout, non-200) yields
    ``(False, [])``. The caller must never crash on this.
    """
    now = time.monotonic()
    cached = _ollama_probe_cache.get_cached(now)
    if cached is not None:
        return cached

    ollama_url = get_paper_ingestion_settings().ollama_base_url
    try:
        async with pinned_async_client(JARVIS_SERVICE_POLICY, timeout=3.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
        if resp.status_code != 200:
            # Log so a reachable-but-erroring Ollama (e.g. 503 during startup)
            # is greppable, not silently indistinguishable from "unreachable".
            logger.warning("setup-status: Ollama /api/tags returned %s", resp.status_code)
            result: tuple[bool, list[str]] = (False, [])
            _ollama_probe_cache.set(now, result)
            return result
        data = resp.json()
        installed = [m.get("name", "") for m in data.get("models", [])]
        if _models_match(installed):
            result = (True, [])
        else:
            # Ollama is reachable but provisioning is incomplete.  Build a
            # short missing-pieces list so SystemCheck can show "still pulling"
            # rather than the generic "not ready" message.
            # Chat family uses a stable "qwen3:" prefix; the embedder name is
            # config-derived (_EMBEDDER_BASE from EMBEDDING_MODEL_NAME) — hence
            # the asymmetric checks.
            missing: list[str] = []
            if not any(n.startswith("qwen3:") for n in installed):
                missing.append("qwen3 chat model")
            if not any(n.startswith(_EMBEDDER_BASE) for n in installed):
                missing.append(EMBEDDING_MODEL_NAME)
            result = (False, missing)
    except Exception:
        logger.warning("setup-status: Ollama probe failed", exc_info=True)
        result = (False, [])
    _ollama_probe_cache.set(now, result)
    return result


async def _fetch_litellm_deployments_safe() -> list[Any] | None:
    """Fetch LiteLLM deployments for the model-warnings probe; None on failure."""
    try:
        from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
            get_litellm_deployments,
        )

        return await get_litellm_deployments()
    except Exception:
        logger.debug("model_warnings: LiteLLM probe failed — skipping", exc_info=True)
        return None


def _build_role_routing_map(deployments: list[Any]) -> dict[str, str]:
    """Build role → routed-model map (ollama/ prefix stripped + :latest stripped)."""
    role_to_routed: dict[str, str] = {}
    for dep in deployments:
        alias = dep.model_name
        if alias not in _MODEL_ROLES:
            continue
        params = dep.litellm_params
        routed_full = str(params.get("model", ""))
        if not routed_full:
            continue
        routed_full = strip_ollama_prefix(routed_full)
        role_to_routed[alias] = _strip_latest(routed_full)
    return role_to_routed


async def _fetch_installed_ollama_models(ollama_url: str) -> set[str] | None:
    """Fetch installed Ollama model names (:latest-stripped); None on failure."""
    try:
        async with pinned_async_client(JARVIS_SERVICE_POLICY, timeout=3.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
        if resp.status_code != 200:
            # Reachable-but-erroring Ollama (e.g. 503 during startup): log the
            # status so it is greppable, then degrade to no warnings.
            logger.debug(
                "model_warnings: Ollama /api/tags returned %s — skipping", resp.status_code
            )
            return None
        data = resp.json()
        return {_strip_latest(m.get("name", "")) for m in data.get("models", [])}
    except Exception:
        logger.debug("model_warnings: Ollama probe failed — skipping", exc_info=True)
        return None


async def _compute_model_warnings() -> list[str]:
    """Return per-role routing warnings for GET /api/system/setup-status.

    Compares the roles LiteLLM is *currently* routing against the set of models
    Ollama has pulled, using the same ``:latest``-tolerant normalization as the
    routing-truth consistency check.  Always returns ``[]`` when LiteLLM or
    Ollama is unreachable — the endpoint must never fail because of this probe.

    Result is cached ~30 s (``_model_warnings_cache``) so setup-status polling
    does not re-hit litellm + ollama on every request.
    """
    now = time.monotonic()
    cached = _model_warnings_cache.get_cached(now)
    if cached is not None:
        return cached

    deployments = await _fetch_litellm_deployments_safe()
    if deployments is None:
        _model_warnings_cache.set(now, [])
        return []

    role_to_routed = _build_role_routing_map(deployments)
    if not role_to_routed:
        _model_warnings_cache.set(now, [])
        return []

    ollama_url = get_paper_ingestion_settings().ollama_base_url
    installed = await _fetch_installed_ollama_models(ollama_url)
    if installed is None:
        _model_warnings_cache.set(now, [])
        return []

    # Emit a warning for each Ollama role that routes a model not yet pulled.
    # Cloud models (containing "/") are skipped — pulled check is Ollama-specific.
    warnings: list[str] = []
    for role in ("smart", "fast"):
        routed = role_to_routed.get(role)
        if routed is None or "/" in routed:
            continue
        if routed not in installed:
            warnings.append(f"{role} routes to {routed} which is not pulled")

    _model_warnings_cache.set(now, warnings)
    return warnings


async def _cloud_key_presence(pool: asyncpg.Pool) -> dict[str, bool]:
    """Return whether each registered provider has usable access configured.

    Delegates to the one shared access predicate, so the picker's presence map
    and the assignment save gate can never disagree about a provider.
    """
    try:
        return await provider_access_configured(PROVIDER_REGISTRY, pool)
    except Exception:
        logger.warning("Could not load cloud provider key presence", exc_info=True)
        return {provider.id: False for provider in PROVIDER_REGISTRY}


_MODEL_ROLES = ("smart", "fast", "embed")


class SystemModelsWithDeliveryResponse(SystemModelsResponse):
    """``SystemModelsResponse`` plus per-role LiteLLM routing truth.

    ``delivery`` maps each role (smart/fast/embed) to ``"applied"`` (LiteLLM
    routes the committed model) or ``"pending_restart"`` (the config row is
    committed but LiteLLM has not yet accepted the update — the reconciler
    keeps retrying until it succeeds). Empty when the delivery state could not
    be read. Additive field; existing consumers of the base shape are unaffected.

    ``routing`` maps each role to the model LiteLLM is *currently* routing
    (the ``litellm_params.model`` string, normalized to strip the provider
    prefix so it compares directly to ``current`` values). Absent / empty when
    LiteLLM is unreachable. Additive; degrades honestly.

    ``consistent`` is True when every role that has a stored ``current`` intent
    also has a matching ``routing`` entry. Roles without a stored intent are
    not considered. False when LiteLLM is unreachable and there is stored
    intent that cannot be verified.

    ``provider_lists`` carries one summary per configured provider — how many
    live models were listed, when, whether the listing was cut short, what was
    excluded, and any error — so the UI can say what is not being shown instead
    of presenting a short list as complete. It must stay declared here:
    Pydantic drops undeclared top-level keys silently at both the
    ``model_validate`` call and the route's ``response_model`` filter.
    """

    delivery: dict[str, str] = Field(default_factory=dict)
    routing: dict[str, str] = Field(default_factory=dict)
    consistent: bool = True
    provider_lists: dict[str, dict[str, Any]] = Field(default_factory=dict)


async def _load_current_model_assignments(
    db_pool: asyncpg.Pool,
) -> tuple[dict[str, Any], str | None]:
    """Load committed smart/fast/embed model assignments. issue is set on read failure."""
    current: dict[str, Any] = {}
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM user_config "
                "WHERE key = ANY($1::text[]) AND user_id IS NULL",
                ["llm.smart_model", "llm.fast_model", "llm.embed_model"],
            )
        for r in rows:
            short_key = r["key"].replace("llm.", "")
            val = r["value"]
            # Strip wrapping quotes from JSONB-encoded strings
            if isinstance(val, str):
                val = val.strip('"')
            current[short_key] = val
    except Exception:
        logger.warning("Could not load current model assignments", exc_info=True)
        return current, "Could not load current model assignments."
    return current, None


async def _load_model_delivery_state(db_pool: asyncpg.Pool) -> dict[str, str]:
    """Per-role delivery state: roles in llm.delivery_pending await LiteLLM acceptance."""
    # On read failure delivery stays empty — absence of a claim, never a phantom "applied".
    from paper_ingestion.services.config_write import (  # noqa: PLC0415
        _DELIVERY_PENDING_KEY,
        _fetch_system_config_values,
    )

    try:
        pending_values = await _fetch_system_config_values(db_pool, [_DELIVERY_PENDING_KEY])
        raw_pending = pending_values.get(_DELIVERY_PENDING_KEY)
        pending_roles = {str(r) for r in raw_pending} if isinstance(raw_pending, list) else set()
        return {
            role: "pending_restart" if role in pending_roles else "applied" for role in _MODEL_ROLES
        }
    except Exception:
        logger.warning("Could not load model delivery state", exc_info=True)
        return {}


async def _load_routing_truth(current: dict[str, Any]) -> tuple[dict[str, str], bool]:
    """Read what LiteLLM actually routes now and compare against the committed intent."""
    # Provider prefix is normalized ("ollama/qwen3:8b" → "qwen3:8b") so the comparison
    # is apples-to-apples with how `current` stores model names.
    try:
        from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
            get_litellm_deployments,
        )

        deployments = await get_litellm_deployments()
        # Build a role → routed-model map from the deployment list.
        # Each deployment entry has model_name == alias (smart/fast/embed) and
        # litellm_params.model == the full routed model string (e.g. "ollama/qwen3:8b").
        routing: dict[str, str] = {}
        for dep in deployments:
            alias = dep.model_name
            if alias not in _MODEL_ROLES:
                continue
            params = dep.litellm_params
            routed_full = str(params.get("model", ""))
            if not routed_full:
                continue
            # Normalize: strip provider prefix so "ollama/qwen3:8b" → "qwen3:8b".
            # Cloud models (anthropic/…, openai/…) keep the prefix because that
            # is how `current` stores cloud model assignments too.
            routed_normalized = strip_ollama_prefix(routed_full)
            # Normalize :latest so "qwen3:8b:latest" and "qwen3:8b" compare equal.
            routed_normalized = _strip_latest(routed_normalized)
            # Multiple deployments per alias can exist mid-replace; the
            # reconciler removes stale duplicates on its next pass.
            routing[alias] = routed_normalized

        # Consistency check: every role that has a stored intent must be routing
        # that exact model.  Roles with no stored intent are skipped — no intent
        # means no expectation to violate.  Both sides are :latest-normalized so
        # a direct-API-created row never shows false divergence.
        all_consistent = True
        for role in _MODEL_ROLES:
            role_key = f"{role}_model"
            intent = current.get(role_key)
            if not intent:
                continue
            routed = routing.get(role)
            if _strip_latest(routed or "") != _strip_latest(intent):
                all_consistent = False
        return routing, all_consistent
    except Exception:
        logger.warning("Could not load LiteLLM routing state", exc_info=True)
        # If there is stored intent we cannot verify → not consistent.
        consistent = not any(current.get(f"{role}_model") for role in _MODEL_ROLES)
        return {}, consistent


async def _load_installed_ollama_models(
    http: httpx.AsyncClient, ollama_url: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch installed Ollama models. issue is set on read failure."""
    installed: list[dict[str, Any]] = []
    try:
        resp = await http.get(f"{ollama_url}/api/tags", timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("models", []):
                installed.append(
                    {
                        "name": m.get("name", ""),
                        "size": m.get("size", 0),
                        "parameter_size": m.get("details", {}).get("parameter_size", ""),
                        "quantization": m.get("details", {}).get("quantization_level", ""),
                    }
                )
    except Exception:
        logger.warning("Could not load installed Ollama models", exc_info=True)
        return installed, "Could not load installed Ollama models."
    return installed, None


async def _load_ollama_runtime_count(
    http: httpx.AsyncClient, ollama_url: str
) -> tuple[int | None, str | None]:
    """Fetch the count of currently-loaded Ollama runtime models. issue is set on read failure."""
    try:
        resp = await http.get(f"{ollama_url}/api/ps", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            return len(data.get("models", [])), None
    except Exception:
        logger.warning("Could not load Ollama runtime status", exc_info=True)
        return None, "Could not load Ollama runtime status."
    return None, None


async def _load_num_ctx_overrides(db_pool: asyncpg.Pool, machine_id: str | None) -> dict[str, int]:
    """Fetch per-machine num_ctx overrides so fit_detail reflects the user's chosen context."""
    num_ctx_per_role: dict[str, int] = {}
    if not machine_id:
        return num_ctx_per_role
    num_ctx_keys = [f"llm.{machine_id}.{role}_num_ctx" for role in ("smart", "fast", "embed")]
    try:
        async with db_pool.acquire() as conn:
            num_ctx_rows = await conn.fetch(
                "SELECT key, value FROM user_config "
                "WHERE key = ANY($1::text[]) AND user_id IS NULL",
                num_ctx_keys,
            )
        for row in num_ctx_rows:
            raw_key = row["key"]  # e.g. "llm.host.smart_num_ctx"
            role_part = raw_key.split(".")[-1].replace("_num_ctx", "")
            raw_val = row["value"]
            try:
                num_ctx_per_role[role_part] = int(raw_val)
            except (TypeError, ValueError):
                pass
    except Exception:
        logger.warning("Could not load per-machine num_ctx config", exc_info=True)
    return num_ctx_per_role


def _stamp_live_provenance(
    entries: Iterable[Any], provider_lists: dict[str, ProviderModelList]
) -> None:
    """Mark entries that came from a provider's live list with their origin.

    ``source`` and ``fetched_at`` are not ``ModelStatusDict`` keys, so they are
    added to the payload dicts after the status machinery has run rather than
    forced into that shape.
    """
    fetched_at_by_id: dict[str, str | None] = {
        entry.id: (listing.fetched_at.isoformat() if listing.fetched_at else None)
        for listing in provider_lists.values()
        for entry in listing.entries
    }
    for item in entries:
        model_id = str(item["id"])
        if model_id in fetched_at_by_id:
            item["source"] = "provider"
            item["fetched_at"] = fetched_at_by_id[model_id]


async def _get_system_models_data(request: Request) -> SystemModelsWithDeliveryResponse:
    """Inner logic for /models — no auth check; callers must enforce admin gate."""
    ollama_url = get_paper_ingestion_settings().ollama_base_url
    http = request.app.state.http_client
    result: dict[str, Any] = {
        "status": "ok",
        "installed": [],
        "hardware": {},
        "current": {},
        "issues": {},
        "catalog": [],
        "recommendations": {},
        "embedding_contract": {
            "model": EMBEDDING_MODEL_NAME,
            "dimension": EMBEDDING_DIMENSION,
            "change_requires_reindex": True,
        },
    }

    cloud_api_keys: dict[str, bool] = {"anthropic": False, "openai": False, "google": False}
    result["current"], current_issue = await _load_current_model_assignments(
        request.app.state.db_pool
    )
    if current_issue:
        result["issues"]["current"] = current_issue

    result["delivery"] = await _load_model_delivery_state(request.app.state.db_pool)

    result["routing"], result["consistent"] = await _load_routing_truth(result["current"])

    cloud_api_keys = await _cloud_key_presence(request.app.state.db_pool)

    result["installed"], installed_issue = await _load_installed_ollama_models(http, ollama_url)
    if installed_issue:
        result["issues"]["installed"] = installed_issue

    ollama_running, runtime_issue = await _load_ollama_runtime_count(http, ollama_url)
    if ollama_running is not None:
        result["hardware"]["ollama_running"] = ollama_running
    if runtime_issue:
        result["issues"]["runtime"] = runtime_issue

    try:
        validate_embedding_configuration(
            model_name=EMBEDDING_MODEL_NAME,
            dimension=EMBEDDING_DIMENSION,
        )
    except RuntimeError as exc:
        result["issues"]["embedding_config"] = str(exc)

    hardware: HardwareInfo = await async_get_cached_hardware(request.app.state)
    result["hardware"].update(hardware.to_dict())

    num_ctx_per_role = await _load_num_ctx_overrides(request.app.state.db_pool, hardware.machine_id)

    provider_lists = await fetch_all_provider_models(
        [provider.id for provider in PROVIDER_REGISTRY if cloud_api_keys.get(provider.id)],
        db_pool=request.app.state.db_pool,
        http_client=http,
    )
    extra_entries = tuple(entry for listing in provider_lists.values() for entry in listing.entries)
    result["provider_lists"] = {
        provider_id: listing.summary() for provider_id, listing in provider_lists.items()
    }

    result["catalog"] = build_model_statuses(
        installed=result["installed"],
        current=result["current"],
        embedding_model_name=EMBEDDING_MODEL_NAME,
        hardware=hardware,
        cloud_api_keys=cloud_api_keys,
        num_ctx_per_role=num_ctx_per_role,
        extra_entries=extra_entries,
    )
    result["recommendations"] = {
        role: recommendations_for_role(
            role,  # type: ignore[arg-type]
            installed=result["installed"],
            current=result["current"],
            embedding_model_name=EMBEDDING_MODEL_NAME,
            hardware=hardware,
            cloud_api_keys=cloud_api_keys,
            extra_entries=extra_entries,
        )
        for role in ("smart", "fast", "embed")
    }
    _stamp_live_provenance(result["catalog"], provider_lists)
    for entries in result["recommendations"].values():
        _stamp_live_provenance(entries, provider_lists)
    # Advisory per-VRAM default-model recommendation.  Convert vram_gb → MiB
    # for recommend_models(); pass None when the probe reported 0 (CPU-only /
    # probe failure) so the None-path safe-default logic fires rather than
    # treating 0 MiB as a concrete GPU measurement.
    vram_mb_for_rec: int | None = round(hardware.vram_gb * 1024) if hardware.vram_gb > 0.0 else None
    hw_rec = recommend_models(vram_mb_for_rec)
    result["hardware_recommendation"] = {
        "vram_mb": hw_rec.vram_mb,
        "bucket": hw_rec.bucket.name,
        "summary": hw_rec.summary,
        "aliases": [
            {
                "alias": a.alias,
                "model": a.model,
                "confirm_on_target": a.confirm_on_target,
                "notes": a.notes,
            }
            for a in hw_rec.aliases
        ],
    }
    result["status"] = "ok" if not result["issues"] else "degraded"
    return SystemModelsWithDeliveryResponse.model_validate(result)
