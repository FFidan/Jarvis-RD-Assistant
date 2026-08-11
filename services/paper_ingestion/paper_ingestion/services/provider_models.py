"""Live model lists for configured cloud providers, with a bounded in-process cache."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Any, Literal, cast, get_args

import httpx
from jarvis_common.maintenance import ensure_outbound_egress_allowed
from jarvis_common.model_catalog import (
    MetadataField,
    MetadataFieldSource,
    ModelCatalogEntry,
    Provider,
    Role,
)

from paper_ingestion.services.litellm_config import (
    get_provider_api_key,
    get_provider_base_url,
)
from paper_ingestion.services.llm_provider_registry import (
    ProviderDefinition,
    provider_for_id,
    provider_for_prefix,
    validate_custom_openai_base_url_for_outbound,
)
from paper_ingestion.services.model_identifiers import (
    NAMESPACED_PROVIDER_KINDS,
    validate_model_name,
    validate_namespaced_model_suffix,
)
from paper_ingestion.services.model_lifecycle import MODEL_CATALOG, normalize_model_tag
from paper_ingestion.services.provider_test import _OPENAI_COMPATIBLE_MODEL_URLS

__all__ = [
    "ProviderModelList",
    "classify_live_model",
    "fetch_all_provider_models",
    "fetch_provider_models",
    "live_entry_for_model",
    "models_url_for",
    "reset_provider_model_cache",
]

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300.0
# Failures are cached too, under a much shorter TTL: an unreachable provider
# would otherwise be retried on every models-page load, each retry costing up
# to the fetch budget in page latency. Kept short so a provider whose operator
# just fixed its key or endpoint looks broken for at most this long.
_FAILURE_CACHE_TTL_SECONDS = 30.0
_MAX_MODELS_PER_PROVIDER = 500
_MAX_MODEL_ID_CHARS = 128
# The first body-parsing path in this codebase: a hostile or misconfigured
# endpoint chooses the payload, so cap the bytes read and the pages followed
# before any of it becomes Python objects.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TOKENS_PER_MILLION = Decimal(1_000_000)
_MAX_PRICE_INPUT_CHARS = 64
_MAX_PRICE_SIGNIFICANT_DIGITS = 32
_MAX_PRICE_ADJUSTED_EXPONENT = 100
_MAX_PAGES = 20
_PER_PROVIDER_TIMEOUT_SECONDS = 8.0
# httpx's timeout bounds each read, not the whole exchange, so a server dripping
# bytes below that interval can stream for as long as it likes across as many
# pages as the cap allows. One provider is fetched on its own for a single
# assignment save, where the sweep's total budget does not apply, so the ceiling
# has to live here.
_PER_PROVIDER_BUDGET_SECONDS = 10.0
_TOTAL_FETCH_BUDGET_SECONDS = 12.0

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"
_GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

_ID_KEYS = ("id", "name", "model")
_LIST_KEYS = ("data", "models", "list")

Capability = Literal["chat", "embed", "other", "unknown"]
ProviderPricing = tuple[str | None, str | None, str | None]

_EMBED_MARKER = "embed"
# Conservative deny-list of non-chat model families, matched case-insensitively
# as a substring of the final path segment.
_NON_CHAT_MARKERS = (
    "whisper",
    "tts",
    "dall-e",
    "moderation",
    "rerank",
    "transcribe",
    "audio",
    "realtime",
    "image",
    "clip",
)
# Providers whose /models response reports no capabilities: recognise their
# published chat families and treat everything else as unknown. Entries here are
# a fallback only — a provider that describes its own models is read directly,
# so this list does not have to keep pace with every family a vendor launches.
_CHAT_ID_FAMILIES: Mapping[str, tuple[str, ...]] = {
    "openai": ("gpt-", "chatgpt-"),
    "deepseek": ("deepseek-",),
    "mistral": (
        "mistral-",
        "ministral-",
        "magistral-",
        "devstral-",
        "pixtral-",
        "codestral",
        "open-mistral",
        "open-mixtral",
    ),
    "moonshot": ("moonshot-", "kimi-"),
    "zai": ("glm-",),
}
_OPENAI_REASONING_FAMILY = re.compile(r"o\d")

_EMBED_NOTES = (
    "Cloud embedding models cannot be assigned: the embedding role is locked "
    "to the built-in catalog."
)
_UNKNOWN_CAPABILITY_NOTES = (
    "Capabilities were not reported, so JARVIS cannot safely assign this model to a role."
)

# What each capability earns a model: the roles it may hold, whether it can be
# assigned at all, and the blocker text shown when it cannot. One ruling, so the
# picker and the save gate cannot describe the same model differently.
_CAPABILITY_RULING: dict[Capability, tuple[tuple[Role, ...], bool, str]] = {
    "chat": (("smart", "fast"), True, ""),
    "embed": (("embed",), False, _EMBED_NOTES),
    "unknown": (("smart", "fast"), False, _UNKNOWN_CAPABILITY_NOTES),
}

_PROVIDER_LITERALS: frozenset[str] = frozenset(get_args(Provider))


@dataclass(frozen=True, slots=True)
class ProviderModelList:
    """One provider's live model listing, or the reason there is none.

    ``fetched_at`` is ``None`` when the list was never successfully fetched.
    ``truncated`` marks a listing cut short by the per-provider cap while the
    provider still advertised further pages, so callers can say so instead of
    implying completeness.
    """

    provider: str
    entries: tuple[ModelCatalogEntry, ...] = ()
    fetched_at: datetime | None = None
    error: str | None = None
    truncated: bool = False
    excluded: Mapping[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Return the JSON-safe per-provider summary served to the UI."""
        return {
            "model_count": len(self.entries),
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "error": self.error,
            "truncated": self.truncated,
            "excluded": dict(self.excluded),
        }


