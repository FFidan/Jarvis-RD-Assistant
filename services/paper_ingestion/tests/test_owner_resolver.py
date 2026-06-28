"""Unit tests for jarvis_common.owner.resolve_owner_user_id.

Env ``OWNER_USER_ID`` wins; otherwise the DB system row written by first-admin
setup decides. A malformed DB row is ignored (logged) rather than raised so a
bad row can never lock out API-key login.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock


async def test_env_owner_wins_without_touching_db(monkeypatch) -> None:
    from jarvis_common.owner import resolve_owner_user_id

    monkeypatch.setenv("OWNER_USER_ID", "7")
    conn = AsyncMock()

    assert await resolve_owner_user_id(conn) == 7
    conn.fetchval.assert_not_called()


async def test_db_row_used_when_env_unset(monkeypatch) -> None:
    from jarvis_common.owner import OWNER_USER_ID_CONFIG_KEY, resolve_owner_user_id

    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=5)

    assert await resolve_owner_user_id(conn) == 5
    conn.fetchval.assert_awaited_once()
    assert conn.fetchval.await_args.args[1] == OWNER_USER_ID_CONFIG_KEY


async def test_no_row_returns_none(monkeypatch) -> None:
    from jarvis_common.owner import resolve_owner_user_id

    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)

    assert await resolve_owner_user_id(conn) is None


async def test_malformed_row_ignored_and_warns(monkeypatch, caplog) -> None:
    from jarvis_common.owner import resolve_owner_user_id

    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    conn = AsyncMock()
    # A malformed jsonb value decodes (via the pool's json codec) to a string.
    conn.fetchval = AsyncMock(return_value="x")

    with caplog.at_level(logging.WARNING, logger="jarvis_common.owner"):
        assert await resolve_owner_user_id(conn) is None

    assert any("owner.user_id" in r.message for r in caplog.records)
