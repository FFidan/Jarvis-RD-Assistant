"""Tests for X-Owner-User-Id override audit-log emission.

Verified identifiers:
- libs/jarvis_common/jarvis_common/auth.py:453-464 — audit-log success block in
  current_user_id_with_owner_override (calls log_audit with action="auth.owner_override.used")
- libs/jarvis_common/jarvis_common/auth.py:352 — current_user_id_with_owner_override definition
- libs/jarvis_common/jarvis_common/audit.py:42 — log_audit(pool, *, action, resource, user_id, metadata)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common import auth


def _make_request(
    *,
    user_id_header: str = "42",
    api_key_header: str = "test-key",
    client_ip: str = "127.0.0.1",
    path: str = "/api/executive/my-day",
    pool: object | None = None,
) -> MagicMock:
    """Build a minimal mock Request for override tests."""
    request = MagicMock()
    # state must NOT have user_id so session path is skipped
    request.state = SimpleNamespace()
    request.headers = {
        "X-Owner-User-Id": user_id_header,
        "X-API-Key": api_key_header,
    }
    request.client = SimpleNamespace(host=client_ip)
    request.url = SimpleNamespace(path=path)

    # Guard (c) reads request.app.state.db_pool inline; we attach the pool here.
    state_ns = SimpleNamespace(db_pool=pool)
    request.app = SimpleNamespace(state=state_ns)

    return request


def _make_pool(*, user_exists: bool = True) -> MagicMock:
    """Return a mock asyncpg.Pool whose fetchval returns 1 (user exists) or None."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=1 if user_exists else None)
    return pool


@pytest.mark.asyncio
async def test_owner_override_success_emits_audit_log() -> None:
    """Successful X-Owner-User-Id override must emit log_audit(action='auth.owner_override.used')."""
    from jarvis_common.auth import current_user_id_with_owner_override

    mock_pool = _make_pool(user_exists=True)
    mock_request = _make_request(pool=mock_pool)

    with (
        patch("jarvis_common.auth._CACHED_API_KEY", "test-key"),
        patch("jarvis_common.auth._ip_in_allowlist", return_value=True),
        patch("jarvis_common.auth._request_db_pool", return_value=mock_pool),
        patch("jarvis_common.auth.log_audit", new_callable=AsyncMock) as mock_log_audit,
    ):
        result = await current_user_id_with_owner_override(mock_request, api_key="test-key")

    assert result == 42
    mock_log_audit.assert_awaited_once()
    call_kwargs = mock_log_audit.call_args.kwargs
    assert call_kwargs.get("action") == "auth.owner_override.used"
    assert call_kwargs.get("user_id") == "42"
    assert call_kwargs.get("resource") == "/api/executive/my-day"