class _ProviderListError(Exception):
    """Recoverable failure while reading one provider's model list."""


# provider id -> (expiry deadline on the cache clock, listing)
_cache: dict[str, tuple[float, ProviderModelList]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _cache_clock() -> float:
    """Monotonic clock backing the listing cache TTL."""
    return time.monotonic()


def reset_provider_model_cache() -> None:
    """Drop every cached listing so the next call fetches afresh."""
    _cache.clear()
    _locks.clear()


def models_url_for(provider: ProviderDefinition, base_url: str | None) -> str | None:
    """Return the model-list URL for *provider*, or ``None`` when it has none.

    Covers every registry entry, including the custom endpoint whose URL is
    derived from the admin-configured base URL and is absent until one is set.
    """
    if provider.id == "anthropic":
        return _ANTHROPIC_MODELS_URL
    if provider.id == "google":
        return _GOOGLE_MODELS_URL
    if provider.id == "custom_openai_compatible":
        return f"{base_url.rstrip('/')}/models" if base_url else None
    return _OPENAI_COMPATIBLE_MODEL_URLS.get(provider.id)


def _auth_headers(provider: ProviderDefinition, api_key: str | None) -> dict[str, str]:
    """Return the provider's auth headers, or none at all when there is no key."""
    if not api_key:
        return {}
    if provider.id == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION}
    if provider.id == "google":
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def _parse_model_items(payload: Any) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Extract ``(model_id, raw_item)`` pairs shape-agnostically.

    Returns ``(items, observed_top_level_keys)``. Providers are not switched on:
    several envelopes are unverified, so accept any of the known list keys and
    any of the known id keys, and report what was actually seen when nothing
    parses. The raw item is kept because the capability classifier reads
    provider metadata off it; a bare-string item gets ``{}``.
    """
    if not isinstance(payload, dict):
        return [], []
    observed = sorted(str(key) for key in payload)
    raw_items: list[Any] = []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            raw_items = value
            break
    items: list[tuple[str, dict[str, Any]]] = []
    for item in raw_items:
        if isinstance(item, str) and item:
            items.append((item, {}))
        elif isinstance(item, dict):
            for key in _ID_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value:
                    # Google returns "models/gemini-..."; strip the collection prefix.
                    items.append((value.removeprefix("models/"), item))
                    break
    return items, observed


def _more_pages_offered(payload: Any) -> bool:
    """Return whether the payload says more models exist beyond this page.

    Some providers advertise the next page as a URL rather than as parameters we
    know how to rebuild. We cannot follow it, but we can decline to call a list
    complete when the provider itself says it is not.
    """
    if not isinstance(payload, dict):
        return False
    links = payload.get("links")
    return isinstance(links, dict) and bool(links.get("next"))


def _next_page_params(payload: Any) -> dict[str, str] | None:
    """Return the query parameters for the next page, or ``None`` at the end."""
    if not isinstance(payload, dict):
        return None
    if payload.get("has_more") is True:
        last_id = payload.get("last_id")
        if isinstance(last_id, str) and last_id:
            return {"after_id": last_id}
    token = payload.get("nextPageToken")
    if isinstance(token, str) and token:
        return {"pageToken": token}
    return None


def _declared_capability(raw: Mapping[str, Any]) -> Capability | None:
    """Read the capability the entry itself declares, or ``None`` when it declares none.

    Reading what a provider says beats matching id prefixes: a prefix list is a
    standing bet that no vendor will ever name a family we have not heard of,
    and that bet loses on every launch.
    """
    methods = raw.get("supportedGenerationMethods")
    if isinstance(methods, list):
        names = {str(method) for method in methods}
        if "generateContent" in names:
            return "chat"
        if "embedContent" in names:
            return "embed"
        return "unknown"

    capabilities = raw.get("capabilities")
    if isinstance(capabilities, Mapping) and "completion_chat" in capabilities:
        return "chat" if capabilities["completion_chat"] else "other"

    return None


def _capability_from_id(provider_id: str, segment: str) -> Capability:
    """Decide from the id alone: the non-chat deny-list first, then published chat families."""
    if _EMBED_MARKER in segment:
        return "embed"
    if any(marker in segment for marker in _NON_CHAT_MARKERS):
        return "other"
    families = _CHAT_ID_FAMILIES.get(provider_id)
    if families is None:
        # Routers and self-hosted endpoints serve chat completions; ids that
        # survived the deny-list are taken as chat.
        return "chat" if provider_id in {"openrouter", "custom_openai_compatible"} else "unknown"
    if segment.startswith(families):
        return "chat"
    if provider_id == "openai" and _OPENAI_REASONING_FAMILY.match(segment):
        return "chat"
    return "unknown"


def classify_live_model(provider_id: str, model_id: str, raw: Mapping[str, Any]) -> Capability:
    """Classify what a live-listed model can do, preferring the provider's own signal.

    Entry metadata wins where the envelope carries it; otherwise provider-level
    facts, a conservative non-chat deny-list, and published chat families decide.
    Anything left over is ``"unknown"`` and is never offered for a role.
    """
    declared = _declared_capability(raw)
    if declared is not None:
        return declared
    # Anthropic's model list contains only chat models.
    if provider_id == "anthropic":
        return "chat"
    return _capability_from_id(provider_id, model_id.rsplit("/", 1)[-1].lower())


def _as_provider(provider_id: str) -> Provider:
    """Narrow a registry id to the catalog's ``Provider`` literal, or raise."""
    if provider_id not in _PROVIDER_LITERALS:
        raise ValueError(f"Provider {provider_id!r} has no model-catalog counterpart")
    return cast(Provider, provider_id)


