"""Capability-gated, bounded provider account snapshots."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from jarvis_common.maintenance import OutboundEgressBlockedError, ensure_outbound_egress_allowed
from jarvis_common.pinned_transport import PUBLIC_ONLY, pinned_async_client

from paper_ingestion.services.litellm_config import get_provider_api_key
from paper_ingestion.services.llm_provider_registry import AccountCapability, provider_for_id

_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_ACCOUNT_TIMEOUT_SECONDS = 10.0
_MAX_ACCOUNT_RESPONSE_BYTES = 64 * 1024
_OPENROUTER_ACCOUNT_FIELDS = (
    "is_free_tier",
    "usage",
    "usage_daily",
    "usage_weekly",
    "usage_monthly",
    "limit",
    "limit_remaining",
    "limit_reset",
    "expires_at",
)
_ACCOUNT_VALUE = bool | int | float | str | None


@dataclass(frozen=True, slots=True)
class ProviderAccountSnapshot:
    """Sanitized provider account data suitable for the administrator UI."""

    provider: str
    capability: AccountCapability
    data: dict[str, _ACCOUNT_VALUE] = field(default_factory=dict)
    error_code: str | None = None


async def _read_openrouter_key(client: httpx.AsyncClient, api_key: str) -> Mapping[str, Any]:
    """Fetch and size-bound OpenRouter's current-key response before decoding it."""
    body = bytearray()
    async with client.stream(
        "GET",
        _OPENROUTER_KEY_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_ACCOUNT_TIMEOUT_SECONDS,
    ) as response:
        if response.status_code >= 400:
            raise _AccountFetchError("provider_http_error")
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_ACCOUNT_RESPONSE_BYTES:
                raise _AccountFetchError("provider_response_too_large")
    try:
        payload = json.loads(bytes(body))
    except ValueError as exc:
        raise _AccountFetchError("provider_response_invalid") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), Mapping):
        raise _AccountFetchError("provider_response_invalid")
    return payload["data"]


class _AccountFetchError(Exception):
    """A stable, sanitized account-snapshot failure code."""


def _allowlisted_openrouter_data(payload: Mapping[str, Any]) -> dict[str, _ACCOUNT_VALUE]:
    """Discard every current-key field outside the documented non-identity allow-list."""
    return {
        name: value
        for name in _OPENROUTER_ACCOUNT_FIELDS
        if name in payload and _safe_account_value(value := payload[name])
    }


def _safe_account_value(value: object) -> bool:
    """Keep JSON scalars only; non-finite floats are not safe account metadata."""
    return (
        value is None
        or isinstance(value, bool | int | str)
        or (isinstance(value, float) and math.isfinite(value))
    )


async def fetch_provider_account(provider_id: str, *, db_pool: Any) -> ProviderAccountSnapshot:
    """Return a capability-gated account snapshot without sharing the app HTTP client."""
    provider = provider_for_id(provider_id)
    if provider.account_capability != "current_key":
        return ProviderAccountSnapshot(provider=provider.id, capability=provider.account_capability)

    try:
        ensure_outbound_egress_allowed("cloud provider account snapshot")
    except OutboundEgressBlockedError:
        return ProviderAccountSnapshot(
            provider=provider.id,
            capability=provider.account_capability,
            error_code="egress_blocked",
        )

    try:
        api_key = await get_provider_api_key(provider.id, db_pool)
    except Exception:  # noqa: BLE001 - secret resolution must not become an API detail
        api_key = None
    if not api_key:
        return ProviderAccountSnapshot(
            provider=provider.id,
            capability=provider.account_capability,
            error_code="api_key_unavailable",
        )

    try:
        ensure_outbound_egress_allowed("cloud provider account snapshot")
        async with asyncio.timeout(_ACCOUNT_TIMEOUT_SECONDS):
            async with pinned_async_client(
                PUBLIC_ONLY, timeout=httpx.Timeout(_ACCOUNT_TIMEOUT_SECONDS)
            ) as client:
                payload = await _read_openrouter_key(client, api_key)
    except OutboundEgressBlockedError:
        return ProviderAccountSnapshot(
            provider=provider.id,
            capability=provider.account_capability,
            error_code="egress_blocked",
        )
    except _AccountFetchError as exc:
        return ProviderAccountSnapshot(
            provider=provider.id,
            capability=provider.account_capability,
            error_code=str(exc),
        )
    except TimeoutError:
        return ProviderAccountSnapshot(
            provider=provider.id,
            capability=provider.account_capability,
            error_code="provider_request_timed_out",
        )
    except httpx.HTTPError:
        return ProviderAccountSnapshot(
            provider=provider.id,
            capability=provider.account_capability,
            error_code="provider_request_failed",
        )

    return ProviderAccountSnapshot(
        provider=provider.id,
        capability=provider.account_capability,
        data=_allowlisted_openrouter_data(payload),
    )
