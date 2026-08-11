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
from paper_ingestion.services.llm_provider_registry import (
    AccountCapability,
    ProviderDefinition,
    provider_for_id,
)

_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
_MOONSHOT_BALANCE_URL = "https://api.moonshot.ai/v1/users/me/balance"
_DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
_ACCOUNT_URLS = {
    "openrouter": _OPENROUTER_KEY_URL,
    "moonshot": _MOONSHOT_BALANCE_URL,
    "deepseek": _DEEPSEEK_BALANCE_URL,
}
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
_MOONSHOT_BALANCE_FIELDS = ("available_balance", "voucher_balance", "cash_balance")
_ACCOUNT_VALUE = bool | int | float | str | None
_DEEPSEEK_CURRENCIES = frozenset({"CNY", "USD"})


@dataclass(frozen=True, slots=True)
class ProviderAccountSnapshot:
    """Sanitized provider account data suitable for the administrator UI."""

    provider: str
    capability: AccountCapability
    data: dict[str, _ACCOUNT_VALUE] = field(default_factory=dict)
    error_code: str | None = None


def _snapshot(
    provider: ProviderDefinition,
    *,
    data: dict[str, _ACCOUNT_VALUE] | None = None,
    error_code: str | None = None,
) -> ProviderAccountSnapshot:
    """Build one account response without repeating provider identity fields."""
    return ProviderAccountSnapshot(
        provider=provider.id,
        capability=provider.account_capability,
        data=data or {},
        error_code=error_code,
    )


async def _read_account_payload(
    client: httpx.AsyncClient, *, url: str, api_key: str
) -> Mapping[str, Any]:
    """Fetch one sealed account endpoint with a bounded bearer-authenticated GET."""
    body = bytearray()
    async with client.stream(
        "GET",
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_ACCOUNT_TIMEOUT_SECONDS,
    ) as response:
        if response.status_code >= 400:
            raise _AccountFetchError(_provider_http_error_code(response.status_code))
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_ACCOUNT_RESPONSE_BYTES:
                raise _AccountFetchError("provider_response_too_large")
    try:
        payload = json.loads(bytes(body))
    except ValueError as exc:
        raise _AccountFetchError("provider_response_invalid") from exc
    if not isinstance(payload, Mapping):
        raise _AccountFetchError("provider_response_invalid")
    return payload


class _AccountFetchError(Exception):
    """A stable, sanitized account-snapshot failure code."""


def _provider_http_error_code(status_code: int) -> str:
    """Classify provider HTTP failures without retaining response details."""
    if status_code in {401, 403}:
        return "provider_authentication_failed"
    if status_code == 402:
        return "provider_payment_required"
    if status_code == 429:
        return "provider_rate_limited"
    if status_code >= 500:
        return "provider_unavailable"
    return "provider_http_error"


def _allowlisted_openrouter_data(payload: Mapping[str, Any]) -> dict[str, _ACCOUNT_VALUE]:
    """Discard every current-key field outside the documented non-identity allow-list."""
    return {
        name: value
        for name in _OPENROUTER_ACCOUNT_FIELDS
        if name in payload and _safe_account_value(value := payload[name])
    }


def _allowlisted_moonshot_balance(payload: Mapping[str, Any]) -> dict[str, _ACCOUNT_VALUE]:
    """Return only documented non-identifying scalar Moonshot balance fields."""
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise _AccountFetchError("provider_response_invalid")
    return {
        name: value
        for name in _MOONSHOT_BALANCE_FIELDS
        if name in data and _safe_account_value(value := data[name])
    }


def _allowlisted_deepseek_balance(payload: Mapping[str, Any]) -> dict[str, _ACCOUNT_VALUE]:
    """Flatten documented DeepSeek balances into bounded, currency-qualified scalars."""
    result: dict[str, _ACCOUNT_VALUE] = {}
    if "is_available" in payload and _safe_account_value(value := payload["is_available"]):
        result["is_available"] = value
    balances = payload.get("balance_infos")
    if not isinstance(balances, list):
        raise _AccountFetchError("provider_response_invalid")
    for balance in balances:
        if not isinstance(balance, Mapping):
            continue
        currency = balance.get("currency")
        if currency not in _DEEPSEEK_CURRENCIES:
            continue
        currency_suffix = str(currency).lower()
        for source_name in ("total_balance", "granted_balance", "topped_up_balance"):
            if source_name in balance and _safe_account_value(value := balance[source_name]):
                result[f"{source_name}_{currency_suffix}"] = value
    return result


def _safe_account_value(value: object) -> bool:
    """Keep JSON scalars only; non-finite floats are not safe account metadata."""
    return (
        value is None
        or isinstance(value, bool | int | str)
        or (isinstance(value, float) and math.isfinite(value))
    )


async def _provider_api_key(provider_id: str, db_pool: Any) -> str | None:
    """Resolve a provider key without exposing secret-storage failures."""
    try:
        return await get_provider_api_key(provider_id, db_pool)
    except Exception:  # noqa: BLE001 - secret resolution must not become an API detail
        return None


async def _fetch_account_payload(
    *, request_url: str, api_key: str
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Fetch one account payload and reduce transport failures to stable codes."""
    try:
        ensure_outbound_egress_allowed("cloud provider account snapshot")
        async with asyncio.timeout(_ACCOUNT_TIMEOUT_SECONDS):
            async with pinned_async_client(
                PUBLIC_ONLY, timeout=httpx.Timeout(_ACCOUNT_TIMEOUT_SECONDS)
            ) as client:
                return await _read_account_payload(client, url=request_url, api_key=api_key), None
    except OutboundEgressBlockedError:
        return None, "egress_blocked"
    except _AccountFetchError as exc:
        return None, str(exc)
    except (TimeoutError, httpx.TimeoutException):
        return None, "provider_request_timed_out"
    except httpx.HTTPError:
        return None, "provider_request_failed"


def _allowlisted_provider_data(
    provider_id: str, payload: Mapping[str, Any]
) -> dict[str, _ACCOUNT_VALUE]:
    """Select the documented, non-identifying account fields for one provider."""
    if provider_id == "openrouter":
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise _AccountFetchError("provider_response_invalid")
        return _allowlisted_openrouter_data(data)
    if provider_id == "moonshot":
        return _allowlisted_moonshot_balance(payload)
    return _allowlisted_deepseek_balance(payload)


async def fetch_provider_account(provider_id: str, *, db_pool: Any) -> ProviderAccountSnapshot:
    """Return a capability-gated account snapshot without sharing the app HTTP client."""
    provider = provider_for_id(provider_id)
    request_url = _ACCOUNT_URLS.get(provider.id)
    if provider.account_capability == "unavailable" or request_url is None:
        return _snapshot(provider)

    try:
        ensure_outbound_egress_allowed("cloud provider account snapshot")
    except OutboundEgressBlockedError:
        return _snapshot(provider, error_code="egress_blocked")

    api_key = await _provider_api_key(provider.id, db_pool)
    if not api_key:
        return _snapshot(provider, error_code="api_key_unavailable")

    payload, error_code = await _fetch_account_payload(request_url=request_url, api_key=api_key)
    if error_code is not None:
        return _snapshot(provider, error_code=error_code)
    assert payload is not None  # The fetch helper always returns a payload or an error.
    try:
        data = _allowlisted_provider_data(provider.id, payload)
    except _AccountFetchError as exc:
        return _snapshot(provider, error_code=str(exc))
    return _snapshot(provider, data=data)