def live_model_entry(
    provider_id: Provider,
    model_id: str,
    *,
    fetched_at: datetime | None,
    capability: Capability,
    pricing: ProviderPricing = (None, None, None),
    raw: Mapping[str, Any] | None = None,
) -> ModelCatalogEntry:
    """Build a catalog entry for one live-listed provider model."""
    roles, assignable, notes = _CAPABILITY_RULING[capability]
    input_price, output_price, price_source = pricing
    raw = raw or {}
    display_name = _live_display_name(provider_id, raw) or model_id
    context_tokens = _live_context_tokens(provider_id, raw)
    description = _live_description(provider_id, raw)
    lifecycle = _live_lifecycle(provider_id, raw)
    capabilities = _live_capabilities(provider_id, raw)
    field_sources: dict[MetadataField, MetadataFieldSource] = {}
    fetched_at_text = fetched_at.isoformat() if fetched_at else ""
    for name, value in (
        ("context_tokens", context_tokens),
        ("description", description),
        ("capabilities", capabilities),
        ("lifecycle", lifecycle),
        ("input_price_per_million", input_price),
        ("output_price_per_million", output_price),
    ):
        if value not in (None, "", (), 0):
            field_sources[cast(MetadataField, name)] = {
                "kind": "api_reported",
                "fetched_at": fetched_at_text,
            }
    return ModelCatalogEntry(
        id=f"{provider_for_id(provider_id).assignment_prefix}{model_id}",
        name=display_name,
        provider=provider_id,
        ollama_tag=None,
        roles=roles,
        vram_gb=0.0,
        disk_gb=0.0,
        context_tokens=context_tokens,
        license="Provider terms apply",
        tier=0,
        description=description or "Offered by this provider's live model list.",
        notes=notes,
        last_reviewed=fetched_at.date().isoformat() if fetched_at else "",
        phase="advanced",
        assignable=assignable,
        input_price_per_million=input_price,
        output_price_per_million=output_price,
        price_source=price_source,
        capabilities=capabilities,
        lifecycle=lifecycle,
        field_sources=field_sources,
    )


