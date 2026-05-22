"""Phase 2 WS-2F: first-run setup wizard router unit tests.

Mocks the DB pool (asyncpg-shaped) so the suite runs without Docker. Exercises
status / system-check / SMTP / first-admin / cloud-LLM-keys endpoints plus the
``require_unconfigured_or_admin`` access-control gate.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import paper_ingestion.routers.setup as setup_router
import pytest
from fastapi import HTTPException, Response
from httpx import ASGITransport
from jarvis_common.testing import make_pool_and_conn

# ---------------------------------------------------------------------------
# Module-wide: disable the SlowAPI limiter so direct-call (non-ASGI) tests
# that pass SimpleNamespace request objects are not rejected by the decorator.
# The ASGI rate-limit tests re-enable it inside their own fixture.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_limiter_for_direct_call_tests():
    """Disable the rate limiter for all direct-call tests in this module.

    Direct-call tests (calling handler functions directly with SimpleNamespace
    mock requests) cannot satisfy SlowAPI's isinstance(request, Request) check.
    The ASGI rate-limit tests override this by re-enabling the limiter inside
    their own fixture.
    """
    from paper_ingestion.deps import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original


# ---------------------------------------------------------------------------
# Pool / request fixtures
# ---------------------------------------------------------------------------


# Keep local: setup router accesses request.app.state.db_pool/qdrant_client/http_client
# which jarvis_common.make_request does not populate on app.state.
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


# Collapsed (Phase C): test_status_returns_unconfigured_when_no_admins
# Survivor: test_setup_contract.py::test_a131_status_unconfigured_when_no_admin

# Collapsed (Phase C): test_status_returns_configured_when_admin_exists
# Survivor: test_setup_contract.py::test_a131_status_configured_when_admin_exists


@pytest.mark.asyncio
async def test_status_fail_open_when_db_explodes() -> None:
    """A DB outage during status MUST NOT 500 — return unconfigured so the
    operator can recover via the wizard / system-check endpoint."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("pool dead"))
    pool, _ = make_pool_and_conn(conn=conn)
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
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    # No exception means "allowed".
    await setup_router.require_unconfigured_or_admin(request)


@pytest.mark.asyncio
async def test_require_dep_allows_admin_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool, user_role="admin")

    await setup_router.require_unconfigured_or_admin(request)


@pytest.mark.asyncio
async def test_require_dep_rejects_non_admin_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool, user_role="user")

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.require_unconfigured_or_admin(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_dep_rejects_anon_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)  # no user_role on state

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.require_unconfigured_or_admin(request)
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# /api/setup/admin — first-admin atomicity
# ---------------------------------------------------------------------------


# Collapsed (Phase C): test_create_first_admin_inserts_user_and_session_and_sets_cookie
# Survivor: test_setup_contract.py::test_a135_create_first_admin_inserts_user_and_session

# Collapsed (Phase C): test_create_first_admin_rejects_when_admin_already_exists
# Survivor: test_setup_contract.py::test_a135_create_admin_409_when_admin_already_exists


@pytest.mark.asyncio
async def test_create_first_admin_rejects_existing_email() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetchrow = AsyncMock(return_value={"id": 1, "deleted_at": None})
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)
    response = Response()

    body = setup_router.AdminBody(email="dup@example.com")
    with pytest.raises(HTTPException) as exc_info:
        await setup_router.create_first_admin(body, request, response)
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# /api/setup/smtp — test_send is mocked
# ---------------------------------------------------------------------------


# Collapsed (Phase C): test_smtp_save_persists_config_rows
# Survivor: test_setup_contract.py::test_a134_smtp_post_persists_to_db


