"""Tests for paper_ingestion.routers.source_config (Task B5 + B-1b).

Covers:
  - PATCH /api/settings/sources/{source_type}: config merge, admin-only, 404 on unknown type
  - POST  /api/settings/sources/{source_type}/clear-cooldown: resets source_health, admin-only
  - B-1b regression: JSONB args must be native dict (asyncpg codec auto-encodes; pre-serialised
    strings cause double-encoding — stored as a JSON string scalar instead of an object).

All DB calls are mocked via the project's _make_pool_and_conn() pattern from conftest.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import paper_ingestion.routers.source_config as sc_router
import pytest
from fastapi import HTTPException
from jarvis_common.testing import make_request

from tests.conftest import _make_pool_and_conn


# ---------------------------------------------------------------------------
# _validate_source_type
# ---------------------------------------------------------------------------


def test_validate_source_type_known_passes():
    """No exception for a type returned by the registry."""
    with patch.object(sc_router, "get_source_class", return_value=object()):
        sc_router._validate_source_type("arxiv")  # should not raise


def test_validate_source_type_unknown_raises_404():
    """404 for a source_type not in the registry."""
    with (
        patch.object(sc_router, "get_source_class", return_value=None),
        patch.object(sc_router, "get_all_source_types", return_value=["arxiv", "pubmed"]),
    ):
        with pytest.raises(HTTPException) as exc_info:
            sc_router._validate_source_type("not_a_source")
        assert exc_info.value.status_code == 404
        assert "not_a_source" in exc_info.value.detail


# ---------------------------------------------------------------------------
# PATCH update_source_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_source_config_merges_api_key():
    """PATCH updates the config row when source_type is valid."""
    pool, conn = _make_pool_and_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch.object(sc_router, "get_source_class", return_value=object()):
        body = sc_router.SourceConfigBody(api_key="my-key-123")
        result = await sc_router.update_source_config("semantic_scholar", body, db_pool=pool)

    assert result == {"ok": True}
    conn.execute.assert_called_once()
    sql, *args = conn.execute.await_args.args
    assert "UPDATE paper_sources" in sql
    assert "COALESCE" in sql
    # B-1b: asyncpg JSONB codec auto-encodes — arg must be a native dict, not a JSON string.
    assert isinstance(args[0], dict), "JSONB arg must be native dict (asyncpg auto-encodes)"
    assert args[0]["api_key"] == "my-key-123"
    assert args[1] == "semantic_scholar"


@pytest.mark.asyncio
async def test_update_source_config_merges_email():
    """PATCH with email-only writes email to config."""
    pool, conn = _make_pool_and_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch.object(sc_router, "get_source_class", return_value=object()):
        body = sc_router.SourceConfigBody(email="researcher@example.com")
        result = await sc_router.update_source_config("openalex", body, db_pool=pool)

    assert result == {"ok": True}
    _, payload, src_type = conn.execute.await_args.args
    # B-1b: asyncpg JSONB codec auto-encodes — arg must be a native dict, not a JSON string.
    assert isinstance(payload, dict), "JSONB arg must be native dict (asyncpg auto-encodes)"
    assert payload["email"] == "researcher@example.com"
    assert "api_key" not in payload
    assert src_type == "openalex"


@pytest.mark.asyncio
async def test_update_source_config_unknown_type_raises_404():
    """PATCH returns 404 for an unregistered source_type."""
    pool, _conn = _make_pool_and_conn()

    with (
        patch.object(sc_router, "get_source_class", return_value=None),
        patch.object(sc_router, "get_all_source_types", return_value=["arxiv"]),
    ):
        body = sc_router.SourceConfigBody(api_key="k")
        with pytest.raises(HTTPException) as exc_info:
            await sc_router.update_source_config("ghost_source", body, db_pool=pool)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_update_source_config_no_fields_raises_400():
    """PATCH with no api_key or email raises 400."""
    pool, _conn = _make_pool_and_conn()

    with patch.object(sc_router, "get_source_class", return_value=object()):
        body = sc_router.SourceConfigBody()  # both None
        with pytest.raises(HTTPException) as exc_info:
            await sc_router.update_source_config("arxiv", body, db_pool=pool)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_update_source_config_upserts_when_row_absent():
    """PATCH falls back to INSERT when UPDATE affects 0 rows."""
    pool, conn = _make_pool_and_conn()
    # First call = UPDATE 0; second call = INSERT (execute returns nothing meaningful)
    conn.execute = AsyncMock(side_effect=["UPDATE 0", None])

    with patch.object(sc_router, "get_source_class", return_value=object()):
        body = sc_router.SourceConfigBody(api_key="new-key")
        result = await sc_router.update_source_config("pubmed", body, db_pool=pool)

    assert result == {"ok": True}
    assert conn.execute.await_count == 2
    insert_sql = conn.execute.await_args_list[1].args[0]
    assert "INSERT INTO paper_sources" in insert_sql


@pytest.mark.asyncio
async def test_update_source_config_jsonb_arg_is_dict_not_str():
    """B-1b regression: JSONB arg passed to UPDATE must be a native dict.

    asyncpg registers a JSONB codec that auto-encodes Python dicts.  Passing a
    pre-serialised JSON string produces a double-encode: the stored value becomes
    a JSON string scalar instead of an object, breaking all JSON-merge operations.
    """
    pool, conn = _make_pool_and_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch.object(sc_router, "get_source_class", return_value=object()):
        body = sc_router.SourceConfigBody(api_key="test-key", email="x@example.com")
        await sc_router.update_source_config("arxiv", body, db_pool=pool)

    sql, jsonb_arg, _src_type = conn.execute.await_args.args
    assert "UPDATE paper_sources" in sql
    assert isinstance(jsonb_arg, dict), (
        "JSONB arg must be a native dict — got "
        f"{type(jsonb_arg).__name__!r} which would double-encode"
    )
    assert jsonb_arg == {"api_key": "test-key", "email": "x@example.com"}


@pytest.mark.asyncio
async def test_update_source_config_insert_fallback_jsonb_arg_is_dict():
    """B-1b regression: JSONB arg passed to INSERT fallback must also be a native dict."""
    pool, conn = _make_pool_and_conn()
    # First execute = UPDATE 0 (row absent), second = INSERT
    conn.execute = AsyncMock(side_effect=["UPDATE 0", None])

    with patch.object(sc_router, "get_source_class", return_value=object()):
        body = sc_router.SourceConfigBody(api_key="fallback-key")
        await sc_router.update_source_config("pubmed", body, db_pool=pool)

    assert conn.execute.await_count == 2
    # INSERT call: conn.execute(sql, source_type, updates)
    insert_call_args = conn.execute.await_args_list[1].args
    jsonb_arg = insert_call_args[2]  # position 2: updates dict
    assert isinstance(jsonb_arg, dict), (
        "JSONB arg must be a native dict — got "
        f"{type(jsonb_arg).__name__!r} which would double-encode"
    )
    assert jsonb_arg.get("api_key") == "fallback-key"


@pytest.mark.asyncio
async def test_update_source_config_admin_required():
    """require_admin raises 403 for non-admin callers."""
    request = make_request(role="user")
    with pytest.raises(HTTPException) as exc_info:
        await sc_router.require_admin(request)
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# POST clear_source_cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_cooldown_resets_source_health():
    """POST clear-cooldown issues UPDATE with ok status and NULL cooldown_until."""
    pool, conn = _make_pool_and_conn()
    conn.execute = AsyncMock(return_value=None)
    request = make_request(1)

    with patch.object(sc_router, "get_source_class", return_value=object()):
        result = await sc_router.clear_source_cooldown("arxiv", request, db_pool=pool)

    assert result == {"ok": True}
    conn.execute.assert_called_once()
    sql, src_type = conn.execute.await_args.args
    assert "UPDATE source_health" in sql
    assert "cooldown_until" in sql
    assert "consecutive_failures" in sql
    assert "last_status" in sql
    assert src_type == "arxiv"


@pytest.mark.asyncio
async def test_clear_cooldown_unknown_type_raises_404():
    """POST clear-cooldown returns 404 for an unregistered source_type."""
    pool, _conn = _make_pool_and_conn()
    request = make_request(1)

    with (
        patch.object(sc_router, "get_source_class", return_value=None),
        patch.object(sc_router, "get_all_source_types", return_value=["arxiv"]),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await sc_router.clear_source_cooldown("ghost_source", request, db_pool=pool)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_clear_cooldown_admin_required():
    """require_admin raises 403 for non-admin callers."""
    request = make_request(role="user")
    with pytest.raises(HTTPException) as exc_info:
        await sc_router.require_admin(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_clear_cooldown_all_source_health_rows_targeted():
    """UPDATE targets all rows for source_type (global + per-user) via WHERE source_type=$1."""
    pool, conn = _make_pool_and_conn()
    conn.execute = AsyncMock(return_value=None)
    request = make_request(99)

    with patch.object(sc_router, "get_source_class", return_value=object()):
        await sc_router.clear_source_cooldown("pubmed", request, db_pool=pool)

    sql, src_type = conn.execute.await_args.args
    # The WHERE clause must not filter by user_id — it resets all rows for the source
    assert "WHERE source_type" in sql
    assert src_type == "pubmed"