def _live_context_tokens(provider_id: Provider, raw: Mapping[str, Any]) -> int:
    """Return a documented provider context limit, otherwise zero."""
    field_names = {
        "google": ("inputTokenLimit",),
        "mistral": ("max_context_length",),
        "moonshot": ("context_length",),
        "openrouter": ("context_length", "context_window"),
    }.get(provider_id, ())
    value = next((raw.get(name) for name in field_names if name in raw), 0)
    return value if isinstance(value, int) and 0 < value <= 10_000_000 else 0


def _live_display_name(provider_id: Provider, raw: Mapping[str, Any]) -> str:
    """Return a bounded provider display name when the documented field is present."""
    field_name = {
        "anthropic": "display_name",
        "google": "displayName",
        "openrouter": "name",
    }.get(provider_id)
    value = raw.get(field_name) if field_name is not None else None
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not 0 < len(candidate) <= 256 or candidate.startswith("models/"):
        return ""
    return candidate


def _live_description(provider_id: Provider, raw: Mapping[str, Any]) -> str:
    """Return a bounded documented description without treating arbitrary values as text."""
    value = raw.get("description") if provider_id in {"google", "openrouter"} else None
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= 4_000 else ""


def _live_capabilities(provider_id: Provider, raw: Mapping[str, Any]) -> tuple[str, ...]:
    """Normalize only provider-reported capability fields into display labels."""
    values: list[str] = []
    if provider_id == "openrouter" and isinstance(architecture := raw.get("architecture"), Mapping):
        for field_name in ("input_modalities", "output_modalities"):
            value = architecture.get(field_name)
            if isinstance(value, list):
                values.extend(item for item in value if isinstance(item, str) and len(item) <= 64)
    elif provider_id == "google":
        methods = raw.get("supportedGenerationMethods")
        if isinstance(methods, list):
            values.extend(item for item in methods if isinstance(item, str) and len(item) <= 64)
        if raw.get("thinking") is True:
            values.append("thinking")
    elif provider_id == "mistral" and isinstance(capabilities := raw.get("capabilities"), Mapping):
        values.extend(
            name.removeprefix("completion_").replace("_", " ")
            for name, enabled in capabilities.items()
            if enabled is True and isinstance(name, str) and len(name) <= 64
        )
    elif provider_id == "moonshot":
        for field_name, label in (
            ("supports_image_in", "image input"),
            ("supports_video_in", "video input"),
            ("supports_reasoning", "reasoning"),
        ):
            if raw.get(field_name) is True:
                values.append(label)
    return tuple(dict.fromkeys(values))


def _live_lifecycle(provider_id: Provider, raw: Mapping[str, Any]) -> str | None:
    """Expose only explicit lifecycle signals instead of guessing from model names."""
    if provider_id == "mistral" and raw.get("archived") is True:
        return "deprecated"
    if provider_id == "openrouter" and raw.get("status") in {"active", "deprecated"}:
        return cast(str, raw["status"])
    expiration = raw.get("expiration_date") if provider_id == "openrouter" else None
    if isinstance(expiration, str) and 0 < len(expiration) <= 32:
        return f"expires {expiration}"
    return None


