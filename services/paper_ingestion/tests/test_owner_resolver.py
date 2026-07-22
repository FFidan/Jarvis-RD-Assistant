"""Unit tests for the typed deployment-owner resolver."""

from __future__ import annotations

from unittest.mock import AsyncMock


def _live_admin(user_id: int) -> dict[str, object]:
    return {"id": user_id, "role": "admin", "deleted_at": None}


async def test_env_owner_wins_without_touching_config_row(monkeypatch) -> None:
    from jarvis_common.owner import resolve_owner_identity

    monkeypatch.setenv("OWNER_USER_ID", "7")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_live_admin(7))

    identity = await resolve_owner_identity(conn)
    assert (identity.source, identity.state, identity.user_id) == ("environment", "valid", 7)
    conn.fetchval.assert_not_called()
    conn.fetchrow.assert_awaited_once()


async def test_db_row_used_when_env_unset(monkeypatch) -> None:
    from jarvis_common.owner import OWNER_USER_ID_CONFIG_KEY, resolve_owner_identity

    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=5)
    conn.fetchrow = AsyncMock(return_value=_live_admin(5))

    identity = await resolve_owner_identity(conn)
    assert (identity.source, identity.state, identity.user_id) == ("database", "valid", 5)
    conn.fetchval.assert_awaited_once()
    assert conn.fetchval.await_args.args[1] == OWNER_USER_ID_CONFIG_KEY


async def test_no_row_returns_none(monkeypatch) -> None:
    from jarvis_common.owner import resolve_owner_identity

    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)

    identity = await resolve_owner_identity(conn)
    assert (identity.source, identity.state, identity.user_id) == ("none", "missing", None)
    conn.fetchrow.assert_not_called()


async def test_malformed_database_row_is_distinct_from_missing(monkeypatch) -> None:
    from jarvis_common.owner import resolve_owner_identity

    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="x")

    identity = await resolve_owner_identity(conn)
    assert (identity.source, identity.state, identity.user_id) == (
        "database",
        "invalid_value",
        None,
    )
    conn.fetchrow.assert_not_called()


async def test_malformed_environment_value_is_authoritative(monkeypatch) -> None:
    from jarvis_common.owner import resolve_owner_identity

    monkeypatch.setenv("OWNER_USER_ID", "not-an-id")
    conn = AsyncMock()

    identity = await resolve_owner_identity(conn)
    assert (identity.source, identity.state, identity.user_id) == (
        "environment",
        "invalid_value",
        None,
    )
    conn.fetchval.assert_not_called()
    conn.fetchrow.assert_not_called()


async def test_owner_target_must_be_a_live_admin(monkeypatch) -> None:
    from jarvis_common.owner import resolve_owner_identity

    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    cases = [
        (None, "missing_or_deleted_user"),
        ({"id": 9, "role": "admin", "deleted_at": "deleted"}, "missing_or_deleted_user"),
        ({"id": 9, "role": "user", "deleted_at": None}, "non_admin_user"),
    ]
    for row, expected_state in cases:
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=9)
        conn.fetchrow = AsyncMock(return_value=row)
        identity = await resolve_owner_identity(conn)
        assert (identity.source, identity.state, identity.user_id) == (
            "database",
            expected_state,
            9,
        )


async def test_legacy_id_helper_authorizes_only_valid_identity(monkeypatch) -> None:
    from jarvis_common.owner import resolve_owner_user_id

    monkeypatch.delenv("OWNER_USER_ID", raising=False)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=11)
    conn.fetchrow = AsyncMock(return_value={"id": 11, "role": "user", "deleted_at": None})

    assert await resolve_owner_user_id(conn) is None
