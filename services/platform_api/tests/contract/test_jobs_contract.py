"""Platform public unified-jobs facade contracts."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from jarvis_common.testing_contract_apps import make_contract_client as _client

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _insert_job(conn, user_id: int, *, status: str = "todo") -> str:
    """Insert a registered Research job for public facade read contracts."""
    job_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO ops.procrastinate_jobs (queue_name, task_name, args, status)
        VALUES ('paper_ingestion', 'paper.process', $1::jsonb, $2)
        """,
        {"job_id": job_id, "user_id": user_id, "paper_id": 1},
        status,
    )
    return job_id


async def test_public_create_validates_payload_and_dispatches_to_owner(
    contract_two_users, _platform_app_with_pool, _configure_api_key, contract_conn, monkeypatch
) -> None:
    """Platform validates the unchanged public payload before owner dispatch."""
    dispatch = AsyncMock(return_value="owner-job-id")
    monkeypatch.setattr("platform_api.routers.jobs._dispatch_to_owner", dispatch)
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_platform_runtime")

    async with _client(_platform_app_with_pool, contract_two_users.cookie_a) as client:
        response = await client.post(
            "/api/jobs", json={"kind": "paper.process", "payload": {"paper_id": 1}}
        )
        invalid = await client.post("/api/jobs", json={"kind": "paper.process", "payload": {}})

    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == "owner-job-id"
    assert response.json()["status"] == "queued"
    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[:3] == (
        "paper.process",
        {"paper_id": 1},
        contract_two_users.user_a_id,
    )
    assert invalid.status_code == 422


async def test_public_get_list_stream_and_cancel_remain_owner_scoped(
    contract_two_users, _platform_app_with_pool, _configure_api_key, contract_conn
) -> None:
    """The facade keeps response shape, SSE, cancellation, and 404 IDOR behavior."""
    job_id = await _insert_job(contract_conn, contract_two_users.user_a_id, status="succeeded")
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_platform_runtime")

    async with _client(_platform_app_with_pool, contract_two_users.cookie_a) as client:
        get_response = await client.get(f"/api/jobs/{job_id}")
        list_response = await client.get("/api/jobs")
        stream_response = await client.get(f"/api/jobs/{job_id}/stream")
    async with _client(_platform_app_with_pool, contract_two_users.cookie_b) as client:
        foreign_get = await client.get(f"/api/jobs/{job_id}")
        foreign_cancel = await client.post(f"/api/jobs/{job_id}/cancel")

    assert get_response.status_code == 200, get_response.text
    assert get_response.json()["id"] == job_id
    assert job_id in {row["id"] for row in list_response.json()}
    assert stream_response.status_code == 200, stream_response.text
    assert "data:" in stream_response.text
    assert "succeeded" in stream_response.text
    assert foreign_get.status_code == 404
    assert foreign_cancel.status_code == 404


async def test_public_cancel_updates_the_owner_job(
    contract_two_users, _platform_app_with_pool, _configure_api_key, contract_conn
) -> None:
    """Platform cancels only the caller-owned job via Operations capability."""
    job_id = await _insert_job(contract_conn, contract_two_users.user_a_id)
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_platform_runtime")
    async with _client(_platform_app_with_pool, contract_two_users.cookie_a) as client:
        response = await client.post(f"/api/jobs/{job_id}/cancel")
        current = await client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert current.status_code == 200, current.text
    assert current.json()["status"] == "cancelled"


async def test_owner_dispatch_outage_is_a_public_503(monkeypatch) -> None:
    """Owner-network failures stay an outage, never a fake job response."""
    from platform_api.routers.jobs import _dispatch_to_owner

    class _Signer:
        def issue(self, **_kwargs: object) -> str:
            return "assertion"

    class _Client(httpx.AsyncClient):
        async def post(self, *args: object, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("owner unavailable")

    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {})()})()})()
    monkeypatch.setattr("platform_api.routers.jobs.IdentityAssertionSigner", _Signer)
    request.app.state.identity_signer = _Signer()
    request.app.state.http_client = _Client()
    monkeypatch.setattr(
        "platform_api.routers.jobs.get_platform_settings",
        lambda: type(
            "Settings",
            (),
            {"research_api_url": "http://research", "learning_api_url": "http://learning"},
        )(),
    )

    with pytest.raises(Exception) as exc_info:
        await _dispatch_to_owner("paper.process", {"paper_id": 1}, 7, request)
    assert getattr(exc_info.value, "status_code", None) == 503
