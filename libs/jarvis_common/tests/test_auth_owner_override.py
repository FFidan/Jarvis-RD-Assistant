"""Tests for current_user_id_with_owner_override (Sprint A).

Security contract:
- Session-authenticated caller wins (no override check).
- X-Owner-User-Id is trusted ONLY when:
    (a) valid JARVIS_API_KEY is present,
    (b) source IP is within OWNER_OVERRIDE_ALLOWED_CIDRS,
    (c) the supplied user_id exists in the users table.
- Header present but (a) fails → 403.
- Header present but (b) fails (non-allowlisted IP) → 403.
- Header present but (c) fails (unknown user_id) → 403.
- Header absent → None (no error).
- Session user takes priority over header.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from jarvis_common.auth import current_user_id_with_owner_override, refresh_api_key_cache

# ---------------------------------------------------------------------------
# Case-insensitive header dict (mimics real HTTP / Starlette Headers)
# ---------------------------------------------------------------------------


class _CIDict(dict):
    """Case-insensitive dict for HTTP header mocking."""

    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key.lower(), default)

    def __contains__(self, key):  # type: ignore[override]
        return super().__contains__(key.lower())

    def __setitem__(self, key, value):
        super().__setitem__(key.lower(), value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NO_POOL = object()  # sentinel: no db_pool at all (distinct from "pool exists, user not found")


def _make_request(
    *,
    client_ip: str = "127.0.0.1",
    owner_user_id_header: str | None = None,
    api_key_header: str | None = None,
    state_user_id: int | None = None,
    db_pool_fetchval: object = _NO_POOL,
) -> MagicMock:
    """Build a mock FastAPI Request with the given characteristics.

    ``db_pool_fetchval``:
    - Omitted / ``_NO_POOL`` → ``app.state.db_pool`` is ``None`` (no pool configured).
    - ``1`` (or any truthy int) → pool exists, ``fetchval`` returns that value (user found).
    - ``None`` → pool exists but ``fetchval`` returns ``None`` (user not found in DB).
    """
    request = MagicMock()

    # Client
    request.client = SimpleNamespace(host=client_ip)

    # Headers — use case-insensitive dict to mimic real HTTP / Starlette Headers
    headers: _CIDict = _CIDict()
    if owner_user_id_header is not None:
        headers["x-owner-user-id"] = owner_user_id_header
    if api_key_header is not None:
        headers["x-api-key"] = api_key_header

    request.headers = headers

    # request.state — user_id set by session middleware
    state = SimpleNamespace()
    if state_user_id is not None:
        state.user_id = state_user_id
    request.state = state

    # app.state.db_pool
    if db_pool_fetchval is _NO_POOL:
        request.app = SimpleNamespace(state=SimpleNamespace(db_pool=None))
    else:
        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=db_pool_fetchval)
        request.app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    return request


async def _call(request: MagicMock, api_key_param: str | None = None) -> int | None:
    """Invoke current_user_id_with_owner_override directly (bypassing FastAPI DI)."""
    return await current_user_id_with_owner_override(request, api_key=api_key_param)


# ---------------------------------------------------------------------------
# Session-user priority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_user_wins_over_override_header(monkeypatch):
    """When request.state.user_id is set, the override header is ignored."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    refresh_api_key_cache()

    request = _make_request(
        state_user_id=5,
        owner_user_id_header="999",
        api_key_header="supersecretkey1234567890abcdef12",
    )
    result = await _call(request, api_key_param="supersecretkey1234567890abcdef12")
    assert result == 5  # session user wins, not 999


