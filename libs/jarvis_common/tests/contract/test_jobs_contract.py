"""Contract tests for build_jobs_router ownership enforcement (RD-DA-001 / RD-DA-002).

Tests exercise the real ``build_jobs_router`` factory with a real DB connection
so ownership checks hit actual SQL rather than mocks.

Covered:
  A. POST /api/jobs with card.generate: user B cannot enqueue for user A's
     paper → 403 before defer_async (RD-DA-001).
  B. POST /api/jobs without a session (API-key only) → 401 (RD-DA-002).
  C. GET /api/jobs/{id}: user B gets 404 for user A's job (IDOR — RD-DA-003).
  D. GET /api/jobs: list scoped to caller's own jobs only.
  E. POST /api/jobs/{id}/cancel: user B gets 404 for user A's job.
  F. GET /api/jobs/{id}/stream: user B gets 404 before SSE begins.

NOTE (schema gating): tests C–F insert rows directly into ``procrastinate_jobs``
to seed jobs without going through the enqueue path. They require the
``procrastinate_jobs`` table, which IS present in the contract DB (db/init.sql:178).
"""

from __future__ import annotations

from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from jarvis_common.testing import SharedConnPool
from jarvis_common.testing_contract_apps import make_contract_client
from pydantic import BaseModel

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


class _CardGeneratePayload(BaseModel):
    kind: Literal["card.generate"]
    paper_id: int
    deck_id: int
    max_cards: int = 5


def _card_paper_extractor(payload: dict) -> int | None:
    v = payload.get("paper_id")
    return v if isinstance(v, int) else None


def _build_jobs_app(contract_conn, runtime_role: str):
    """Build the legacy router under one exact runtime identity."""
    from jarvis_common.jobs_router import build_jobs_router
    from jarvis_common.testing_auth import SignedIdentityMiddleware

    shared = SharedConnPool(contract_conn, session_authorization=runtime_role)

    limiter_stub = MagicMock()
    limiter_stub.enabled = False
    limiter_stub.limit = lambda _spec: lambda f: f

    router = build_jobs_router(
        service_name="contract_test",
        public_kinds=frozenset({"card.generate"}),
        get_db_pool=lambda: shared,
        limiter=limiter_stub,
        payload_schemas={"card.generate": _CardGeneratePayload},
        paper_ownership_extractor=_card_paper_extractor,
    )

    app = FastAPI()
    app.include_router(router, dependencies=[])
    app.state.db_pool = shared

    return SignedIdentityMiddleware(
        app,
        audience="learning",
        session_pool=shared.with_session_authorization("jarvis_platform_runtime"),
    )


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _jobs_app(contract_conn):
    """Legacy read router with the Platform facade's capability identity."""
    yield _build_jobs_app(contract_conn, "jarvis_platform_runtime")


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _owner_jobs_app(contract_conn):
    """Legacy create router with the Learning owner's runtime identity."""
    yield _build_jobs_app(contract_conn, "jarvis_learning_runtime")


def _authed_client(app, cookie: str):
    return make_contract_client(app, cookie)


def _api_key_client(app):
    return make_contract_client(app, None)


# ---------------------------------------------------------------------------
# RD-DA-001: card.generate enqueue rejected when caller does not own paper
# ---------------------------------------------------------------------------