@pytest.mark.asyncio
async def test_smtp_test_send_invokes_aiosmtplib(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    from jarvis_common.crypto import _load_fernet  # noqa: PLC0415

    _load_fernet.cache_clear()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()
    pool, _ = make_pool_and_conn(conn=conn)
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
    pool, _ = make_pool_and_conn(conn=conn)
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
# GET /api/setup/smtp — masked read, admin-gated
# ---------------------------------------------------------------------------


def _smtp_rows(*, with_password: bool) -> list[dict]:
    rows = [
        {"key": "smtp.host", "value": "smtp.example.com", "encrypted_value": None},
        {"key": "smtp.port", "value": 587, "encrypted_value": None},
        {"key": "smtp.user", "value": "mailer", "encrypted_value": None},
        {"key": "smtp.from", "value": "noreply@example.com", "encrypted_value": None},
    ]
    if with_password:
        rows.append({"key": "smtp.pass", "value": None, "encrypted_value": b"gAAA-ciphertext"})
    return rows


@pytest.mark.asyncio
async def test_get_smtp_config_returns_masked_shape_when_unconfigured() -> None:
    """Bootstrap mode (no admins): GET is reachable and never leaks the password.

    Response shape must equal the frontend SmtpConfig type:
    {host, port, user, from_email, has_password}.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)  # no admins → bootstrap, gate open
    conn.fetch = AsyncMock(return_value=_smtp_rows(with_password=True))
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    res = await setup_router.get_smtp_config(request)

    assert res.host == "smtp.example.com"
    assert res.port == 587
    assert res.user == "mailer"
    assert res.from_email == "noreply@example.com"
    assert res.has_password is True
    # The plaintext / ciphertext password must never appear on the response.
    dumped = res.model_dump()
    assert set(dumped) == {
        "host",
        "port",
        "user",
        "from_email",
        "has_password",
        "restart_required",
    }
    assert "password" not in dumped
    assert "gAAA-ciphertext" not in str(dumped)
    # UI-1: the sender now resolves SMTP from user_config at send time
    # (jarvis_common.email._effective_smtp), so no restart is needed.
    assert res.restart_required is False


@pytest.mark.asyncio
async def test_get_smtp_config_has_password_false_when_no_pass_row() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=_smtp_rows(with_password=False))
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    res = await setup_router.get_smtp_config(request)

    assert res.has_password is False
    assert res.host == "smtp.example.com"


@pytest.mark.asyncio
async def test_get_smtp_config_empty_when_nothing_persisted() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[])
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    res = await setup_router.get_smtp_config(request)

    assert res.host is None
    assert res.port is None
    assert res.user is None
    assert res.from_email is None
    assert res.has_password is False


@pytest.mark.parametrize(
    ("user_role", "expected_status"),
    [
        pytest.param("admin", 200, id="admin_200"),
        pytest.param("user", 403, id="non_admin_403"),
        pytest.param(None, 403, id="anon_403"),
    ],
)
@pytest.mark.asyncio
async def test_get_smtp_config_by_role(user_role, expected_status) -> None:
    """get_smtp_config enforces auth-tier: admin allowed, non-admin and anon rejected."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # admin exists → gate locked down
    conn.fetch = AsyncMock(return_value=_smtp_rows(with_password=True))
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool, user_role=user_role)

    if expected_status == 200:
        res = await setup_router.get_smtp_config(request)
        assert res.has_password is True
    else:
        with pytest.raises(HTTPException) as exc_info:
            await setup_router.get_smtp_config(request)
        assert exc_info.value.status_code == expected_status


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
    pool, _ = make_pool_and_conn(conn=conn)
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
    pool, _ = make_pool_and_conn(conn=conn)
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
    pool, _ = make_pool_and_conn(conn=conn)

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


# ---------------------------------------------------------------------------
# H16: per-endpoint rate limits on setup router
#
# Two complementary strategies:
# 1. Structural: verify each handler is registered in limiter._route_limits.
#    Used for body-bearing endpoints where SlowAPI's __globals__ issue prevents
#    FastAPI from resolving Pydantic annotations via the ASGI stack
#    (setup.py uses ``from __future__ import annotations``).
# 2. ASGI live 429: used only for system_check (no body param).
# ---------------------------------------------------------------------------


def _make_setup_pool_conn() -> tuple[MagicMock, AsyncMock]:
    """Return a (pool, conn) pair wired up for setup router calls."""
    conn: AsyncMock = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def setup_app_fixture():
    """ASGI app fixture for setup-router rate-limit tests.

    Re-enables the limiter (the autouse _disable_limiter_for_direct_call_tests
    fixture disables it module-wide; we need it on for ASGI rate-limit tests).
    Bypasses API-key auth and wires a mock DB pool.
    """
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    mock_pool, conn = _make_setup_pool_conn()
    app.state.db_pool = mock_pool
    app.state.http_client = AsyncMock()
    app.state.qdrant_client = None

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None

    # Re-enable so rate-limit decorators are exercised through the ASGI stack.
    limiter.enabled = True

    yield app, conn

    limiter.enabled = False  # autouse fixture restores to original on teardown
    app.dependency_overrides.clear()


def _unique_ip() -> str:
    """Generate a unique 10.x.x.x IP for per-test SlowAPI bucket isolation."""
    raw = uuid.uuid4().int & 0xFFFFFF  # 24 bits
    a = (raw >> 16) & 0xFF
    b = (raw >> 8) & 0xFF
    c = raw & 0xFF
    return f"10.{a}.{b}.{c}"


