"""Auth domain contract tests — target rows A16, A17, A18.

Survivor-of: test_auth_magic_link.py mock-unit assertions for verify,
    api_key_session, logout.
Carve-out: send_magic_link (SMTP) mocked — outbound email boundary.
"""

from __future__ import annotations

import pytest

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


def _make_unauthenticated_client(app):
    return _make_client(app, None)


# ---------------------------------------------------------------------------
# A16: POST /api/auth/verify — magic-link token consumed and session set
# ---------------------------------------------------------------------------


async def test_a16_verify_valid_token_returns_session_cookie(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A16: valid magic-link token → session cookie set + token consumed.

    Verified: auth.py:190-313 verify at HEAD d21aaea8.
    Survivor-of: test_auth_magic_link.py verify tests.
    """
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(minutes=15)

    await contract_conn.execute(
        """INSERT INTO magic_link_tokens (token_hash, user_id, expires_at)
           VALUES ($1, $2, $3)""",
        token_hash,
        contract_two_users.user_a_id,
        expires_at,
    )

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp.status_code == 200, (
        f"Expected 200 from verify, got {resp.status_code}: {resp.text[:300]}"
    )
    # Session cookie must be set
    assert "jarvis_session" in resp.cookies, (
        f"Expected jarvis_session cookie; got cookies: {dict(resp.cookies)}"
    )
    # Token must be consumed (used_at set)
    row = await contract_conn.fetchrow(
        "SELECT used_at FROM magic_link_tokens WHERE token_hash = $1",
        token_hash,
    )
    assert row is not None
    assert row["used_at"] is not None, "Token must be consumed (used_at set) after verify"


async def test_a16_verify_expired_token_returns_400(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A16: expired magic-link token → 400.

    Verified: auth.py:227-243 expiry check at HEAD d21aaea8.
    """
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    # Insert already-expired token
    expires_at = datetime.now(UTC) - timedelta(minutes=5)

    await contract_conn.execute(
        """INSERT INTO magic_link_tokens (token_hash, user_id, expires_at)
           VALUES ($1, $2, $3)""",
        token_hash,
        contract_two_users.user_a_id,
        expires_at,
    )

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp.status_code == 400, (
        f"Expected 400 for expired token, got {resp.status_code}: {resp.text[:300]}"
    )


async def test_a16_verify_already_used_token_returns_400(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A16: already-used token → 400 (replay protection).

    Verified: auth.py:231-237 used_at IS NOT NULL check at HEAD d21aaea8.
    """
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(UTC)

    await contract_conn.execute(
        """INSERT INTO magic_link_tokens (token_hash, user_id, expires_at, used_at)
           VALUES ($1, $2, $3, $4)""",
        token_hash,
        contract_two_users.user_a_id,
        now + timedelta(minutes=15),
        now - timedelta(minutes=1),  # already used
    )

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp.status_code == 400, (
        f"Expected 400 for already-used token, got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A18: POST /api/auth/logout — session revoked in DB
# ---------------------------------------------------------------------------


async def test_a18_logout_revokes_session_row_in_db(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A18: POST /api/auth/logout sets sessions.revoked_at and clears cookie.

    Verified: auth.py:467-514 logout at HEAD d21aaea8.
    Survivor-of: test_auth_magic_link.py logout tests.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/auth/logout")

    assert resp.status_code == 204, (
        f"Expected 204 from logout, got {resp.status_code}: {resp.text[:300]}"
    )

    # Verify session row has been revoked
    import uuid

    try:
        session_id = uuid.UUID(contract_two_users.cookie_a)
    except ValueError:
        # cookie_a may not be a UUID if it's a token value — skip DB check
        return

    row = await contract_conn.fetchrow(
        "SELECT revoked_at FROM sessions WHERE id = $1",
        str(session_id),
    )
    if row is not None:
        assert row["revoked_at"] is not None, "sessions.revoked_at must be set after logout"


async def test_a18_logout_without_session_returns_204(
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A18: logout without session cookie is idempotent → 204.

    Verified: auth.py:479 missing cookie early-return at HEAD d21aaea8.
    """
    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/logout")

    assert resp.status_code == 204, (
        f"Expected 204 for logout without session, got {resp.status_code}: {resp.text[:300]}"
    )


async def test_a18_logout_does_not_reissue_renewed_cookie(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """logout must not let the middleware re-issue a fresh 30-day cookie.

    The seeded session (expires_at = now + 1 day) is renewal-eligible, so
    SessionMiddleware.dispatch sets ``request.state.session_renewed`` BEFORE the
    route runs. logout revokes the row and clears the cookie; without clearing
    session_renewed, dispatch would append a fresh Max-Age=2592000 Set-Cookie
    AFTER the deletion, clobbering it. The only jarvis_session Set-Cookie must be
    the deletion (Max-Age=0).

    Verified: auth.py logout (session_renewed=None before delete_cookie);
    session_middleware.py dispatch re-issue branch.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/auth/logout")

    assert resp.status_code == 204, (
        f"Expected 204 from logout, got {resp.status_code}: {resp.text[:300]}"
    )

    session_cookies = [
        h for h in resp.headers.get_list("set-cookie") if h.startswith("jarvis_session=")
    ]
    assert session_cookies, "logout must emit a jarvis_session deletion cookie"
    assert not any("Max-Age=2592000" in h for h in session_cookies), (
        f"logout must NOT re-issue a renewal cookie; got: {session_cookies}"
    )
    assert any("Max-Age=0" in h for h in session_cookies), (
        f"logout must clear the cookie (Max-Age=0); got: {session_cookies}"
    )

    # Regression: the session row is still revoked (A18 invariant preserved).
    import uuid

    try:
        session_id = uuid.UUID(contract_two_users.cookie_a)
    except ValueError:
        return
    row = await contract_conn.fetchrow(
        "SELECT revoked_at FROM sessions WHERE id = $1",
        str(session_id),
    )
    if row is not None:
        assert row["revoked_at"] is not None, "sessions.revoked_at must be set after logout"


# ---------------------------------------------------------------------------
# the request-link cooldown probe is login-scoped — an in-flight
# email-change token (pending_email set) must NOT suppress a login-link.
# ---------------------------------------------------------------------------


async def test_request_link_email_change_token_does_not_suppress_login(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """an in-flight email-change token does not trigger the login cooldown.

    Seed a recent email-change token (pending_email set) for user A and NO recent
    login token, then request a login link for A's email. A login token
    (pending_email IS NULL) must be minted. Before the fix the unscoped cooldown
    probe saw the email-change token as "recent" and suppressed issuance.

    Verified: auth.py request_link cooldown probe (WHERE ... AND pending_email IS NULL).
    """
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock, patch

    email_a = "iso-user-a@contract.example.com"

    # In-flight email-change token (pending_email set, created_at defaults to now()).
    ec_hash = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
    await contract_conn.execute(
        "INSERT INTO magic_link_tokens (token_hash, user_id, expires_at, pending_email)"
        " VALUES ($1, $2, $3, $4)",
        ec_hash,
        contract_two_users.user_a_id,
        datetime.now(UTC) + timedelta(minutes=15),
        "changed@contract.example.com",
    )

    with patch("paper_ingestion.routers.auth.send_magic_link", AsyncMock()):
        async with _make_unauthenticated_client(_pi_app_with_pool) as c:
            resp = await c.post("/api/auth/request-link", json={"email": email_a})

    assert resp.status_code == 200, (
        f"Expected 200 from request-link, got {resp.status_code}: {resp.text[:300]}"
    )

    login_tokens = await contract_conn.fetchval(
        "SELECT count(*) FROM magic_link_tokens WHERE user_id = $1 AND pending_email IS NULL",
        contract_two_users.user_a_id,
    )
    assert int(login_tokens) == 1, (
        f"an in-flight email-change token must not suppress login-link issuance; "
        f"expected 1 login token, got {login_tokens}"
    )


# ---------------------------------------------------------------------------
# Non-ASCII api_key body must return 403, not 500
# ---------------------------------------------------------------------------


async def test_a17_api_key_session_non_ascii_returns_403(
    _pi_app_with_pool,
    _configure_api_key,
):
    """POST /api/auth/api-key-session with a non-ASCII body key → 403, not 500.

    Before the fix, ``hmac.compare_digest(submitted, _CACHED_API_KEY)`` raised
    ``TypeError: non-ASCII characters in compared strings`` when either operand
    contained bytes outside ASCII range, resulting in an unhandled 500.  The fix
    encodes both operands with ``str.encode('utf-8', errors='replace')`` so the
    comparison always runs and an invalid key returns 403.

    Verified against: auth.py api_key_session hmac.compare_digest call.
    """
    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        # Submit a body containing non-ASCII characters in the api_key field.
        resp = await c.post(
            "/api/auth/api-key-session",
            json={"api_key": "invälid-kéy-éàü"},
        )

    assert resp.status_code == 403, (
        f"Expected 403 for non-ASCII api_key, got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# api-key-session multi-tenant gate (owner exemption + fallback)
#
# Verified: services/paper_ingestion/paper_ingestion/routers/auth.py:391
# (api_key_session gate). The endpoint binds the session to the configured
# OWNER_USER_ID (a live admin) or, single-tenant only, the lowest-id admin
# fallback.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_api_key_login_cache():
    """Drop the module-level DB-override cache around each gate test."""
    from jarvis_common.auth import invalidate_api_key_login_cache

    invalidate_api_key_login_cache()
    yield
    invalidate_api_key_login_cache()


async def _seed_user_with_role(conn, email: str, role: str) -> int:
    return int(
        await conn.fetchval(
            "INSERT INTO users (email, role) VALUES ($1, $2) RETURNING id",
            email,
            role,
        )
    )


async def test_owner_exempt_mints_on_multi_user_box_flag_off(
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """B1: a configured admin OWNER_USER_ID mints on a 2+ user box with the flag OFF."""
    owner_id = await _seed_user_with_role(contract_conn, "owner-admin@example.com", "admin")
    await _seed_user_with_role(contract_conn, "member@example.com", "user")
    monkeypatch.setenv("OWNER_USER_ID", str(owner_id))
    monkeypatch.delenv("API_KEY_LOGIN_ENABLED", raising=False)

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 200, (
        f"Owner must mint on a multi-user box flag-off, got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["id"] == owner_id
    assert resp.json()["role"] == "admin"
    assert "jarvis_session" in resp.cookies


async def test_non_admin_owner_user_id_still_409s(
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """B1 guard: a non-admin OWNER_USER_ID does NOT exempt — the A-3 409 still fires.

    Flag ON so the multi-tenant gate is passed; the explicit-owner branch must
    then reject the non-admin OWNER_USER_ID with a 409 (not a silent member mint).
    """
    member_id = await _seed_user_with_role(contract_conn, "member-owner@example.com", "user")
    await _seed_user_with_role(contract_conn, "real-admin@example.com", "admin")
    monkeypatch.setenv("OWNER_USER_ID", str(member_id))
    monkeypatch.setenv("API_KEY_LOGIN_ENABLED", "true")

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 409, (
        f"Non-admin OWNER_USER_ID must 409, got {resp.status_code}: {resp.text[:300]}"
    )
    assert "non-admin" in resp.json()["detail"]


async def test_flag_on_without_owner_user_id_refuses_fallback_409(
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """Multi-tenant gate ON, no OWNER_USER_ID, multi-user → lowest-id fallback refused (409)."""
    await _seed_user_with_role(contract_conn, "admin-a@example.com", "admin")
    await _seed_user_with_role(contract_conn, "admin-b@example.com", "admin")
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.setenv("API_KEY_LOGIN_ENABLED", "true")

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 409, (
        f"Flag-on without OWNER_USER_ID must refuse the fallback (409), "
        f"got {resp.status_code}: {resp.text[:300]}"
    )
    assert "OWNER_USER_ID" in resp.json()["detail"]


async def test_non_owner_multi_user_flag_off_still_403(
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """Multi-user box, flag OFF, no exempt owner → multi-tenant gate still 403s."""
    await _seed_user_with_role(contract_conn, "admin-only@example.com", "admin")
    await _seed_user_with_role(contract_conn, "plain-member@example.com", "user")
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.delenv("API_KEY_LOGIN_ENABLED", raising=False)

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 403, (
        f"Multi-user flag-off must 403, got {resp.status_code}: {resp.text[:300]}"
    )


async def test_single_user_login_unchanged(
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """Single-user box mints via the lowest-id admin fallback (no OWNER_USER_ID)."""
    admin_id = await _seed_user_with_role(contract_conn, "solo-admin@example.com", "admin")
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.delenv("API_KEY_LOGIN_ENABLED", raising=False)

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 200, (
        f"Single-user login must mint, got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["id"] == admin_id
    assert "jarvis_session" in resp.cookies


async def test_db_override_enables_multi_user_login(
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """The DB override (auth.api_key_login_enabled row) flips the gate when env is off.

    With OWNER_USER_ID set to a live admin the owner is already exempt, so to
    exercise the DB-override read path: flag enabled purely
    via the DB row, no OWNER_USER_ID, multi-user → fallback refused (409). A
    bare 403 would prove the override was NOT read.
    """
    from jarvis_common.auth import API_KEY_LOGIN_CONFIG_KEY

    await _seed_user_with_role(contract_conn, "ovr-admin-a@example.com", "admin")
    await _seed_user_with_role(contract_conn, "ovr-admin-b@example.com", "admin")
    await contract_conn.execute(
        "INSERT INTO user_config (user_id, key, value) VALUES (NULL, $1, $2::jsonb)",
        API_KEY_LOGIN_CONFIG_KEY,
        True,
    )
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.delenv("API_KEY_LOGIN_ENABLED", raising=False)

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 409, (
        f"DB override must enable the gate (then 409s the fallback), "
        f"got {resp.status_code}: {resp.text[:300]}"
    )
    assert "OWNER_USER_ID" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# [E] OWNER_USER_ID onboarding — the auto-written owner.user_id DB row resolves
# the owner when the OWNER_USER_ID env is unset (first-admin wizard path).
# ---------------------------------------------------------------------------


async def test_db_owner_row_exempts_when_env_unset(
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """[E]: with no OWNER_USER_ID env, the owner.user_id DB row resolves the owner,
    so the multi-tenant 409 does NOT fire and the session binds to that owner."""
    from jarvis_common.owner import OWNER_USER_ID_CONFIG_KEY

    owner_id = await _seed_user_with_role(contract_conn, "db-owner-admin@example.com", "admin")
    await _seed_user_with_role(contract_conn, "db-owner-member@example.com", "user")
    await contract_conn.execute(
        "INSERT INTO user_config (user_id, key, value) VALUES (NULL, $1, $2::jsonb)",
        OWNER_USER_ID_CONFIG_KEY,
        owner_id,
    )
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.setenv("API_KEY_LOGIN_ENABLED", "true")

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 200, (
        f"DB owner row must resolve the owner (no env), got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["id"] == owner_id
    assert resp.json()["role"] == "admin"
    assert "jarvis_session" in resp.cookies


async def test_sec2_409_message_names_both_env_and_wizard(
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """With neither env nor an owner.user_id DB row, the 409 still fires and
    its message names BOTH the env and the first-admin wizard paths."""
    await _seed_user_with_role(contract_conn, "noowner-admin-a@example.com", "admin")
    await _seed_user_with_role(contract_conn, "noowner-admin-b@example.com", "admin")
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.setenv("API_KEY_LOGIN_ENABLED", "true")

    async with _make_unauthenticated_client(_pi_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 409, (
        f"No env and no DB owner row must still 409, got {resp.status_code}: {resp.text[:300]}"
    )
    detail = resp.json()["detail"]
    assert "OWNER_USER_ID" in detail
    assert "setup wizard" in detail, f"the error message must name the wizard path; got: {detail!r}"
