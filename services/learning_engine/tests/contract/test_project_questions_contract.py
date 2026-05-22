"""Project questions + activity feed contract tests — A210, A211.

Covers:
- DELETE /api/questions/{id}             (A210) — owner delete; non-owner 404
- GET /api/projects/{id}/activity        (A211) — UNION feed kinds + non-owner 404

A208 (list) and A209 (create IDOR) are already covered in test_le_contract.py.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import UTC, datetime, timedelta

from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_API_KEY = "le-contract-pq-test-key"


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
# §A210 — DELETE /api/questions/{id} — owner deletes; non-owner gets 404
# ---------------------------------------------------------------------------


async def test_delete_question_owner_gets_204(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A can delete their own question — 204 and row gone from DB.

    Exercises the real ``DELETE FROM project_questions WHERE id = $1 AND
    user_id = $2`` ownership filter.
    """
    project_id = contract_two_users.project_id_a
    # Seed a question for user A
    question_id = await contract_conn.fetchval(
        """INSERT INTO project_questions (project_id, user_id, body)
           VALUES ($1, $2, 'Contract question to delete')
           RETURNING id""",
        project_id,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/questions/{question_id}")

    assert resp.status_code == 204, (
        f"Owner expected 204 deleting question {question_id}; "
        f"got {resp.status_code}: {resp.text[:300]}"
    )
    still_exists = await contract_conn.fetchval(
        "SELECT id FROM project_questions WHERE id = $1",
        question_id,
    )
    assert still_exists is None, f"Question {question_id} still in DB after owner DELETE 204"


async def test_delete_question_user_b_gets_404(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B cannot delete user A's question — must get 404 (IDOR guard).

    Collapses test_project_questions.py's SQL-substring assertion
    ``"WHERE id = $1 AND user_id = $2"`` to a real DB scoping proof.
    """
    project_id = contract_two_users.project_id_a
    question_id = await contract_conn.fetchval(
        """INSERT INTO project_questions (project_id, user_id, body)
           VALUES ($1, $2, 'Question that B tries to delete')
           RETURNING id""",
        project_id,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/questions/{question_id}")

    assert resp.status_code != 401, (
        f"DELETE /api/questions/{question_id}: got 401 — session wiring bug"
    )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} trying to delete user A's question "
        f"{question_id} (expected 404). Body: {resp.text[:300]}"
    )
    # Verify the question was NOT deleted
    still_exists = await contract_conn.fetchval(
        "SELECT id FROM project_questions WHERE id = $1",
        question_id,
    )
    assert still_exists is not None, (
        f"Question {question_id} was deleted despite user B getting 404 — IDOR data-mutation bug"
    )


# ---------------------------------------------------------------------------
# §A211 — GET /api/projects/{id}/activity — UNION feed + non-owner 404
# ---------------------------------------------------------------------------


async def test_list_project_activity_non_owner_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot access user A's project activity feed — must get 404.

    Exercises the _assert_project_owner guard in list_project_activity.
    """
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/projects/{project_id}/activity")

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} accessing user A's activity feed "
        f"for project {project_id} (expected 404). Body: {resp.text[:300]}"
    )


