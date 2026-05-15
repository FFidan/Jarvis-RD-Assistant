"""WS-AUTH: strict role/identity boundary tests.

Contract after WS-AUTH:
- ``require_admin``: only an explicit ``role == 'admin'`` session passes;
  API-key-only callers (no session role) and ``role == 'user'`` get 403.
- ``require_admin_or_api_key``: the old lax behaviour — API-key-only callers
  pass, only an explicit non-admin session is rejected.
- ``current_user_id_strict``: 401 when no session; int when present.
- ``current_user_id_strict_with_owner_override``: 401 when neither a session
  nor a valid owner override resolves.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from jarvis_common.auth import (
    current_user_id_strict,
    current_user_id_strict_with_owner_override,
    refresh_api_key_cache,
    require_admin,
    require_admin_or_api_key,
)


def _request(*, user_role: object = "__absent__", user_id: object = "__absent__") -> MagicMock:
    """Build a mock Request; ``__absent__`` leaves the attr unset (API-key path)."""
    request = MagicMock()
    state = SimpleNamespace()
    if user_role != "__absent__":
        state.user_role = user_role
    if user_id != "__absent__":
        state.user_id = user_id
    request.state = state
    request.url = SimpleNamespace(path="/api/x")
    request.client = SimpleNamespace(host="127.0.0.1")
    request.app = SimpleNamespace(state=SimpleNamespace(db_pool=None))
    return request


# ---------------------------------------------------------------------------
# require_admin — now strict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_admin_rejects_api_key_only_no_session():
    with pytest.raises(HTTPException) as exc:
        await require_admin(_request())  # user_role absent
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_rejects_user_role_session():
    with pytest.raises(HTTPException) as exc:
        await require_admin(_request(user_role="user"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_passes_admin_session():
    assert await require_admin(_request(user_role="admin")) is None


# ---------------------------------------------------------------------------
# require_admin_or_api_key — old lax behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_admin_or_api_key_passes_api_key_only():
    assert await require_admin_or_api_key(_request()) is None  # no role → allowed


@pytest.mark.asyncio
async def test_require_admin_or_api_key_passes_admin():
    assert await require_admin_or_api_key(_request(user_role="admin")) is None


@pytest.mark.asyncio
async def test_require_admin_or_api_key_rejects_non_admin_session():
    with pytest.raises(HTTPException) as exc:
        await require_admin_or_api_key(_request(user_role="user"))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# current_user_id_strict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_user_id_strict_raises_401_when_no_session():
    with pytest.raises(HTTPException) as exc:
        await current_user_id_strict(_request())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_current_user_id_strict_returns_int_when_set():
    result = await current_user_id_strict(_request(user_id=7))
    assert result == 7


# ---------------------------------------------------------------------------
# current_user_id_strict_with_owner_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_owner_override_raises_401_when_unresolved(monkeypatch):
    """No session and no X-Owner-User-Id header → 401 (not None)."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    refresh_api_key_cache()

    request = MagicMock()
    request.state = SimpleNamespace()  # no user_id
    request.url = SimpleNamespace(path="/api/x")
    request.client = SimpleNamespace(host="127.0.0.1")
    request.headers = {}  # no owner override header
    request.app = SimpleNamespace(state=SimpleNamespace(db_pool=None))

    with pytest.raises(HTTPException) as exc:
        await current_user_id_strict_with_owner_override(request, api_key=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_strict_owner_override_returns_session_user(monkeypatch):
    """Session user resolves → returned as int, no 401."""
    monkeypatch.setenv("JARVIS_API_KEY", "supersecretkey1234567890abcdef12")
    refresh_api_key_cache()

    request = MagicMock()
    request.state = SimpleNamespace(user_id=42)
    request.url = SimpleNamespace(path="/api/x")
    request.client = SimpleNamespace(host="127.0.0.1")
    request.headers = {}
    request.app = SimpleNamespace(state=SimpleNamespace(db_pool=None))

    result = await current_user_id_strict_with_owner_override(request, api_key=None)
    assert result == 42


@pytest.mark.asyncio
async def test_current_user_id_strict_audits_failure_best_effort():
    """A db_pool present means log_audit is attempted; a failing pool still 401s."""
    request = _request()  # no session
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=RuntimeError("pool down"))
    request.app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with pytest.raises(HTTPException) as exc:
        await current_user_id_strict(request)
    assert exc.value.status_code == 401
