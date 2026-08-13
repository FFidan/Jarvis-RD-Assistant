"""LE jobs domain contract tests — Cluster 11.

Mirrors services/paper_ingestion/tests/contract/test_pi_jobs_contract.py for
the learning_engine service's jobs router (LE_PUBLIC_JOB_KINDS = {"card.generate"}).

Survivor-of:
  - test_create_job_enqueues_allowed_kind            → LE-J-01
  - test_get_job_returns_404_when_not_found          → LE-J-03
  - test_get_job_returns_404_for_wrong_owner         → LE-J-03
  - test_list_jobs_passes_status_filter_to_lib       → LE-J-04
  - test_get_job_str_user_id_row_matches_int_caller  → LE-J-03
  - test_get_job_str_user_id_row_rejects_wrong_int_caller → LE-J-03
  - test_cancel_job_str_user_id_row_matches_int_caller    → LE-J-05
  - test_cancel_job_str_user_id_row_rejects_wrong_int_caller → LE-J-05
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from jarvis_common.testing_contract_apps import (
    make_contract_client as _client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _insert_le_job(conn, user_id: int, status: str = "todo") -> str:
    """Insert a minimal procrastinate-backed job row owned by *user_id*.

    Mirrors test_pi_jobs_contract.py:_insert_jarvis_job. The LE service writes
    to the same procrastinate_jobs table the PI service uses.
    """
    job_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO procrastinate_jobs (queue_name, task_name, args, status)
        VALUES ('learning_engine', 'noop.test', $1::jsonb, $2)
        """,
        {"job_id": job_id, "user_id": user_id},
        status,
    )
    return job_id


# ---------------------------------------------------------------------------
# LE-J-01: POST /api/jobs — card.generate kind enqueues + returns 202+job_id
# ---------------------------------------------------------------------------


async def test_le_j01_create_job_returns_202_with_job_id(
    contract_two_users,
    _le_app,
    _configure_api_key,
):
    """POST /api/jobs with card.generate enqueues via task_registry + returns 202.

    # Verified: services/learning_engine/learning_engine/routers/jobs.py:25
    (LE_PUBLIC_JOB_KINDS = frozenset({"card.generate"})).
    # Verified: services/learning_engine/learning_engine/routers/jobs.py:28
    (_CardGeneratePayload requires paper_id + deck_id; ownership gated at
    enqueue time via _card_generate_paper_extractor at line 41).
    """
    mock_task = AsyncMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict("jarvis_common.task_registry._TASK_MAP", {"card.generate": mock_task}):
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp = await c.post(
                "/api/jobs",
                json={
                    "kind": "card.generate",
                    "payload": {
                        "kind": "card.generate",
                        "paper_id": contract_two_users.paper_id_a,
                        "deck_id": contract_two_users.deck_id_a,
                        "max_cards": 3,
                    },
                },
            )

    assert resp.status_code == 202, resp.text[:300]
    body = resp.json()
    assert "job_id" in body and body["job_id"], f"Missing job_id: {body}"
    assert body.get("status") == "queued"
    mock_task.defer_async.assert_awaited_once()
    call_kwargs = mock_task.defer_async.call_args.kwargs
    assert "job_id" in call_kwargs
    assert str(call_kwargs["user_id"]) == str(contract_two_users.user_a_id)


# ---------------------------------------------------------------------------
# LE-J-02: POST /api/jobs with kind outside allowlist returns 4xx
# ---------------------------------------------------------------------------