def _reviewed_field_source(entry: ModelCatalogEntry) -> MetadataFieldSource:
    """Build the conservative provenance used when reviewed metadata fills a live gap."""
    return {"kind": "reviewed_catalog", "reviewed_at": entry.last_reviewed}


def _merge_live_with_reviewed(
    live: ModelCatalogEntry, reviewed: ModelCatalogEntry
) -> ModelCatalogEntry:
    """Merge model metadata while preserving reviewed routing and valid live facts."""
    sources = dict(reviewed.field_sources)
    sources.update(live.field_sources)
    reviewed_values: dict[str, object] = {
        "context_tokens": reviewed.context_tokens,
        "description": reviewed.description,
        "capabilities": reviewed.capabilities,
        "lifecycle": reviewed.lifecycle,
        "input_price_per_million": reviewed.input_price_per_million,
        "output_price_per_million": reviewed.output_price_per_million,
    }
    for field_name, value in reviewed_values.items():
        if value not in (None, "", (), 0) and field_name not in sources:
            sources[cast(MetadataField, field_name)] = _reviewed_field_source(reviewed)
    return replace(
        reviewed,
        name=live.name if live.name != live.id.rsplit("/", 1)[-1] else reviewed.name,
        context_tokens=live.context_tokens or reviewed.context_tokens,
        description=live.description
        if live.field_sources.get("description", {}).get("kind") == "api_reported"
        else reviewed.description,
        input_price_per_million=live.input_price_per_million or reviewed.input_price_per_million,
        output_price_per_million=live.output_price_per_million or reviewed.output_price_per_million,
        price_source=live.price_source or reviewed.price_source,
        capabilities=live.capabilities or reviewed.capabilities,
        lifecycle=live.lifecycle or reviewed.lifecycle,
        field_sources=sources,
    )


