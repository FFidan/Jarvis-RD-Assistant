"""Signed downstream identity contracts backed by Platform session state.

The test gateway resolves the browser cookie with Platform authority and sends
one signed assertion to Research. Research neither queries nor renews Platform
sessions. Active sessions authenticate; expired or revoked sessions do not; no
downstream response reissues the Platform cookie or changes its expiry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from jarvis_common.testing import SharedConnPool
from jarvis_common.testing_contract_apps import (
    PITestAppOptions,
    patch_pi_test_app,
)
from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)
from jarvis_common.testing_db import _seed_user

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Fixture: minimal PI app wired to contract_conn
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app(contract_conn):
    """paper_ingestion app with db_pool wired to the contract connection."""
    from jarvis_common.testing_auth import SignedIdentityMiddleware
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn, session_authorization="jarvis_research_runtime")
    with patch_pi_test_app(
        shared,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(remove_identity_overrides=False, disable_limiter=True),
    ) as wired_app:
        yield SignedIdentityMiddleware(
            wired_app,
            audience="research",
            session_pool=shared.with_session_authorization("jarvis_platform_runtime"),
        )


# ---------------------------------------------------------------------------
# Helper: insert session with a custom expires_at / revoked_at
# ---------------------------------------------------------------------------


async def _seed_session(conn, user_id: int, *, expires_at: datetime, revoked_at=None) -> str:
    """Insert a session row; return its id (UUID string = cookie value).

    Verified: session_middleware.py:44-56 — SELECT s.user_id, s.expires_at,
    s.revoked_at, u.email, u.role, u.deleted_at FROM sessions WHERE s.id = $1::uuid.
    """
    session_id = await conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at, revoked_at)
           VALUES ($1, $2, $3)
           RETURNING id""",
        user_id,
        expires_at,
        revoked_at,
    )
    return str(session_id)


# ---------------------------------------------------------------------------
# A. Active session resolves identity → authed route succeeds
# ---------------------------------------------------------------------------


