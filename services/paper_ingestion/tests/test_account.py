"""UI_v3 §I Account — GET/PATCH /api/account + verified email change.

Mocks the DB pool so the suite runs without Docker, mirroring
``test_auth_magic_link.py``. Covers:

- GET/PATCH strictly self-scoped (no path param, no cross-user read/write).
- display_name update applies immediately.
- email change is verified (token issued, users.email NOT mutated until
  confirm), mirroring the magic-link verify pattern.
- duplicate-email rejected (409) at request time and confirm time.
- confirm-email rejects unknown/expired/used/wrong-user/non-pending tokens.
- regression: admin user-mgmt router is a separate prefix, untouched.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import paper_ingestion.routers.account as account_router
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from tests._auth_fakes import build_mock_pool_with_txn, build_request_account


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pool / request stubs — delegated to shared _auth_fakes (D5-03)
# ---------------------------------------------------------------------------

_build_mock_pool = build_mock_pool_with_txn


def _build_request(pool: MagicMock, *, user_id: int | None = 1):
    """Build a Request stub.

    NOTE: ``services/paper_ingestion/tests/conftest.py::_default_authenticated_user``
    is an autouse fixture that patches every router module's
    ``current_user_id_strict`` to ``AsyncMock(return_value=1)``. So the
    *effective* authenticated caller in these unit tests is user 1 regardless
    of ``request.state.user_id``. Tests that need a different user / a 401
    re-patch ``account_router.current_user_id_strict`` in their own scope
    (the documented IDOR-test pattern).
    """
    return build_request_account(pool, user_id=user_id)


def _account_row(**over):
    base = {
        "id": 1,
        "email": "old@example.com",
        "role": "admin",
        "display_name": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_login_at": None,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# GET /api/account — strictly self-scoped
# ---------------------------------------------------------------------------


# Collapsed (Phase C): test_get_account_returns_current_user
# Survivor: test_account_contract.py::test_a1_get_account_returns_own_profile


@pytest.mark.asyncio
async def test_get_account_no_session_raises_401(monkeypatch) -> None:
    """Sessionless caller → 401 (re-patch the resolver to the real strict
    behaviour, overriding the autouse user-1 stub)."""
    monkeypatch.setenv("DEV_MODE", "true")

    async def _raise_401(_request):
        raise HTTPException(status_code=401, detail="Authentication required")

    monkeypatch.setattr(account_router, "current_user_id_strict", _raise_401)
    conn = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=None)

    with pytest.raises(HTTPException) as exc:
        await account_router.get_account.__wrapped__(request)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_account_self_scoped_no_path_param() -> None:
    """Defence-in-depth: the route signature exposes no user-id path param."""
    import inspect

    sig = inspect.signature(account_router.get_account.__wrapped__)
    assert list(sig.parameters) == ["request"]
    # Route path carries no user id segment.
    routes = [
        r
        for r in account_router.router.routes
        if isinstance(r, APIRoute) and r.path == "/api/account"
    ]
    assert routes and all("{" not in r.path for r in routes)


# ---------------------------------------------------------------------------
# PATCH /api/account — display_name immediate
# ---------------------------------------------------------------------------


# Collapsed (Phase C): test_patch_display_name_applies_immediately
# Survivor: test_account_contract.py::test_a2_patch_account_display_name_persists


# ---------------------------------------------------------------------------
# PATCH /api/account — email change is VERIFIED, never silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_email_issues_token_and_does_not_mutate_email(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _account_row(),  # current
            None,  # uniqueness clash check → free
            _account_row(),  # refreshed (email UNCHANGED)
        ]
    )
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    sent: list[tuple[str, str]] = []

    async def fake_send(email, link, *, pool=None):
        sent.append((email, link))

    monkeypatch.setattr(account_router, "send_magic_link", fake_send)

    result = await account_router.update_account.__wrapped__(
        account_router.AccountUpdate(email="new@example.com"), request
    )

    assert result.email_verification_sent is True
    # users.email must NOT have been mutated by the PATCH.
    assert result.account.email == "old@example.com"
    executed = [c.args[0] for c in conn.execute.await_args_list]
    assert any("INSERT INTO magic_link_tokens" in s for s in executed)
    assert not any("UPDATE users SET email" in s for s in executed)
    # Verification link delivered to the NEW address only.
    assert len(sent) == 1
    assert sent[0][0] == "new@example.com"
    assert "token=" in sent[0][1]


# Collapsed (Phase C): test_patch_email_duplicate_rejected_409
# Survivor: test_account_contract.py::test_a2_patch_account_email_clash_returns_409


@pytest.mark.asyncio
async def test_patch_email_same_as_current_is_noop(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_account_row(), _account_row()])
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    result = await account_router.update_account.__wrapped__(
        account_router.AccountUpdate(email="OLD@example.com"), request
    )

    assert result.email_verification_sent is False
    executed = [c.args[0] for c in conn.execute.await_args_list]
    assert not any("INSERT INTO magic_link_tokens" in s for s in executed)


# ---------------------------------------------------------------------------
# POST /api/account/confirm-email — token consume swaps email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_email_happy_path_swaps_email(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {  # token row — owned by the resolved caller (user 1)
                "user_id": 1,
                "expires_at": datetime.now(UTC) + timedelta(minutes=10),
                "used_at": None,
                "pending_email": "new@example.com",
            },
            None,  # confirm-time uniqueness re-check → free
            _account_row(email="new@example.com"),  # refreshed
        ]
    )
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    result = await account_router.confirm_email_change.__wrapped__(
        account_router.ConfirmEmailChangeBody(token="A" * 32), request
    )

    assert result.email == "new@example.com"
    executed = [c.args[0] for c in conn.execute.await_args_list]
    assert any("UPDATE magic_link_tokens SET used_at" in s for s in executed)
    assert any("UPDATE users SET email" in s for s in executed)


@pytest.mark.asyncio
async def test_confirm_email_unknown_token_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc:
        await account_router.confirm_email_change.__wrapped__(
            account_router.ConfirmEmailChangeBody(token="A" * 32), request
        )
    assert exc.value.status_code == 400
    executed = [c.args[0] for c in conn.execute.await_args_list]
    assert not any("UPDATE users SET email" in s for s in executed)


@pytest.mark.asyncio
async def test_confirm_email_expired_token_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "user_id": 7,
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
            "used_at": None,
            "pending_email": "new@example.com",
        }
    )
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc:
        await account_router.confirm_email_change.__wrapped__(
            account_router.ConfirmEmailChangeBody(token="A" * 32), request
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_confirm_email_used_token_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "user_id": 7,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "used_at": datetime.now(UTC) - timedelta(seconds=5),
            "pending_email": "new@example.com",
        }
    )
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc:
        await account_router.confirm_email_change.__wrapped__(
            account_router.ConfirmEmailChangeBody(token="A" * 32), request
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_confirm_email_cross_user_token_rejected(monkeypatch) -> None:
    """A valid token owned by user 99 cannot be confirmed by caller 7."""
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "user_id": 99,  # different owner
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "used_at": None,
            "pending_email": "new@example.com",
        }
    )
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc:
        await account_router.confirm_email_change.__wrapped__(
            account_router.ConfirmEmailChangeBody(token="A" * 32), request
        )
    assert exc.value.status_code == 400
    executed = [c.args[0] for c in conn.execute.await_args_list]
    assert not any("UPDATE users SET email" in s for s in executed)


@pytest.mark.asyncio
async def test_confirm_email_non_pending_token_rejected(monkeypatch) -> None:
    """A plain login magic-link token (pending_email NULL) is not usable here."""
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "user_id": 7,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "used_at": None,
            "pending_email": None,
        }
    )
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc:
        await account_router.confirm_email_change.__wrapped__(
            account_router.ConfirmEmailChangeBody(token="A" * 32), request
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_confirm_email_duplicate_at_confirm_time_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "user_id": 1,
                "expires_at": datetime.now(UTC) + timedelta(minutes=10),
                "used_at": None,
                "pending_email": "new@example.com",
            },
            {"id": 88},  # someone grabbed it between request and confirm
        ]
    )
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc:
        await account_router.confirm_email_change.__wrapped__(
            account_router.ConfirmEmailChangeBody(token="A" * 32), request
        )
    assert exc.value.status_code == 409
    executed = [c.args[0] for c in conn.execute.await_args_list]
    assert not any("UPDATE users SET email" in s for s in executed)


# ---------------------------------------------------------------------------
# Regression: admin user-mgmt is a SEPARATE, untouched surface
# ---------------------------------------------------------------------------


def test_account_router_prefix_is_isolated_from_admin() -> None:
    """The account router must not collide with /api/admin/* user-mgmt."""
    import paper_ingestion.routers.admin as admin_router

    assert account_router.router.prefix == "/api/account"
    assert admin_router.router.prefix == "/api/admin"
    acct_paths = {r.path for r in account_router.router.routes if isinstance(r, APIRoute)}
    admin_paths = {r.path for r in admin_router.router.routes if isinstance(r, APIRoute)}
    # No path overlap, and account exposes no /users management surface.
    assert acct_paths.isdisjoint(admin_paths)
    assert not any("/users" in p for p in acct_paths)


def test_token_hash_helper_is_reused_from_auth() -> None:
    """Crypto is imported from auth, not re-implemented."""
    from paper_ingestion.routers.auth import _hash_token

    assert account_router._hash_token is _hash_token
    assert account_router._hash_token("test-token") == _hash("test-token")