def _normalize_openrouter_price(value: object) -> str | None:
    """Convert one documented OpenRouter per-token price into a per-million string."""
    if not isinstance(value, str) or len(value) > _MAX_PRICE_INPUT_CHARS:
        return None
    try:
        price = Decimal(value)
        significant_digits = len(price.as_tuple().digits)
        if (
            significant_digits > _MAX_PRICE_SIGNIFICANT_DIGITS
            or abs(price.adjusted()) > _MAX_PRICE_ADJUSTED_EXPONENT
        ):
            return None
        normalized = format((price * _TOKENS_PER_MILLION).normalize(), "f")
    except (DecimalException, ValueError):
        return None
    if not price.is_finite() or price < 0:
        return None
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _openrouter_pricing(raw: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return verified OpenRouter pricing only when both documented rates are usable."""
    pricing = raw.get("pricing")
    if not isinstance(pricing, Mapping):
        return None, None, None
    input_price = _normalize_openrouter_price(pricing.get("prompt"))
    output_price = _normalize_openrouter_price(pricing.get("completion"))
    if input_price is None or output_price is None:
        return None, None, None
    return input_price, output_price, "openrouter"


def _validation_error(provider: ProviderDefinition, model_id: str) -> str | None:
    """Return why *model_id* is unusable for *provider*, or ``None`` when valid."""
    if len(model_id) > _MAX_MODEL_ID_CHARS:
        return "model id exceeds the maximum length"
    try:
        if provider.kind in NAMESPACED_PROVIDER_KINDS:
            validate_namespaced_model_suffix(model_id)
        else:
            validate_model_name(model_id)
    except ValueError as exc:
        return str(exc)
    return None


_CATALOG_BY_ID: dict[str, ModelCatalogEntry] = {
    normalize_model_tag(entry.id): entry for entry in MODEL_CATALOG
}


def _build_entries(
    provider: ProviderDefinition,
    items: Sequence[tuple[str, dict[str, Any]]],
    fetched_at: datetime,
) -> tuple[tuple[ModelCatalogEntry, ...], dict[str, int]]:
    """Classify, validate and synthesize catalog entries for one provider's listing."""
    catalog_provider = _as_provider(provider.id)
    entries: list[ModelCatalogEntry] = []
    excluded = {"non_chat": 0, "unknown": 0, "invalid": 0}
    seen: set[str] = set()
    for model_id, raw in items:
        reason = _validation_error(provider, model_id)
        if reason is not None:
            excluded["invalid"] += 1
            continue
        capability = classify_live_model(provider.id, model_id, raw)
        if capability == "other":
            excluded["non_chat"] += 1
            continue
        assignment_id = f"{provider.assignment_prefix}{model_id}"
        normalized = normalize_model_tag(assignment_id)
        if normalized in seen:
            continue
        seen.add(normalized)
        if capability == "unknown":
            excluded["unknown"] += 1
        pricing = _openrouter_pricing(raw) if provider.id == "openrouter" else (None, None, None)
        live = live_model_entry(
            catalog_provider,
            model_id,
            fetched_at=fetched_at,
            capability=capability,
            pricing=pricing,
            raw=raw,
        )
        reviewed = _CATALOG_BY_ID.get(normalized)
        entries.append(_merge_live_with_reviewed(live, reviewed) if reviewed is not None else live)
    return tuple(entries), excluded


async def _read_json_page(
    http_client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    params: Mapping[str, str] | None,
) -> Any:
    """GET one page, refusing a body larger than the byte cap before decoding it."""
    body = bytearray()
    async with http_client.stream(
        "GET",
        url,
        headers=dict(headers),
        params=dict(params) if params else None,
        timeout=_PER_PROVIDER_TIMEOUT_SECONDS,
    ) as response:
        if response.status_code >= 400:
            raise _ProviderListError(f"provider returned HTTP {response.status_code}")
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise _ProviderListError("provider model list exceeded the response size limit")
    try:
        return json.loads(bytes(body))
    except ValueError as exc:
        raise _ProviderListError("provider model list was not valid JSON") from exc


async def _collect_items(
    http_client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """Follow the provider's pagination up to the cap; returns (items, truncated)."""
    items: list[tuple[str, dict[str, Any]]] = []
    params: dict[str, str] | None = None
    truncated = False
    for _ in range(_MAX_PAGES):
        payload = await _read_json_page(http_client, url, headers=headers, params=params)
        page_items, observed = _parse_model_items(payload)
        if not page_items and not items:
            logger.warning(
                "provider model list yielded no models; observed top-level keys: %s",
                observed,
            )
        items.extend(page_items)
        params = _next_page_params(payload)
        if _more_pages_offered(payload) and params is None:
            # The provider says there is more but describes the next page in a
            # form we cannot rebuild. Report the list as partial rather than
            # presenting what we did read as everything.
            truncated = True
            break
        if len(items) >= _MAX_MODELS_PER_PROVIDER:
            # A single unpaginated page can overshoot the cap on its own, so the
            # slice below drops models even when there is no next page to ask for.
            truncated = params is not None or len(items) > _MAX_MODELS_PER_PROVIDER
            break
        if params is None:
            break
    else:
        truncated = True
    return items[:_MAX_MODELS_PER_PROVIDER], truncated


def _cached(provider_id: str) -> ProviderModelList | None:
    cached = _cache.get(provider_id)
    return cached[1] if cached is not None else None


def _fresh_cached(provider_id: str) -> ProviderModelList | None:
    cached = _cache.get(provider_id)
    if cached is None:
        return None
    expires_at, listing = cached
    return listing if _cache_clock() < expires_at else None


def _fetch_failure_reason(exc: BaseException) -> str:
    """Describe a fetch failure in terms the operator sees beside the provider."""
    if isinstance(exc, TimeoutError):
        return "provider model list timed out"
    if isinstance(exc, _ProviderListError):
        return str(exc)
    return "provider request failed"


def _stale_or_error(provider_id: str, error: str) -> ProviderModelList:
    """Serve the previous listing (with its original timestamp) or an error entry.

    Either way the result is re-cached under the failure TTL, so a failing
    provider is retried at most once per that window rather than on every
    models-page load.
    """
    stale = _cached(provider_id)
    result = (
        stale
        if stale is not None and stale.entries
        else ProviderModelList(provider=provider_id, error=error)
    )
    _cache[provider_id] = (_cache_clock() + _FAILURE_CACHE_TTL_SECONDS, result)
    return result


async def fetch_provider_models(
    provider_id: str,
    *,
    db_pool: Any,
    http_client: httpx.AsyncClient,
) -> ProviderModelList:
    """Return *provider_id*'s live model list, fetching it at most once per TTL.

    Concurrent callers for the same provider coalesce behind one lock, so N
    settings-page loads issue one outbound sweep rather than N.
    """
    provider = provider_for_id(provider_id)
    lock = _locks.setdefault(provider_id, asyncio.Lock())
    async with lock:
        fresh = _fresh_cached(provider_id)
        if fresh is not None:
            return fresh

        ensure_outbound_egress_allowed("cloud provider model list")

        base_url = await get_provider_base_url(provider_id, db_pool)
        if provider.id == "custom_openai_compatible" and base_url:
            try:
                await validate_custom_openai_base_url_for_outbound(base_url)
            except ValueError as exc:
                return _stale_or_error(provider_id, str(exc))

        url = models_url_for(provider, base_url)
        if url is None:
            return _stale_or_error(provider_id, "no model list endpoint is configured")

        api_key = await get_provider_api_key(provider_id, db_pool)
        try:
            async with asyncio.timeout(_PER_PROVIDER_BUDGET_SECONDS):
                items, truncated = await _collect_items(
                    http_client, url, headers=_auth_headers(provider, api_key)
                )
        except (TimeoutError, _ProviderListError, httpx.HTTPError) as exc:
            reason = _fetch_failure_reason(exc)
            logger.warning("provider %s model list failed: %s", provider_id, reason, exc_info=True)
            return _stale_or_error(provider_id, reason)

        fetched_at = datetime.now(UTC)
        entries, excluded = _build_entries(provider, items, fetched_at)
        listing = ProviderModelList(
            provider=provider_id,
            entries=entries,
            fetched_at=fetched_at,
            truncated=truncated,
            excluded=excluded,
        )
        _cache[provider_id] = (_cache_clock() + _CACHE_TTL_SECONDS, listing)
        return listing


async def fetch_all_provider_models(
    provider_ids: Sequence[str],
    *,
    db_pool: Any,
    http_client: httpx.AsyncClient,
) -> dict[str, ProviderModelList]:
    """Fetch every configured provider's list concurrently under one total budget.

    The models page blocks on this, so a slow provider yields its cached list or
    an error entry rather than holding the whole response open.
    """
    if not provider_ids:
        return {}
    tasks = {
        provider_id: asyncio.ensure_future(
            fetch_provider_models(provider_id, db_pool=db_pool, http_client=http_client)
        )
        for provider_id in provider_ids
    }
    try:
        async with asyncio.timeout(_TOTAL_FETCH_BUDGET_SECONDS):
            await asyncio.gather(*tasks.values(), return_exceptions=True)
    except TimeoutError:
        logger.warning("provider model list sweep exceeded its total budget")

    results: dict[str, ProviderModelList] = {}
    for provider_id, task in tasks.items():
        if not task.done():
            task.cancel()
            results[provider_id] = _stale_or_error(provider_id, "provider model list timed out")
            continue
        if task.cancelled():
            results[provider_id] = _stale_or_error(provider_id, "provider model list timed out")
            continue
        error = task.exception()
        if error is None:
            results[provider_id] = task.result()
        else:
            logger.warning("provider %s model list raised %s", provider_id, type(error).__name__)
            results[provider_id] = _stale_or_error(provider_id, "provider model list unavailable")
    return results


async def live_entry_for_model(
    model_id: str,
    *,
    db_pool: Any,
    http_client: httpx.AsyncClient,
) -> ModelCatalogEntry | None:
    """Return the synthesized catalog entry for a live-listed id, or ``None``.

    A provider that cannot be reached falls back to its cached listing; it never
    raises, so an unreachable provider cannot block an assignment.
    """
    if "/" not in model_id:
        return None
    prefix, _ = model_id.split("/", 1)
    provider = provider_for_prefix(prefix)
    if provider is None:
        return None
    try:
        listing = await fetch_provider_models(provider.id, db_pool=db_pool, http_client=http_client)
    except Exception:
        logger.warning("could not consult %s model list", provider.id, exc_info=True)
        listing = _cached(provider.id) or ProviderModelList(provider=provider.id)
    normalized = normalize_model_tag(model_id)
    for entry in listing.entries:
        if normalize_model_tag(entry.id) == normalized:
            return entry
    return None
