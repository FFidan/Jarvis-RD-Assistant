"""Jobs domain contract tests — target rows A176-A180.

Survivor-of: (all NONE — no prior contract coverage).
Carve-out: task_registry._TASK_MAP is a declared external-boundary carve-out
(testing.py:255). POST /api/jobs enqueues via procrastinate tasks; calling
defer_async against a real procrastinate worker would require a live broker.
The approach here:
  - A176 (create_job): mock the task's defer_async so the endpoint proceeds
    past enqueue; assert the job row written to the jarvis_jobs legacy table OR
    the 202 response + job_id shape (the endpoint writes to procrastinate, not
    jarvis_jobs — so we verify the HTTP contract, not a DB row).
  - A177 (get_job): insert directly into jarvis_jobs; assert ownership + 404.
  - A178 (list_jobs): insert rows; assert list scoping.
  - A179 (stream_job): IDIOMATIC-MOCK-ONLY — SSE stream requires a live
    procrastinate job that eventually transitions; skipped per YAGNI.
  - A180 (cancel_job): insert job; assert ownership + 404 for non-owner.

Row deferred:
  A179 GET /api/jobs/{id}/stream  — SSE requires live procrastinate worker;
       IDIOMATIC-MOCK-ONLY (test_jobs_sse_ownership.py mock-unit covers
       ownership rejection).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _insert_jarvis_job(conn, user_id: int, status: str = "todo") -> str:
    """Insert a minimal procrastinate-backed job row owned by *user_id*."""
    job_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO procrastinate_jobs (queue_name, task_name, args, status)
        VALUES ('paper_ingestion', 'noop.test', $1::jsonb, $2)
        """,
        {"job_id": job_id, "user_id": user_id},
        status,
    )
    return job_id


# ---------------------------------------------------------------------------
# A176: POST /api/jobs — job row response + user_id scoping
# ---------------------------------------------------------------------------


async def test_a176_create_job_returns_202_with_job_id(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    monkeypatch,
):
    """Covers map row A176: POST /api/jobs returns 202 with job_id.

    Verified: jobs_router.py:175-234 create_job — defer_async + JobCreateResponse.
    task_registry is a declared carve-out; defer_async is mocked to avoid
    needing a live procrastinate broker while still exercising the HTTP contract.
    """
    monkeypatch.setenv("JARVIS_ENABLE_TEST_JOBS", "1")

    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"noop.test": mock_task}):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.post(
                "/api/jobs",
                json={"kind": "noop.test", "payload": {}},
            )

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert "job_id" in body, f"Missing 'job_id' in response: {body}"
    assert "status" in body, f"Missing 'status' in response: {body}"
    assert body["status"] == "queued"
    assert body["job_id"]  # non-empty UUID string

    mock_task.defer_async.assert_awaited_once()
    call_kwargs = mock_task.defer_async.call_args.kwargs
    assert "job_id" in call_kwargs
    assert "user_id" in call_kwargs
    assert str(call_kwargs["user_id"]) == str(contract_two_users.user_a_id)


async def test_a176_create_job_unknown_kind_returns_error(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A176 edge: POST /api/jobs with unknown kind returns 4xx.

    Verified: jobs_router.py:187-199 — 422 (discriminated mode) for unknown kind.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/jobs",
            json={"kind": "does.not.exist", "payload": {}},
        )

    assert resp.status_code in (400, 422), (
        f"Expected 400/422 for unknown kind, got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A177: GET /api/jobs/{id} — owner gets row; non-owner gets 404
# ---------------------------------------------------------------------------


async def test_a177_get_job_owner_gets_row_non_owner_gets_404(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A177: GET /api/jobs/{id} ownership enforcement.

    Verified: jobs_router.py:241-251 get_job — get_unified + _owner_matches.
    """
    job_id = await _insert_jarvis_job(contract_conn, contract_two_users.user_a_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.get(f"/api/jobs/{job_id}")

    assert resp_a.status_code == 200, resp_a.text[:300]
    body = resp_a.json()
    assert body["id"] == job_id

    # Non-owner gets 404 (not 403, to avoid leaking job existence)
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.get(f"/api/jobs/{job_id}")
    assert resp_b.status_code == 404, f"Expected 404 for non-owner, got {resp_b.status_code}"


# ---------------------------------------------------------------------------
# A178: GET /api/jobs — list scoped to current user
# ---------------------------------------------------------------------------


async def test_a178_list_jobs_returns_only_own_jobs(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A178: GET /api/jobs returns only current user's job rows.

    Verified: jobs_router.py:258-274 list_jobs — jobs_lib.list_jobs with user_id filter.
    """
    job_id_a = await _insert_jarvis_job(contract_conn, contract_two_users.user_a_id)
    job_id_b = await _insert_jarvis_job(contract_conn, contract_two_users.user_b_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/jobs")

    assert resp.status_code == 200, resp.text[:300]
    jobs = resp.json()
    assert isinstance(jobs, list), f"Expected list, got {type(jobs)}"

    job_ids = [j["id"] for j in jobs]
    assert job_id_a in job_ids, f"User A's job {job_id_a} missing from list"
    assert job_id_b not in job_ids, f"User B's job {job_id_b} leaked into User A's job list — IDOR"


# ---------------------------------------------------------------------------
# A180: POST /api/jobs/{id}/cancel — owner can cancel; non-owner gets 404
# ---------------------------------------------------------------------------


async def test_a180_cancel_job_owner_ok_non_owner_404(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A180: POST /api/jobs/{id}/cancel ownership enforcement.

    Verified: jobs_router.py:316-332 cancel_job — get_unified + _owner_matches.
    Note: the procrastinate cancel_job_by_id_async call is skipped when
    get_procrastinate_job_for_jarvis_id returns None (no matching procrastinate
    row for a directly-inserted jarvis_jobs row), so the endpoint returns {"ok": True}
    without touching the live broker.
    """
    job_id = await _insert_jarvis_job(contract_conn, contract_two_users.user_a_id, status="todo")

    # Non-owner gets 404
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.post(f"/api/jobs/{job_id}/cancel")
    assert resp_b.status_code == 404, f"Expected 404 for non-owner cancel, got {resp_b.status_code}"

    mock_manager = MagicMock()
    mock_manager.cancel_job_by_id_async = AsyncMock()
    mock_app = MagicMock()
    mock_app.job_manager = mock_manager

    # Owner can cancel; task_registry.app is the declared external boundary.
    with patch("jarvis_common.task_registry.app", mock_app):
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp_a = await c.post(f"/api/jobs/{job_id}/cancel")
    assert resp_a.status_code == 200, resp_a.text[:300]
    body = resp_a.json()
    assert body.get("ok") is True, f'Expected {{"ok": true}}, got {body}'
    mock_manager.cancel_job_by_id_async.assert_awaited_once()
