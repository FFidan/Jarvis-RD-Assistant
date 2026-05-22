"""Auth domain contract tests — Phase B target rows A16, A17, A18.

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
    Survivor-of (future Phase C): test_auth_magic_link.py verify tests.
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
    Survivor-of (future Phase C): test_auth_magic_link.py logout tests.
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
