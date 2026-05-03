"""Smoke tests for jarvis_common.audit.log_audit."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.audit import log_audit


@pytest.mark.asyncio
async def test_log_audit_inserts_correct_sql():
    """log_audit should execute INSERT with the right positional parameters."""
    conn = AsyncMock()
    conn.execute = AsyncMock()

    pool = MagicMock()
    # Make pool.acquire() work as an async context manager returning conn
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await log_audit(
        pool,
        action="delete_topic",
        resource="topic:42",
        user_id="user-1",
        metadata={"extra": "info"},
    )

    conn.execute.assert_awaited_once()
    call_args = conn.execute.call_args
    # First positional arg is the SQL string
    sql: str = call_args[0][0]
    assert "INSERT INTO audit_log" in sql
    # Remaining positional args are the parameter values ($1..$4)
    params = call_args[0][1:]
    assert params[0] == "user-1"  # user_id
    assert params[1] == "delete_topic"  # action
    assert params[2] == "topic:42"  # resource
    assert params[3] == {"extra": "info"}  # metadata


@pytest.mark.asyncio
async def test_log_audit_defaults_metadata_to_empty_dict():
    """log_audit should pass {} when metadata is omitted."""
    conn = AsyncMock()
    conn.execute = AsyncMock()

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await log_audit(pool, action="pulse_generate_enqueued", resource="pulse:deck")

    call_args = conn.execute.call_args
    metadata_param = call_args[0][4]  # $4 = metadata
    assert metadata_param == {}


@pytest.mark.asyncio
async def test_log_audit_never_raises_on_db_error():
    """log_audit must silently swallow DB errors (best-effort logging)."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    # Should not raise
    await log_audit(pool, action="test_action", resource="test:resource")


@pytest.mark.asyncio
async def test_log_audit_passes_small_metadata_unchanged():
    """Metadata under 4 KB must be stored as-is."""
    conn = AsyncMock()
    conn.execute = AsyncMock()

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    small = {"key": "value", "n": 42, "items": list(range(10))}
    await log_audit(pool, action="ok", resource="r", metadata=small)

    metadata_param = conn.execute.call_args[0][4]
    assert metadata_param == small
    assert "_truncated" not in metadata_param


@pytest.mark.asyncio
async def test_log_audit_truncates_oversize_metadata():
    """Metadata > 4 KB is replaced with a truncation marker."""
    conn = AsyncMock()
    conn.execute = AsyncMock()

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    # 8 KB string -> well over the 4 KB limit even after JSON encoding overhead
    huge = {"blob": "x" * 8192}
    await log_audit(pool, action="dangerous", resource="r", metadata=huge)

    metadata_param = conn.execute.call_args[0][4]
    assert metadata_param.get("_truncated") is True
    assert isinstance(metadata_param.get("_size"), int)
    assert metadata_param["_size"] > 4096
    assert "blob" not in metadata_param  # original payload dropped


@pytest.mark.asyncio
async def test_log_audit_user_id_defaults_to_none():
    """log_audit user_id should be None when not supplied."""
    conn = AsyncMock()
    conn.execute = AsyncMock()

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await log_audit(pool, action="telegram_pairing_created", resource="telegram:pairing")

    call_args = conn.execute.call_args
    user_id_param = call_args[0][1]  # $1 = user_id
    assert user_id_param is None
