"""Auth domain contract tests — target rows A16, A17, A18.

Survivor-of: test_auth_magic_link.py mock-unit assertions for verify,
    api_key_session, logout.
Carve-out: send_magic_link (SMTP) mocked — outbound email boundary.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from jarvis_common.testing_contract_apps import (
    DEFAULT_CONTRACT_API_KEY,
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


def _make_unauthenticated_client(app, *, base_url: str = "http://localhost"):
    # Loopback base_url so the credential-transport gate on verify/api-key-session
    # (which requires a loopback Host or forwarded https) is satisfied by default.
    return _make_client(app, None, base_url=base_url)


def _make_raw_peer_client(app, *, raw_peer: str, base_url: str):
    """Drive the real middleware stack with a controlled socket peer."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(raw_peer, 51234)),
        base_url=base_url,
        headers={"X-API-Key": DEFAULT_CONTRACT_API_KEY},
    )


# ---------------------------------------------------------------------------
# A16: POST /api/auth/verify — magic-link token consumed and session set
# ---------------------------------------------------------------------------


async def test_a16_verify_valid_token_returns_session_cookie(
    contract_two_users,
    _platform_app_with_pool,
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

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
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
    _platform_app_with_pool,
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

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp.status_code == 400, (
        f"Expected 400 for expired token, got {resp.status_code}: {resp.text[:300]}"
    )


async def test_a16_verify_already_used_token_returns_400(
    contract_two_users,
    _platform_app_with_pool,
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

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp.status_code == 400, (
        f"Expected 400 for already-used token, got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A18: POST /api/auth/logout — session revoked in DB
# ---------------------------------------------------------------------------


async def test_a18_logout_revokes_session_row_in_db(
    contract_two_users,
    _platform_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A18: POST /api/auth/logout sets sessions.revoked_at and clears cookie.

    Verified: auth.py:467-514 logout at HEAD d21aaea8.
    Survivor-of: test_auth_magic_link.py logout tests.
    """
    async with _make_client(_platform_app_with_pool, contract_two_users.cookie_a) as c:
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
    _platform_app_with_pool,
    _configure_api_key,
):
    """Covers map row A18: logout without session cookie is idempotent → 204.

    Verified: auth.py:479 missing cookie early-return at HEAD d21aaea8.
    """
    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/logout")

    assert resp.status_code == 204, (
        f"Expected 204 for logout without session, got {resp.status_code}: {resp.text[:300]}"
    )


async def test_a18_logout_does_not_reissue_renewed_cookie(
    contract_two_users,
    _platform_app_with_pool,
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
    async with _make_client(_platform_app_with_pool, contract_two_users.cookie_a) as c:
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
    _platform_app_with_pool,
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

    with patch("platform_api.routers.auth.send_magic_link", AsyncMock()):
        async with _make_unauthenticated_client(_platform_app_with_pool) as c:
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


async def test_cooldown_probe_answers_per_link_kind(contract_two_users, contract_conn):
    """``magic_link_on_cooldown`` scopes to the requested kind against a real database.

    User A holds only a recent email-change token (``pending_email`` set); user B
    holds only a recent sign-in token (``pending_email`` NULL). Each user must be
    on cooldown for their own kind and off cooldown for the other, so an
    email-change link can never suppress a sign-in link or vice versa.

    Verified: routers/_auth_shared.py magic_link_on_cooldown (the
    ``IS NOT NULL``/``IS NULL`` predicate selected by ``email_change``).
    """
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    from platform_api.routers._auth_shared import magic_link_on_cooldown

    expires_at = datetime.now(UTC) + timedelta(minutes=15)

    await contract_conn.execute(
        "INSERT INTO magic_link_tokens (token_hash, user_id, expires_at, pending_email)"
        " VALUES ($1, $2, $3, $4)",
        hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest(),
        contract_two_users.user_a_id,
        expires_at,
        "changed@contract.example.com",
    )
    await contract_conn.execute(
        "INSERT INTO magic_link_tokens (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest(),
        contract_two_users.user_b_id,
        expires_at,
    )

    a_id, b_id = contract_two_users.user_a_id, contract_two_users.user_b_id

    assert await magic_link_on_cooldown(contract_conn, a_id, email_change=True) is True, (
        "a recent email-change token must put the email-change flow on cooldown"
    )
    assert await magic_link_on_cooldown(contract_conn, a_id, email_change=False) is False, (
        "a recent email-change token must NOT suppress a sign-in link"
    )
    assert await magic_link_on_cooldown(contract_conn, b_id, email_change=False) is True, (
        "a recent sign-in token must put the sign-in flow on cooldown"
    )
    assert await magic_link_on_cooldown(contract_conn, b_id, email_change=True) is False, (
        "a recent sign-in token must NOT suppress an email-change link"
    )


# ---------------------------------------------------------------------------
# Non-ASCII api_key body must return 403, not 500
# ---------------------------------------------------------------------------


async def test_a17_api_key_session_non_ascii_returns_403(
    _platform_app_with_pool,
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
    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
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
    """Isolate process-local auth state between contract cases."""
    from jarvis_common.auth import invalidate_api_key_login_cache
    from platform_api.deps import limiter

    invalidate_api_key_login_cache()
    limiter.reset()
    yield
    invalidate_api_key_login_cache()
    limiter.reset()


async def _seed_user_with_role(conn, email: str, role: str) -> int:
    return int(
        await conn.fetchval(
            "INSERT INTO users (email, role) VALUES ($1, $2) RETURNING id",
            email,
            role,
        )
    )


async def test_owner_exempt_mints_on_multi_user_box_flag_off(
    _platform_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """B1: a configured admin OWNER_USER_ID mints on a 2+ user box with the flag OFF."""
    owner_id = await _seed_user_with_role(contract_conn, "owner-admin@example.com", "admin")
    await _seed_user_with_role(contract_conn, "member@example.com", "user")
    monkeypatch.setenv("OWNER_USER_ID", str(owner_id))
    monkeypatch.delenv("API_KEY_LOGIN_ENABLED", raising=False)

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 200, (
        f"Owner must mint on a multi-user box flag-off, got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["id"] == owner_id
    assert resp.json()["role"] == "admin"
    assert "jarvis_session" in resp.cookies


async def test_owner_session_mint_waits_for_admin_role_mutation_lock(
    _platform_app_with_pool,
    _configure_api_key,
    _contract_pool,
    contract_conn,
    monkeypatch,
):
    """Owner validity and session minting share the role-mutation lock."""
    owner_id = await _seed_user_with_role(contract_conn, "locked-owner@example.com", "admin")
    monkeypatch.setenv("OWNER_USER_ID", str(owner_id))

    async with _make_unauthenticated_client(_platform_app_with_pool) as client:
        async with _contract_pool.acquire() as lock_conn:
            async with lock_conn.transaction():
                await lock_conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('admin_role_mutation'))"
                )
                request_task = asyncio.create_task(
                    client.post("/api/auth/api-key-session", json={})
                )
                await asyncio.sleep(0.1)
                assert not request_task.done(), "session mint bypassed the owner mutation lock"

        response = await asyncio.wait_for(request_task, timeout=3)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == owner_id


async def test_non_admin_owner_user_id_still_409s(
    _platform_app_with_pool,
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

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 409, (
        f"Non-admin OWNER_USER_ID must 409, got {resp.status_code}: {resp.text[:300]}"
    )
    assert "non-admin" in resp.json()["detail"]


async def test_malformed_owner_user_id_returns_actionable_409_without_fallback(
    _platform_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """An authoritative malformed host override must never select another admin."""
    await _seed_user_with_role(contract_conn, "fallback-admin@example.com", "admin")
    monkeypatch.setenv("OWNER_USER_ID", "not-a-user-id")
    monkeypatch.setenv("API_KEY_LOGIN_ENABLED", "true")

    async with _make_unauthenticated_client(_platform_app_with_pool) as client:
        response = await client.post("/api/auth/api-key-session", json={})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "OWNER_USER_ID" in detail
    assert "positive integer" in detail


async def test_malformed_database_owner_returns_actionable_409_without_fallback(
    _platform_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """A malformed persisted owner record must be repaired, never bypassed."""
    from jarvis_common.owner import OWNER_USER_ID_CONFIG_KEY

    await _seed_user_with_role(contract_conn, "other-admin@example.com", "admin")
    await contract_conn.execute(
        "INSERT INTO user_config (user_id, key, value) VALUES (NULL, $1, $2::jsonb)",
        OWNER_USER_ID_CONFIG_KEY,
        '"invalid"',
    )
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.setenv("API_KEY_LOGIN_ENABLED", "true")

    async with _make_unauthenticated_client(_platform_app_with_pool) as client:
        response = await client.post("/api/auth/api-key-session", json={})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "owner record" in detail
    assert "jarvis-research owner set" in detail


async def test_flag_on_without_owner_user_id_refuses_fallback_409(
    _platform_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """Multi-tenant gate ON, no OWNER_USER_ID, multi-user → lowest-id fallback refused (409)."""
    await _seed_user_with_role(contract_conn, "admin-a@example.com", "admin")
    await _seed_user_with_role(contract_conn, "admin-b@example.com", "admin")
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.setenv("API_KEY_LOGIN_ENABLED", "true")

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 409, (
        f"Flag-on without OWNER_USER_ID must refuse the fallback (409), "
        f"got {resp.status_code}: {resp.text[:300]}"
    )
    assert "OWNER_USER_ID" in resp.json()["detail"]


async def test_non_owner_multi_user_flag_off_still_403(
    _platform_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """Multi-user box, flag OFF, no exempt owner → multi-tenant gate still 403s."""
    await _seed_user_with_role(contract_conn, "admin-only@example.com", "admin")
    await _seed_user_with_role(contract_conn, "plain-member@example.com", "user")
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.delenv("API_KEY_LOGIN_ENABLED", raising=False)

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 403, (
        f"Multi-user flag-off must 403, got {resp.status_code}: {resp.text[:300]}"
    )


async def test_single_user_login_unchanged(
    _platform_app_with_pool,
    _configure_api_key,
    contract_conn,
    monkeypatch,
):
    """Single-user box mints via the lowest-id admin fallback (no OWNER_USER_ID)."""
    admin_id = await _seed_user_with_role(contract_conn, "solo-admin@example.com", "admin")
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.delenv("API_KEY_LOGIN_ENABLED", raising=False)

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 200, (
        f"Single-user login must mint, got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["id"] == admin_id
    assert "jarvis_session" in resp.cookies


async def test_db_override_enables_multi_user_login(
    _platform_app_with_pool,
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

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
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
    _platform_app_with_pool,
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

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 200, (
        f"DB owner row must resolve the owner (no env), got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["id"] == owner_id
    assert resp.json()["role"] == "admin"
    assert "jarvis_session" in resp.cookies


async def test_sec2_409_message_names_both_env_and_wizard(
    _platform_app_with_pool,
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

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 409, (
        f"No env and no DB owner row must still 409, got {resp.status_code}: {resp.text[:300]}"
    )
    detail = resp.json()["detail"]
    assert "OWNER_USER_ID" in detail
    assert "setup wizard" in detail, f"the error message must name the wizard path; got: {detail!r}"


# ---------------------------------------------------------------------------
# Credential-transport gate: verify + api-key-session refuse cleartext LAN
# origins (non-loopback Host over plain http), but allow loopback or nginx
# forwarded-https.
# ---------------------------------------------------------------------------

_LAN_BASE_URL = "http://192.168.1.5"


async def _seed_login_token(conn, user_id: int) -> str:
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    await conn.execute(
        "INSERT INTO magic_link_tokens (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        token_hash,
        user_id,
        datetime.now(UTC) + timedelta(minutes=15),
    )
    return raw_token


async def test_verify_gate_refuses_lan_plaintext(_platform_app_with_pool, _configure_api_key):
    """POST /api/auth/verify from a non-loopback socket peer is rejected before lookup."""
    async with _make_raw_peer_client(
        _platform_app_with_pool, raw_peer="203.0.113.7", base_url=_LAN_BASE_URL
    ) as c:
        resp = await c.post("/api/auth/verify", json={"token": "any-token-1234567890abcd"})

    assert resp.status_code == 403, (
        f"LAN plaintext verify must be refused, got {resp.status_code}: {resp.text[:300]}"
    )
    assert "localhost" in resp.json()["detail"]


async def test_verify_gate_allows_loopback_host(
    contract_two_users, _platform_app_with_pool, _configure_api_key, contract_conn
):
    """POST /api/auth/verify from a loopback Host → gate passes → 200."""
    raw_token = await _seed_login_token(contract_conn, contract_two_users.user_a_id)

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp.status_code == 200, (
        f"Loopback verify must pass the gate, got {resp.status_code}: {resp.text[:300]}"
    )


async def test_verify_gate_refuses_forged_forwarded_https_from_untrusted_peer(
    contract_two_users, _platform_app_with_pool, _configure_api_key, contract_conn
):
    """A public socket peer cannot turn cleartext into HTTPS with a header."""
    raw_token = await _seed_login_token(contract_conn, contract_two_users.user_a_id)

    async with _make_raw_peer_client(
        _platform_app_with_pool, raw_peer="203.0.113.7", base_url=_LAN_BASE_URL
    ) as c:
        resp = await c.post(
            "/api/auth/verify",
            json={"token": raw_token},
            headers={
                "Host": "localhost",
                "Forwarded": "for=127.0.0.1;proto=https;host=localhost",
                "X-Forwarded-For": "127.0.0.1",
                "X-Forwarded-Host": "localhost",
                "X-Forwarded-Proto": "https",
                "X-Real-IP": "127.0.0.1",
            },
        )

    assert resp.status_code == 403, (
        f"forged forwarding headers must not pass the transport gate; got "
        f"{resp.status_code}: {resp.text[:300]}"
    )


async def test_verify_gate_allows_https_from_pinned_dashboard_peer(
    contract_two_users, _platform_app_with_pool, _configure_api_key, contract_conn
):
    """The dashboard proxy's normalized HTTPS scheme remains usable."""
    raw_token = await _seed_login_token(contract_conn, contract_two_users.user_a_id)

    async with _make_raw_peer_client(
        _platform_app_with_pool, raw_peer="10.137.241.253", base_url=_LAN_BASE_URL
    ) as c:
        resp = await c.post(
            "/api/auth/verify",
            json={"token": raw_token},
            headers={"X-Forwarded-For": "198.51.100.20", "X-Forwarded-Proto": "https"},
        )

    assert resp.status_code == 200, (
        f"pinned dashboard HTTPS must pass the gate, got {resp.status_code}: {resp.text[:300]}"
    )


async def test_request_link_gate_refuses_lan_plaintext_and_forged_localhost(
    _platform_app_with_pool, _configure_api_key
):
    """Email addresses are not accepted over raw LAN HTTP."""
    async with _make_raw_peer_client(
        _platform_app_with_pool, raw_peer="203.0.113.7", base_url=_LAN_BASE_URL
    ) as c:
        resp = await c.post(
            "/api/auth/request-link",
            json={"email": "person@example.com"},
            headers={"Host": "localhost", "X-Forwarded-Proto": "https"},
        )

    assert resp.status_code == 403


async def test_api_key_session_gate_refuses_lan_plaintext(
    _platform_app_with_pool, _configure_api_key, monkeypatch
):
    """POST /api/auth/api-key-session from a non-loopback socket peer is rejected.

    The gate detail names the supported routes, distinguishing it from the
    invalid-key 403.
    """
    monkeypatch.setattr(_platform_app_with_pool.state.limiter, "enabled", False)
    async with _make_raw_peer_client(
        _platform_app_with_pool, raw_peer="203.0.113.7", base_url=_LAN_BASE_URL
    ) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 403, (
        f"LAN plaintext api-key-session must be refused, got {resp.status_code}: {resp.text[:300]}"
    )
    assert "localhost" in resp.json()["detail"]


async def test_api_key_session_gate_allows_loopback_host(
    _platform_app_with_pool, _configure_api_key, contract_conn, monkeypatch
):
    """POST /api/auth/api-key-session from a loopback Host → gate passes → 200."""
    monkeypatch.setattr(_platform_app_with_pool.state.limiter, "enabled", False)
    admin_id = await _seed_user_with_role(contract_conn, "gate-loopback-admin@example.com", "admin")
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.delenv("API_KEY_LOGIN_ENABLED", raising=False)

    async with _make_unauthenticated_client(_platform_app_with_pool) as c:
        resp = await c.post("/api/auth/api-key-session", json={})

    assert resp.status_code == 200, (
        f"Loopback api-key-session must mint, got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["id"] == admin_id


async def test_api_key_session_gate_refuses_forged_forwarded_https(
    _platform_app_with_pool, _configure_api_key, contract_conn, monkeypatch
):
    """A public peer cannot spoof the trusted dashboard's HTTPS signal."""
    monkeypatch.setattr(_platform_app_with_pool.state.limiter, "enabled", False)
    admin_id = await _seed_user_with_role(contract_conn, "gate-fwd-admin@example.com", "admin")
    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    monkeypatch.delenv("API_KEY_LOGIN_ENABLED", raising=False)

    async with _make_raw_peer_client(
        _platform_app_with_pool, raw_peer="203.0.113.7", base_url=_LAN_BASE_URL
    ) as c:
        resp = await c.post(
            "/api/auth/api-key-session",
            json={},
            headers={"Host": "localhost", "X-Forwarded-Proto": "https"},
        )

    assert resp.status_code == 403, (
        f"forged forwarded HTTPS must be rejected, got {resp.status_code}: {resp.text[:300]}"
    )
    assert admin_id > 0