async def test_list_project_activity_owner_gets_200_with_correct_kinds(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """Owner's activity feed returns items with valid 'kind' values.

    Seeds a completed task and verifies the UNION query returns
    'completed_task' kind rows. Collapses test_project_questions.py's
    SQL-substring assertion about the UNION structure to a behavioral proof.
    """
    project_id = contract_two_users.project_id_a
    user_a_id = contract_two_users.user_a_id

    # Seed a completed task so the feed has at least one 'completed_task' row
    await contract_conn.execute(
        """INSERT INTO tasks (project_id, title, status, completed_at, user_id)
           VALUES ($1, 'Activity feed task', 'done', NOW(), $2)""",
        project_id,
        user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/projects/{project_id}/activity")

    assert resp.status_code == 200, (
        f"GET /api/projects/{project_id}/activity for owner failed: "
        f"{resp.status_code}: {resp.text[:300]}"
    )
    items = resp.json()
    # May be empty if all seeds have NULL completed_at; at minimum must be a list
    assert isinstance(items, list), f"Expected list; got {type(items)}"

    valid_kinds = {"added_paper", "completed_task", "completed_milestone"}
    for item in items:
        assert "kind" in item, f"Activity item missing 'kind': {item}"
        assert item["kind"] in valid_kinds, (
            f"Unexpected kind {item['kind']!r}; valid: {valid_kinds}"
        )
        assert "ts" in item, f"Activity item missing 'ts': {item}"
        assert "label" in item, f"Activity item missing 'label': {item}"

    # Verify the completed task we just seeded appears in the feed
    kinds_present = {item["kind"] for item in items}
    assert "completed_task" in kinds_present, (
        f"Expected 'completed_task' in activity feed kinds {kinds_present} "
        f"after seeding a done task"
    )


# ---------------------------------------------------------------------------
# §A212 — POST /api/projects/{id}/questions — row written with caller user_id
# ---------------------------------------------------------------------------


async def test_create_question_row_has_caller_user_id(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/projects/{id}/questions inserts row with correct user_id.

    Collapses test_project_questions.py's SQL-text assertion to a real DB proof.
    # Verified: services/learning_engine/learning_engine/routers/project_questions.py:98-108
    """
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/projects/{project_id}/questions",
            json={"body": "Contract question E1"},
        )

    assert resp.status_code == 201, (
        f"POST /api/projects/{project_id}/questions failed: {resp.status_code}: {resp.text[:300]}"
    )
    question_id = resp.json()["id"]
    db_user_id = await contract_conn.fetchval(
        "SELECT user_id FROM project_questions WHERE id = $1",
        question_id,
    )
    assert db_user_id == contract_two_users.user_a_id, (
        f"Question {question_id} has user_id={db_user_id}; expected {contract_two_users.user_a_id}"
    )


# ---------------------------------------------------------------------------
# §A213 — GET /api/projects/{id}/questions — list returns only own questions
# ---------------------------------------------------------------------------


async def test_list_questions_returns_only_own(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/projects/{id}/questions lists only questions owned by the caller.

    Seeds a question for user A; user B has a different project (no access to A's).
    Also verifies user A can list their own question.
    # Verified: services/learning_engine/learning_engine/routers/project_questions.py:52-73
    """
    project_id = contract_two_users.project_id_a
    question_id = await contract_conn.fetchval(
        """INSERT INTO project_questions (project_id, user_id, body)
           VALUES ($1, $2, 'Isolated question E1')
           RETURNING id""",
        project_id,
        contract_two_users.user_a_id,
    )

    # User A can see their own question
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_a = await c.get(f"/api/projects/{project_id}/questions")
    assert resp_a.status_code == 200, (
        f"User A list questions failed: {resp_a.status_code}: {resp_a.text[:300]}"
    )
    ids_a = [q["id"] for q in resp_a.json()]
    assert question_id in ids_a, f"User A's question {question_id} missing from list; got {ids_a}"

    # User B gets 404 (project not theirs)
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get(f"/api/projects/{project_id}/questions")
    assert resp_b.status_code == 404, (
        f"IDOR: user B got {resp_b.status_code} listing A's questions (expected 404)"
    )


# ---------------------------------------------------------------------------
# §A214 — update-idempotency: re-creating question doesn't change first row
# ---------------------------------------------------------------------------


async def test_create_question_idempotency_on_duplicate_body(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/projects/{id}/questions with same body creates separate rows (no de-dup).

    Verifies two POST calls produce two separate rows — confirming the insert has
    no UNIQUE constraint on (project_id, user_id, body). This is idempotency
    documentation: the caller is responsible for de-dup, not the API.
    # Verified: services/learning_engine/learning_engine/routers/project_questions.py:96-108
    """
    project_id = contract_two_users.project_id_a
    body_text = "Duplicate question body contract"

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp1 = await c.post(f"/api/projects/{project_id}/questions", json={"body": body_text})
        resp2 = await c.post(f"/api/projects/{project_id}/questions", json={"body": body_text})

    assert resp1.status_code == 201 and resp2.status_code == 201, (
        f"Expected both creates to succeed; got {resp1.status_code}, {resp2.status_code}"
    )
    id1, id2 = resp1.json()["id"], resp2.json()["id"]
    assert id1 != id2, (
        f"Expected two separate question rows; both returned id={id1} — unexpected de-dup"
    )

    count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM project_questions WHERE project_id = $1 AND body = $2",
        project_id,
        body_text,
    )
    assert count >= 2, f"Expected >= 2 rows with same body; found {count}"
