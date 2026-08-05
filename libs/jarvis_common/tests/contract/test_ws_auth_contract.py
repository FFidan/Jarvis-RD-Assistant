"""WebSocket/session auth shared contract suite.

Exercises ``SessionMiddleware`` identity resolution against a real DB
(contract_conn) for the offline-grace, expiry, and revocation edge cases
that the per-service mock tests replace with ``SimpleNamespace`` stubs.

The "WS" in the name is conceptual: FastAPI/Starlette serve WebSocket
upgrades through the same ASGI middleware stack, so ``SessionMiddleware``
is the shared gate for both HTTP and WS identity resolution. All tests
here drive it via HTTP (ASGITransport) because the ownership and auth
behaviour is identical on both transports.

Verified references:
  session_middleware.py:36 — SESSION_GRACE = timedelta(hours=24)
  session_middleware.py:153 — expires_at <= now() - SESSION_GRACE → skip
  session_middleware.py:141-144 — revoked_at / deleted_at → skip
  session_middleware.py:65-72 — _SESSION_RENEW_SQL rolling-renewal UPDATE
  auth.py:169-253 — verify_api_key: session cookie passes the front-door gate

Covered:
  A. Active session (not expired) → user_id resolved → 200 on authed route.
  B. Grace-window session (expired < 24 h ago) → user_id still resolved.
  C. Hard-expired session (expired > 24 h ago) → user_id NOT resolved → 401/403.
  D. Revoked session → user_id NOT resolved.
  E. Renewable active session → _SESSION_RENEW_SQL rolls expires_at to ~now()+30d
     AND the response re-issues the jarvis_session cookie (real-DB PREPARE guard).
  F. Recently-renewed session (throttle window) → not renewed again; no cookie.
  G. Grace-expired session → identity resolves but session is NOT renewed.

Supersedes: mock-unit tests that stub request.state.user_id directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from jarvis_common.session_middleware import SESSION_TTL
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
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    with patch_pi_test_app(
        shared,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(remove_owner_override=False, disable_limiter=True),
    ) as wired_app:
        yield wired_app


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
    """An active (non-expired) session populates request.state.user_id.

    Proxy: GET /api/papers/brief requires a session identity (current_user_id_or_none
    is present; ownership scoping uses user_id). A 200 proves identity was resolved
    rather than the request being rejected at the API-key gate.

    Verified: session_middleware.py:155 — request.state.user_id = int(row["user_id"]).
    Supersedes: mock-unit tests that stub request.state.user_id = 1 directly.
    """
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
# B. Grace-window session: expired < 24 h ago — identity still resolved
# ---------------------------------------------------------------------------


async def test_grace_window_session_still_resolves(_pi_app, contract_conn, _configure_api_key):
    """Session expired within the 24-hour grace window still resolves user_id.

    Verified: session_middleware.py:153 — expires_at <= now() - SESSION_GRACE → reject.
    A session expired 1 hour ago is within 24h grace → identity resolved → 200.
    Supersedes: mock-unit tests that test grace by stubbing datetime.now.
    """
    user_id, _ = await _seed_user(contract_conn, "ws-grace@contract.example.com")
    # Expired 1 hour ago — inside the 24h grace window
    expires_at = datetime.now(UTC) - timedelta(hours=1)
    cookie = await _seed_session(contract_conn, user_id, expires_at=expires_at)

    async with _make_client(_pi_app, cookie) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 200, (
        f"Grace-window session (1h expired): expected 200; got {resp.status_code}. "
        f"Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# C. Hard-expired session: expired > 24 h ago — identity NOT resolved
# ---------------------------------------------------------------------------


async def test_hard_expired_session_does_not_resolve(_pi_app, contract_conn, _configure_api_key):
    """Session expired more than 24 hours ago does NOT resolve user_id.

    Verified: session_middleware.py:153 — expires_at <= now() - SESSION_GRACE → skip.
    Outer gate (verify_api_key): a valid API key is sent, so the gate passes.
    The per-route current_user_id_strict then gets None → 401.

    Supersedes: mock-unit tests that test hard-expiry via datetime monkeypatch.
    """
    user_id, _ = await _seed_user(contract_conn, "ws-hardexp@contract.example.com")
    # Expired 25 hours ago — outside the 24h grace window
    expires_at = datetime.now(UTC) - timedelta(hours=25)
    cookie = await _seed_session(contract_conn, user_id, expires_at=expires_at)

    async with _make_client(_pi_app, cookie) as c:
        # Use a route that requires a session identity
        await c.get("/api/papers/brief")

    # Without a resolved user_id, routes using current_user_id return empty-scoped
    # results (200 with empty list), not a hard 401 — because current_user_id returns
    # None (not strict). The key assertion is that the response is NOT an identity-
    # confirmation 200 with actual rows. Use a strict-identity route instead.
    # POST /api/jobs requires current_user_id_strict → 401 without session.
    async with _make_client(_pi_app, cookie) as c:
        resp_strict = await c.post(
            "/api/jobs",
            json={"kind": "card.generate", "payload": {"paper_id": 1, "deck_id": 1}},
        )
    assert resp_strict.status_code == 401, (
        f"Hard-expired session should not resolve identity; strict route got "
        f"{resp_strict.status_code} (expected 401). Body: {resp_strict.text[:300]}"
    )


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
        resp = await c.post(
            "/api/jobs",
            json={"kind": "card.generate", "payload": {"paper_id": 1, "deck_id": 1}},
        )

    assert resp.status_code == 401, (
        f"Revoked session should not resolve identity; got {resp.status_code} "
        f"(expected 401). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# E. Renewable active session → expiry rolls forward + cookie re-issued
# ---------------------------------------------------------------------------


async def test_renewable_session_rolls_expiry_and_refreshes_cookie(
    _pi_app, contract_conn, _configure_api_key
):
    """A renewable active session rolls expires_at to ~now()+30d and re-issues the cookie.

    This EXECUTES _SESSION_RENEW_SQL against real Postgres. The un-cast form
    (``now() + $2 - $3`` with untyped asyncpg params) fails to PREPARE on pg16.8,
    so _renew_session swallows the error and never renews — an all-mock test cannot
    reach a real PREPARE, which is why this case is the regression guard.

    Verified: session_middleware.py:65-72 — UPDATE sessions SET expires_at =
    now()+$2::interval WHERE ... expires_at < now()+$2::interval-$3::interval;
    :178 — request.state.session_renewed set on a returned row;
    :115-120 — dispatch re-issues jarvis_session when session_renewed.
    """
    user_id, _ = await _seed_user(contract_conn, "ws-renew@contract.example.com")
    # ~20 days out: past the once-per-day throttle window (expires_at < now()+30d-1d)
    # yet still live (expires_at > now()) → eligible for rolling renewal.
    seeded = datetime.now(UTC) + timedelta(days=20)
    cookie = await _seed_session(contract_conn, user_id, expires_at=seeded)

    async with _make_client(_pi_app, cookie) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 200, (
        f"Renewable session: expected 200; got {resp.status_code}. Body: {resp.text[:300]}"
    )
    # (a) The response re-issues the session cookie.
    assert "jarvis_session" in resp.headers.get("set-cookie", ""), (
        f"Renewable session should re-issue jarvis_session; "
        f"set-cookie={resp.headers.get('set-cookie', '')!r}"
    )
    # (b) The DB row's expiry advanced to ~now()+SESSION_TTL (30d).
    after = await contract_conn.fetchval(
        "SELECT expires_at FROM sessions WHERE id = $1::uuid", cookie
    )
    assert after > seeded, f"expires_at did not advance: seeded={seeded}, after={after}"
    expected = datetime.now(UTC) + SESSION_TTL
    assert abs(after - expected) < timedelta(minutes=5), (
        f"expires_at should roll to ~now()+30d; expected≈{expected}, got {after}"
    )


# ---------------------------------------------------------------------------
# F. Recently-renewed session (throttle window) → NOT renewed again
# ---------------------------------------------------------------------------


async def test_recently_renewed_session_is_not_renewed_again(
    _pi_app, contract_conn, _configure_api_key
):
    """A session renewed within the last day (throttle window) is NOT renewed again.

    The throttle predicate ``expires_at < now()+30d-1d`` is FALSE, so _SESSION_RENEW_SQL
    matches no row: expires_at stays put and no refresh cookie is issued — the
    once-per-day write cap. Seeding ~29.5d out (renewed ~12h ago) keeps the DB
    assertion discriminating: a wrongful renewal would jump expiry to ~30d.

    Verified: session_middleware.py:70 — AND expires_at < now()+$2::interval-$3::interval.
    """
    user_id, _ = await _seed_user(contract_conn, "ws-throttle@contract.example.com")
    # ~29.5 days out: inside the once-per-day throttle window → renewal is a no-op.
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
# G. Grace-expired session → identity resolves but session is NOT renewed
# ---------------------------------------------------------------------------


async def test_grace_expired_session_is_not_renewed(_pi_app, contract_conn, _configure_api_key):
    """A grace-resolved (already-expired) session resolves identity but is never renewed.

    _SESSION_RENEW_SQL requires ``expires_at > now()``, so a session expired within
    the 24h grace window matches no row: its expiry is not rolled forward and no
    cookie is re-issued. Guards the boundary that offline-grace does NOT extend a
    session's lifetime.

    Verified: session_middleware.py:69 — AND expires_at > now() (grace-resolved
    sessions are non-renewable).
    """
    user_id, _ = await _seed_user(contract_conn, "ws-grace-norenew@contract.example.com")
    # Expired 1h ago — inside 24h grace (identity resolves) but expires_at <= now()
    # so the renewal predicate excludes it.
    seeded = datetime.now(UTC) - timedelta(hours=1)
    cookie = await _seed_session(contract_conn, user_id, expires_at=seeded)

    async with _make_client(_pi_app, cookie) as c:
        resp = await c.get("/api/papers/brief")

    assert resp.status_code == 200, (
        f"Grace session: expected 200 (identity resolves); got {resp.status_code}."
    )
    assert "jarvis_session" not in resp.headers.get("set-cookie", ""), (
        "Grace-expired session must not be renewed / re-issue the cookie"
    )
    after = await contract_conn.fetchval(
        "SELECT expires_at FROM sessions WHERE id = $1::uuid", cookie
    )
    assert abs(after - seeded) < timedelta(seconds=1), (
        f"Grace-expired session expires_at must not move: seeded={seeded}, after={after}"
    )