# ---------------------------------------------------------------------------
# No header → None (not an error)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_header_returns_none(monkeypatch):
    """Absent X-Owner-User-Id with no session → None without raising."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    refresh_api_key_cache()

    request = _make_request()
    result = await _call(request, api_key_param=None)
    assert result is None


# ---------------------------------------------------------------------------
# Guard (a): API key required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_rejected_without_api_key(monkeypatch):
    """X-Owner-User-Id without a valid API key → 403."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    refresh_api_key_cache()

    request = _make_request(
        owner_user_id_header="10",
        api_key_header=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        await _call(request, api_key_param=None)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_override_rejected_with_wrong_api_key(monkeypatch):
    """X-Owner-User-Id with an incorrect API key → 403."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    refresh_api_key_cache()

    request = _make_request(
        owner_user_id_header="10",
        api_key_header="wrongkey",
    )
    with pytest.raises(HTTPException) as exc_info:
        await _call(request, api_key_param="wrongkey")
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Guard (b): IP allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_rejected_from_non_allowlisted_ip(monkeypatch):
    """X-Owner-User-Id from a public IP (not loopback/docker-bridge) → 403."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", "127.0.0.0/8,172.16.0.0/12")
    refresh_api_key_cache()

    request = _make_request(
        client_ip="8.8.8.8",  # public IP — not in allowlist
        owner_user_id_header="10",
        api_key_header="supersecretkey1234567890abcdef12",
    )
    with pytest.raises(HTTPException) as exc_info:
        await _call(request, api_key_param="supersecretkey1234567890abcdef12")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_override_accepted_from_loopback_ip(monkeypatch):
    """X-Owner-User-Id from loopback (127.0.0.1) is accepted when key + user are valid."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", "127.0.0.0/8,172.16.0.0/12")
    refresh_api_key_cache()

    request = _make_request(
        client_ip="127.0.0.1",
        owner_user_id_header="42",
        db_pool_fetchval=1,  # user exists
    )
    result = await _call(request, api_key_param="supersecretkey1234567890abcdef12")
    assert result == 42


@pytest.mark.asyncio
async def test_override_accepted_from_docker_bridge_ip(monkeypatch):
    """X-Owner-User-Id from a docker-bridge IP (172.17.0.x) is accepted."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", "127.0.0.0/8,172.16.0.0/12")
    refresh_api_key_cache()

    request = _make_request(
        client_ip="172.17.0.2",
        owner_user_id_header="99",
        db_pool_fetchval=1,  # user exists
    )
    result = await _call(request, api_key_param="supersecretkey1234567890abcdef12")
    assert result == 99


# ---------------------------------------------------------------------------
# Guard (c): user_id must exist in DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_rejected_with_nonexistent_user_id(monkeypatch):
    """X-Owner-User-Id referencing an unknown user_id → 403."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", "127.0.0.0/8,172.16.0.0/12")
    refresh_api_key_cache()

    request = _make_request(
        client_ip="127.0.0.1",
        owner_user_id_header="9999",
        db_pool_fetchval=None,  # user does NOT exist
    )
    with pytest.raises(HTTPException) as exc_info:
        await _call(request, api_key_param="supersecretkey1234567890abcdef12")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_override_accepted_with_existing_user_id(monkeypatch):
    """All three guards pass → resolved user_id returned."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", "127.0.0.0/8,172.16.0.0/12")
    refresh_api_key_cache()

    request = _make_request(
        client_ip="127.0.0.1",
        owner_user_id_header="7",
        db_pool_fetchval=1,  # user exists
    )
    result = await _call(request, api_key_param="supersecretkey1234567890abcdef12")
    assert result == 7


# ---------------------------------------------------------------------------
# Edge: non-integer header value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_rejected_with_non_integer_header(monkeypatch):
    """Non-integer X-Owner-User-Id → 403 (not a crash)."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", "127.0.0.0/8,172.16.0.0/12")
    refresh_api_key_cache()

    request = _make_request(
        client_ip="127.0.0.1",
        owner_user_id_header="not-an-int",
    )
    with pytest.raises(HTTPException) as exc_info:
        await _call(request, api_key_param="supersecretkey1234567890abcdef12")
    assert exc_info.value.status_code == 403
