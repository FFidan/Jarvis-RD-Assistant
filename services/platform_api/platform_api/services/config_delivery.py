"""Deliver Platform-owned configuration effects to Research durably."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI
from jarvis_common.crypto import decrypt_secret
from jarvis_common.identity_assertions import IdentityAssertionSigner

from platform_api.config import get_platform_settings
from platform_api.repos import config_delivery

logger = logging.getLogger(__name__)

_RETRY_INTERVAL_SECONDS = 5.0
_RESEARCH_PATH_PREFIX = "/internal/platform/config"
_RESEARCH_SCOPE = ("research:config:write",)
_DELIVERY_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


@dataclass(frozen=True, slots=True)
class ConfigCommand:
    """One identity-bound configuration command sent to Research."""

    key: str
    value: Any
    user_id: int | None
    user_role: str | None
    session_id: str | None
    request_id: str
    zotero_scope_changed: bool = False


def _plaintext_value(record: config_delivery.ConfigDelivery) -> Any:
    """Resolve one stored value without retaining decrypted material."""
    if record.encrypted_value is None:
        return record.value
    try:
        return decrypt_secret(record.encrypted_value.decode("ascii"))
    except Exception as exc:  # noqa: BLE001 - normalize key/tamper failures for durable retry
        raise ValueError("stored encrypted configuration is unreadable") from exc


async def _send_value(
    *,
    client: httpx.AsyncClient,
    signer: IdentityAssertionSigner,
    command: ConfigCommand,
    phase: str,
) -> dict[str, Any]:
    """Send one version-bound Research command with no database resource held."""
    path = f"{_RESEARCH_PATH_PREFIX}/{command.key}"
    assertion_user_id = command.user_id or 1
    assertion = signer.issue(
        audience="research",
        subject=f"user:{assertion_user_id}",
        principal="browser",
        user_id=assertion_user_id,
        user_role=command.user_role,
        session_id=command.session_id,
        request_id=command.request_id,
        request_method="PUT",
        request_path=path,
        scopes=_RESEARCH_SCOPE,
    )
    response = await client.put(
        f"{get_platform_settings().research_api_url}{path}",
        json={
            "value": command.value,
            "phase": phase,
            "zotero_scope_changed": command.zotero_scope_changed,
        },
        headers={"X-Jarvis-Identity": assertion, "X-Request-Id": command.request_id},
        timeout=310.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Research configuration response must be an object")
    warnings = payload.get("schedule_apply_warnings")
    if (
        payload.get("key") != command.key
        or "value" not in payload
        or not isinstance(warnings, list)
        or not all(isinstance(item, str) for item in warnings)
    ):
        raise ValueError("Research configuration response has an invalid shape")
    return payload


async def validate_value(
    *,
    client: httpx.AsyncClient,
    signer: IdentityAssertionSigner,
    command: ConfigCommand,
) -> None:
    """Validate a desired value in Research without applying side effects."""
    await _send_value(
        client=client,
        signer=signer,
        command=command,
        phase="validate",
    )


async def deliver(
    *,
    pool: Any,
    client: httpx.AsyncClient,
    signer: IdentityAssertionSigner,
    delivery_id: uuid.UUID,
) -> tuple[bool, dict[str, Any] | None]:
    """Apply and conditionally acknowledge one current delivery version."""
    record = await config_delivery.get_delivery(pool, delivery_id)
    if record is None:
        return True, None
    lock = _DELIVERY_LOCKS.setdefault((record.scope_user_id, record.key), asyncio.Lock())
    async with lock:
        # A later request may have replaced this version before it reached the
        # lock. Never execute a superseded side effect.
        record = await config_delivery.get_delivery(pool, delivery_id)
        if record is None:
            return True, None
        try:
            payload = await _send_value(
                client=client,
                signer=signer,
                command=ConfigCommand(
                    key=record.key,
                    value=_plaintext_value(record),
                    user_id=record.user_id,
                    user_role=record.user_role,
                    session_id=record.session_id,
                    request_id=str(record.delivery_id),
                    zotero_scope_changed=record.zotero_scope_changed,
                ),
                phase="apply",
            )
            await config_delivery.apply_research_config_effects(
                pool,
                roles=payload.get("litellm_delivery_roles", []),
                pending=payload.get("litellm_delivery_pending"),
                effective_num_ctx_role=payload.get("effective_num_ctx_role"),
                effective_num_ctx_value=payload.get("effective_num_ctx_value"),
            )
        except (httpx.HTTPError, ValueError, UnicodeError) as exc:
            await config_delivery.record_retry(pool, delivery_id, str(exc))
            return False, None
        return await config_delivery.mark_applied(pool, delivery_id), payload


async def deliver_id(app: FastAPI, delivery_id: uuid.UUID) -> tuple[bool, dict[str, Any] | None]:
    """Deliver one version using application-owned runtime resources."""
    return await deliver(
        pool=app.state.db_pool,
        client=app.state.http_client,
        signer=app.state.identity_signer,
        delivery_id=delivery_id,
    )


async def process_due_deliveries(app: FastAPI, *, limit: int = 20) -> int:
    """Run one bounded retry pass over current pending configuration versions."""
    delivery_ids = await config_delivery.due_delivery_ids(app.state.db_pool, limit=limit)
    for delivery_id in delivery_ids:
        try:
            await deliver_id(app, delivery_id)
        except Exception:  # noqa: BLE001 - one corrupt delivery must not stop later retries
            logger.exception("Configuration delivery retry failed")
    return len(delivery_ids)


async def _reconciler_loop(app: FastAPI) -> None:
    while True:
        try:
            await process_due_deliveries(app)
        except Exception:  # noqa: BLE001 - lifecycle worker must remain available
            logger.exception("Configuration delivery pass failed")
        await asyncio.sleep(_RETRY_INTERVAL_SECONDS)


async def start_reconciler(app: FastAPI) -> None:
    """Start the Platform-owned bounded configuration delivery worker."""
    app.state.config_delivery_task = asyncio.create_task(
        _reconciler_loop(app), name="platform-config-delivery"
    )


async def stop_reconciler(app: FastAPI) -> None:
    """Cancel and join the configuration delivery worker."""
    task: asyncio.Task[None] = app.state.config_delivery_task
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


__all__ = [
    "ConfigCommand",
    "deliver",
    "deliver_id",
    "process_due_deliveries",
    "start_reconciler",
    "stop_reconciler",
    "validate_value",
]
