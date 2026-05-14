"""Direct tests for shared jarvis_common helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from jarvis_common.db_helpers import (
    delete_or_404,
    dynamic_update,
    fmt_safe,
    init_pg_connection,
    quote_ident,
    validated_model,
)


def test_quote_ident_escapes_quotes_and_blocks_null_bytes():
    """quote_ident should preserve SQL identifier safety."""
    assert quote_ident('paper"notes') == '"paper""notes"'

    with pytest.raises(ValueError, match="null byte"):
        quote_ident("bad\x00name")


def test_fmt_safe_escapes_curly_braces_for_format_calls():
    """fmt_safe prevents user strings from breaking later format() calls."""
    assert fmt_safe("{x} -> {y}") == "{{x}} -> {{y}}"


def test_validated_model_allows_known_aliases_and_falls_back(caplog):
    """validated_model should only pass through whitelisted LiteLLM aliases."""
    assert validated_model("fast") == "fast"

    with caplog.at_level("WARNING"):
        assert validated_model("bad model!") == "smart"

    assert "Ignoring invalid model" in caplog.text


@pytest.mark.asyncio
async def test_init_pg_connection_registers_json_and_jsonb_codecs():
    """init_pg_connection should register both JSON codecs with asyncpg."""
    conn = AsyncMock()

    await init_pg_connection(conn)

    assert conn.set_type_codec.await_count == 2
    calls = conn.set_type_codec.await_args_list
    assert calls[0].args[0] == "jsonb"
    assert calls[1].args[0] == "json"


@pytest.mark.asyncio
async def test_dynamic_update_serializes_jsonb_and_extra_sets():
    """dynamic_update passes native dict to asyncpg for JSONB columns (no json.dumps).

    The asyncpg global JSONB codec (registered via init_pg_connection) handles
    serialisation.  dynamic_update must NOT call json.dumps itself — that would
    produce a double-encoded JSON string-of-a-string on the wire (JC-001).
    """
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": 7, "config": {"enabled": True}}

    row = await dynamic_update(
        conn,
        "user_config",
        7,
        updates={"value": {"enabled": True}},
        allowed_columns=frozenset({"value"}),
        jsonb_columns=frozenset({"value"}),
        extra_sets=["updated_at = NOW()"],
    )

    sql = conn.fetchrow.await_args.args[0]
    params = conn.fetchrow.await_args.args[1:]

    assert 'UPDATE "user_config" SET "value" = $2::jsonb, updated_at = NOW()' in sql
    # JC-001 fix: asyncpg codec handles serialisation — raw dict is passed, NOT json.dumps()  # nolint:jsonb-double-encode
    assert params == (7, {"enabled": True})
    assert row == {"id": 7, "config": {"enabled": True}}


@pytest.mark.asyncio
async def test_dynamic_update_rejects_invalid_columns_and_tables():
    """dynamic_update should fail closed before building unsafe SQL."""
    conn = AsyncMock()

    with pytest.raises(HTTPException, match="Invalid field"):
        await dynamic_update(
            conn,
            "topics",
            1,
            updates={"bad_column": "x"},
            allowed_columns=frozenset({"name"}),
        )

    with pytest.raises(ValueError, match="not in allowed list"):
        await dynamic_update(
            conn,
            "papers",
            1,
            updates={"name": "x"},
            allowed_columns=frozenset({"name"}),
        )


@pytest.mark.asyncio
async def test_delete_or_404_raises_when_no_rows_are_deleted():
    """delete_or_404 should convert empty DELETE results into HTTP 404."""
    conn = AsyncMock()
    conn.execute.return_value = "DELETE 0"

    with pytest.raises(HTTPException, match="Missing"):
        await delete_or_404(conn, "DELETE FROM topics WHERE id = $1", 9, detail="Missing")


@pytest.mark.asyncio
async def test_delete_or_404_returns_cleanly_when_delete_succeeds():
    """delete_or_404 should not raise when the DELETE statement affects rows."""
    conn = AsyncMock()
    conn.execute.return_value = "DELETE 1"

    await delete_or_404(conn, "DELETE FROM topics WHERE id = $1", 9)

    conn.execute.assert_awaited_once_with("DELETE FROM topics WHERE id = $1", 9)