@pytest.mark.asyncio
async def test_owner_override_audit_log_includes_client_ip() -> None:
    """Audit log metadata must include the client IP."""
    from jarvis_common.auth import current_user_id_with_owner_override

    mock_pool = _make_pool(user_exists=True)
    mock_request = _make_request(pool=mock_pool, client_ip="127.0.0.1")

    with (
        patch("jarvis_common.auth._CACHED_API_KEY", "test-key"),
        patch("jarvis_common.auth._ip_in_allowlist", return_value=True),
        patch("jarvis_common.auth._request_db_pool", return_value=mock_pool),
        patch("jarvis_common.auth.log_audit", new_callable=AsyncMock) as mock_log_audit,
    ):
        await current_user_id_with_owner_override(mock_request, api_key="test-key")

    call_kwargs = mock_log_audit.call_args.kwargs
    metadata = call_kwargs.get("metadata", {})
    assert "client_ip" in metadata
    assert metadata["client_ip"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_owner_override_audit_pool_none_skips_log() -> None:
    """When _request_db_pool returns None, log_audit is NOT called (pool unavailable)."""
    from jarvis_common.auth import current_user_id_with_owner_override

    mock_pool = _make_pool(user_exists=True)
    mock_request = _make_request(pool=mock_pool)

    with (
        patch("jarvis_common.auth._CACHED_API_KEY", "test-key"),
        patch("jarvis_common.auth._ip_in_allowlist", return_value=True),
        # Guard (c) still uses mock_pool via request.app.state.db_pool;
        # the AUDIT path sees None from _request_db_pool → skips the log call.
        patch("jarvis_common.auth._request_db_pool", return_value=None),
        patch("jarvis_common.auth.log_audit", new_callable=AsyncMock) as mock_log_audit,
    ):
        result = await current_user_id_with_owner_override(mock_request, api_key="test-key")

    assert result == 42
    mock_log_audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_override_audit_log_failure_does_not_block_request() -> None:
    """A transient log_audit exception must NOT propagate — request still succeeds."""
    from jarvis_common.auth import current_user_id_with_owner_override

    mock_pool = _make_pool(user_exists=True)
    mock_request = _make_request(pool=mock_pool)

    async def _exploding_log_audit(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise RuntimeError("DB connection lost")

    with (
        patch("jarvis_common.auth._CACHED_API_KEY", "test-key"),
        patch("jarvis_common.auth._ip_in_allowlist", return_value=True),
        patch("jarvis_common.auth._request_db_pool", return_value=mock_pool),
        patch("jarvis_common.auth.log_audit", side_effect=_exploding_log_audit),
    ):
        # Should NOT raise despite the audit log throwing
        result = await current_user_id_with_owner_override(mock_request, api_key="test-key")

    assert result == 42


@pytest.mark.asyncio
async def test_owner_override_failed_api_key_does_not_emit_audit_log() -> None:
    """A rejected override (bad API key) must NOT emit the success audit event."""
    from fastapi import HTTPException
    from jarvis_common.auth import current_user_id_with_owner_override

    mock_request = _make_request(api_key_header="wrong-key")

    with (
        patch("jarvis_common.auth._CACHED_API_KEY", "correct-key"),
        patch("jarvis_common.auth.log_audit", new_callable=AsyncMock) as mock_log_audit,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await current_user_id_with_owner_override(mock_request, api_key="wrong-key")

    assert exc_info.value.status_code == 403
    mock_log_audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# CFG-OWNERCACHE-1: cache tests
# ---------------------------------------------------------------------------


def test_refresh_allowed_networks_cache_exists() -> None:
    """refresh_allowed_networks_cache() must be importable and not raise."""
    from jarvis_common.auth import refresh_allowed_networks_cache

    refresh_allowed_networks_cache()  # must not raise


# ---------------------------------------------------------------------------
# non-integer X-Owner-User-Id must return 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_override_non_integer_header_raises_403() -> None:
    """X-Owner-User-Id with a non-integer value must raise HTTPException(403).

    Guards (a) API key and (b) IP allowlist are patched to pass so the parse
    step (``int(raw_override)``) is reached and raises the 403 before any DB
    access occurs.

    Production path: auth.py current_user_id_with_owner_override — the
    ``try: int(raw_override) / except (ValueError, TypeError)`` block raises
    HTTPException(403, detail="X-Owner-User-Id must be an integer").
    """
    from fastapi import HTTPException
    from jarvis_common.auth import current_user_id_with_owner_override

    mock_request = _make_request(user_id_header="not-an-integer")

    with (
        patch("jarvis_common.auth._CACHED_API_KEY", "test-key"),
        patch("jarvis_common.auth._ip_in_allowlist", return_value=True),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await current_user_id_with_owner_override(mock_request, api_key="test-key")

    assert exc_info.value.status_code == 403
    assert "integer" in exc_info.value.detail.lower()


def test_allowed_networks_parsed_once_per_cache_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    """_parse_allowed_networks must be called once per cache fill, not per _ip_in_allowlist call."""
    call_count = 0
    original = auth._parse_allowed_networks

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        return original(*args, **kwargs)

    # Reset the cache so we start from a clean state.
    monkeypatch.setattr(auth, "_CACHED_ALLOWED_NETWORKS", None)
    monkeypatch.setattr(auth, "_parse_allowed_networks", counting)

    auth.refresh_allowed_networks_cache()
    auth._ip_in_allowlist("127.0.0.1")
    auth._ip_in_allowlist("127.0.0.1")

    assert call_count == 1, f"Networks must be parsed once, not per-call; got {call_count}"