@pytest.mark.parametrize(
    "handler_name,expected_limit",
    [
        ("system_check", "10 per 1 minute"),
        ("get_smtp_config", "30 per 1 minute"),
        ("configure_smtp", "10 per 1 minute"),
        ("create_first_admin", "3 per 1 minute"),
        ("configure_cloud_llm_keys", "10 per 1 minute"),
        ("configure_telegram_bot_token", "10 per 1 minute"),
        ("get_telegram_bot_token_status", "30 per 1 minute"),
        ("configure_setup_mode", "10 per 1 minute"),
    ],
)
def test_setup_handlers_registered_in_limiter(handler_name: str, expected_limit: str) -> None:
    """H16 (structural): each setup handler is registered in limiter._route_limits.

    setup.py uses ``from __future__ import annotations`` which stringifies all
    type annotations.  SlowAPI's ``@functools.wraps`` wrapper copies
    ``__globals__`` from slowapi.extension, not from setup.py, so FastAPI
    cannot resolve Pydantic body types via ASGI for body-bearing endpoints.
    The structural check verifies the decorator is present and registered.
    """
    from paper_ingestion.deps import limiter
    from paper_ingestion.routers import setup as setup_mod

    handler_func = getattr(setup_mod, handler_name)
    route_key = f"{handler_func.__module__}.{handler_func.__name__}"
    assert route_key in limiter._route_limits, (
        f"Handler {handler_name!r} (key {route_key!r}) not found in "
        "limiter._route_limits. The @limiter.limit decorator may be missing."
    )
    registered_limits = [str(lim.limit) for lim in limiter._route_limits[route_key]]
    assert any(expected_limit in lim for lim in registered_limits), (
        f"Expected limit {expected_limit!r} not found for {handler_name!r}. "
        f"Registered limit strings: {registered_limits}"
    )


