"""Platform-owned orchestration for resumable account erasure."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
from fastapi import FastAPI
from jarvis_common.identity_assertions import IdentityAssertionSigner

from platform_api.config import PlatformSettings, get_platform_settings
from platform_api.repos import erasure

_COORDINATOR_INTERVAL_SECONDS = 30.0
logger = logging.getLogger(__name__)


class ErasureNotEligibleError(RuntimeError):
    """Raised when an account remains inside its restore grace window."""


@dataclass(frozen=True, slots=True)
class _OwnerTarget:
    """One fixed owner endpoint and its required capability."""

    audience: str
    scope: str
    origin: str
    path: str
    user_id: int


@dataclass(frozen=True, slots=True)
class _ErasureContext:
    """Stable resources and identity for one erasure advancement pass."""

    app: FastAPI
    pool: asyncpg.Pool
    signer: IdentityAssertionSigner
    config: PlatformSettings
    request_id: uuid.UUID
    record: erasure.ErasureRequest


async def _owner_command(
    client: httpx.AsyncClient,
    signer: IdentityAssertionSigner,
    *,
    target: _OwnerTarget,
) -> dict[str, object]:
    """Send one exact signed owner command and validate its object response."""
    command_request_id = str(uuid.uuid4())
    assertion = signer.issue(
        audience=target.audience,
        subject="service:platform",
        principal="platform",
        user_id=target.user_id,
        request_id=command_request_id,
        request_method="POST",
        request_path=target.path,
        scopes=(target.scope,),
    )
    response = await client.post(
        f"{target.origin.rstrip('/')}{target.path}",
        headers={"X-Jarvis-Identity": assertion, "X-Request-Id": command_request_id},
        json={"user_id": target.user_id},
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("invalid owner acknowledgement")
    return payload


def _require_grace_elapsed(record: erasure.ErasureRequest) -> None:
    """Require a still-disabled account whose full restore window elapsed."""
    now = datetime.now(UTC)
    if (
        record.deleted_at is None
        or record.eligible_at > now
        or record.deleted_at + timedelta(days=30) > now
    ):
        raise ErasureNotEligibleError("account erasure is still inside the restore grace")


async def _advance_qdrant(
    context: _ErasureContext,
    phase: erasure.ErasureState,
    acknowledged: set[str],
) -> erasure.ErasureState:
    """Complete and persist the zero-residual vector deletion phase."""
    if "qdrant" not in acknowledged:
        if phase is not erasure.ErasureState.QDRANT_PENDING:
            raise ValueError("Qdrant acknowledgement is missing after its durable phase")
        path = f"/internal/domains/erasure/{context.request_id}/qdrant"
        payload = await _owner_command(
            context.app.state.http_client,
            context.signer,
            target=_OwnerTarget(
                audience="research",
                scope="research:erasure:write",
                origin=context.config.research_api_url,
                path=path,
                user_id=context.record.user_id,
            ),
        )
        receipt = payload.get("receipt")
        if not isinstance(receipt, dict) or receipt.get("residual_points") != 0:
            raise ValueError("Qdrant acknowledgement is incomplete")
        await erasure.acknowledge(context.pool, context.request_id, "qdrant", receipt)
    if phase is erasure.ErasureState.QDRANT_PENDING:
        return await erasure.transition(
            context.pool,
            context.request_id,
            erasure.ErasureState.RESEARCH_PENDING,
        )
    return phase


async def _advance_research(
    context: _ErasureContext,
    phase: erasure.ErasureState,
    acknowledged: set[str],
) -> erasure.ErasureState:
    """Complete and persist the Research-owned relational deletion phase."""
    if "research" not in acknowledged:
        if phase is not erasure.ErasureState.RESEARCH_PENDING:
            raise ValueError("Research acknowledgement is missing after its durable phase")
        path = f"/internal/domains/erasure/{context.request_id}/research"
        payload = await _owner_command(
            context.app.state.http_client,
            context.signer,
            target=_OwnerTarget(
                audience="research",
                scope="research:erasure:write",
                origin=context.config.research_api_url,
                path=path,
                user_id=context.record.user_id,
            ),
        )
        if payload.get("acknowledged") is not True:
            raise ValueError("Research acknowledgement is incomplete")
        await erasure.acknowledge(
            context.pool,
            context.request_id,
            "research",
            {"acknowledged": True},
        )
    if phase is erasure.ErasureState.RESEARCH_PENDING:
        return await erasure.transition(
            context.pool,
            context.request_id,
            erasure.ErasureState.LEARNING_PENDING,
        )
    return phase


async def _advance_learning(
    context: _ErasureContext,
    phase: erasure.ErasureState,
    acknowledged: set[str],
) -> erasure.ErasureState:
    """Complete the Learning-owned deletion phase and mark the request ready."""
    if "learning" not in acknowledged:
        if phase is not erasure.ErasureState.LEARNING_PENDING:
            raise ValueError("Learning acknowledgement is missing after its durable phase")
        path = f"/internal/domains/erasure/{context.request_id}"
        payload = await _owner_command(
            context.app.state.http_client,
            context.signer,
            target=_OwnerTarget(
                audience="learning",
                scope="learning:erasure:write",
                origin=context.config.learning_api_url,
                path=path,
                user_id=context.record.user_id,
            ),
        )
        if payload.get("acknowledged") is not True:
            raise ValueError("Learning acknowledgement is incomplete")
        await erasure.acknowledge(
            context.pool,
            context.request_id,
            "learning",
            {"acknowledged": True},
        )
    return await erasure.transition(context.pool, context.request_id, erasure.ErasureState.READY)


async def process_request(
    app: FastAPI,
    request_id: uuid.UUID,
    *,
    settings: PlatformSettings | None = None,
) -> erasure.ErasureState:
    """Advance one eligible request through only its incomplete owner phases.

    Parameters
    ----------
    app : FastAPI
        Platform application carrying the runtime pool, HTTP client and signer.
    request_id : uuid.UUID
        Durable request to advance.
    settings : PlatformSettings, optional
        Explicit settings snapshot for tests; runtime uses process settings.

    Returns
    -------
    ErasureState
        Persisted state after this bounded pass.

    Raises
    ------
    LookupError
        If the request does not exist.
    ErasureNotEligibleError
        If the account is restored or remains inside the restore window.
    """
    pool: asyncpg.Pool = app.state.db_pool
    signer: IdentityAssertionSigner = app.state.identity_signer
    record = await erasure.get_request(pool, request_id)
    if record is None:
        raise LookupError(f"erasure request {request_id} does not exist")
    if record.state in {
        erasure.ErasureState.COMPLETE,
        erasure.ErasureState.ATTENTION_REQUIRED,
        erasure.ErasureState.READY,
    }:
        return record.state
    _require_grace_elapsed(record)
    if record.state is erasure.ErasureState.REQUESTED:
        try:
            record = await erasure.begin_destructive_phases(pool, request_id)
        except ValueError as exc:
            raise ErasureNotEligibleError(str(exc)) from exc

    config = settings or get_platform_settings()
    acknowledged = await erasure.acknowledged_domains(pool, request_id)
    phase = record.resume_state if record.state is erasure.ErasureState.RETRY_WAIT else record.state
    if record.state is erasure.ErasureState.RETRY_WAIT:
        phase = await erasure.transition(pool, request_id, phase)

    context = _ErasureContext(app, pool, signer, config, request_id, record)
    try:
        phase = await _advance_qdrant(context, phase, acknowledged)
        phase = await _advance_research(context, phase, acknowledged)
        return await _advance_learning(context, phase, acknowledged)
    except (httpx.HTTPError, RuntimeError, ValueError):
        return await erasure.record_retry(pool, request_id, phase)


async def process_due_requests(app: FastAPI, *, limit: int = 20) -> int:
    """Run one bounded pass over eligible incomplete requests."""
    request_ids = await erasure.due_request_ids(app.state.db_pool, limit=limit)
    for request_id in request_ids:
        try:
            await process_request(app, request_id)
        except (ErasureNotEligibleError, LookupError):
            # A concurrent restore may cancel a request after the due snapshot.
            continue
        except Exception:  # noqa: BLE001
            logger.exception(
                "Account erasure request failed",
                extra={"request_id": str(request_id)},
            )
    return len(request_ids)


async def _coordinator_loop(app: FastAPI) -> None:
    while True:
        try:
            await process_due_requests(app)
        except Exception:  # noqa: BLE001
            logger.exception("Account erasure pass failed")
        await asyncio.sleep(_COORDINATOR_INTERVAL_SECONDS)


async def start_coordinator(app: FastAPI) -> None:
    """Start the Platform-owned due-request driver."""
    app.state.erasure_coordinator_task = asyncio.create_task(
        _coordinator_loop(app),
        name="platform-erasure-coordinator",
    )


async def stop_coordinator(app: FastAPI) -> None:
    """Cancel and join the due-request driver during application shutdown."""
    task: asyncio.Task[None] = app.state.erasure_coordinator_task
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


__all__ = [
    "ErasureNotEligibleError",
    "process_due_requests",
    "process_request",
    "start_coordinator",
    "stop_coordinator",
]
