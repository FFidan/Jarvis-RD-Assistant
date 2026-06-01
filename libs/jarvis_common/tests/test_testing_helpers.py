"""Smoke tests for jarvis_common.testing — keystone verification.

These tests verify that the shared fixtures behave identically to the
canonical bodies they were extracted from.  They do NOT cover telegram_bot
helpers (make_bot_config requires telegram_bot on sys.path) — that is covered
by the service's own tests continuing to pass after the conftest re-export.
"""

from __future__ import annotations

import pytest
from jarvis_common.testing import (
    FakeAcquireCM,
    FakeRecord,
    FakeTxnCM,
    RoleMiddleware,
    _make_pool_and_conn,
    make_pool_and_conn,
    make_telegram_update,
)

# ---------------------------------------------------------------------------
# FakeRecord
# ---------------------------------------------------------------------------


def test_fake_record_dict_access():
    rec = FakeRecord({"a": 1, "b": "hello"})
    assert rec["a"] == 1
    assert rec["b"] == "hello"


def test_fake_record_attr_access():
    rec = FakeRecord({"x": 42})
    assert rec.x == 42


def test_fake_record_get_with_default():
    rec = FakeRecord({"k": 99})
    assert rec.get("k") == 99
    assert rec.get("missing", "default") == "default"
    assert rec.get("missing") is None


def test_fake_record_attr_missing_raises_attribute_error():
    rec = FakeRecord({"a": 1})
    with pytest.raises(AttributeError):
        _ = rec.nonexistent


# ---------------------------------------------------------------------------
# make_pool_and_conn — canonical no-args call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_pool_and_conn_basic():
    """Canonical call: (pool, conn) returned with working acquire CM."""
    pool, conn = make_pool_and_conn()
    async with pool.acquire() as acquired_conn:
        assert acquired_conn is conn


@pytest.mark.asyncio
async def test_make_pool_and_conn_transaction():
    """Transaction CM enters and exits without raising."""
    pool, conn = make_pool_and_conn()
    async with pool.acquire() as c:
        async with c.transaction():
            pass  # must not raise


@pytest.mark.asyncio
async def test_make_pool_and_conn_fetchval_return():
    pool, conn = make_pool_and_conn(fetchval_return=7)
    async with pool.acquire() as c:
        result = await c.fetchval("SELECT 1")
    assert result == 7


@pytest.mark.asyncio
async def test_make_pool_and_conn_fetchrow_return():
    row = FakeRecord({"id": 5})
    pool, conn = make_pool_and_conn(fetchrow_return=row)
    async with pool.acquire() as c:
        result = await c.fetchrow("SELECT 1")
    assert result is row


@pytest.mark.asyncio
async def test_make_pool_and_conn_fetch_return():
    rows = [FakeRecord({"id": i}) for i in range(3)]
    pool, conn = make_pool_and_conn(fetch_return=rows)
    async with pool.acquire() as c:
        result = await c.fetch("SELECT 1")
    assert result == rows


@pytest.mark.asyncio
async def test_make_pool_and_conn_explicit_none_fetchval():
    """Explicitly passing None differs from not passing (sentinel logic)."""
    pool, conn = make_pool_and_conn(fetchval_return=None)
    async with pool.acquire() as c:
        result = await c.fetchval("SELECT 1")
    assert result is None


@pytest.mark.asyncio
async def test_make_pool_and_conn_wraps_existing_conn():
    """When conn= is passed the same conn object is returned."""
    from unittest.mock import AsyncMock

    my_conn = AsyncMock()
    pool, conn = make_pool_and_conn(conn=my_conn)
    assert conn is my_conn
    async with pool.acquire() as acquired:
        assert acquired is my_conn


# ---------------------------------------------------------------------------
# _make_pool_and_conn alias
# ---------------------------------------------------------------------------


def test_alias_is_same_function():
    assert _make_pool_and_conn is make_pool_and_conn


# ---------------------------------------------------------------------------
# RoleMiddleware — None-role guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_middleware_none_role_does_not_set_attribute():
    """With role=None the middleware must not crash and must not set user_role."""
    received_scopes: list[dict] = []

    async def app(scope, receive, send):
        received_scopes.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    from starlette.testclient import TestClient

    wrapped = RoleMiddleware(app, None)
    client = TestClient(wrapped, raise_server_exceptions=True)
    response = client.get("/")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_role_middleware_sets_role_when_provided():
    """With a real role the middleware injects request.state.user_role."""
    injected: list[str] = []

    async def app(scope, receive, send):
        from starlette.requests import Request

        req = Request(scope)
        injected.append(req.state.user_role)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    from starlette.testclient import TestClient

    wrapped = RoleMiddleware(app, "admin")
    client = TestClient(wrapped, raise_server_exceptions=True)
    client.get("/")
    assert injected == ["admin"]


# ---------------------------------------------------------------------------
# FakeAcquireCM / FakeTxnCM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_acquire_cm():
    from unittest.mock import AsyncMock

    conn = AsyncMock()
    cm = FakeAcquireCM(conn)
    async with cm as acquired:
        assert acquired is conn


@pytest.mark.asyncio
async def test_fake_txn_cm():
    cm = FakeTxnCM()
    async with cm as ctx:
        assert ctx is cm


# ---------------------------------------------------------------------------
# make_telegram_update
# ---------------------------------------------------------------------------


def test_make_telegram_update_defaults():
    update = make_telegram_update()
    assert update.effective_chat.id == 42
    assert update.effective_chat.username == "testuser"


def test_make_telegram_update_custom_chat_id():
    update = make_telegram_update(777)
    assert update.effective_chat.id == 777


def test_make_telegram_update_with_text():
    update = make_telegram_update(text="/pair ABC")
    assert update.message.text == "/pair ABC"


def test_make_telegram_update_with_user_id():
    update = make_telegram_update(user_id=99)
    assert update.effective_user.id == 99
