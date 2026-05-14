"""Phase 2 WS-2F: first-run setup wizard router unit tests.

Mocks the DB pool (asyncpg-shaped) so the suite runs without Docker. Exercises
status / system-check / SMTP / first-admin / cloud-LLM-keys endpoints plus the
``require_unconfigured_or_admin`` access-control gate.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import paper_ingestion.routers.setup as setup_router
import pytest
from fastapi import HTTPException, Response

# ---------------------------------------------------------------------------
# Pool / request fixtures
# ---------------------------------------------------------------------------


def _build_mock_pool(conn: AsyncMock) -> MagicMock:
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


def _build_request(
    pool: MagicMock,
    *,
    user_role: str | None = None,
    qdrant: MagicMock | None = None,
    http: MagicMock | None = None,
) -> SimpleNamespace:
    state = SimpleNamespace(
        db_pool=pool,
        qdrant_client=qdrant,
        http_client=http,
    )
    if user_role is not None:
        state.user_role = user_role

    app = SimpleNamespace(state=state)
    return SimpleNamespace(
        app=app,
        state=state,
        cookies={},
    )


# ---------------------------------------------------------------------------
# /api/setup/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_unconfigured_when_no_admins() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    res = await setup_router.get_status(request)
    assert res.configured is False


@pytest.mark.asyncio
async def test_status_returns_configured_when_admin_exists() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    res = await setup_router.get_status(request)
    assert res.configured is True


@pytest.mark.asyncio
async def test_status_fail_open_when_db_explodes() -> None:
    """A DB outage during status MUST NOT 500 — return unconfigured so the
    operator can recover via the wizard / system-check endpoint."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("pool dead"))
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    res = await setup_router.get_status(request)
    assert res.configured is False


# ---------------------------------------------------------------------------
# require_unconfigured_or_admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_dep_allows_when_no_admins() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    # No exception means "allowed".
    await setup_router.require_unconfigured_or_admin(request)


@pytest.mark.asyncio
async def test_require_dep_allows_admin_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_role="admin")

    await setup_router.require_unconfigured_or_admin(request)


@pytest.mark.asyncio
async def test_require_dep_rejects_non_admin_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_role="user")

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.require_unconfigured_or_admin(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_dep_rejects_anon_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool = _build_mock_pool(conn)
    request = _build_request(pool)  # no user_role on state

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.require_unconfigured_or_admin(request)
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# /api/setup/admin — first-admin atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_first_admin_inserts_user_and_session_and_sets_cookie() -> None:
    conn = AsyncMock()
    session_uuid = uuid.uuid4()

    # fetchval: first call = admin count (0); second call = INSERT sessions RETURNING id.
    conn.fetchval = AsyncMock(side_effect=[0, session_uuid])
    # fetchrow: first = SELECT existing user (None); second = INSERT users RETURNING ...
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,
            {"id": 42, "email": "owner@example.com", "role": "admin"},
        ]
    )
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()

    body = setup_router.AdminBody(email="owner@example.com")
    res = await setup_router.create_first_admin(body, request, response)

    assert res.id == 42
    assert res.email == "owner@example.com"
    assert res.role == "admin"

    # Cookie must be set on the response.
    set_cookie_headers = [v for k, v in response.raw_headers if k.lower() == b"set-cookie"]
    assert any(b"jarvis_session=" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_create_first_admin_rejects_when_admin_already_exists() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # admin count > 0
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()

    body = setup_router.AdminBody(email="late@example.com")
    with pytest.raises(HTTPException) as exc_info:
        await setup_router.create_first_admin(body, request, response)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_create_first_admin_rejects_existing_email() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetchrow = AsyncMock(return_value={"id": 1, "deleted_at": None})
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()

    body = setup_router.AdminBody(email="dup@example.com")
    with pytest.raises(HTTPException) as exc_info:
        await setup_router.create_first_admin(body, request, response)
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# /api/setup/smtp — test_send is mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_save_persists_config_rows(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    # Clear the LRU cache so the new key is picked up.
    from jarvis_common.crypto import _load_fernet  # noqa: PLC0415

    _load_fernet.cache_clear()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)  # no admins yet → bypass auth
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        user="user",
        from_email="from@example.com",
        test_send=False,
        **{"pass": "p4ss"},
    )
    res = await setup_router.configure_smtp(body, request)
    assert res.saved is True
    assert res.test_sent is None

    # 5 INSERTs: host, port, user, from, pass.
    assert conn.execute.await_count == 5