@pytest.mark.asyncio
async def test_system_check_returns_429_after_threshold(setup_app_fixture) -> None:
    """H16 (ASGI): system_check fires 429 after 10 requests from the same IP.

    system_check has no Pydantic body parameter so FastAPI can resolve its
    annotations correctly through the ASGI stack.  One live end-to-end 429
    test for the setup router.
    """
    app, conn = setup_app_fixture
    conn.execute = AsyncMock()
    http_resp_mock = AsyncMock()
    http_resp_mock.status_code = 200
    app.state.http_client.get = AsyncMock(return_value=http_resp_mock)

    ip = _unique_ip()
    headers = {"X-Forwarded-For": ip}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for i in range(10):
            resp = await client.post("/api/setup/system-check", headers=headers)
            assert resp.status_code != 429, (
                f"system_check was unexpectedly rate-limited on call {i + 1}/10"
            )

        resp = await client.post("/api/setup/system-check", headers=headers)
        assert resp.status_code == 429, (
            f"Expected 429 on call 11 to /api/setup/system-check, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# UI-2: cloud-LLM key re-push (no restart)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_llm_keys_repushes_active_cloud_alias(monkeypatch) -> None:
    """Saving a key for a provider that is the active alias pushes it live."""
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    from jarvis_common.crypto import _load_fernet  # noqa: PLC0415

    _load_fernet.cache_clear()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)  # bootstrap, gate open
    conn.execute = AsyncMock()
    # llm.smart_model routes to an Anthropic cloud model; others absent.
    conn.fetchrow = AsyncMock(
        side_effect=lambda *a, **k: (
            {"value": "anthropic/claude-sonnet-4-5"} if a[1] == "llm.smart_model" else None
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    called: list[tuple] = []

    async def _fake_update(alias_key, model_id, db_pool=None):
        called.append((alias_key, model_id))
        return True

    reloaded: list[bool] = []

    async def _fake_reload():
        reloaded.append(True)
        return True

    import paper_ingestion.services.litellm_config as llc  # noqa: PLC0415

    monkeypatch.setattr(llc, "update_litellm_model", _fake_update)
    monkeypatch.setattr(llc, "reload_litellm", _fake_reload)

    body = setup_router.CloudLlmKeysBody(anthropic="sk-ant-newkey")
    res = await setup_router.configure_cloud_llm_keys(body, request)

    assert res.saved_providers == ["anthropic"]
    assert res.applied_now == ["anthropic"]
    assert res.restart_required is False
    assert called == [("llm.smart_model", "anthropic/claude-sonnet-4-5")]
    assert reloaded == [True]


@pytest.mark.asyncio
async def test_cloud_llm_keys_repush_failure_sets_restart_required(monkeypatch) -> None:
    """A RuntimeError from the live push does not bubble; restart_required=True."""
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    from jarvis_common.crypto import _load_fernet  # noqa: PLC0415

    _load_fernet.cache_clear()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=lambda *a, **k: (
            {"value": "anthropic/claude-sonnet-4-5"} if a[1] == "llm.smart_model" else None
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    async def _boom(alias_key, model_id, db_pool=None):
        raise RuntimeError("LiteLLM /config/update failed")

    import paper_ingestion.services.litellm_config as llc  # noqa: PLC0415

    monkeypatch.setattr(llc, "update_litellm_model", _boom)
    monkeypatch.setattr(llc, "reload_litellm", AsyncMock(return_value=True))

    body = setup_router.CloudLlmKeysBody(anthropic="sk-ant-newkey")
    res = await setup_router.configure_cloud_llm_keys(body, request)

    assert res.saved_providers == ["anthropic"]
    assert res.applied_now == []
    assert res.restart_required is True


@pytest.mark.asyncio
async def test_cloud_llm_keys_no_repush_when_no_active_cloud_alias(monkeypatch) -> None:
    """Saving a key with no matching active alias persists but does not push."""
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    from jarvis_common.crypto import _load_fernet  # noqa: PLC0415

    _load_fernet.cache_clear()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()
    # All aliases are local Ollama models — no cloud prefix.
    conn.fetchrow = AsyncMock(return_value={"value": "mistral-nemo"})
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    import paper_ingestion.services.litellm_config as llc  # noqa: PLC0415

    monkeypatch.setattr(
        llc, "update_litellm_model", AsyncMock(side_effect=AssertionError("must not push"))
    )
    monkeypatch.setattr(llc, "reload_litellm", AsyncMock(return_value=True))

    body = setup_router.CloudLlmKeysBody(openai="sk-openai")
    res = await setup_router.configure_cloud_llm_keys(body, request)

    assert res.saved_providers == ["openai"]
    assert res.applied_now == []
    assert res.restart_required is False


# ---------------------------------------------------------------------------
# UI-4: Telegram bot token endpoints
# ---------------------------------------------------------------------------

_VALID_TG_TOKEN = "123456789:AAFakeTokenSecret_abcdefghij-KLMNOP"


@pytest.mark.asyncio
async def test_telegram_token_persists_encrypted(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    from jarvis_common.crypto import _load_fernet  # noqa: PLC0415

    _load_fernet.cache_clear()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    body = setup_router.TelegramBotTokenBody(token=_VALID_TG_TOKEN)
    res = await setup_router.configure_telegram_bot_token(body, request)

    assert res.saved is True
    assert res.restart_required is True
    assert conn.execute.await_count == 1
    # Encrypted path → ciphertext (BYTEA), not the plaintext token.
    args = conn.execute.call_args.args
    stored = args[2]  # $2 = encrypted_value
    assert isinstance(stored, bytes)
    assert _VALID_TG_TOKEN.encode() not in stored


@pytest.mark.asyncio
async def test_telegram_token_invalid_format_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
        setup_router.TelegramBotTokenBody(token="not-a-valid-token-but-long-enough")


@pytest.mark.asyncio
async def test_telegram_token_status_masked_true_false() -> None:
    # has_token True when an encrypted row exists.
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetchrow = AsyncMock(return_value={"value": None, "encrypted_value": b"gAAA-cipher"})
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    res = await setup_router.get_telegram_bot_token_status(request)
    assert res.has_token is True
    dumped = res.model_dump()
    assert set(dumped) == {"has_token"}
    assert "gAAA-cipher" not in str(dumped)

    # has_token False when no row.
    conn.fetchrow = AsyncMock(return_value=None)
    res2 = await setup_router.get_telegram_bot_token_status(request)
    assert res2.has_token is False


@pytest.mark.asyncio
async def test_telegram_token_rejected_for_non_admin_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # admins exist
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool, user_role="user")

    body = setup_router.TelegramBotTokenBody(token=_VALID_TG_TOKEN)
    with pytest.raises(HTTPException) as exc_info:
        await setup_router.configure_telegram_bot_token(body, request)
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# UI-5: single↔multi mode toggle
# ---------------------------------------------------------------------------


# Collapsed (Phase C): test_setup_mode_persists
# Survivor: test_setup_contract.py::test_a139_setup_mode_persisted_to_db
# SQL-arg assertions (args[1]=="setup.mode", args[2]=="multi") — B1-09 class.
# Contract A139 verifies mode+restart_required in response and value in DB.


@pytest.mark.asyncio
async def test_setup_mode_enum_violation_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
        setup_router.SetupModeBody(mode="hybrid")


@pytest.mark.asyncio
async def test_setup_mode_rejected_for_non_admin_when_configured() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # admins exist
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool, user_role="user")

    body = setup_router.SetupModeBody(mode="single")
    with pytest.raises(HTTPException) as exc_info:
        await setup_router.configure_setup_mode(body, request)
    assert exc_info.value.status_code == 403