async def test_create_job_card_generate_rejects_non_owner_paper(
    contract_two_users, _owner_jobs_app, _configure_api_key
):
    """RD-DA-001: user B cannot enqueue card.generate for user A's paper → 403.

    The paper_ownership_extractor wired into build_jobs_router must fire before
    defer_async so that cross-user paper-ID injection is blocked at enqueue time.
    """
    import jarvis_common.task_registry as task_registry

    paper_id_a = contract_two_users.paper_id_a

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate": mock_task}):
        async with _authed_client(_owner_jobs_app, contract_two_users.cookie_b) as c:
            resp = await c.post(
                "/api/jobs",
                json={
                    "kind": "card.generate",
                    "payload": {
                        "paper_id": paper_id_a,
                        "deck_id": 1,
                        "max_cards": 3,
                    },
                },
            )

    assert resp.status_code == 403, (
        f"RD-DA-001: user B got {resp.status_code} enqueueing card.generate for user A's "
        f"paper {paper_id_a} (expected 403). Body: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# RD-DA-002: create_job requires a browser session (no user_id → 401)
# ---------------------------------------------------------------------------


async def test_create_job_requires_session_identity(
    contract_two_users, _owner_jobs_app, _configure_api_key
):
    """RD-DA-002: API-key-only caller (no session cookie) → 401 for create_job.

    current_user_id_strict raises 401 when request.state.user_id is absent.
    This closes the path where user_id=None bypassed paper ownership checks.
    """
    import jarvis_common.task_registry as task_registry

    paper_id_a = contract_two_users.paper_id_a

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate": mock_task}):
        # No cookie — API-key only
        async with _api_key_client(_owner_jobs_app) as c:
            resp = await c.post(
                "/api/jobs",
                json={
                    "kind": "card.generate",
                    "payload": {
                        "paper_id": paper_id_a,
                        "deck_id": 1,
                        "max_cards": 3,
                    },
                },
            )

    assert resp.status_code == 401, (
        f"RD-DA-002: API-key-only caller got {resp.status_code} (expected 401). "
        f"Body: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# Helpers: seed a procrastinate_jobs row for a given user
#
# Verified: db/init.sql:178-192 — procrastinate_jobs schema (id, queue_name,
#   task_name, args::jsonb, status).  args carries "job_id" (JARVIS UUID) and
#   "user_id" (int); get_unified reads args->>'user_id' for ownership.
# Verified: jobs.py:300-309 — get_unified delegates to
#   get_procrastinate_job_for_jarvis_id which queries args->>'job_id'.
# ---------------------------------------------------------------------------


async def _seed_pj(conn, *, user_id: int, job_id: str, status: str = "todo") -> None:
    """Insert a minimal ``procrastinate_jobs`` row owned by *user_id*.

    Verified: db/init.sql:178-192 — procrastinate_jobs columns used here.
    Pass a TERMINAL status (succeeded/failed/cancelled) when the test opens the
    SSE stream, so ``stream_job_events`` emits the terminal frame and exits
    instead of polling to the ``MAX_STREAM_SECONDS`` (750s) ceiling.
    """
    await conn.execute(
        """
        INSERT INTO procrastinate_jobs (queue_name, task_name, args, status)
        VALUES ('learning_engine', 'card.generate', $1::jsonb, $2::procrastinate_job_status)
        """,
        {"job_id": job_id, "user_id": user_id},
        status,
    )


# ---------------------------------------------------------------------------
# RD-DA-003: GET /api/jobs/{job_id} — non-owner gets 404
# ---------------------------------------------------------------------------


async def test_get_job_owner_gets_row_non_owner_gets_404(
    contract_two_users, _jobs_app, _configure_api_key
):
    """RD-DA-003: GET /api/jobs/{id} — owner gets 200, non-owner gets 404.

    Verified: jobs_router.py:250-252 — if row is None or not _owner_matches → 404.
    Verified: jobs_router.py:80-94 — _owner_matches coerces str/int user_id.
    Supersedes: mock-unit test_jobs_router.py::test_get_job_not_owner.
    """
    import uuid

    job_id = str(uuid.uuid4())
    # _jobs_app.state.db_pool is SharedConnPool wrapping contract_conn
    shared = _jobs_app.state.db_pool
    await _seed_pj(shared._conn, user_id=contract_two_users.user_a_id, job_id=job_id)

    # Non-owner (user B) should get 404
    async with _authed_client(_jobs_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get(f"/api/jobs/{job_id}")
    assert resp_b.status_code == 404, (
        f"Non-owner got {resp_b.status_code} (expected 404). Body: {resp_b.text[:300]}"
    )

    # Owner (user A) should get 200
    async with _authed_client(_jobs_app, contract_two_users.cookie_a) as c:
        resp_a = await c.get(f"/api/jobs/{job_id}")
    assert resp_a.status_code == 200, (
        f"Owner got {resp_a.status_code} (expected 200). Body: {resp_a.text[:300]}"
    )


# ---------------------------------------------------------------------------
# GET /api/jobs — list scoped to caller only
# ---------------------------------------------------------------------------


async def test_list_jobs_returns_only_caller_jobs(
    contract_two_users, _jobs_app, _configure_api_key
):
    """GET /api/jobs lists only the caller's own jobs (user_id filter in SQL).

    Verified: jobs_router.py:268-275 — list_jobs(..., user_id=str(user_id) if user_id else None).
    Verified: jobs.py:528-539 — $3 user_id parameter scopes WHERE clause.
    Supersedes: mock-unit test_jobs_router.py::test_list_jobs_user_scoped.
    """
    import uuid

    job_a = str(uuid.uuid4())
    shared = _jobs_app.state.db_pool

    # Seed one job for user A, one for user B
    for uid, jid in [
        (contract_two_users.user_a_id, job_a),
        (contract_two_users.user_b_id, str(uuid.uuid4())),
    ]:
        await _seed_pj(shared._conn, user_id=uid, job_id=jid)

    # User A's list should contain job_a, not user B's job
    async with _authed_client(_jobs_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/jobs")
    assert resp.status_code == 200
    ids = [r.get("id") for r in resp.json()]
    assert job_a in ids, f"Owner's job {job_a} not in list: {ids}"
    # All returned jobs must belong to user A
    for row in resp.json():
        assert str(row.get("user_id")) == str(contract_two_users.user_a_id), (
            f"list_jobs returned a row not owned by user A: {row}"
        )


# ---------------------------------------------------------------------------
# POST /api/jobs/{job_id}/cancel — non-owner gets 404
# ---------------------------------------------------------------------------


async def test_cancel_job_owner_ok_non_owner_404(contract_two_users, _jobs_app, _configure_api_key):
    """POST /api/jobs/{id}/cancel — non-owner gets 404 before any cancellation.

    Verified: jobs_router.py:324-326 — row is None or not _owner_matches → 404.
    Supersedes: mock-unit test_jobs_router.py::test_cancel_job_non_owner.
    """
    import uuid

    job_id = str(uuid.uuid4())
    shared = _jobs_app.state.db_pool
    await _seed_pj(shared._conn, user_id=contract_two_users.user_a_id, job_id=job_id)

    # Non-owner must get 404 before the router imports/calls the broker app.
    async with _authed_client(_jobs_app, contract_two_users.cookie_b) as c:
        resp_b = await c.post(f"/api/jobs/{job_id}/cancel")

    assert resp_b.status_code == 404, (
        f"Non-owner got {resp_b.status_code} (expected 404). Body: {resp_b.text[:300]}"
    )


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/stream — non-owner gets 404 before SSE begins
# ---------------------------------------------------------------------------


async def test_stream_job_owner_only(contract_two_users, _jobs_app, _configure_api_key):
    """GET /api/jobs/{id}/stream — non-owner gets 404; ownership checked before SSE opens.

    Verified: jobs_router.py:299-301 — initial lookup + _owner_matches check before
    StreamingResponse is returned; 404 fires synchronously.
    Supersedes: mock-unit test_jobs_router.py::test_stream_job_non_owner_404.
    """
    import uuid

    job_id = str(uuid.uuid4())
    shared = _jobs_app.state.db_pool
    await _seed_pj(shared._conn, user_id=contract_two_users.user_a_id, job_id=job_id)

    # User B (non-owner) must get 404 synchronously
    async with _authed_client(_jobs_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get(f"/api/jobs/{job_id}/stream")
    assert resp_b.status_code == 404, (
        f"Non-owner stream check: got {resp_b.status_code} (expected 404). "
        f"Body: {resp_b.text[:300]}"
    )


# ---------------------------------------------------------------------------
# Anonymous callers rejected on SSE + list_jobs
# ---------------------------------------------------------------------------


async def test_anonymous_stream_returns_401(_jobs_app, _configure_api_key):
    """Anonymous SSE GET (no session) returns 401.

    stream_job now uses current_user_id_strict so any caller without a valid
    browser session receives 401 before the ownership check runs.

    Verified: jobs_router.py — stream_job uses Depends(current_user_id_strict).
    """
    import uuid

    job_id = str(uuid.uuid4())

    async with _api_key_client(_jobs_app) as c:
        resp = await c.get(f"/api/jobs/{job_id}/stream")
    assert resp.status_code == 401, (
        f"Anonymous SSE: got {resp.status_code} (expected 401). Body: {resp.text[:300]}"
    )


async def test_anonymous_list_jobs_returns_401(_jobs_app, _configure_api_key):
    """Anonymous list_jobs GET (no session) returns 401.

    list_jobs now uses current_user_id_strict so an API-key-only caller cannot
    retrieve a job list without a resolved user identity.

    Verified: jobs_router.py — list_jobs uses Depends(current_user_id_strict).
    """
    async with _api_key_client(_jobs_app) as c:
        resp = await c.get("/api/jobs")
    assert resp.status_code == 401, (
        f"Anonymous list_jobs: got {resp.status_code} (expected 401). Body: {resp.text[:300]}"
    )


async def test_owner_matches_rejects_none_caller():
    """_owner_matches(None, None) is False — NULL-row jobs require authenticated callers.

    The old behaviour returned True for (None, None),
    meaning anonymous callers matched system-owned NULL-row jobs via SSE.
    The fix makes caller_user_id=None always return False.

    Verified: jobs_router.py _owner_matches — first guard: if caller_user_id is None: return False.
    """
    from jarvis_common.jobs_router import _owner_matches

    assert _owner_matches(None, None) is False, "_owner_matches(None, None) must be False"
    assert _owner_matches(42, None) is False, "_owner_matches(42, None) must be False"


async def test_owner_can_stream_own_job(contract_two_users, _jobs_app, _configure_api_key):
    """Authenticated owner gets 200 text/event-stream on their own job.

    Verifies the happy-path: authenticated owners must
    still be able to open the SSE stream.

    Verified: jobs_router.py stream_job — owner check passes → StreamingResponse.
    """
    import uuid

    job_id = str(uuid.uuid4())
    shared = _jobs_app.state.db_pool
    # Seed the job already TERMINAL so stream_job_events emits its frame and exits
    # immediately — otherwise the in-process ASGITransport never delivers the
    # client disconnect and the stream polls to the 750s MAX_STREAM_SECONDS ceiling.
    await _seed_pj(
        shared._conn, user_id=contract_two_users.user_a_id, job_id=job_id, status="succeeded"
    )

    async with _authed_client(_jobs_app, contract_two_users.cookie_a) as c:
        # Stream the SSE endpoint and check status + headers WITHOUT draining the
        # event-stream body. A plain ``await c.get(...)`` blocks until the server
        # closes the stream (its idle timeout, ~12 min) — this test only needs to
        # verify the stream OPENS for the owner. Mirrors test_jobs_sse_ownership.py.
        async with c.stream("GET", f"/api/jobs/{job_id}/stream") as resp:
            assert resp.status_code == 200, f"Owner SSE: got {resp.status_code} (expected 200)."
            ct = resp.headers.get("content-type", "")
            assert "text/event-stream" in ct, f"Expected text/event-stream, got {ct!r}"


async def test_non_owner_stream_returns_404_not_403(
    contract_two_users, _jobs_app, _configure_api_key
):
    """Authenticated non-owner gets 404 (NOT 403) on another user's job SSE.

    404 is deliberate: leaking job existence to unauthorized callers via 403
    would expose whether a job_id exists. The router always responds 404 on
    ownership mismatch.

    Verified: jobs_router.py stream_job — _owner_matches false → 404.
    """
    import uuid

    job_id = str(uuid.uuid4())
    shared = _jobs_app.state.db_pool
    await _seed_pj(shared._conn, user_id=contract_two_users.user_a_id, job_id=job_id)

    async with _authed_client(_jobs_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/jobs/{job_id}/stream")
    assert resp.status_code == 404, (
        f"Non-owner SSE: got {resp.status_code} (expected 404, not 403). Body: {resp.text[:300]}"
    )