@pytest.mark.asyncio
async def test_smtp_test_send_invokes_aiosmtplib(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    from jarvis_common.crypto import _load_fernet  # noqa: PLC0415

    _load_fernet.cache_clear()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    sent_calls: list[dict] = []

    async def _fake_send(message, **kw):
        sent_calls.append(kw)

    fake_aiosmtplib = SimpleNamespace(send=_fake_send)
    monkeypatch.setitem(__import__("sys").modules, "aiosmtplib", fake_aiosmtplib)

    body = setup_router.SmtpBody(
        host="smtp.test",
        port=465,
        from_email="from@example.com",
        test_send=True,
        test_recipient="dst@example.com",
    )
    res = await setup_router.configure_smtp(body, request)
    assert res.test_sent is True
    assert res.test_error is None
    assert len(sent_calls) == 1
    assert sent_calls[0]["hostname"] == "smtp.test"
    assert sent_calls[0]["port"] == 465
    # Port 465 → implicit TLS; not start_tls.
    assert sent_calls[0]["use_tls"] is True
    assert sent_calls[0]["start_tls"] is False


@pytest.mark.asyncio
async def test_smtp_test_send_failure_returns_error_string(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    from jarvis_common.crypto import _load_fernet  # noqa: PLC0415

    _load_fernet.cache_clear()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    async def _boom(*args, **kw):
        raise RuntimeError("relay refused")

    fake_aiosmtplib = SimpleNamespace(send=_boom)
    monkeypatch.setitem(__import__("sys").modules, "aiosmtplib", fake_aiosmtplib)

    body = setup_router.SmtpBody(
        host="smtp.test",
        port=587,
        from_email="from@example.com",
        test_send=True,
    )
    res = await setup_router.configure_smtp(body, request)
    assert res.test_sent is False
    assert "relay refused" in (res.test_error or "")


# ---------------------------------------------------------------------------
# /api/setup/cloud-llm-keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_llm_keys_persists_only_provided(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    from jarvis_common.crypto import _load_fernet  # noqa: PLC0415

    _load_fernet.cache_clear()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    body = setup_router.CloudLlmKeysBody(openai="sk-x", gemini=None, anthropic="   ")
    res = await setup_router.configure_cloud_llm_keys(body, request)
    # Only openai persisted (anthropic is whitespace-only, gemini is None).
    assert res.saved_providers == ["openai"]
    assert conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_cloud_llm_keys_rejected_for_non_admin_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # admins exist
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_role="user")

    body = setup_router.CloudLlmKeysBody(openai="sk-x")
    with pytest.raises(HTTPException) as exc_info:
        await setup_router.configure_cloud_llm_keys(body, request)
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# _persist_config — H3 regression: no json.dumps double-encode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_config_passes_value_directly_not_json_string() -> None:
    """asyncpg's JSONB codec (registered via init_pg_connection) serialises values
    automatically.  _persist_config MUST pass the raw Python object, not a
    json.dumps()-encoded string.  Wrapping with json.dumps would double-encode,
    storing '"{\"k\": 1}"' instead of '{"k": 1}' in the JSONB column.

    Regression guard for H3 (audit closeout 2026-05-14).
    """
    conn = AsyncMock()
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)

    dict_value = {"smtp_host": "mail.example.com", "port": 587}

    await setup_router._persist_config(pool, "smtp.config", dict_value, encrypted=False)

    assert conn.execute.await_count == 1
    call_args = conn.execute.call_args
    # The value passed as the second positional parameter ($2) must be the
    # original dict, NOT a JSON-encoded string.
    positional_args = call_args.args
    passed_value = positional_args[2]  # $1=key, $2=value
    assert isinstance(passed_value, dict), (
        f"_persist_config must pass the raw dict to asyncpg (got {type(passed_value).__name__!r}); "
        "json.dumps double-encodes and corrupts user_config.value"
    )
    assert passed_value == dict_value
