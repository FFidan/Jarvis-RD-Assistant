"""Executive dashboard contract tests — A192, A193, A194, A195, A196, A197.

Covers:
- GET  /api/executive/intent/today  (A192) — scoped to caller's user_id
- POST /api/executive/intent/today  (A193) — upserts daily_intent row for caller
- GET  /api/executive/my-day        (A194) — response shape + scoped to caller
- GET  /api/executive/my-day-bundle (A195) — bundle keys present + scoped to caller
- POST /api/executive/tasks         (A196) — quick-add task; 404 for non-owned project
- POST /api/executive/focus/log     (A197) — daily_log incremented; 404 for non-owned task
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import UTC, datetime, timedelta

from jarvis_common.testing import SharedConnPool

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "le-contract-executive-test-key"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _le_app(contract_conn):
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


# ---------------------------------------------------------------------------
# §A192 — GET /api/executive/intent/today
# ---------------------------------------------------------------------------


async def test_get_intent_today_scoped_to_caller(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/executive/intent/today returns caller's intent text (scoped by user_id).

    Seeds a daily_intent row for user A and verifies user B receives null/empty
    (their own — not A's). Collapses test_intent.py's mock-pool assertion.
    """
    # Seed intent for user A
    await contract_conn.execute(
        """INSERT INTO daily_intent (user_id, intent_date, intent_text)
           VALUES ($1, CURRENT_DATE, 'Contract intent for A')
           ON CONFLICT (user_id, intent_date)
           DO UPDATE SET intent_text = EXCLUDED.intent_text""",
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/executive/intent/today")
    assert resp_a.status_code == 200, (
        f"GET intent for user A failed: {resp_a.status_code}: {resp_a.text[:300]}"
    )
    body_a = resp_a.json()
    assert body_a.get("intent") == "Contract intent for A", (
        f"User A expected their own intent; got {body_a}"
    )

    # User B should NOT see user A's intent
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/executive/intent/today")
    assert resp_b.status_code == 200, (
        f"GET intent for user B failed: {resp_b.status_code}: {resp_b.text[:300]}"
    )
    body_b = resp_b.json()
    assert body_b.get("intent") != "Contract intent for A", (
        f"IDOR: user B received user A's intent text: {body_b}"
    )


# ---------------------------------------------------------------------------
# §A193 — POST /api/executive/intent/today
# ---------------------------------------------------------------------------


async def test_save_intent_today_upserts_for_caller(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/executive/intent/today upserts a daily_intent row for the caller.

    Collapses test_intent.py's SQL-text check to a real DB upsert proof.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/executive/intent/today",
            json={"intent": "Upserted intent contract"},
        )

    assert resp.status_code in (200, 201), (
        f"POST intent failed: {resp.status_code}: {resp.text[:300]}"
    )

    db_text = await contract_conn.fetchval(
        """SELECT intent_text FROM daily_intent
           WHERE user_id = $1 AND intent_date = CURRENT_DATE""",
        contract_two_users.user_a_id,
    )
    assert db_text == "Upserted intent contract", f"DB intent_text mismatch; got {db_text!r}"


# ---------------------------------------------------------------------------
# §A194 — GET /api/executive/my-day — response shape + scoping
# ---------------------------------------------------------------------------


async def test_get_my_day_response_shape(contract_two_users, _le_app, _configure_api_key):
    """GET /api/executive/my-day returns expected top-level keys.

    Collapses test_executive.py's SQL-dispatch fragmentation assertions to a
    response-shape behavioral contract. The endpoint aggregates six concurrent
    DB reads; we assert the shape is correct under seeded inputs.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/executive/my-day")

    assert resp.status_code == 200, (
        f"GET /api/executive/my-day failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    required_keys = {
        "tasks",
        "cards_due",
        "recommendations",
        "today_focus_hours",
        "focus_streak_days",
        "project_pulse",
    }
    missing = required_keys - set(body.keys())
    assert not missing, f"my-day response missing keys: {missing}. Got: {list(body.keys())}"
    assert isinstance(body["tasks"], list)
    assert isinstance(body["cards_due"], int) and body["cards_due"] >= 0
    assert isinstance(body["focus_streak_days"], int) and body["focus_streak_days"] >= 0
    assert isinstance(body["project_pulse"], list)


async def test_get_my_day_tasks_scoped_to_caller(contract_two_users, _le_app, _configure_api_key):
    """GET /api/executive/my-day tasks list does not include user B's tasks.

    User A's task_id_a has title containing ZZZ-ISOLATION-A-TASK; user B must
    not see it in their my-day response.
    """
    from jarvis_common.testing import A_TASK_TITLE

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/executive/my-day")

    assert resp.status_code == 200
    task_titles = [t["title"] for t in resp.json()["tasks"]]
    assert A_TASK_TITLE not in task_titles, (
        f"IDOR: user B's my-day contains user A's task title {A_TASK_TITLE!r}. "
        f"All titles: {task_titles}"
    )


# ---------------------------------------------------------------------------
# §A195 — GET /api/executive/my-day-bundle — bundle keys present
# ---------------------------------------------------------------------------


async def test_get_my_day_bundle_response_keys(contract_two_users, _le_app, _configure_api_key):
    """GET /api/executive/my-day-bundle returns all required bundle keys.

    Collapses test_executive.py's bundle-key assertions (SQL-dispatch fragments)
    to a behavioral shape contract.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/executive/my-day-bundle")

    assert resp.status_code == 200, (
        f"GET my-day-bundle failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    required_keys = {"tasks", "intent", "threads", "yesterday", "journal"}
    missing = required_keys - set(body.keys())
    assert not missing, f"my-day-bundle missing keys: {missing}. Got: {list(body.keys())}"
    assert isinstance(body["tasks"], list)
    assert isinstance(body["threads"], list)
    # yesterday must have required subkeys
    yday = body["yesterday"]
    assert "date" in yday and "focused_hours" in yday and "tasks_done" in yday, (
        f"yesterday missing subkeys: {yday}"
    )


# ---------------------------------------------------------------------------
# §A196 — POST /api/executive/tasks — quick-add; 404 for non-owned project
# ---------------------------------------------------------------------------


async def test_quick_add_task_non_owned_project_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot quick-add a task to user A's project — 404 (IDOR guard)."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            "/api/executive/tasks",
            json={"title": "Injected Task", "project_id": project_id},
        )

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} quick-adding task to user A's project "
        f"{project_id} (expected 404). Body: {resp.text[:300]}"
    )


