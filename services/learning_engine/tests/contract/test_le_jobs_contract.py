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
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_API_KEY = "le-jobs-contract-test-key-do-not-use-in-prod"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _le_app(contract_conn):
    """learning_engine app wired to the contract conn pool.

    FSRSManager / AnkiExporter / card_generator / http_client kept as idiomatic
    mocks (carve-out registry §5.2). Limiter disabled so rate-limit 429s never
    interfere with ownership / IDOR assertions.

    Pattern matches test_le_contract.py:_le_app.
    """
    from learning_engine.deps import get_anki_exporter, get_db_pool, get_fsrs_manager
    from learning_engine.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_http = getattr(app.state, "http_client", None)
    original_fsrs = getattr(app.state, "fsrs_manager", None)
    original_exporter = getattr(app.state, "anki_exporter", None)
    original_generator = getattr(app.state, "card_generator", None)

    mock_fsrs = MagicMock()
    _now = datetime.now(UTC)
    mock_fsrs.create_new_card.return_value = ({}, _now)
    mock_fsrs.schedule_review.return_value = ({}, {}, _now + timedelta(days=1))

    app.state.db_pool = shared
    app.state.http_client = AsyncMock()
    app.state.fsrs_manager = mock_fsrs
    app.state.anki_exporter = MagicMock()
    app.state.card_generator = AsyncMock()
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[get_fsrs_manager] = lambda: mock_fsrs
    app.dependency_overrides[get_anki_exporter] = lambda: MagicMock()

    from learning_engine.deps import limiter

    limiter_was_enabled = limiter.enabled
    limiter.enabled = False

    try:
        yield app
    finally:
        limiter.enabled = limiter_was_enabled
        if original_pool is None:
            if hasattr(app.state, "db_pool"):
                del app.state.db_pool
        else:
            app.state.db_pool = original_pool
        if original_http is None:
            if hasattr(app.state, "http_client"):
                del app.state.http_client
        else:
            app.state.http_client = original_http
        if original_fsrs is None:
            if hasattr(app.state, "fsrs_manager"):
                del app.state.fsrs_manager
        else:
            app.state.fsrs_manager = original_fsrs
        if original_exporter is None:
            if hasattr(app.state, "anki_exporter"):
                del app.state.anki_exporter
        else:
            app.state.anki_exporter = original_exporter
        if original_generator is None:
            if hasattr(app.state, "card_generator"):
                del app.state.card_generator
        else:
            app.state.card_generator = original_generator
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_fsrs_manager, None)
        app.dependency_overrides.pop(get_anki_exporter, None)


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    from jarvis_common import auth as _auth
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_API_KEY)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


def _client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


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
    """POST /api/jobs with a kind absent from LE_PUBLIC_JOB_KINDS returns 400/422.

    # Verified: services/learning_engine/learning_engine/routers/jobs.py:25
    (LE_PUBLIC_JOB_KINDS = {"card.generate"} — narrow allowlist).
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

    # Verified: libs/jarvis_common/jarvis_common/jobs_router.py:101
    (build_jobs_router constructs get_job with user_id check via jobs_lib.get_unified
    and _owner_matches).
    """
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


# ---------------------------------------------------------------------------
# LE-J-04: GET /api/jobs — list scoped to caller
# ---------------------------------------------------------------------------


async def test_le_j04_list_jobs_scoped_to_owner(
    contract_two_users,
    _le_app,
    _configure_api_key,
    contract_conn,
):
    """GET /api/jobs returns only the caller's jobs.

    # Verified: libs/jarvis_common/jarvis_common/jobs_router.py:101
    (build_jobs_router constructs list_jobs with user_id filter via
    jobs_lib.list_jobs).
    """
    job_a = await _insert_le_job(contract_conn, contract_two_users.user_a_id)
    job_b = await _insert_le_job(contract_conn, contract_two_users.user_b_id)

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/jobs")

    assert resp.status_code == 200, resp.text[:300]
    ids = [j["id"] for j in resp.json()]
    assert job_a in ids, f"User A's job {job_a} missing from own list: {ids}"
    assert job_b not in ids, f"IDOR leak — user A saw user B's job {job_b}"


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

    # Verified: libs/jarvis_common/jarvis_common/jobs_router.py:101
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
