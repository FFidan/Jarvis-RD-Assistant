"""Shared pool/request stubs for auth and admin unit tests (D5-03).

These helpers replace 6 identical (or near-identical) local definitions of
``_build_mock_pool`` / ``_build_request`` spread across:
  - test_auth_magic_link.py
  - test_account.py         (uses txn variant)
  - test_admin_users.py
  - test_admin_user_deletion.py

``test_api_key_session.py`` keeps its own local ``_build_request`` because
that file's request stub adds ``headers`` (``X-API-Key``) which no other file
needs.

Two variants of ``build_mock_pool`` are provided:

``build_mock_pool_with_txn(conn)``
    Wires ``conn.transaction`` to a working async CM.  Required by any router
    that calls ``async with conn.transaction():`` (auth, account).

``build_mock_pool(conn)``
    Plain acquire-only pool without a transaction CM.  Used by admin routes
    that manage transactions externally or not at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def build_mock_pool_with_txn(conn: AsyncMock) -> MagicMock:
    """Return a pool mock whose acquire() yields *conn* and that wires conn.transaction()."""
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def build_mock_pool(conn: AsyncMock) -> MagicMock:
    """Return a plain acquire-only pool mock (no transaction CM)."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def build_request_auth(
    pool: MagicMock,
    *,
    cookies: dict[str, str] | None = None,
    url_path: str = "/api/auth/request-link",
) -> SimpleNamespace:
    """Build a Request stub for auth router tests (test_auth_magic_link.py style)."""
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    url = SimpleNamespace(
        path=url_path,
        replace=lambda **kw: SimpleNamespace(__str__=lambda self: "https://x/auth/verify"),
    )
    return SimpleNamespace(
        url=url,
        app=app,
        client=SimpleNamespace(host="127.0.0.1"),
        cookies=cookies or {},
        state=SimpleNamespace(),
    )


def build_request_admin(
    pool: MagicMock,
    *,
    user_id: int | None = None,
    user_role: str | None = None,
) -> SimpleNamespace:
    """Build a Request stub for admin router tests (test_admin_users.py style)."""
    state = SimpleNamespace(db_pool=pool)
    if user_id is not None:
        state.user_id = user_id
    if user_role is not None:
        state.user_role = user_role

    app = SimpleNamespace(state=state)
    url = SimpleNamespace(
        path="/api/admin/users",
        replace=lambda **kw: SimpleNamespace(__str__=lambda self: "https://x/auth/verify"),
    )
    return SimpleNamespace(
        url=url,
        app=app,
        client=SimpleNamespace(host="127.0.0.1"),
        cookies={},
        state=state,
    )


def build_request_account(
    pool: MagicMock,
    *,
    user_id: int | None = 1,
) -> SimpleNamespace:
    """Build a Request stub for account router tests (test_account.py style)."""
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    url = SimpleNamespace(
        path="/api/account",
        replace=lambda **kw: SimpleNamespace(
            __str__=lambda self: "https://x/settings?section=account&item=profile"
        ),
    )
    return SimpleNamespace(
        url=url,
        app=app,
        client=SimpleNamespace(host="127.0.0.1"),
        cookies={},
        state=SimpleNamespace(user_id=user_id) if user_id is not None else SimpleNamespace(),
    )
