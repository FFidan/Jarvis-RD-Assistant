"""Tests for settings, nudges, sources, and analytics endpoints.

Covers:
- Config: GET /api/config, GET /api/config/{key}, PUT /api/config/{key}
- Nudges: GET /api/nudges, PUT /api/nudges/{id}
- Sources: GET /api/sources, PUT /api/sources/{id}
- Analytics: GET /api/analytics/papers-by-source, GET /api/analytics/papers-by-status
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(UTC)


def _make_lenient_require_admin():
    """Return an async require_admin stub: allow unset role, 403 explicit non-admin.

    Legacy settings tests have no session (``request.state.user_role`` unset)
    and only assert config SQL/response shape — they should pass. Negative
    tests inject an explicit non-admin role and must still receive 403, so the
    stub mirrors the real guard for that case instead of blanket-allowing.
    """
    from fastapi import HTTPException, Request, status

    async def _lenient(request: Request) -> None:
        role = getattr(request.state, "user_role", None)
        if role is not None and role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin role required",
            )

    return _lenient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked DB pool and disabled auth."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import settings as _settings_mod

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    # WS-AUTH: settings/config endpoints are admin-gated. Most of these tests
    # predate the strict require_admin guard and have no session at all.
    # Use a faithful stub that grants access when no role is set (legacy
    # session-less callers) but still 403s an *explicit* non-admin role, so
    # the negative tests (which inject role="member") keep exercising the
    # real gate. Covers both Depends(require_admin) routes and the in-body
    # `await require_admin(request)` calls (set_config path).
    _lenient = _make_lenient_require_admin()
    app.dependency_overrides[require_admin] = _lenient
    _orig_require_admin = _settings_mod.require_admin
    _settings_mod.require_admin = _lenient
    yield app, conn, mock_http
    _settings_mod.require_admin = _orig_require_admin
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Tests: Config CRUD
# ---------------------------------------------------------------------------


# Collapsed (E2.PI): test_list_config
# Survivor: test_settings_contract.py::test_list_config_returns_list
# GET /api/config returns list of config entries verified with real DB.


@pytest.mark.asyncio
async def test_get_config_found(_app):
    """GET /api/config/{key} returns the config entry when found."""
    app, conn, _ = _app
    conn.fetchrow.return_value = FakeRecord(key="llm.smart_model", value="mistral-nemo")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/config/llm.smart_model")

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "llm.smart_model"
    assert body["value"] == "mistral-nemo"


# Collapsed (Phase C): test_get_config_not_found
# Survivor: test_settings_contract.py::test_get_config_key_not_found_returns_404
# Exact same 404 assertion exercised with real DB — B1-09 class (fetchrow=None mock).


@pytest.mark.asyncio
async def test_get_personal_config_does_not_leak_system_default_to_non_admin(_app):
    """DOM-A-09: non-admin caller gets 404 when no personal row exists, not the NULL-row default.

    Seed scenario: a NULL-row (system default) exists for a personal key.
    Caller B has user_id=7, non-admin role, and no per-user row.
    Expected: 404, not the system-default value.
    """
    from paper_ingestion.routers import settings

    _app_obj, conn, _ = _app
    # fetchrow returns None when queried by user_id only (no NULL-row fallback in SQL)
    conn.fetchrow.return_value = None

    request = SimpleNamespace(state=SimpleNamespace(user_id=7, user_role="member"))

    # Use a known PERSONAL_KEY so _classify_config_key returns "personal".
    personal_key = "fsrs.desired_retention"

    with pytest.raises(Exception) as exc_info:
        await settings.get_config.__wrapped__(
            request,
            key=personal_key,
            db_pool=_app_obj.state.db_pool,
        )

    from fastapi import HTTPException

    assert isinstance(exc_info.value, HTTPException)
    assert exc_info.value.status_code == 404

    # Verify the SQL sent to the DB does NOT include "user_id IS NULL" fallback.
    call_args = conn.fetchrow.await_args
    assert call_args is not None
    sql_issued = call_args.args[0]
    assert "user_id IS NULL" not in sql_issued, (
        "Non-admin personal-key fetch must not fall back to NULL-row: "
        f"issued SQL was: {sql_issued!r}"
    )


@pytest.mark.asyncio
async def test_admin_still_sees_system_default(_app):
    """DOM-A-09: admin caller gets the NULL-row fallback when no per-user row exists."""
    from paper_ingestion.routers import settings

    _app_obj, conn, _ = _app
    # Admin path: fetchrow returns the NULL-row system default.
    conn.fetchrow.return_value = FakeRecord(
        key="fsrs.desired_retention",
        value='"0.9"',
        encrypted_value=None,
        user_id=None,
    )

    request = SimpleNamespace(state=SimpleNamespace(user_id=99, user_role="admin"))

    personal_key = "fsrs.desired_retention"
    result = await settings.get_config.__wrapped__(
        request,
        key=personal_key,
        db_pool=_app_obj.state.db_pool,
    )

    assert result.key == personal_key

    # Verify the SQL includes the NULL-row fallback clause.
    call_args = conn.fetchrow.await_args
    assert call_args is not None
    sql_issued = call_args.args[0]
    assert "user_id IS NULL" in sql_issued, (
        f"Admin personal-key fetch must include NULL-row fallback: issued SQL was: {sql_issued!r}"
    )


# Collapsed (E2.PI): test_set_config_allowed_key
# Survivor: test_settings_contract.py::test_put_config_string_value_round_trip
# PUT /api/config/{key} persists and returns config value verified with real DB round-trip.


@pytest.mark.asyncio
async def test_set_config_rejects_unpulled_catalog_model(_app):
    """Local catalog models must be pulled before assignment."""
    app, conn, mock_http = _app
    mock_http.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"models": []}),
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/llm.smart_model",
            json={"key": "llm.smart_model", "value": "qwen3:4b"},
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Model not pulled. Pull it first."
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_config_rejects_nonassignable_embedding_candidate(_app):
    """Advanced/future embedding catalog entries cannot be assigned by accident.

    Uses ``mxbai-embed-large`` as the non-assignable test subject — flipped to
    phase=future after qwen3-embedding:4b was promoted to assignable=true on
    2026-05-07. Catalog still ships at least one Ollama-installed but
    intentionally non-assignable embedding entry so this guard remains
    meaningful.
    """
    app, conn, mock_http = _app
    mock_http.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"models": [{"name": "mxbai-embed-large"}]}),
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/llm.embed_model",
            json={"key": "llm.embed_model", "value": "mxbai-embed-large"},
        )

    assert resp.status_code == 422
    assert "not assignable yet" in resp.json()["detail"]
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_config_does_not_persist_when_litellm_update_fails(_app, monkeypatch):
    """Model assignments are atomic: failed LiteLLM update leaves DB unchanged."""
    app, conn, mock_http = _app
    mock_http.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"models": [{"name": "qwen3:4b"}]}),
    )

    async def fail_update(*args, **kwargs):
        raise RuntimeError("LiteLLM config is read-only")

    monkeypatch.setattr("paper_ingestion.routers.settings.update_litellm_model", fail_update)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/llm.smart_model",
            json={"key": "llm.smart_model", "value": "qwen3:4b"},
        )

    assert resp.status_code == 400
    assert "LiteLLM config is read-only" in resp.json()["detail"]
    conn.execute.assert_not_awaited()


async def _write_runtime_config(pool, key: str, value, update_litellm_model_fn):
    from paper_ingestion.services.settings_service import write_config

    return await write_config(
        db_pool=pool,
        scheduler=MagicMock(),
        http_client=AsyncMock(),
        ollama_url="http://ollama",
        key=key,
        value=value,
        caller_user_id=1,
        update_litellm_model_fn=update_litellm_model_fn,
    )


@pytest.mark.asyncio
async def test_dynamic_num_ctx_updates_current_role_model_before_persist(monkeypatch):
    """Per-machine num_ctx writes push the current local role assignment to LiteLLM first."""
    pool, conn = _make_pool_and_conn(
        fetch_return=[FakeRecord(key="llm.smart_model", value="qwen3:14b")]
    )
    calls = []

    async def capture_update(*args, **kwargs):
        calls.append((args, kwargs, conn.execute.await_count))
        return True

    monkeypatch.setattr(
        "paper_ingestion.services.litellm_config.reload_litellm",
        AsyncMock(return_value=True),
    )

    result = await _write_runtime_config(
        pool,
        "llm.host-rtx5060.smart_num_ctx",
        32768,
        capture_update,
    )

    assert result == 32768
    assert len(calls) == 1
    args, kwargs, execute_count = calls[0]
    assert args == ("llm.smart_model", "qwen3:14b")
    assert kwargs["db_pool"] is pool
    assert kwargs["machine_id"] == "host-rtx5060"
    assert kwargs["num_ctx"] == 32768
    assert execute_count == 0
    conn.execute.assert_awaited()


@pytest.mark.asyncio
async def test_dynamic_num_ctx_does_not_persist_when_litellm_reports_no_update(monkeypatch):
    """A local num_ctx write aborts if LiteLLM cannot update the assigned alias."""
    pool, conn = _make_pool_and_conn(
        fetch_return=[FakeRecord(key="llm.smart_model", value="qwen3:14b")]
    )
    update_litellm_model_fn = AsyncMock(return_value=False)
    reload_litellm = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "paper_ingestion.services.litellm_config.reload_litellm",
        reload_litellm,
    )

    with pytest.raises(HTTPException) as exc_info:
        await _write_runtime_config(
            pool,
            "llm.host-rtx5060.smart_num_ctx",
            32768,
            update_litellm_model_fn,
        )

    assert exc_info.value.status_code == 400
    assert "was not updated" in str(exc_info.value.detail)
    update_litellm_model_fn.assert_awaited_once()
    reload_litellm.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_num_ctx_cloud_assignment_persists_without_litellm_update(monkeypatch):
    """Cloud-assigned roles keep num_ctx as UI state without sending it to LiteLLM."""
    pool, conn = _make_pool_and_conn(
        fetch_return=[FakeRecord(key="llm.smart_model", value="openai/gpt-4o")]
    )
    update_litellm_model_fn = AsyncMock(return_value=True)
    reload_litellm = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "paper_ingestion.services.litellm_config.reload_litellm",
        reload_litellm,
    )

    result = await _write_runtime_config(
        pool,
        "llm.host-rtx5060.smart_num_ctx",
        32768,
        update_litellm_model_fn,
    )

    assert result == 32768
    update_litellm_model_fn.assert_not_awaited()
    reload_litellm.assert_not_awaited()
    conn.execute.assert_awaited()


@pytest.mark.asyncio
async def test_litellm_cloud_update_omits_num_ctx_extra_body(monkeypatch):
    """Direct cloud alias updates ignore num_ctx because it is local-Ollama-only."""
    from paper_ingestion.services import litellm_config

    pool, _conn = _make_pool_and_conn(fetchrow_return=FakeRecord(value="sk-test"))
    captured = {}

    async def capture_post_config_update(alias, model_name, api_key, extra_body=None):
        captured.update(
            alias=alias,
            model_name=model_name,
            api_key=api_key,
            extra_body=extra_body,
        )
        return True

    monkeypatch.setattr(litellm_config, "_post_config_update", capture_post_config_update)

    result = await litellm_config.update_litellm_model(
        "llm.smart_model",
        "openai/gpt-4o",
        db_pool=pool,
        machine_id="host-rtx5060",
        num_ctx=32768,
    )

    assert result is True
    assert captured == {
        "alias": "smart",
        "model_name": "openai/gpt-4o",
        "api_key": "sk-test",
        "extra_body": None,
    }


@pytest.mark.asyncio
async def test_thinking_toggle_updates_only_matching_assigned_roles(monkeypatch):
    """Thinking toggles refresh every role assigned to that model on the machine."""
    pool, conn = _make_pool_and_conn(
        fetch_return=[
            FakeRecord(key="llm.smart_model", value="qwen3:14b"),
            FakeRecord(key="llm.fast_model", value="qwen3:14b"),
            FakeRecord(key="llm.embed_model", value="mxbai-embed-large"),
        ]
    )
    calls = []

    async def capture_update(*args, **kwargs):
        calls.append((args, kwargs, conn.execute.await_count))
        return True

    monkeypatch.setattr(
        "paper_ingestion.services.litellm_config.reload_litellm",
        AsyncMock(return_value=True),
    )

    result = await _write_runtime_config(
        pool,
        "llm.host-rtx5060.thinking_disabled.qwen3:14b",
        True,
        capture_update,
    )

    assert result is True
    assert [args for args, _kwargs, _count in calls] == [
        ("llm.smart_model", "qwen3:14b"),
        ("llm.fast_model", "qwen3:14b"),
    ]
    assert all(kwargs["machine_id"] == "host-rtx5060" for _args, kwargs, _count in calls)
    assert all(kwargs["thinking_disabled"] is True for _args, kwargs, _count in calls)
    assert all(execute_count == 0 for _args, _kwargs, execute_count in calls)
    conn.execute.assert_awaited()


@pytest.mark.asyncio
async def test_dynamic_litellm_failure_leaves_db_unchanged(monkeypatch):
    """Dynamic hardware settings preserve LiteLLM-first rollback semantics."""
    from fastapi import HTTPException

    pool, conn = _make_pool_and_conn(
        fetch_return=[FakeRecord(key="llm.smart_model", value="qwen3:14b")]
    )

    async def fail_update(*args, **kwargs):
        raise RuntimeError("LiteLLM dynamic update failed")

    monkeypatch.setattr(
        "paper_ingestion.services.litellm_config.reload_litellm",
        AsyncMock(return_value=True),
    )

    with pytest.raises(HTTPException) as exc_info:
        await _write_runtime_config(
            pool,
            "llm.host-rtx5060.smart_num_ctx",
            32768,
            fail_update,
        )

    assert exc_info.value.status_code == 400
    assert "LiteLLM dynamic update failed" in str(exc_info.value.detail)
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("llm.host-rtx5060.smart_num_ctx", 0, id="num_ctx_zero"),
        pytest.param(
            "llm.host-rtx5060.thinking_disabled.qwen3:14b",
            "true",
            id="thinking_string",
        ),
    ],
)
async def test_invalid_dynamic_litellm_values_return_400(key, value):
    """Dynamic LiteLLM hardware settings keep strict value validation."""
    from fastapi import HTTPException

    pool, conn = _make_pool_and_conn()
    update_litellm_model_fn = AsyncMock(return_value=True)

    with pytest.raises(HTTPException) as exc_info:
        await _write_runtime_config(pool, key, value, update_litellm_model_fn)

    assert exc_info.value.status_code == 400
    update_litellm_model_fn.assert_not_awaited()
    conn.execute.assert_not_awaited()


# Collapsed (E2.PI): test_set_config_disallowed_key
# Survivor: test_settings_contract.py::test_put_config_ghost_key_returns_400
# PUT /api/config/{ghost-key} returns 400 Unknown config key verified with real DB.


# ---------------------------------------------------------------------------
# Tests: Nudges
# ---------------------------------------------------------------------------

# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


# Cluster 1 deletion (2026-05-22): superseded by test_pi_settings_extended_contract.py (S-01..S-08).


@pytest.mark.asyncio
async def test_set_config_invalid_cron_returns_400(_app):
    """PUT /api/config/pulse.cron rejects an invalid cron expression."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/pulse.cron",
            json={"key": "pulse.cron", "value": "not a cron"},
        )

    assert resp.status_code == 400
    assert "cron" in resp.json()["detail"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_key", "bad_value"),
    [
        pytest.param("pulse.weights", {"bad_key": 0.5}, id="weights_wrong_keys"),
        pytest.param("pulse.deck_size", "10", id="deck_size_string"),
    ],
)
async def test_set_config_invalid_value_returns_400(_app, config_key, bad_value):
    """PUT /api/config/{key} rejects invalid values with 400."""
    app, _conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            f"/api/config/{config_key}",
            json={"key": config_key, "value": bad_value},
        )

    assert resp.status_code == 400