async def test_quick_add_task_creates_row_with_user_id(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/executive/tasks creates a task with the caller's user_id."""
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/executive/tasks",
            json={"title": "Quick Task Contract"},
        )

    assert resp.status_code == 201, f"Quick-add task failed: {resp.status_code}: {resp.text[:300]}"
    task_id = resp.json()["id"]
    db_user_id = await contract_conn.fetchval(
        "SELECT user_id FROM tasks WHERE id = $1",
        task_id,
    )
    assert db_user_id == contract_two_users.user_a_id, (
        f"Quick-added task {task_id} has user_id={db_user_id}; "
        f"expected {contract_two_users.user_a_id}"
    )


# ---------------------------------------------------------------------------
# §A197 — POST /api/executive/focus/log — daily_log upsert; 404 non-owned task
# ---------------------------------------------------------------------------


async def test_log_focus_session_non_owned_task_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot log focus against user A's task — 404 (IDOR guard)."""
    task_id_a = contract_two_users.task_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            "/api/executive/focus/log",
            json={"duration_minutes": 25, "task_id": task_id_a},
        )

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} logging focus against user A's task "
        f"{task_id_a} (expected 404). Body: {resp.text[:300]}"
    )


async def test_log_focus_session_persists_to_daily_log(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/executive/focus/log upserts today's daily_log.focus_hours for caller.

    Collapses test_executive.py's SQL-substring assertions about the UPSERT
    structure to a real DB persistence proof.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/executive/focus/log",
            json={"duration_minutes": 30},
        )

    assert resp.status_code == 200, f"POST focus/log failed: {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "recorded_hours" in body, f"Response missing recorded_hours: {body}"
    assert body["recorded_hours"] > 0, f"recorded_hours should be > 0; got {body}"

    # Verify the daily_log row was created/updated for user A
    focus_hours = await contract_conn.fetchval(
        """SELECT focus_hours FROM daily_log
           WHERE user_id = $1 AND log_date = CURRENT_DATE""",
        contract_two_users.user_a_id,
    )
    assert focus_hours is not None and focus_hours > 0, (
        f"Expected daily_log.focus_hours > 0 for user A after focus log; got {focus_hours}"
    )
