"""Focused contracts for durable Platform-to-Research configuration delivery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from jarvis_common.testing import make_pool_and_conn
from platform_api.repos import config_delivery as repository
from platform_api.routers.internal_services import (
    ResearchConfigEffectsRequest,
    report_research_config_effects,
)
from platform_api.services import config_delivery as service


def _record(delivery_id: uuid.UUID) -> repository.ConfigDelivery:
    """Build one pending delivery without retaining a plaintext secret."""
    return repository.ConfigDelivery(
        delivery_id=delivery_id,
        scope_user_id=7,
        user_id=7,
        user_role="admin",
        session_id="session-7",
        zotero_scope_changed=False,
        key="pulse.enabled",
        value=True,
        encrypted_value=None,
        attempts=0,
        next_attempt_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_research_effect_command_persists_only_through_platform_pool() -> None:
    """The authenticated Research report uses Platform's owning transaction."""
    pool, conn = make_pool_and_conn(fetchrow_return=None, execute_return="INSERT 0 1")

    response = await report_research_config_effects(
        ResearchConfigEffectsRequest(
            roles=["smart"],
            pending=False,
            effective_num_ctx_role="smart",
            effective_num_ctx_value=4096,
        ),
        "research",
        pool,
    )

    assert response.status_code == 204
    assert conn.transaction.call_count == 1
    assert conn.execute.await_count == 2


@pytest.mark.asyncio
async def test_research_effect_command_rejects_other_service_principals() -> None:
    """Learning and Telegram cannot mutate Platform configuration state."""
    for principal in ("learning", "telegram"):
        with pytest.raises(HTTPException) as denied:
            await report_research_config_effects(
                ResearchConfigEffectsRequest(roles=["smart"], pending=True),
                principal,
                object(),
            )
        assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_deliver_acknowledges_only_the_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded delivery cannot clear the newer desired version."""
    delivery_id = uuid.uuid4()
    pool = object()
    get_delivery = AsyncMock(side_effect=[_record(delivery_id), None])
    send_value = AsyncMock(return_value={"key": "pulse.enabled", "value": True})
    mark_applied = AsyncMock(return_value=False)
    monkeypatch.setattr(repository, "get_delivery", get_delivery)
    monkeypatch.setattr(repository, "mark_applied", mark_applied)
    monkeypatch.setattr(service, "_send_value", send_value)

    applied, payload = await service.deliver(
        pool=pool,
        client=AsyncMock(spec=httpx.AsyncClient),
        signer=object(),
        delivery_id=delivery_id,
    )

    assert applied is True
    assert payload is None
    send_value.assert_not_awaited()
    mark_applied.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_failure_schedules_retry_without_false_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Research outage leaves the same version pending and inspectable."""
    delivery_id = uuid.uuid4()
    pool = object()
    request = httpx.Request("PUT", "http://research/internal/platform/config/pulse.enabled")
    monkeypatch.setattr(
        repository,
        "get_delivery",
        AsyncMock(side_effect=[_record(delivery_id), _record(delivery_id)]),
    )
    monkeypatch.setattr(
        service,
        "_send_value",
        AsyncMock(side_effect=httpx.ConnectError("unavailable", request=request)),
    )
    record_retry = AsyncMock(return_value=True)
    mark_applied = AsyncMock(return_value=True)
    monkeypatch.setattr(repository, "record_retry", record_retry)
    monkeypatch.setattr(repository, "mark_applied", mark_applied)

    applied, payload = await service.deliver(
        pool=pool,
        client=AsyncMock(spec=httpx.AsyncClient),
        signer=object(),
        delivery_id=delivery_id,
    )

    assert (applied, payload) == (False, None)
    record_retry.assert_awaited_once()
    assert record_retry.await_args.args[:2] == (pool, delivery_id)
    mark_applied.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_delivery_is_an_idempotent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying a version already replaced or applied is harmless."""
    monkeypatch.setattr(repository, "get_delivery", AsyncMock(return_value=None))
    send_value = AsyncMock()
    monkeypatch.setattr(service, "_send_value", send_value)

    result = await service.deliver(
        pool=object(),
        client=AsyncMock(spec=httpx.AsyncClient),
        signer=object(),
        delivery_id=uuid.uuid4(),
    )

    assert result == (True, None)
    send_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_value_writes_value_and_delivery_in_one_transaction() -> None:
    """The desired value and retry record share one database transaction."""
    pool, conn = make_pool_and_conn(execute_return="INSERT 0 1")

    delivery_id = await repository.persist_value(
        pool,
        repository.ConfigWrite(
            user_id=None,
            actor_user_id=7,
            user_role="admin",
            session_id="session-7",
            key="pulse.enabled",
            value=True,
            encrypted_value=None,
        ),
    )

    assert isinstance(delivery_id, uuid.UUID)
    assert conn.transaction.call_count == 1
    assert conn.execute.await_count == 2
    value_write, delivery_write = conn.execute.await_args_list
    assert value_write.args[1:] == (None, "pulse.enabled", True)
    assert delivery_write.args[1:4] == (0, 7, "pulse.enabled")
    assert delivery_write.args[4] == delivery_id


@pytest.mark.asyncio
async def test_retry_pass_is_bounded_and_attempts_every_selected_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failed record cannot prevent another due version from running."""
    first, second = uuid.uuid4(), uuid.uuid4()
    due = AsyncMock(return_value=[first, second])
    deliver_id = AsyncMock(side_effect=[RuntimeError("bad record"), (True, None)])
    monkeypatch.setattr(repository, "due_delivery_ids", due)
    monkeypatch.setattr(service, "deliver_id", deliver_id)
    app = FastAPI()
    app.state.db_pool = object()

    processed = await service.process_due_deliveries(app, limit=2)

    assert processed == 2
    due.assert_awaited_once_with(app.state.db_pool, limit=2)
    assert [call.args[1] for call in deliver_id.await_args_list] == [first, second]


@pytest.mark.asyncio
async def test_send_rejects_a_partial_or_non_object_acknowledgement() -> None:
    """Malformed Research success bodies cannot clear a durable delivery."""
    signer = MagicMock()
    signer.issue.return_value = "signed"
    client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.json.side_effect = [
        {"key": "pulse.enabled", "schedule_apply_warnings": []},
        ["pulse.enabled"],
    ]
    client.put.return_value = response

    for _ in range(2):
        with pytest.raises(ValueError, match="response"):
            await service._send_value(
                client=client,
                signer=signer,
                command=service.ConfigCommand(
                    key="pulse.enabled",
                    value=True,
                    user_id=7,
                    user_role="admin",
                    session_id="session-7",
                    request_id=str(uuid.uuid4()),
                ),
                phase="apply",
            )
