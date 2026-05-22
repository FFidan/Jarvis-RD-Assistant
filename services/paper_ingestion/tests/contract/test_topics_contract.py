"""Topics domain contract tests — Phase B target rows A163-A169.

Survivor-of: (all NONE — no prior contract coverage).

Rows covered:
  A163 GET    /api/topics                   — global list returned from DB
  A164 GET    /api/topics/subscriptions     — only caller's subscribed topic_ids
  A165 PUT    /api/topics/{id}/subscribe    — subscription row created for user
  A166 DELETE /api/topics/{id}/subscribe   — subscription row deleted for user
  A167 POST   /api/topics                   — admin-only; new topic row persisted
  A168 PUT    /api/topics/{id}              — admin-only; fields updated; 404 non-existent
  A169 DELETE /api/topics/{id}              — admin-only; row deleted; 404 non-existent

Admin routes (A167-A169): require_admin reads request.state.user_role.
The _pi_app_with_pool fixture removes the owner-override so the real session
path runs.  We inject a mock admin session via a custom admin fixture that
seeds an admin user and uses their session cookie.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import httpx

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_API_KEY = "topics-contract-key-phase-b-do-not-use-in-prod"


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


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared

    removed_override = app.dependency_overrides.pop(
        current_user_id_strict_with_owner_override, None
    )
    had_override = removed_override is not None

    yield app

    if original_pool is None:
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
    else:
        app.state.db_pool = original_pool

    if had_override:
        app.dependency_overrides[current_user_id_strict_with_owner_override] = removed_override


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _admin_cookie(contract_conn) -> str:
    """Seed an admin user and return their session cookie.

    require_admin reads request.state.user_role which SessionMiddleware sets
    from the sessions.user_id → users.role lookup.  We seed role='admin' so
    the real middleware path grants access.
    """
    admin_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('admin@contract.example.com', 'admin') RETURNING id"
    )
    session_id = await contract_conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day') RETURNING id""",
        admin_id,
    )
    return str(session_id)


def _make_client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# A163: GET /api/topics — global topic list returned from DB
# ---------------------------------------------------------------------------