async def test_active_session_resolves_user_id(_pi_app, contract_conn, _configure_api_key):
    """An active Platform session becomes signed Research identity."""
    user_id, _ = await _seed_user(contract_conn, "ws-active@contract.example.com")
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    cookie = await _seed_session(contract_conn, user_id, expires_at=expires_at)

    async with _make_client(_pi_app, cookie) as c:
        resp = await c.get("/api/papers/brief")

    # 200 proves session resolved (not 401 "missing session" from current_user_id_strict)
    assert resp.status_code == 200, (
        f"Active session: expected 200; got {resp.status_code}. Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# B. Expired session is rejected at the gateway
# ---------------------------------------------------------------------------


async def test_expired_session_does_not_resolve(_pi_app, contract_conn, _configure_api_key):
    """The gateway refuses an expired Platform session."""
    user_id, _ = await _seed_user(contract_conn, "ws-grace@contract.example.com")
    expires_at = datetime.now(UTC) - timedelta(hours=1)
    cookie = await _seed_session(contract_conn, user_id, expires_at=expires_at)

    async with _make_client(_pi_app, cookie) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# C. Long-expired session is also rejected
# ---------------------------------------------------------------------------


async def test_hard_expired_session_does_not_resolve(_pi_app, contract_conn, _configure_api_key):
    """A long-expired Platform session does not become signed identity."""
    user_id, _ = await _seed_user(contract_conn, "ws-hardexp@contract.example.com")
    # Expired 25 hours ago — outside the 24h grace window
    expires_at = datetime.now(UTC) - timedelta(hours=25)
    cookie = await _seed_session(contract_conn, user_id, expires_at=expires_at)

    async with _make_client(_pi_app, cookie) as c:
        response = await c.get("/api/papers/brief")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# D. Revoked session → identity NOT resolved
# ---------------------------------------------------------------------------


async def test_revoked_session_does_not_resolve(_pi_app, contract_conn, _configure_api_key):
    """A revoked session (revoked_at IS NOT NULL) never resolves user_id.

    Verified: session_middleware.py:141-142 — if row["revoked_at"] is not None: return.
    Supersedes: mock-unit tests that assert the revoked branch.
    """
    user_id, _ = await _seed_user(contract_conn, "ws-revoked@contract.example.com")
    expires_at = datetime.now(UTC) + timedelta(hours=24)  # would be valid except for revocation
    revoked_at = datetime.now(UTC) - timedelta(minutes=5)
    cookie = await _seed_session(
        contract_conn, user_id, expires_at=expires_at, revoked_at=revoked_at
    )

    async with _make_client(_pi_app, cookie) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 401, (
        f"Revoked session should not resolve identity; got {resp.status_code} "
        f"(expected 401). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# E. Downstream requests cannot renew Platform sessions
# ---------------------------------------------------------------------------


async def test_downstream_request_does_not_renew_platform_session(
    _pi_app, contract_conn, _configure_api_key
):
    """Research authenticates but never renews the Platform session."""
    user_id, _ = await _seed_user(contract_conn, "ws-renew@contract.example.com")
    seeded = datetime.now(UTC) + timedelta(days=20)
    cookie = await _seed_session(contract_conn, user_id, expires_at=seeded)

    async with _make_client(_pi_app, cookie) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 200, (
        f"Renewable session: expected 200; got {resp.status_code}. Body: {resp.text[:300]}"
    )
    assert "jarvis_session" not in resp.headers.get("set-cookie", "")
    after = await contract_conn.fetchval(
        "SELECT expires_at FROM sessions WHERE id = $1::uuid", cookie
    )
    assert abs(after - seeded) < timedelta(seconds=1)


# ---------------------------------------------------------------------------
# F. A recently renewed Platform session also remains unchanged downstream
# ---------------------------------------------------------------------------


async def test_recently_renewed_session_is_not_renewed_again(
    _pi_app, contract_conn, _configure_api_key
):
    """A recent Platform renewal is not repeated by Research."""
    user_id, _ = await _seed_user(contract_conn, "ws-throttle@contract.example.com")
    seeded = datetime.now(UTC) + timedelta(days=29, hours=12)
    cookie = await _seed_session(contract_conn, user_id, expires_at=seeded)

    async with _make_client(_pi_app, cookie) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 200, (
        f"Throttled session: expected 200; got {resp.status_code}. Body: {resp.text[:300]}"
    )
    assert "jarvis_session" not in resp.headers.get("set-cookie", ""), (
        f"Throttled session must not re-issue the cookie; "
        f"set-cookie={resp.headers.get('set-cookie', '')!r}"
    )
    after = await contract_conn.fetchval(
        "SELECT expires_at FROM sessions WHERE id = $1::uuid", cookie
    )
    assert abs(after - seeded) < timedelta(seconds=1), (
        f"Throttled session expires_at must not move: seeded={seeded}, after={after}"
    )


# ---------------------------------------------------------------------------
# G. Rejected expired sessions remain unchanged
# ---------------------------------------------------------------------------


async def test_expired_session_is_not_renewed(_pi_app, contract_conn, _configure_api_key):
    """An expired session is rejected and remains unchanged."""
    user_id, _ = await _seed_user(contract_conn, "ws-grace-norenew@contract.example.com")
    seeded = datetime.now(UTC) - timedelta(hours=1)
    cookie = await _seed_session(contract_conn, user_id, expires_at=seeded)

    async with _make_client(_pi_app, cookie) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 401
    assert "jarvis_session" not in resp.headers.get("set-cookie", ""), (
        "Expired session must not be renewed or reissue the cookie"
    )
    after = await contract_conn.fetchval(
        "SELECT expires_at FROM sessions WHERE id = $1::uuid", cookie
    )
    assert abs(after - seeded) < timedelta(seconds=1), (
        f"Expired session expires_at must not move: seeded={seeded}, after={after}"
    )