# Collapsed (E2.PI): test_set_config_l2_lambda_valid_accepted
# Survivor: test_settings_contract.py::test_put_pulse_l2_lambda_round_trip
# PUT /api/config/pulse.l2_lambda accepts float in [0, 2] and persists — verified with real DB.

# Collapsed (E2.PI): test_set_config_l2_lambda_out_of_range_returns_400
# Survivor: test_settings_contract.py::test_put_pulse_l2_lambda_out_of_range_returns_400
# PUT /api/config/pulse.l2_lambda rejects value > 2.0 — verified with real DB.


# ---------------------------------------------------------------------------
# Setup wizard whitelist (A1)
# ---------------------------------------------------------------------------


# Collapsed (E2.PI): test_set_setup_completed_accepts_bool
# Survivor: test_settings_contract.py::test_put_setup_completed_persists_true
# PUT /api/config/setup.completed accepts and persists true — verified with real DB.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_key", "string_value"),
    [
        pytest.param("setup.completed", "true", id="setup_completed_string"),
        pytest.param("telegram.owner_chat_id", "123", id="telegram_chat_id_string"),
    ],
)
async def test_set_config_rejects_wrong_type_string(_app, config_key, string_value):
    """PUT /api/config/{key} rejects a string value where a non-string type is required."""
    app, _conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            f"/api/config/{config_key}",
            json={"key": config_key, "value": string_value},
        )

    assert resp.status_code == 400