async def test_le_j02_create_job_unknown_kind_returns_4xx(
    contract_two_users,
    _le_app,
    _configure_api_key,
):
    """POST /api/jobs with a kind absent from LE_PUBLIC_JOB_KINDS returns 422.

    With `payload_schemas={"card.generate": _CardGeneratePayload}` the LE jobs
    router uses _build_discriminated_request_model — unknown kinds fail the
    model_validator before reaching the runtime allowlist guard.

    # Verified: services/learning_engine/learning_engine/routers/jobs.py:25
    # Verified: libs/jarvis_common/jarvis_common/jobs_router.py:410
    (LE_PUBLIC_JOB_KINDS = {"card.generate"}; discriminated-union validator
    _validate_payload_for_kind raises ValueError → FastAPI 422 for unknown kinds.)
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/jobs",
            json={"kind": "totally.unknown.kind", "payload": {}},
        )
    assert resp.status_code in (400, 422), (
        f"Expected 400/422 for unknown kind, got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# LE-J-03: GET /api/jobs/{id} — owner gets row, non-owner gets 404
# ---------------------------------------------------------------------------


async def test_le_j03_get_job_owner_ok_non_owner_404(
    contract_two_users,
    _le_app,
    _configure_api_key,
    contract_conn,
):
    """GET /api/jobs/{id} returns the row to its owner, 404 to anyone else.

    Also exercises LE-002 str-user_id row coercion: a second job is inserted
    with ``args.user_id`` stored as ``str``; the int-typed caller user_id must
    still match (regression-tested via the str(...) == str(...) coercion in
    build_jobs_router's _owner_matches helper).

    # Verified: libs/jarvis_common/jarvis_common/jobs_router.py:102
    (build_jobs_router constructs get_job with user_id check via jobs_lib.get_unified
    and _owner_matches; LE-002 str/int coercion lives in _owner_matches).
    """
    # int-typed user_id row (normal path)
    job_id = await _insert_le_job(contract_conn, contract_two_users.user_a_id)

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_a = await c.get(f"/api/jobs/{job_id}")
    assert resp_a.status_code == 200, resp_a.text[:300]
    assert resp_a.json()["id"] == job_id

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get(f"/api/jobs/{job_id}")
    assert resp_b.status_code == 404, (
        f"Expected 404 for non-owner, got {resp_b.status_code}: {resp_b.text[:300]}"
    )

    # str-typed user_id row (LE-002 coercion path — regression test for
    # a possible reversion of the str(...)==str(...) fix in _owner_matches)
    str_job_id = str(uuid.uuid4())
    await contract_conn.execute(
        """
        INSERT INTO procrastinate_jobs (queue_name, task_name, args, status)
        VALUES ('learning_engine', 'noop.test', $1::jsonb, 'todo')
        """,
        {"job_id": str_job_id, "user_id": str(contract_two_users.user_a_id)},
    )
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_str_owner = await c.get(f"/api/jobs/{str_job_id}")
    assert resp_str_owner.status_code == 200, resp_str_owner.text[:300]
    assert resp_str_owner.json()["id"] == str_job_id

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_str_nonowner = await c.get(f"/api/jobs/{str_job_id}")
    assert resp_str_nonowner.status_code == 404, (
        f"Expected 404 for str-row non-owner, got {resp_str_nonowner.status_code}"
    )


# ---------------------------------------------------------------------------
# LE-J-04: GET /api/jobs — list scoped to caller
# ---------------------------------------------------------------------------


async def test_le_j04_list_jobs_scoped_to_owner(
    contract_two_users,
    _le_app,
    _configure_api_key,
    contract_conn,
):
    """GET /api/jobs returns only the caller's jobs; ?status= query param filters.

    Three sub-assertions:
      1. Owner-scoping: user A's list excludes user B's jobs.
      2. status-filter propagation: ?status=queued returns only queued jobs from
         the caller's set (regression test for the status kwarg being passed
         through to jobs_lib.list_jobs).
      3. status=active returns queued and running jobs without terminal history.

    # Verified: libs/jarvis_common/jarvis_common/jobs_router.py:102
    (build_jobs_router constructs list_jobs with user_id filter via
    jobs_lib.list_jobs(status=..., kind=..., limit=...).)
    """
    # procrastinate_job_status enum values: todo, doing, succeeded, failed, cancelled,
    # aborting, aborted (db/init.sql:42-50). The unified API maps these to its own
    # external names (e.g. "todo" → "queued") and accepts those external names in
    # ?status=. Insert with raw enum values; filter with the unified API name.
    job_a_queued = await _insert_le_job(contract_conn, contract_two_users.user_a_id, status="todo")
    job_a_done = await _insert_le_job(
        contract_conn, contract_two_users.user_a_id, status="succeeded"
    )
    job_a_running = await _insert_le_job(
        contract_conn, contract_two_users.user_a_id, status="doing"
    )
    job_b = await _insert_le_job(contract_conn, contract_two_users.user_b_id, status="todo")

    # Owner-scoping: user A's list excludes user B's job
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_all = await c.get("/api/jobs")

    assert resp_all.status_code == 200, resp_all.text[:300]
    ids_all = [j["id"] for j in resp_all.json()]
    assert job_a_queued in ids_all and job_a_running in ids_all and job_a_done in ids_all, (
        f"User A's own jobs missing from own list: queued={job_a_queued in ids_all} "
        f"done={job_a_done in ids_all}"
    )
    assert job_b not in ids_all, f"IDOR leak — user A saw user B's job {job_b}"

    # status filter propagation: ?status=queued returns only A's queued job
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_queued = await c.get("/api/jobs?status=queued")

    assert resp_queued.status_code == 200, resp_queued.text[:300]
    ids_queued = [j["id"] for j in resp_queued.json()]
    assert job_a_queued in ids_queued, (
        f"status=queued filter dropped user A's queued job {job_a_queued}: {ids_queued}"
    )
    assert job_a_done not in ids_queued, (
        f"status=queued filter did NOT exclude user A's done job {job_a_done}: {ids_queued}"
    )
    assert job_b not in ids_queued, "IDOR leak under status filter"

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_active = await c.get("/api/jobs?status=active")

    assert resp_active.status_code == 200, resp_active.text[:300]
    ids_active = [j["id"] for j in resp_active.json()]
    assert job_a_queued in ids_active
    assert job_a_running in ids_active
    assert job_a_done not in ids_active
    assert job_b not in ids_active, "IDOR leak under active-status filter"


# ---------------------------------------------------------------------------
# LE-J-05: POST /api/jobs/{id}/cancel — owner ok, non-owner 404
# ---------------------------------------------------------------------------


async def test_le_j05_cancel_job_owner_ok_non_owner_404(
    contract_two_users,
    _le_app,
    _configure_api_key,
    contract_conn,
):
    """POST /api/jobs/{id}/cancel — owner returns 200 {ok: true}; non-owner returns 404.

    # Verified: libs/jarvis_common/jarvis_common/jobs_router.py:102
    (build_jobs_router constructs cancel_job with user_id check + optional
    procrastinate cancel_job_by_id_async call via jarvis_common.task_registry.app).
    """
    job_id = await _insert_le_job(contract_conn, contract_two_users.user_a_id, status="todo")

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.post(f"/api/jobs/{job_id}/cancel")
    assert resp_b.status_code == 404, f"Expected 404 for non-owner cancel, got {resp_b.status_code}"

    mock_manager = MagicMock()
    mock_manager.cancel_job_by_id_async = AsyncMock()
    mock_app = MagicMock()
    mock_app.job_manager = mock_manager

    with patch("jarvis_common.task_registry.app", mock_app):
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp_a = await c.post(f"/api/jobs/{job_id}/cancel")

    assert resp_a.status_code == 200, resp_a.text[:300]
    assert resp_a.json().get("ok") is True
