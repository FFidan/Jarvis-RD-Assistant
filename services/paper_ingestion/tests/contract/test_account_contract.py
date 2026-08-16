"""Account domain contract tests — target rows A1, A2.

Survivor-of: test_account.py mock-unit assertions for get_account, update_account.
Carve-out: app.state.http_client is MagicMock (outbound HTTP);
    send_magic_link (SMTP) mocked — outbound email boundary.
"""

from __future__ import annotations

import pytest
import httpx

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# A1: GET /api/account — own profile fields returned, no other user's data
# ---------------------------------------------------------------------------


async def test_a1_get_account_without_browser_identity_returns_401(
    _platform_app_with_pool,
    _configure_api_key,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_platform_app_with_pool),
        base_url="http://localhost",
    ) as client:
        response = await client.get("/api/account")

    assert response.status_code == 401


async def test_a1_anonymous_patch_stays_globally_blocked_without_mutation(
    contract_two_users,
    contract_conn,
    _platform_app_with_pool,
    _configure_api_key,
):
    before = await contract_conn.fetchval(
        "SELECT display_name FROM users WHERE id = $1",
        contract_two_users.user_a_id,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_platform_app_with_pool),
        base_url="http://localhost",
    ) as client:
        response = await client.patch(
            "/api/account",
            json={"display_name": "anonymous-mutation-must-not-land"},
        )
    after = await contract_conn.fetchval(
        "SELECT display_name FROM users WHERE id = $1",
        contract_two_users.user_a_id,
    )

    assert response.status_code == 403
    assert after == before