# Collapsed (E2.PI): test_set_telegram_owner_chat_id_accepts_int
# Survivor: test_settings_contract.py::test_put_telegram_owner_chat_id_round_trip
# PUT /api/config/telegram.owner_chat_id accepts integer and persists — verified with real DB.

# Collapsed (E2.PI): test_set_telegram_owner_chat_id_accepts_none
# Survivor: test_settings_contract.py::test_put_telegram_owner_chat_id_null_clears
# PUT /api/config/telegram.owner_chat_id accepts null and clears value — verified with real DB.


# ---------------------------------------------------------------------------
# WEB-C01: no double-encoding of user_config JSONB values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_round_trip_string_no_double_encode(_app):
    """PUT /api/config/pulse.cron must pass raw value to asyncpg, not json.dumps(value).

    Before the WEB-C01 fix, set_config called json.dumps(body.value) before passing
    to asyncpg, which itself has the JSONB codec registered.  This caused the cron
    expression to be stored as '\"0 4 * * *\"' (double-encoded) instead of
    '"0 4 * * *"', breaking croniter parsing in the dashboard Settings editor.

    This test verifies:
    1. The PUT response echoes the original string (not a double-encoded form).
    2. The value passed to conn.execute is the raw Python string, not a JSON string.
    3. A GET round-trip returns the string unchanged.
    4. croniter can parse the returned cron expression without error.
    """
    cron_expr = "0 4 * * *"
    app, conn, _ = _app

    # --- PUT ---
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        put_resp = await client.put(
            "/api/config/pulse.cron",
            json={"key": "pulse.cron", "value": cron_expr},
        )

    assert put_resp.status_code == 200
    put_body = put_resp.json()
    assert put_body["value"] == cron_expr, (
        f"PUT response value {put_body['value']!r} != expected {cron_expr!r} — "
        "double-encode bug may still be present"
    )

    # Verify the value forwarded to asyncpg execute is the raw Python string,
    # NOT json.dumps("0 4 * * *") == '"0 4 * * *"'.
    assert conn.execute.called, "conn.execute was not called"
    # Use call_args_list[0]: set_config now emits a log_event after the UPSERT,
    # so call_args may point to the log_event INSERT. The first call is always the UPSERT.
    _call_args = conn.execute.call_args_list[0]
    positional_args = _call_args.args if _call_args.args else _call_args[0]
    # positional_args: (sql, user_id, key, value) for scoped config writes
    stored_value = positional_args[3]
    assert stored_value == cron_expr, (
        f"asyncpg received {stored_value!r} instead of raw {cron_expr!r} — "
        "json.dumps double-encode bug is still present in set_config"
    )
    assert not stored_value.startswith('"'), (
        f"asyncpg received a JSON-encoded string {stored_value!r}; "
        "the JSONB codec should handle encoding, not the router"
    )

    # --- GET round-trip (mocked fetchrow returns the raw value as asyncpg would) ---
    conn.fetchrow.return_value = FakeRecord(key="pulse.cron", value=cron_expr)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        get_resp = await client.get("/api/config/pulse.cron")

    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["value"] == cron_expr, (
        f"GET returned {get_body['value']!r}; expected {cron_expr!r}"
    )

    # --- croniter must parse the returned expression without error ---
    try:
        from datetime import datetime as _datetime

        from croniter import croniter

        parsed = croniter(get_body["value"], _datetime.now())
        next_run = parsed.get_next(_datetime)
        assert next_run is not None, "croniter could not compute next run from returned cron value"
    except ModuleNotFoundError:
        # croniter is not installed on the host; this assertion runs in Docker.
        pass


