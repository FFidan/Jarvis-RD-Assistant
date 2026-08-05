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

from jarvis_common.testing import make_pool_and_conn


def build_mock_pool_with_txn(conn: AsyncMock) -> MagicMock:
    """Return a pool mock whose acquire() yields *conn* and that wires conn.transaction()."""
    return make_pool_and_conn(conn=conn)[0]


def build_mock_pool(conn: AsyncMock) -> MagicMock:
    """Return a plain acquire-only pool mock (no transaction CM)."""
    return make_pool_and_conn(conn=conn, with_transaction=False)[0]


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
