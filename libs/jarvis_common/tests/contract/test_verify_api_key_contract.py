"""Shared verify_api_key contract suite (audit X-03).

THE authoritative home. Each test exercises the real ``verify_api_key``
implementation against a real DB-backed session/user, NOT a mock.
Replaces the ~177 per-router re-tests scattered across 50 paper_ingestion
test files (those collapse in Sub-wave 4.4 D1/D5; this suite is their
survivor citation).

Branch points under test (derived from auth.py:108-189, session_middleware.py:74-104):
  A. Exempt path → early return (no DB interaction)
  B. No key configured, no session → 401 (misconfigured server)
  C. Key configured, bad key, no session → 403
  D. Key configured, correct key, no session → 200 (passes)
  E. Valid session (real DB: non-expired, non-revoked) → 200 (passes)
  F. Revoked session (real DB: revoked_at IS NOT NULL) → 403
  G. Expired session (real DB: expires_at <= now() - SESSION_GRACE) → 403

Tests E/F/G are the DB-backed branches the existing mock tests (test_verify_api_key_session.py)
cannot exercise. They use the real SessionMiddleware + SharedConnPool against
a real per-test transaction that rolls back after each test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import Depends, FastAPI
from jarvis_common.auth import refresh_api_key_cache, verify_api_key
from jarvis_common.session_middleware import SessionMiddleware
from jarvis_common.settings import get_secrets_settings
from jarvis_common.testing import SharedConnPool, _seed_user

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_KEY = "contract-suite-key-do-not-use-in-prod"


def _make_probe_app(pool_factory) -> FastAPI:
    """Build a minimal FastAPI app with SessionMiddleware + verify_api_key.

    ``pool_factory`` is a callable returning the SharedConnPool (called once
    at app-build time). The app has one route ``GET /probe`` behind
    ``verify_api_key``; a 200 means the dependency passed.
    """
    app = FastAPI()
    app.add_middleware(SessionMiddleware)

    @app.get("/probe", dependencies=[Depends(verify_api_key)])
    async def _probe() -> dict:
        return {"ok": True}

    # wire the shared transactional pool so SessionMiddleware can query sessions
    app.state.db_pool = pool_factory()
    return app


@pytest.fixture()
def _key_env(monkeypatch):
    """Set JARVIS_API_KEY + flush the module-level key cache for the test."""
    monkeypatch.setenv("JARVIS_API_KEY", _TEST_KEY)
    get_secrets_settings.cache_clear()
    refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    refresh_api_key_cache()


@pytest.fixture()
def _no_key_env(monkeypatch):
    """Ensure JARVIS_API_KEY is absent for no-key-configured tests."""
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    get_secrets_settings.cache_clear()
    refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    refresh_api_key_cache()


# ---------------------------------------------------------------------------
# Branch A — exempt paths (no DB, no key)
# Uses a fresh app without any pool wired; exempt-path logic fires first.
# ---------------------------------------------------------------------------


async def test_exempt_health_path_bypasses_auth():
    """Health path is exempt — verify_api_key returns before checking key/session."""
    app = FastAPI()
    app.add_middleware(SessionMiddleware)

    @app.get("/health", dependencies=[Depends(verify_api_key)])
    async def _health_probe():
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200


async def test_exempt_auth_path_bypasses_auth():
    """/api/auth/* is exempt — no X-API-Key required."""
    app = FastAPI()
    app.add_middleware(SessionMiddleware)

    @app.get("/api/auth/request-link", dependencies=[Depends(verify_api_key)])
    async def _auth_probe():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/auth/request-link")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Branch B — no key configured, no session → 401
# ---------------------------------------------------------------------------


async def test_no_key_configured_no_session_returns_401(contract_conn, _no_key_env):
    """Server has no JARVIS_API_KEY set and no session → 401 (misconfigured)."""
    app = _make_probe_app(lambda: SharedConnPool(contract_conn))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/probe")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Branch C — key configured, bad key, no session → 403
# ---------------------------------------------------------------------------


async def test_bad_api_key_no_session_returns_403(contract_conn, _key_env):
    """Bad X-API-Key + no session → 403 (Invalid or missing API key)."""
    app = _make_probe_app(lambda: SharedConnPool(contract_conn))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/probe", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 403


async def test_missing_api_key_no_session_returns_403(contract_conn, _key_env):
    """No X-API-Key header + no session → 403 (key configured but absent)."""
    app = _make_probe_app(lambda: SharedConnPool(contract_conn))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/probe")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Branch D — key configured, correct key, no session → passes
# ---------------------------------------------------------------------------


async def test_correct_api_key_no_session_passes(contract_conn, _key_env):
    """Correct X-API-Key + no session → 200 (auth passes, route executes)."""
    app = _make_probe_app(lambda: SharedConnPool(contract_conn))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/probe", headers={"X-API-Key": _TEST_KEY})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Branch E — valid session (real DB) → passes
# SessionMiddleware queries sessions + users JOIN; sets request.state.user_id.
# verify_api_key sees user_id → returns early regardless of key header.
# ---------------------------------------------------------------------------


async def test_valid_session_cookie_passes_without_api_key(contract_conn, _key_env):
    """Real session row (non-expired, non-revoked) + no X-API-Key → 200.

    This is the DB-backed branch the mock tests cannot exercise.
    SessionMiddleware queries the real sessions table and sets user_id.
    """
    _user_id, cookie = await _seed_user(contract_conn, "valid-session@contract.test")
    app = _make_probe_app(lambda: SharedConnPool(contract_conn))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("jarvis_session", cookie)
        resp = await client.get("/probe")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Branch F — revoked session (real DB: revoked_at IS NOT NULL) → 403
# SessionMiddleware sees revoked_at set → leaves user_id unset.
# verify_api_key falls through to key check → 403 (key mismatch / absent).
# ---------------------------------------------------------------------------


async def test_revoked_session_cookie_returns_403(contract_conn, _key_env):
    """Revoked session (revoked_at set in DB) → user_id unset → 403.

    This is the DB-backed branch proving that session revocation is enforced
    at the middleware layer, not just at the verify_api_key layer.
    """
    user_id, cookie = await _seed_user(contract_conn, "revoked-session@contract.test")
    await contract_conn.execute(
        "UPDATE sessions SET revoked_at = NOW() WHERE id = $1::uuid", cookie
    )
    app = _make_probe_app(lambda: SharedConnPool(contract_conn))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("jarvis_session", cookie)
        resp = await client.get("/probe")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Branch G — expired session (real DB: expires_at <= now() - SESSION_GRACE) → 403
# SESSION_GRACE = timedelta(hours=24); must be > 24h in the past to be rejected.
# ---------------------------------------------------------------------------


async def test_expired_session_cookie_returns_403(contract_conn, _key_env):
    """Session expired > 24h ago (beyond SESSION_GRACE) → user_id unset → 403.

    SESSION_GRACE (24h) means a session expired by ≤24h still resolves for
    offline-sync. We push expires_at to 25h in the past to ensure rejection.
    """
    user_id, cookie = await _seed_user(contract_conn, "expired-session@contract.test")
    beyond_grace = datetime.now(UTC) - timedelta(hours=25)
    await contract_conn.execute(
        "UPDATE sessions SET expires_at = $1 WHERE id = $2::uuid",
        beyond_grace,
        cookie,
    )
    app = _make_probe_app(lambda: SharedConnPool(contract_conn))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("jarvis_session", cookie)
        resp = await client.get("/probe")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Branch G-grace — session within SESSION_GRACE still passes (offline tolerance)
# Expires 1h ago (within the 24h grace) → user_id IS set → 200.
# ---------------------------------------------------------------------------


async def test_session_within_grace_window_still_passes(contract_conn, _key_env):
    """Session expired 1h ago (within 24h SESSION_GRACE) → still accepted.

    This asserts the offline-tolerant grace is enforced correctly: only
    sessions past the full grace period are rejected.
    """
    user_id, cookie = await _seed_user(contract_conn, "grace-session@contract.test")
    within_grace = datetime.now(UTC) - timedelta(hours=1)
    await contract_conn.execute(
        "UPDATE sessions SET expires_at = $1 WHERE id = $2::uuid",
        within_grace,
        cookie,
    )
    app = _make_probe_app(lambda: SharedConnPool(contract_conn))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("jarvis_session", cookie)
        resp = await client.get("/probe")
    assert resp.status_code == 200