async def test_a1_get_account_returns_own_profile(
    contract_two_users,
    _platform_app_with_pool,
    _configure_api_key,
):
    """Covers map row A1: GET /api/account returns caller's own AccountResponse.

    Verified: account.py:89-102 get_account at HEAD d21aaea8.
    Survivor-of: test_account.py mock-unit tests for get_account.
    """
    async with _make_client(_platform_app_with_pool, contract_two_users.cookie_a) as c:
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
    _platform_app_with_pool,
    _configure_api_key,
):
    """Covers map row A1: user B gets their own profile, not user A's data.

    Verified: account.py:95 current_user_id_strict(request) — strictly scoped.
    """
    async with _make_client(_platform_app_with_pool, contract_two_users.cookie_b) as c:
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
    _platform_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A2: PATCH /api/account updates display_name in DB.

    Verified: account.py:107-134 update_account display_name path at HEAD d21aaea8.
    Survivor-of: test_account.py mock-unit tests for update_account.
    """
    new_name = "Contract Test Name"
    async with _make_client(_platform_app_with_pool, contract_two_users.cookie_a) as c:
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
    _platform_app_with_pool,
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

    async with _make_client(_platform_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.patch("/api/account", json={"email": user_b_email})

    assert resp.status_code == 409, (
        f"Expected 409 for duplicate email, got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# E1.PI extensions — magic-link token race conditions
#
# The verify endpoint lives in routers/auth.py; we reuse _platform_app_with_pool
# (it wires db_pool + removes the autouse auth stub) and hit /api/auth/verify,
# which verify_api_key exempts by path prefix before any key comparison.
#
# Verified: auth.py:184-295 (verify — used_at guard + expires_at guard + pending_email guard)
# ---------------------------------------------------------------------------


async def test_e1_magic_link_token_consumed_twice_second_fails(
    contract_conn,
    _platform_app_with_pool,
    _configure_api_key,
):
    """POST /api/auth/verify: consuming the same valid token twice — second attempt returns 400.

    The first verify marks used_at = NOW(); the second call hits the
    `token_row["used_at"] is not None` guard and must return 400.
    Verified: auth.py:219-220 (used_at guard → HTTPException 400).
    Survivor-of: test_auth_magic_link.py token-consumed-twice mock tests.
    """
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    # Seed a real user + valid magic-link token
    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('ml-race-1@contract.example.com', 'user') RETURNING id"
    )
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    await contract_conn.execute(
        "INSERT INTO magic_link_tokens (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        token_hash,
        user_id,
        expires_at,
    )

    # First consume — must succeed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_platform_app_with_pool),
        base_url="http://localhost",
    ) as c:
        resp1 = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp1.status_code == 200, (
        f"First verify must succeed; got {resp1.status_code}: {resp1.text[:200]}"
    )

    # Second consume — same token, now used_at is set → must return 400
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_platform_app_with_pool),
        base_url="http://localhost",
    ) as c:
        resp2 = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp2.status_code == 400, (
        f"Second verify of already-used token must return 400; got {resp2.status_code}: {resp2.text[:200]}"
    )


async def test_e1_magic_link_expired_token_returns_400(
    contract_conn,
    _platform_app_with_pool,
    _configure_api_key,
):
    """POST /api/auth/verify: expired token (expires_at in the past) returns 400.

    Verified: auth.py:221-222 (expires_at <= now guard → HTTPException 400).
    Survivor-of: test_auth_magic_link.py expired-token mock tests.
    """
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('ml-expired@contract.example.com', 'user') RETURNING id"
    )
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    # Set expires_at to 1 second in the past
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    await contract_conn.execute(
        "INSERT INTO magic_link_tokens (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        token_hash,
        user_id,
        expired_at,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_platform_app_with_pool),
        base_url="http://localhost",
    ) as c:
        resp = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp.status_code == 400, (
        f"Expired token must return 400; got {resp.status_code}: {resp.text[:200]}"
    )


async def test_e1_magic_link_invalid_token_returns_400(
    _platform_app_with_pool,
    _configure_api_key,
):
    """POST /api/auth/verify: completely unknown token returns 400 (no row found).

    Verified: auth.py:217-218 (token_row is None guard → HTTPException 400).
    Survivor-of: test_auth_magic_link.py invalid-token mock tests.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_platform_app_with_pool),
        base_url="http://localhost",
    ) as c:
        resp = await c.post(
            "/api/auth/verify", json={"token": "nonexistent-token-xyz-1234567890ab"}
        )

    assert resp.status_code == 400, (
        f"Unknown token must return 400; got {resp.status_code}: {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# A2-email: PATCH /api/account with new email — email_verification_sent=True
# ---------------------------------------------------------------------------


async def test_a2_patch_account_email_verification_sent_true_when_smtp_succeeds(
    contract_two_users,
    contract_conn,
    _platform_app_with_pool,
    _configure_api_key,
    monkeypatch,
):
    """PATCH /api/account with a fresh email → email_verification_sent=True in response.

    send_magic_link is patched to report DELIVERED, exercising the success
    branch of the update_account email-change path.
    Carve-out: send_magic_link (SMTP) mocked — outbound email boundary.
    """
    from unittest.mock import AsyncMock, patch

    from jarvis_common.email import MagicLinkDelivery

    new_email = "smtp-success-contract@contract.example.com"

    with patch(
        "platform_api.routers.account.send_magic_link",
        new=AsyncMock(return_value=MagicLinkDelivery.DELIVERED),
    ):
        async with _make_client(_platform_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.patch("/api/account", json={"email": new_email})

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body.get("email_verification_sent") is True, (
        f"email_verification_sent must be True when SMTP succeeds; body={body}"
    )


async def test_confirm_email_change_soft_deleted_clash_returns_409(
    contract_two_users,
    contract_conn,
    _platform_app_with_pool,
    _configure_api_key,
):
    """POST /api/account/confirm-email: target email belongs to a soft-deleted user → 409, not 500.

    users_email_key UNIQUE (email) is unconditional (db/init.sql:1626), so the
    UPDATE raises asyncpg.UniqueViolationError when the email is held by a
    deleted_at-set row. The handler must translate that to 409.
    Verified: account.py:266 (unconditional UPDATE), admin.py:155 (mirror pattern).
    """
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    # A soft-deleted user already owns the target email (row still occupies the
    # unique index because the constraint ignores deleted_at).
    taken_email = "confirm-clash-deleted@contract.example.com"
    await contract_conn.execute(
        "INSERT INTO users (email, role, deleted_at) VALUES ($1, 'user', NOW())",
        taken_email,
    )

    # Issue a confirm-email-change token for user A pointing at that email.
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    await contract_conn.execute(
        "INSERT INTO magic_link_tokens (token_hash, user_id, expires_at, pending_email)"
        " VALUES ($1, $2, $3, $4)",
        token_hash,
        contract_two_users.user_a_id,
        expires_at,
        taken_email,
    )

    from jarvis_common.testing_contract_apps import make_contract_client as _make_client

    async with _make_client(_platform_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/account/confirm-email", json={"token": raw_token})

    assert resp.status_code == 409, (
        f"Soft-deleted-email clash must return 409, not 500; got {resp.status_code}: {resp.text[:300]}"
    )

    # The failure audit must still fire (the except branch logs it).
    audit = await contract_conn.fetchrow(
        "SELECT 1 FROM audit_log WHERE action = 'account.email.change.failure' AND resource = $1",
        f"users/{contract_two_users.user_a_id}",
    )
    assert audit is not None, "confirm-email failure must be audited even on the constraint path"
