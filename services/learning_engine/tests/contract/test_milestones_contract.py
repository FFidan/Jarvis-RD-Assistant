"""Milestones CRUD contract tests — A201, A202, A203, A204.

Covers:
- GET /api/projects/{id}/milestones    (A201) — owner sees list; non-owner 404
- POST /api/projects/{id}/milestones   (A202) — row inserted with user_id; non-owner 404
- PUT /api/milestones/{id}             (A203) — owner update; non-owner 404
- DELETE /api/milestones/{id}          (A204) — owner delete; non-owner 404
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import UTC, datetime, timedelta

from jarvis_common.testing import SharedConnPool

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "le-contract-milestones-test-key"


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
# §A201 — GET /api/projects/{id}/milestones
# ---------------------------------------------------------------------------


async def test_list_milestones_non_owner_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot list milestones for user A's project — 404 (IDOR guard)."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/projects/{project_id}/milestones")

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} listing milestones for user A's project "
        f"{project_id} (expected 404). Body: {resp.text[:300]}"
    )


async def test_list_milestones_owner_sees_own(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A can list milestones for their own project (positive control)."""
    project_id = contract_two_users.project_id_a
    # Seed a milestone
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'Contract Milestone', NOW() + INTERVAL '7 days', $2)
           RETURNING id""",
        project_id,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/projects/{project_id}/milestones")

    assert resp.status_code == 200, (
        f"GET milestones for owner failed: {resp.status_code}: {resp.text[:300]}"
    )
    ids = [m["id"] for m in resp.json()]
    assert ms_id in ids, f"Seeded milestone {ms_id} not in owner's list {ids}"


# ---------------------------------------------------------------------------
# §A202 — POST /api/projects/{id}/milestones
# ---------------------------------------------------------------------------


async def test_create_milestone_row_has_user_id(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST milestone creates row with correct user_id in DB."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/projects/{project_id}/milestones",
            json={"name": "New Contract MS", "deadline": "2099-12-31T00:00:00Z"},
        )

    assert resp.status_code == 201, f"POST milestone failed: {resp.status_code}: {resp.text[:300]}"
    ms_id = resp.json()["id"]
    db_user_id = await contract_conn.fetchval(
        "SELECT user_id FROM milestones WHERE id = $1",
        ms_id,
    )
    assert db_user_id == contract_two_users.user_a_id, (
        f"Milestone {ms_id} has user_id={db_user_id}; expected {contract_two_users.user_a_id}"
    )


async def test_create_milestone_non_owner_project_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot create a milestone in user A's project — 404."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            f"/api/projects/{project_id}/milestones",
            json={"name": "Injected MS", "deadline": "2099-01-01T00:00:00Z"},
        )

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} creating milestone in user A's project "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A203 — PUT /api/milestones/{id}
# ---------------------------------------------------------------------------


async def test_update_milestone_owner_gets_200(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A can update their own milestone — 200 with updated name."""
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'Original MS', NOW() + INTERVAL '30 days', $2)
           RETURNING id""",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/milestones/{ms_id}", json={"name": "Updated MS Name"})

    assert resp.status_code == 200, (
        f"Owner expected 200 updating milestone {ms_id}; got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["name"] == "Updated MS Name"


async def test_update_milestone_user_b_gets_404(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B cannot update user A's milestone — 404 (IDOR guard)."""
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'B Tries To Edit', NOW() + INTERVAL '30 days', $2)
           RETURNING id""",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.put(f"/api/milestones/{ms_id}", json={"name": "Hijacked"})

    assert resp.status_code != 401, f"PUT /api/milestones/{ms_id}: got 401 — session wiring bug"
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} updating user A's milestone {ms_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A204 — DELETE /api/milestones/{id}
# ---------------------------------------------------------------------------


async def test_delete_milestone_user_b_gets_404(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B cannot delete user A's milestone — 404 (IDOR guard)."""
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'B Tries To Delete', NOW() + INTERVAL '30 days', $2)
           RETURNING id""",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/milestones/{ms_id}")

    assert resp.status_code != 401, f"DELETE /api/milestones/{ms_id}: got 401 — session wiring bug"
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} deleting user A's milestone {ms_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


async def test_delete_milestone_owner_gets_204(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A can delete their own milestone — 204 and row gone from DB."""
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'Deletable MS', NOW() + INTERVAL '30 days', $2)
           RETURNING id""",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/milestones/{ms_id}")

    assert resp.status_code == 204, (
        f"Owner expected 204 deleting milestone {ms_id}; got {resp.status_code}: {resp.text[:300]}"
    )
    still_exists = await contract_conn.fetchval(
        "SELECT id FROM milestones WHERE id = $1",
        ms_id,
    )
    assert still_exists is None, f"Milestone {ms_id} still in DB after DELETE 204"
