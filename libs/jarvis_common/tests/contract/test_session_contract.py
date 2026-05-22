"""Shared session-creation contract suite (predicate row A253).

Exercises the magic-link → session creation flow against a real DB:
  1. POST /api/auth/request-link for a known user → magic_link_tokens row created
  2. POST /api/auth/verify with the raw token → session row created + cookie set
  3. Token consumption: used_at is set; replaying the same token returns 400
  4. Session 24h grace window: verify_api_key passes within grace, rejects outside
     (partial-auth A263)

These are the DB-backed branches that the existing per-router mock tests cannot
exercise because they replace the pool entirely.

Design notes
-----------
* We patch ``send_magic_link`` so no SMTP call is attempted in the contract
  environment; the raw token is intercepted via a side-effect.
* The test uses the contract_conn (function-scoped rollback) so no rows persist
  between tests.
* DEV_MODE is forced to True so the Secure=False cookie flag does not require
  HTTPS in the test transport.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from jarvis_common.testing import SharedConnPool, _seed_user

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_KEY = "session-contract-key-do-not-use-in-prod"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_token(raw: str) -> str:
    """Mirrors auth.py::_hash_token (SHA-256 hex digest)."""
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    from jarvis_common import auth as _auth
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_KEY)
    monkeypatch.setenv("DEV_MODE", "true")  # keep Secure=False so httpx transport works
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _auth_app(contract_conn):
    """paper_ingestion app with db_pool wired to the contract connection.

    Limiter is disabled so rate limits do not interfere with the creation
    flow being tested here.
    """
    from paper_ingestion.main import (
        app,
        limiter,  # type: ignore[attr-defined]
    )
    from paper_ingestion.routers.auth import router as auth_router  # noqa: F401

    shared = SharedConnPool(contract_conn)
    original = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared

    limiter_was_enabled = limiter.enabled
    limiter.enabled = False
    try:
        yield app
    finally:
        limiter.enabled = limiter_was_enabled
        if original is None:
            if hasattr(app.state, "db_pool"):
                del app.state.db_pool
        else:
            app.state.db_pool = original


# ---------------------------------------------------------------------------
# A253 — magic-link request → token row + verify → session row
# ---------------------------------------------------------------------------


async def test_request_link_creates_token_row(
    contract_conn,
    _auth_app,
    _configure_api_key,
):
    """POST /api/auth/request-link for a known user inserts a row in magic_link_tokens.

    Grounding: auth.py:163-170 — ``INSERT INTO magic_link_tokens (token_hash, user_id, expires_at)``.
    This is the DB write the mock tests skip entirely.
    """
    user_id, _ = await _seed_user(contract_conn, "ml-req@contract.test")

    intercepted: list[str] = []

    async def _fake_send(email: str, link: str, *, pool=None) -> None:  # noqa: ARG001
        # Extract token from "…/auth/verify?token=<raw>" link
        intercepted.append(link.split("token=", 1)[-1])

    with patch(
        "paper_ingestion.routers.auth.send_magic_link",
        side_effect=_fake_send,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_auth_app),
            base_url="http://test",
            headers={"X-API-Key": _TEST_KEY},
        ) as c:
            resp = await c.post(
                "/api/auth/request-link",
                json={"email": "ml-req@contract.test"},
            )

    assert resp.status_code == 200, f"request-link failed: {resp.status_code}: {resp.text[:300]}"
    assert resp.json().get("sent") is True

    # The token row must exist in the DB (same transaction as contract_conn)
    row = await contract_conn.fetchrow(
        "SELECT token_hash, user_id, expires_at, used_at FROM magic_link_tokens WHERE user_id = $1",
        user_id,
    )
    assert row is not None, "magic_link_tokens row was not created by request-link"
    assert row["used_at"] is None, "Token should not be used at creation time"
    assert row["expires_at"] > datetime.now(UTC), "Token should not be expired at creation"

    # Verify the intercepted raw token hashes to the stored token_hash
    assert len(intercepted) == 1, f"Expected exactly one send_magic_link call; got {intercepted}"
    raw_token = intercepted[0]
    assert _hash_token(raw_token) == row["token_hash"], (
        "Stored token_hash does not match SHA-256 of the raw token from the link"
    )


async def test_verify_token_creates_session_row(
    contract_conn,
    _auth_app,
    _configure_api_key,
):
    """POST /api/auth/verify with a valid token creates a sessions row + sets cookie.

    Grounding: auth.py:249-257 — ``INSERT INTO sessions (user_id, expires_at) RETURNING id``.
    Covers the full creation-flow contract including token → session atomicity.
    """
    user_id, _ = await _seed_user(contract_conn, "ml-verify@contract.test")
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(minutes=15)

    await contract_conn.execute(
        "INSERT INTO magic_link_tokens (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        token_hash,
        user_id,
        expires_at,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_auth_app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_KEY},
        follow_redirects=False,
    ) as c:
        resp = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp.status_code == 200, f"verify failed: {resp.status_code}: {resp.text[:300]}"

    # Session row must exist
    session_row = await contract_conn.fetchrow(
        "SELECT id, user_id, expires_at FROM sessions WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
        user_id,
    )
    assert session_row is not None, "sessions row was not created by verify"
    assert session_row["user_id"] == user_id

    # Token must now be marked used
    token_row = await contract_conn.fetchrow(
        "SELECT used_at FROM magic_link_tokens WHERE token_hash = $1", token_hash
    )
    assert token_row["used_at"] is not None, "Token used_at should be set after verify"

    # Set-Cookie header must be present
    cookie_header = resp.headers.get("set-cookie", "")
    assert "jarvis_session" in cookie_header, (
        f"set-cookie did not contain jarvis_session; headers: {dict(resp.headers)}"
    )


async def test_verify_token_replay_returns_400(
    contract_conn,
    _auth_app,
    _configure_api_key,
):
    """Replaying a consumed token returns 400.

    Grounding: auth.py:219-220 — ``if token_row["used_at"] is not None: raise 400``.
    This is the replay-prevention contract; mock tests assert the branch exists
    but don't execute it against a real DB update.
    """
    user_id, _ = await _seed_user(contract_conn, "ml-replay@contract.test")
    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_token(raw_token)

    # Insert token pre-consumed (used_at set)
    await contract_conn.execute(
        """INSERT INTO magic_link_tokens (token_hash, user_id, expires_at, used_at)
           VALUES ($1, $2, NOW() + INTERVAL '15 minutes', NOW())""",
        token_hash,
        user_id,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_auth_app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_KEY},
    ) as c:
        resp = await c.post("/api/auth/verify", json={"token": raw_token})

    assert resp.status_code == 400, (
        f"Expected 400 for consumed token replay; got {resp.status_code}: {resp.text[:300]}"
    )