# ---------------------------------------------------------------------------
# SEC-105: test_provider rate limiting + error body sanitization
# ---------------------------------------------------------------------------


def _make_provider_app():
    """Build a test app with auth bypassed but rate limiter ENABLED."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = True
    app.state.limiter.reset()

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    return app, conn


@pytest.mark.asyncio
async def test_test_provider_rate_limited_after_5_calls():
    """POST /api/providers/{provider}/test returns 429 after 5 calls per minute (SEC-105)."""
    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn()
    # No API key configured → returns ok=False quickly (no external HTTP needed)
    conn.fetchrow.return_value = None

    app.state.db_pool = pool
    app.state.limiter.enabled = True
    app.state.limiter.reset()

    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            responses = []
            for _ in range(6):
                resp = await client.post("/api/providers/anthropic/test")
                responses.append(resp.status_code)

        # First 5 must succeed (200), 6th must be rate-limited (429)
        assert responses[:5] == [200] * 5, f"Expected 5×200 but got: {responses[:5]}"
        assert responses[5] == 429, f"Expected 429 on 6th call but got: {responses[5]}"
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = False
        app.state.limiter.reset()


@pytest.mark.asyncio
async def test_test_provider_error_response_does_not_leak_upstream_body():
    """POST /api/providers/{provider}/test sanitizes upstream error body (SEC-105).

    When the upstream provider returns a non-2xx response, the error field must
    contain a generic message like 'provider returned HTTP <status>' rather than
    any portion of the upstream response body (which could contain sensitive
    diagnostic information).
    """
    from unittest.mock import AsyncMock, MagicMock

    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn()
    # Simulate a stored (plaintext legacy) API key so the HTTP probe fires
    conn.fetchrow.return_value = FakeRecord(
        key="llm.anthropic.api_key",
        value="sk-test-key",
        encrypted_value=None,
    )

    app.state.db_pool = pool
    app.state.limiter.enabled = False

    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None

    # Build a mock httpx response with a sensitive-looking body
    sensitive_body = '{"error": {"message": "Invalid API key sk-test-key — account suspended"}}'
    mock_response = MagicMock()
    mock_response.is_success = False
    mock_response.status_code = 401
    mock_response.text = sensitive_body

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    # Build the ASGI test client BEFORE patching so httpx.AsyncClient itself is not mocked
    # for the outer test call — only the inner call inside the router is intercepted.
    asgi_transport = ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=asgi_transport, base_url="http://test"
        ) as test_client:
            with patch(
                "paper_ingestion.routers.settings.httpx.AsyncClient",
                return_value=mock_client,
            ):
                resp = await test_client.post("/api/providers/anthropic/test")

        assert resp.status_code == 200, f"Expected 200 from endpoint, got {resp.status_code}"
        body = resp.json()
        assert body["ok"] is False
        error_msg = body.get("error", "")
        # Must not leak any portion of the upstream body
        assert "sk-test-key" not in error_msg, "API key leaked in error response"
        assert "suspended" not in error_msg, "Upstream body text leaked in error response"
        assert "Invalid" not in error_msg, "Upstream body text leaked in error response"
        # Must contain the sanitized generic message
        assert "401" in error_msg, f"Expected HTTP status code in error, got: {error_msg!r}"
        assert error_msg == "provider returned HTTP 401", (
            f"Error message not sanitized: {error_msg!r}"
        )
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: A.1 Ghost key deletion — deleted keys return 400  (D5-05)
# ---------------------------------------------------------------------------

# (key, value) pairs for keys removed from the allow-list at various clean-up
# waves.  Each must produce HTTP 400 "Unknown config key".
_GHOST_KEYS = [
    ("paper.max_daily", 10),
    ("paper.auto_generate_cards", True),
    ("ui.page_size", 20),
    ("ingestion.max_papers_per_run", 50),
    ("ingestion.chunk_size", 512),
]


# Collapsed (E2.PI): test_ghost_key_returns_400 (parametrized, 5 ghost keys)
# Survivor: test_settings_contract.py::test_put_config_ghost_key_returns_400
#           + test_settings_contract.py::test_put_config_ghost_key_does_not_write_db
# Removed config keys return 400 Unknown config key — verified with real DB.


# ---------------------------------------------------------------------------
# Tests: A.2 FSRS validators
# ---------------------------------------------------------------------------


# Collapsed (E2.PI): test_fsrs_desired_retention_valid_accepted
# Survivor: test_settings_contract.py::test_put_fsrs_desired_retention_round_trip
# PUT /api/config/fsrs.desired_retention accepts valid float in (0,1) and persists — verified with real DB.

# Collapsed (E2.PI): test_fsrs_desired_retention_invalid_rejected (parametrized, 6 bad values)
# Survivor: test_settings_contract.py::test_put_fsrs_desired_retention_invalid_value_returns_400
# PUT /api/config/fsrs.desired_retention rejects out-of-range and wrong-type values — verified with real DB.


@pytest.mark.asyncio
async def test_fsrs_learning_steps_valid_accepted(_app):
    """PUT /api/config/fsrs.learning_steps accepts a valid [int, int] list."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/fsrs.learning_steps",
            json={"key": "fsrs.learning_steps", "value": [5, 20]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "fsrs.learning_steps"
    assert body["value"] == [5, 20]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_value",
    [
        [1],  # only 1 element
        [1, 2, 3],  # 3 elements
        [0, 10],  # zero is not positive
        [-1, 10],  # negative
        [1.5, 10],  # float, not int
        "1,10",  # string
        {"a": 1},  # dict
    ],
)
async def test_fsrs_learning_steps_invalid_rejected(_app, bad_value):
    """PUT /api/config/fsrs.learning_steps rejects malformed values."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/fsrs.learning_steps",
            json={"key": "fsrs.learning_steps", "value": bad_value},
        )

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DOM-A-12: zotero.poll_cron scheduler-refresh failure rolls back DB write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_config_rolls_back_on_scheduler_failure(monkeypatch):
    """PUT /api/config/zotero.poll_cron rolls back the DB write when scheduler refresh fails.

    Before DOM-A-12 the zotero.poll_cron block swallowed the reschedule exception
    with a logger.warning, leaving the DB updated while the live cron remained stale.
    After the fix, the exception is re-raised and the DB write is undone.
    """
    from unittest.mock import AsyncMock, MagicMock

    import httpx
    from httpx import ASGITransport
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool, get_scheduler
    from paper_ingestion.main import app

    # --- DB mock ---------------------------------------------------------
    pool, conn = _make_pool_and_conn()

    # fetchrow: return the existing cron value so the pre-read captures it
    old_cron = "0 3 * * *"
    conn.fetchrow.return_value = FakeRecord(key="zotero.poll_cron", value=old_cron)

    # Track execute calls so we can assert rollback was issued
    execute_calls: list[tuple] = []

    async def _capture_execute(*args, **kwargs):
        execute_calls.append(args)

    conn.execute = AsyncMock(side_effect=_capture_execute)

    # --- Scheduler mock: reschedule_job raises -------------------------
    mock_scheduler = MagicMock()
    mock_scheduler.reschedule_job.side_effect = RuntimeError("job not found")

    # --- Wire overrides ------------------------------------------------
    app.state.db_pool = pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_scheduler] = lambda: mock_scheduler
    app.dependency_overrides[verify_api_key] = lambda: None

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Starlette re-raises unhandled server exceptions through the ASGI transport;
            # catch them here so we can still inspect the rollback side-effect.
            try:
                await client.put(
                    "/api/config/zotero.poll_cron",
                    json={"key": "zotero.poll_cron", "value": "0 5 * * *"},
                )
            except Exception:
                pass  # expected: scheduler RuntimeError propagates as server error

        # The DB rollback execute must have been called: at least one call
        # must reference the old cron value (INSERT … old_cron) or a DELETE.
        rollback_issued = any(
            (
                # rollback INSERT with old value: (sql, user_id, old_cron)
                (len(args) >= 3 and args[2] == old_cron)
                # or DELETE fallback
                or (len(args) >= 1 and isinstance(args[0], str) and "DELETE" in args[0])
            )
            for args in execute_calls
        )
        assert rollback_issued, (
            "Expected a rollback DB execute (old cron re-write or DELETE) after scheduler failure,"
            f" but execute calls were: {execute_calls}"
        )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# B4: SMTP config keys in the settings allow-list (system-scoped, admin-only)
# ---------------------------------------------------------------------------

_SMTP_CONFIG_KEYS = ("smtp.host", "smtp.port", "smtp.user", "smtp.from", "smtp.pass")


@pytest.mark.parametrize("key", _SMTP_CONFIG_KEYS)
def test_smtp_keys_accepted_by_allow_list(key: str) -> None:
    """Each smtp.* key (matching the rows setup.py persists) is allow-listed."""
    from paper_ingestion.routers import settings

    assert key in settings._ALLOWED_CONFIG_KEYS
    assert settings._is_allowed_config_key(key) is True


@pytest.mark.parametrize("key", _SMTP_CONFIG_KEYS)
def test_smtp_keys_classified_system(key: str) -> None:
    """SMTP is deployment-wide → system-scoped (admin-only via require_admin),
    never personal."""
    from paper_ingestion.routers import settings

    assert key in settings.SYSTEM_KEYS
    assert key not in settings.PERSONAL_KEYS
    assert settings._classify_config_key(key) == "system"


def test_smtp_pass_is_encrypted_and_secret() -> None:
    """smtp.pass is persisted as Fernet ciphertext by setup.py; the generic
    /api/config surface must treat it as encrypted + secret so it is masked,
    never returned in plaintext."""
    from paper_ingestion.routers import settings

    assert "smtp.pass" in settings._ENCRYPTED_KEYS
    assert "smtp.pass" in settings._SECRET_KEYS


def test_smtp_pass_resolve_value_is_masked_not_plaintext(monkeypatch) -> None:
    """_resolve_config_value must mask a decrypted smtp.pass — never expose it."""
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "pgyJ7t8w9KYMFgZ-9_M89P0VbyzqWj4Xz9LgSjlvKxs=")
    from jarvis_common.crypto import _load_fernet, encrypt_secret

    _load_fernet.cache_clear()
    from paper_ingestion.routers import settings

    ciphertext = encrypt_secret("sup3r-secret-pw").encode("ascii")
    row = FakeRecord(
        key="smtp.pass",
        value=None,
        encrypted_value=ciphertext,
        user_id=None,
    )
    resolved = settings._resolve_config_value("smtp.pass", row)
    assert resolved is not None
    assert "sup3r-secret-pw" not in resolved
    assert resolved.startswith("****")


# ---------------------------------------------------------------------------
# UI-3: automation.fetch_interval_hours — allow-list, validator, live reschedule
# ---------------------------------------------------------------------------


def test_fetch_interval_key_in_allow_list() -> None:
    """automation.fetch_interval_hours must be in _ALLOWED_CONFIG_KEYS and SYSTEM_KEYS."""
    from paper_ingestion.routers import settings

    assert "automation.fetch_interval_hours" in settings._ALLOWED_CONFIG_KEYS
    assert settings._is_allowed_config_key("automation.fetch_interval_hours") is True
    assert "automation.fetch_interval_hours" in settings.SYSTEM_KEYS
    assert settings._classify_config_key("automation.fetch_interval_hours") == "system"


@pytest.mark.parametrize("bad_value", [0, -1, "24"])
def test_fetch_interval_validator_rejects_invalid(bad_value) -> None:
    """_validate_positive_int rejects zero, negative, and non-int inputs (D5-13)."""
    from paper_ingestion.routers.settings import _validate_positive_int

    with pytest.raises(ValueError):
        _validate_positive_int(bad_value)


@pytest.mark.parametrize("good_value", [1, 24, 168])
def test_fetch_interval_validator_accepts_positive_int(good_value: int) -> None:
    """_validate_positive_int accepts valid positive integers (D5-13)."""
    from paper_ingestion.routers.settings import _validate_positive_int

    _validate_positive_int(good_value)  # must not raise


@pytest.mark.asyncio
async def test_set_fetch_interval_persists_and_reschedules():
    """PUT /api/config/automation.fetch_interval_hours persists the value and calls
    scheduler.reschedule_job('auto_pipeline', trigger=IntervalTrigger(hours=<value>))."""
    from unittest.mock import MagicMock

    import httpx
    from apscheduler.triggers.interval import IntervalTrigger
    from httpx import ASGITransport
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from paper_ingestion.deps import get_db_pool, get_scheduler
    from paper_ingestion.main import app
    from paper_ingestion.routers import settings as _settings_mod

    pool, conn = _make_pool_and_conn()

    # Simulate the auto_pipeline job being present
    mock_job = MagicMock()
    mock_scheduler = MagicMock()
    mock_scheduler.get_job.return_value = mock_job

    _lenient = _make_lenient_require_admin()
    _orig_require_admin = _settings_mod.require_admin
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_scheduler] = lambda: mock_scheduler
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[require_admin] = _lenient
    _settings_mod.require_admin = _lenient

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/config/automation.fetch_interval_hours",
                json={"key": "automation.fetch_interval_hours", "value": 6},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["key"] == "automation.fetch_interval_hours"
        assert body["value"] == 6

        # DB write occurred
        conn.execute.assert_awaited()

        # Scheduler was queried and reschedule was called
        mock_scheduler.get_job.assert_called_with("auto_pipeline")
        mock_scheduler.reschedule_job.assert_called_once()
        call_kwargs = mock_scheduler.reschedule_job.call_args
        assert call_kwargs.args[0] == "auto_pipeline"
        trigger_arg = call_kwargs.kwargs.get("trigger") or call_kwargs.args[1]
        assert isinstance(trigger_arg, IntervalTrigger)
    finally:
        _settings_mod.require_admin = _orig_require_admin
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_set_fetch_interval_missing_job_does_not_500():
    """PUT /api/config/automation.fetch_interval_hours returns 200 even when the
    auto_pipeline job is absent from the scheduler (not-yet-started / first boot)."""
    from unittest.mock import MagicMock

    import httpx
    from httpx import ASGITransport
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from paper_ingestion.deps import get_db_pool, get_scheduler
    from paper_ingestion.main import app
    from paper_ingestion.routers import settings as _settings_mod

    pool, conn = _make_pool_and_conn()

    mock_scheduler = MagicMock()
    mock_scheduler.get_job.return_value = None  # job absent

    _lenient = _make_lenient_require_admin()
    _orig_require_admin = _settings_mod.require_admin
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_scheduler] = lambda: mock_scheduler
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[require_admin] = _lenient
    _settings_mod.require_admin = _lenient

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/config/automation.fetch_interval_hours",
                json={"key": "automation.fetch_interval_hours", "value": 12},
            )

        assert resp.status_code == 200
        # reschedule_job must NOT be called when job is absent
        mock_scheduler.reschedule_job.assert_not_called()
    finally:
        _settings_mod.require_admin = _orig_require_admin
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_set_fetch_interval_rejects_invalid_values(_app):
    """PUT /api/config/automation.fetch_interval_hours rejects 0 and negative values."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp_zero = await client.put(
            "/api/config/automation.fetch_interval_hours",
            json={"key": "automation.fetch_interval_hours", "value": 0},
        )
        resp_neg = await client.put(
            "/api/config/automation.fetch_interval_hours",
            json={"key": "automation.fetch_interval_hours", "value": -5},
        )

    # 400 from the validator (not 422 — the handler raises HTTPException(400, ...))
    assert resp_zero.status_code == 400
    assert resp_neg.status_code == 400
