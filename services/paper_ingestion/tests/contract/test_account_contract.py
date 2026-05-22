"""Account domain contract tests — Phase B target rows A1, A2.

Survivor-of: test_account.py mock-unit assertions for get_account, update_account.
Carve-out: app.state.http_client is MagicMock (outbound HTTP);
    send_magic_link (SMTP) mocked — outbound email boundary.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import httpx

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "account-contract-key-phase-b-do-not-use-in-prod"


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


def _make_client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# A1: GET /api/account — own profile fields returned, no other user's data
# ---------------------------------------------------------------------------


async def test_a1_get_account_returns_own_profile(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A1: GET /api/account returns caller's own AccountResponse.

    Verified: account.py:89-102 get_account at HEAD d21aaea8.
    Survivor-of (future Phase C): test_account.py mock-unit tests for get_account.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/account")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    # AccountResponse must include id, email, role
    for field in ("id", "email", "role"):
        assert field in body, f"Missing field {field!r} in account response: {body}"
    assert body["id"] == contract_two_users.user_a_id, (
        f"Expected user_a_id={contract_two_users.user_a_id}, got {body['id']}"
    )


async def test_a1_get_account_user_b_sees_own_profile(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A1: user B gets their own profile, not user A's data.

    Verified: account.py:95 current_user_id_strict(request) — strictly scoped.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/account")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body["id"] == contract_two_users.user_b_id, (
        f"User B should see their own id={contract_two_users.user_b_id}, got {body['id']}"
    )
    assert body["id"] != contract_two_users.user_a_id, (
        f"User B must not see user A's profile — IDOR: got id={body['id']}"
    )


# ---------------------------------------------------------------------------
# A2: PATCH /api/account — display_name update persists to DB
# ---------------------------------------------------------------------------


async def test_a2_patch_account_display_name_persists(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A2: PATCH /api/account updates display_name in DB.

    Verified: account.py:107-134 update_account display_name path at HEAD d21aaea8.
    Survivor-of (future Phase C): test_account.py mock-unit tests for update_account.
    """
    new_name = "Contract Test Name"
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.patch("/api/account", json={"display_name": new_name})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    # AccountUpdateResponse must indicate success
    assert "email_verification_sent" in body or "display_name" in body or "id" in body, (
        f"Unexpected response shape: {list(body.keys())}"
    )

    # Verify DB row updated
    row = await contract_conn.fetchrow(
        "SELECT display_name FROM users WHERE id = $1",
        contract_two_users.user_a_id,
    )
    assert row is not None
    assert row["display_name"] == new_name, (
        f"display_name not persisted to DB; expected {new_name!r}, got {row['display_name']!r}"
    )


async def test_a2_patch_account_email_clash_returns_409(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A2: PATCH /api/account with already-used email returns 409.

    Verified: account.py:147-156 clash check at HEAD d21aaea8.
    """
    # User B's email already exists — user A requesting that email must get 409
    user_b_email = await contract_conn.fetchval(
        "SELECT email FROM users WHERE id = $1",
        contract_two_users.user_b_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.patch("/api/account", json={"email": user_b_email})

    assert resp.status_code == 409, (
        f"Expected 409 for duplicate email, got {resp.status_code}: {resp.text[:300]}"
    )
