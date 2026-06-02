"""Tests for suppressing the ``auth.session.missing`` audit on known
unauthenticated poll paths (PR2-T7).

The logged-out frontend continuously polls a handful of always-reachable
status endpoints (dashboard metrics, setup gates). Each unauthenticated poll
was emitting an ``auth.session.missing`` audit row, drowning the audit_log in
benign noise (12k+ rows) and burying genuine security events. We suppress the
audit ONLY for an explicit allowlist of those poll paths — the 401 itself is
unchanged, and EVERY other path still audits exactly as before.

Verified identifiers:
- libs/jarvis_common/jarvis_common/auth.py:272 — current_user_id_strict definition
- libs/jarvis_common/jarvis_common/auth.py:288-293 — log_audit(action="auth.session.missing", ...)
- libs/jarvis_common/jarvis_common/audit.py — log_audit(pool, *, action, resource, metadata)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from jarvis_common import auth


def _make_logged_out_request(*, path: str, client_ip: str = "127.0.0.1") -> MagicMock:
    """Build a minimal mock Request with NO resolved session user_id.

    ``request.state`` has no ``user_id`` attribute, so
    ``_resolve_request_user_id`` returns None and the strict resolver takes the
    401 / audit branch.
    """
    request = MagicMock()
    request.state = SimpleNamespace()  # no user_id → unauthenticated
    request.client = SimpleNamespace(host=client_ip)
    request.url = SimpleNamespace(path=path)
    return request


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard/metrics",
        "/api/system/setup-status",
        "/api/setup/status",
    ],
)
@pytest.mark.asyncio
async def test_allowlisted_poll_path_skips_session_missing_audit(path: str) -> None:
    """An allowlisted unauthenticated poll path must NOT emit auth.session.missing,
    yet must still raise the 401."""
    mock_pool = MagicMock()
    mock_request = _make_logged_out_request(path=path)

    with (
        patch("jarvis_common.auth._request_db_pool", return_value=mock_pool),
        patch("jarvis_common.auth.log_audit", new_callable=AsyncMock) as mock_log_audit,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await auth.current_user_id_strict(mock_request)

    assert exc_info.value.status_code == 401
    mock_log_audit.assert_not_awaited()


@pytest.mark.parametrize("path", ["/api/papers", "/api/executive/my-day"])
@pytest.mark.asyncio
async def test_non_allowlisted_path_still_audits_session_missing(path: str) -> None:
    """A non-allowlisted path must STILL emit auth.session.missing and raise 401."""
    mock_pool = MagicMock()
    mock_request = _make_logged_out_request(path=path)

    with (
        patch("jarvis_common.auth._request_db_pool", return_value=mock_pool),
        patch("jarvis_common.auth.log_audit", new_callable=AsyncMock) as mock_log_audit,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await auth.current_user_id_strict(mock_request)

    assert exc_info.value.status_code == 401
    mock_log_audit.assert_awaited_once()
    call_kwargs = mock_log_audit.call_args.kwargs
    assert call_kwargs.get("action") == "auth.session.missing"
    assert call_kwargs.get("resource") == path