async def test_a163_list_topics_returns_global_list(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A163: GET /api/topics returns all topics rows.

    Verified: topics.py:21-27 list_topics — SELECT * FROM topics ORDER BY name.
    Note: topics are global (no user_id column on topics table); _seed_resources
    inserts one topic per user so at least 2 rows exist.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/topics")

    assert resp.status_code == 200, resp.text[:300]
    items = resp.json()
    assert isinstance(items, list), f"Expected list, got {type(items)}"
    # At minimum the seed topics from contract_two_users exist
    assert len(items) >= 1, "Expected at least one topic in global list"
    for item in items:
        assert "id" in item, f"Missing 'id' in topic: {item}"
        assert "name" in item, f"Missing 'name' in topic: {item}"


# ---------------------------------------------------------------------------
# A164: GET /api/topics/subscriptions — only caller's subscribed topic_ids
# ---------------------------------------------------------------------------


async def test_a164_list_subscriptions_returns_only_own_subscriptions(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A164: GET /api/topics/subscriptions returns only caller's topic_ids.

    Verified: topics.py:34-46 list_my_subscriptions — WHERE user_id=$1.
    _seed_resources inserts one subscription per user, so topic_id_a belongs to user A.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/topics/subscriptions")

    assert resp.status_code == 200, resp.text[:300]
    topic_ids = resp.json()
    assert isinstance(topic_ids, list), f"Expected list of ints, got {type(topic_ids)}"
    assert contract_two_users.topic_id_a in topic_ids, (
        f"User A's topic_id {contract_two_users.topic_id_a} not in subscriptions: {topic_ids}"
    )

    # User B's subscriptions must not appear in A's response
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/topics/subscriptions")
    assert resp_b.status_code == 200
    topic_ids_b = resp_b.json()
    # A's topic_id_a must not be in B's subscription list
    assert contract_two_users.topic_id_a not in topic_ids_b, (
        f"User A's topic leaked into User B's subscriptions: {topic_ids_b}"
    )


# ---------------------------------------------------------------------------
# A165: PUT /api/topics/{id}/subscribe — subscription row created
# ---------------------------------------------------------------------------


async def test_a165_subscribe_creates_row_in_db(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A165: PUT /api/topics/{id}/subscribe inserts user_topic_subscriptions row.

    Verified: topics.py:51-68 subscribe_to_topic — INSERT ON CONFLICT DO NOTHING.
    """
    # Create a fresh topic to subscribe to (without using the seeded one)
    new_topic_id = await contract_conn.fetchval(
        "INSERT INTO topics (name, query_terms) VALUES ('Sub Test Topic', ARRAY['sub']) RETURNING id"
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/topics/{new_topic_id}/subscribe")

    assert resp.status_code == 204, resp.text[:300]

    row = await contract_conn.fetchrow(
        "SELECT 1 FROM user_topic_subscriptions WHERE user_id = $1 AND topic_id = $2",
        contract_two_users.user_a_id,
        new_topic_id,
    )
    assert row is not None, "Subscription row not found in DB after PUT /subscribe"

    # Idempotent: second call returns 204 (ON CONFLICT DO NOTHING)
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp2 = await c.put(f"/api/topics/{new_topic_id}/subscribe")
    assert resp2.status_code == 204, f"Idempotent subscribe failed: {resp2.status_code}"


# ---------------------------------------------------------------------------
# A166: DELETE /api/topics/{id}/subscribe — subscription row deleted
# ---------------------------------------------------------------------------


async def test_a166_unsubscribe_deletes_row_from_db(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A166: DELETE /api/topics/{id}/subscribe removes user_topic_subscriptions row.

    Verified: topics.py:73-86 unsubscribe_from_topic — DELETE WHERE user_id=$1 AND topic_id=$2.
    Uses topic_id_a which is already subscribed by user A from _seed_resources.
    """
    topic_id = contract_two_users.topic_id_a

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/topics/{topic_id}/subscribe")

    assert resp.status_code == 204, resp.text[:300]

    row = await contract_conn.fetchrow(
        "SELECT 1 FROM user_topic_subscriptions WHERE user_id = $1 AND topic_id = $2",
        contract_two_users.user_a_id,
        topic_id,
    )
    assert row is None, "Subscription row still present in DB after DELETE /subscribe"


# ---------------------------------------------------------------------------
# A167: POST /api/topics — admin-only; new row persisted; non-admin gets 403
# ---------------------------------------------------------------------------


async def test_a167_create_topic_admin_only_and_row_persists(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    _admin_cookie,
    contract_conn,
):
    """Covers map row A167: POST /api/topics admin-only gate + DB insert.

    Verified: topics.py:96-111 create_topic — Depends(require_admin) + INSERT.
    """
    # Non-admin gets 403
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_user = await c.post(
            "/api/topics",
            json={"name": "Blocked Topic", "query_terms": ["x"]},
        )
    assert resp_user.status_code == 403, f"Expected 403 for non-admin, got {resp_user.status_code}"

    # Admin can create
    async with _make_client(_pi_app_with_pool, _admin_cookie) as c:
        resp_admin = await c.post(
            "/api/topics",
            json={"name": "Admin Created Topic", "query_terms": ["admin", "test"]},
        )
    assert resp_admin.status_code == 201, resp_admin.text[:300]
    body = resp_admin.json()
    assert body["name"] == "Admin Created Topic"

    row = await contract_conn.fetchrow("SELECT name FROM topics WHERE id = $1", body["id"])
    assert row is not None and row["name"] == "Admin Created Topic"


# ---------------------------------------------------------------------------
# A168: PUT /api/topics/{id} — admin-only; fields updated; 404 non-existent
# ---------------------------------------------------------------------------


async def test_a168_update_topic_admin_only_and_404_non_existent(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    _admin_cookie,
    contract_conn,
):
    """Covers map row A168: PUT /api/topics/{id} admin gate + update + 404.

    Verified: topics.py:120-142 update_topic — Depends(require_admin) + dynamic_update.
    """
    topic_id = await contract_conn.fetchval(
        "INSERT INTO topics (name, query_terms) VALUES ('Update Test Topic', ARRAY['q']) RETURNING id"
    )

    # Non-admin gets 403
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_user = await c.put(f"/api/topics/{topic_id}", json={"name": "Hacked"})
    assert resp_user.status_code == 403, f"Expected 403 for non-admin, got {resp_user.status_code}"

    # Admin can update
    async with _make_client(_pi_app_with_pool, _admin_cookie) as c:
        resp_admin = await c.put(f"/api/topics/{topic_id}", json={"name": "Updated Topic Name"})
    assert resp_admin.status_code == 200, resp_admin.text[:300]
    assert resp_admin.json()["name"] == "Updated Topic Name"

    # 404 for non-existent
    async with _make_client(_pi_app_with_pool, _admin_cookie) as c:
        resp_404 = await c.put("/api/topics/999999999", json={"name": "Ghost"})
    assert resp_404.status_code == 404, (
        f"Expected 404 for missing topic, got {resp_404.status_code}"
    )


# ---------------------------------------------------------------------------
# A169: DELETE /api/topics/{id} — admin-only; row deleted; 404 non-existent
# ---------------------------------------------------------------------------


async def test_a169_delete_topic_admin_only_and_row_removed(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    _admin_cookie,
    contract_conn,
):
    """Covers map row A169: DELETE /api/topics/{id} admin gate + DB delete + 404.

    Verified: topics.py:151-167 delete_topic — Depends(require_admin) + delete_or_404.
    """
    topic_id = await contract_conn.fetchval(
        "INSERT INTO topics (name, query_terms) VALUES ('Delete Test Topic', ARRAY['d']) RETURNING id"
    )

    # Non-admin gets 403
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_user = await c.delete(f"/api/topics/{topic_id}")
    assert resp_user.status_code == 403, f"Expected 403 for non-admin, got {resp_user.status_code}"

    # Row still present
    still_there = await contract_conn.fetchval("SELECT id FROM topics WHERE id = $1", topic_id)
    assert still_there is not None, "Topic deleted by non-admin — should not happen"

    # Admin can delete
    async with _make_client(_pi_app_with_pool, _admin_cookie) as c:
        resp_admin = await c.delete(f"/api/topics/{topic_id}")
    assert resp_admin.status_code == 204, resp_admin.text[:300]

    gone = await contract_conn.fetchval("SELECT id FROM topics WHERE id = $1", topic_id)
    assert gone is None, "Topic row still present after admin delete"

    # 404 for already-deleted / non-existent
    async with _make_client(_pi_app_with_pool, _admin_cookie) as c:
        resp_404 = await c.delete(f"/api/topics/{topic_id}")
    assert resp_404.status_code == 404, (
        f"Expected 404 for missing topic, got {resp_404.status_code}"
    )
