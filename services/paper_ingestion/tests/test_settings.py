"""Tests for settings, nudges, sources, and analytics endpoints.

Covers:
- Config: GET /api/config, GET /api/config/{key}, PUT /api/config/{key}
- Nudges: GET /api/nudges, PUT /api/nudges/{id}
- Sources: GET /api/sources, PUT /api/sources/{id}
- Analytics: GET /api/analytics/papers-by-source, GET /api/analytics/papers-by-status
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Dict subclass that supports both dict[key] and .keys() like asyncpg.Record."""

    def keys(self):
        return super().keys()


def _make_pool_and_conn():
    """Create a mock asyncpg Pool whose acquire() returns an async CM."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    # conn.transaction() must return a synchronous async context manager object
    # (AsyncMock attributes are coroutines when called, so use MagicMock here)
    txn_ctx = MagicMock()
    txn_ctx.__aenter__ = AsyncMock(return_value=None)
    txn_ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_ctx)
    return pool, conn


def _now():
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked DB pool and disabled auth."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn, mock_http
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Tests: Config CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_config(_app):
    """GET /api/config returns list of config entries."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(key="llm.smart_model", value="mistral-nemo"),
        FakeRecord(key="llm.fast_model", value="qwen3.5:4b"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/config")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["key"] == "llm.smart_model"


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


@pytest.mark.asyncio
async def test_get_config_not_found(_app):
    """GET /api/config/{key} returns 404 when key does not exist."""
    app, conn, _ = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/config/nonexistent.key")

    assert resp.status_code == 404


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


@pytest.mark.asyncio
async def test_set_config_allowed_key(_app):
    """PUT /api/config/{key} sets a config value for an allowed key."""
    app, conn, mock_http = _app
    mock_http.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"models": [{"name": "qwen3:4b"}]}),
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/llm.smart_model",
            json={"key": "llm.smart_model", "value": "qwen3:4b"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "llm.smart_model"
    assert body["value"] == "qwen3:4b"
    # set_config now emits a log_event (INSERT INTO system_events) in addition to the
    # UPSERT — expect at least one execute call (the config UPSERT).
    conn.execute.assert_awaited()


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


@pytest.mark.asyncio
async def test_set_config_disallowed_key(_app):
    """PUT /api/config/{key} returns 400 for a disallowed key."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/secret.password",
            json={"key": "secret.password", "value": "hunter2"},
        )

    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Tests: Nudges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_nudges(_app):
    """GET /api/nudges returns list of scheduled nudges."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(
            id=1,
            nudge_type="review_reminder",
            cron_expression="0 9 * * *",
            enabled=True,
            config={},
            last_fired_at=None,
            created_at=_now(),
        ),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/nudges")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["nudge_type"] == "review_reminder"


@pytest.mark.asyncio
async def test_update_nudge_found(_app):
    """PUT /api/nudges/{id} updates the nudge when found."""
    app, conn, _ = _app
    existing = FakeRecord(
        id=1,
        nudge_type="review_reminder",
        cron_expression="0 9 * * *",
        enabled=True,
        config={},
        last_fired_at=None,
        created_at=_now(),
    )
    updated = FakeRecord(
        id=1,
        nudge_type="review_reminder",
        cron_expression="0 10 * * *",
        enabled=True,
        config={},
        last_fired_at=None,
        created_at=_now(),
    )
    conn.fetchrow.side_effect = [existing, updated]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/nudges/1",
            json={"cron_expression": "0 10 * * *"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cron_expression"] == "0 10 * * *"


@pytest.mark.asyncio
async def test_update_nudge_not_found(_app):
    """PUT /api/nudges/{id} returns 404 when nudge does not exist."""
    app, conn, _ = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/nudges/999", json={"enabled": False})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sources(_app):
    """GET /api/sources returns list of paper sources."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(
            id=1,
            source_type="arxiv",
            enabled=True,
            config={},
            priority=1,
            display_order=0,
            created_at=_now(),
        ),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sources")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["source_type"] == "arxiv"


@pytest.mark.asyncio
async def test_list_sources_ordered_by_display_order(_app):
    """GET /api/sources issues ORDER BY display_order ASC, id ASC."""

    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(
            id=2,
            source_type="pubmed",
            enabled=True,
            config={},
            priority=1,
            display_order=1,
            created_at=_now(),
        ),
        FakeRecord(
            id=1,
            source_type="arxiv",
            enabled=True,
            config={},
            priority=1,
            display_order=2,
            created_at=_now(),
        ),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sources")

    assert resp.status_code == 200
    # Verify the SQL passed to fetch includes ORDER BY display_order
    fetch_sql = conn.fetch.call_args[0][0]
    assert "display_order" in fetch_sql.lower()
    body = resp.json()
    assert body[0]["source_type"] == "pubmed"
    assert body[1]["source_type"] == "arxiv"


@pytest.mark.asyncio
async def test_reorder_sources_persists_order(_app):
    """PATCH /api/sources/reorder updates display_order and returns ordered list."""
    app, conn, _ = _app
    # First fetch: existing source_types validation
    conn.fetch.side_effect = [
        [
            FakeRecord(source_type="arxiv"),
            FakeRecord(source_type="pubmed"),
            FakeRecord(source_type="openalex"),
        ],
        # Second fetch: return after update
        [
            FakeRecord(
                id=2,
                source_type="pubmed",
                enabled=True,
                config={},
                priority=1,
                display_order=1,
                created_at=_now(),
            ),
            FakeRecord(
                id=3,
                source_type="openalex",
                enabled=True,
                config={},
                priority=1,
                display_order=2,
                created_at=_now(),
            ),
            FakeRecord(
                id=1,
                source_type="arxiv",
                enabled=True,
                config={},
                priority=1,
                display_order=3,
                created_at=_now(),
            ),
        ],
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch(
            "/api/sources/reorder",
            json={"source_types": ["pubmed", "openalex", "arxiv"]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    assert body[0]["source_type"] == "pubmed"
    assert body[0]["display_order"] == 1
    assert body[2]["source_type"] == "arxiv"
    assert body[2]["display_order"] == 3
    # Verify execute was called for each source_type in order
    assert conn.execute.await_count == 3


@pytest.mark.asyncio
async def test_reorder_sources_unknown_source_returns_400(_app):
    """PATCH /api/sources/reorder returns 400 for unknown source_type."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(source_type="arxiv"),
        FakeRecord(source_type="pubmed"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.patch(
            "/api/sources/reorder",
            json={"source_types": ["arxiv", "nonexistent_source"]},
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "nonexistent_source" in detail


@pytest.mark.asyncio
async def test_update_source_found(_app):
    """PUT /api/sources/{id} updates the source when found."""
    app, conn, _ = _app
    existing = FakeRecord(
        id=1,
        source_type="arxiv",
        enabled=True,
        config={},
        priority=1,
        created_at=_now(),
    )
    updated = FakeRecord(
        id=1,
        source_type="arxiv",
        enabled=False,
        config={},
        priority=1,
        created_at=_now(),
    )
    conn.fetchrow.side_effect = [existing, updated]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/sources/1", json={"enabled": False})

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False


@pytest.mark.asyncio
async def test_update_source_not_found(_app):
    """PUT /api/sources/{id} returns 404 when source does not exist."""
    app, conn, _ = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/sources/999", json={"enabled": False})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Analytics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_papers_by_source(_app):
    """GET /api/analytics/papers-by-source returns paper counts by source."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(source_type="arxiv", count=25),
        FakeRecord(source_type="semantic_scholar", count=10),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/analytics/papers-by-source")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["source_type"] == "arxiv"
    assert body[0]["count"] == 25


@pytest.mark.asyncio
async def test_papers_by_status(_app):
    """GET /api/analytics/papers-by-status returns paper counts by status."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(status="new", count=30),
        FakeRecord(status="read", count=15),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/analytics/papers-by-status")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["status"] == "new"
    assert body[0]["count"] == 30


@pytest.mark.asyncio
async def test_papers_by_source_scopes_non_admin_browser_user(_app):
    """Non-admin analytics should not expose other users' corpus size."""
    from paper_ingestion.routers import settings

    _app_obj, conn, _ = _app
    conn.fetch.return_value = [FakeRecord(source_type="arxiv", count=2)]
    request = SimpleNamespace(state=SimpleNamespace(user_id=42, user_role="member"))

    rows = await settings.papers_by_source.__wrapped__(request, db_pool=_app_obj.state.db_pool)

    assert rows == [{"source_type": "arxiv", "count": 2}]
    sql = conn.fetch.await_args.args[0]
    assert "JOIN user_library ul" in sql
    assert "ul.user_id = $1" in sql
    assert conn.fetch.await_args.args[1] == 42


@pytest.mark.asyncio
async def test_papers_by_status_scopes_non_admin_browser_user(_app):
    """Per-state counts for browser users should be derived only from their library."""
    from paper_ingestion.routers import settings

    _app_obj, conn, _ = _app
    conn.fetch.return_value = [FakeRecord(status="inbox", count=1)]
    request = SimpleNamespace(state=SimpleNamespace(user_id=42, user_role="member"))

    rows = await settings.papers_by_status.__wrapped__(request, db_pool=_app_obj.state.db_pool)

    assert rows == [{"status": "inbox", "count": 1}]
    sql = conn.fetch.await_args.args[0]
    assert "JOIN user_library ul" in sql
    assert "pus.user_id IS NOT DISTINCT FROM $1" in sql
    assert conn.fetch.await_args.args[1] == 42


# ---------------------------------------------------------------------------
# Tests: pulse.* config key validation (F1.4)
# ---------------------------------------------------------------------------


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
async def test_set_config_invalid_weights_returns_400(_app):
    """PUT /api/config/pulse.weights rejects a dict with wrong keys."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/pulse.weights",
            json={"key": "pulse.weights", "value": {"bad_key": 0.5}},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_set_config_string_deck_size_returns_400(_app):
    """PUT /api/config/pulse.deck_size rejects a string value."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/pulse.deck_size",
            json={"key": "pulse.deck_size", "value": "10"},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_set_config_l2_lambda_valid_accepted(_app):
    """PUT /api/config/pulse.l2_lambda accepts a float in [0, 2]."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/pulse.l2_lambda",
            json={"key": "pulse.l2_lambda", "value": 0.5},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_set_config_l2_lambda_out_of_range_returns_400(_app):
    """PUT /api/config/pulse.l2_lambda rejects a value > 2.0."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/pulse.l2_lambda",
            json={"key": "pulse.l2_lambda", "value": 5.0},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_set_config_valid_cron_accepted(_app):
    """PUT /api/config/pulse.cron accepts a valid cron expression."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/pulse.cron",
            json={"key": "pulse.cron", "value": "0 4 * * *"},
        )

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Setup wizard whitelist (A1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_setup_completed_accepts_bool(_app):
    """PUT /api/config/setup.completed accepts a boolean value."""
    app, _conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/setup.completed",
            json={"key": "setup.completed", "value": True},
        )

    assert resp.status_code == 200
    assert resp.json()["value"] is True


@pytest.mark.asyncio
async def test_set_setup_completed_rejects_string(_app):
    """PUT /api/config/setup.completed rejects a non-boolean value."""
    app, _conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/setup.completed",
            json={"key": "setup.completed", "value": "true"},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_set_telegram_owner_chat_id_accepts_int(_app):
    """PUT /api/config/telegram.owner_chat_id accepts integer chat ids."""
    app, _conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/telegram.owner_chat_id",
            json={"key": "telegram.owner_chat_id", "value": 123456789},
        )

    assert resp.status_code == 200
    assert resp.json()["value"] == 123456789


@pytest.mark.asyncio
async def test_set_telegram_owner_chat_id_accepts_none(_app):
    """PUT /api/config/telegram.owner_chat_id accepts null to clear pairing."""
    app, _conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/telegram.owner_chat_id",
            json={"key": "telegram.owner_chat_id", "value": None},
        )

    assert resp.status_code == 200
    assert resp.json()["value"] is None


@pytest.mark.asyncio
async def test_set_telegram_owner_chat_id_rejects_string(_app):
    """PUT /api/config/telegram.owner_chat_id rejects a non-integer value."""
    app, _conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/telegram.owner_chat_id",
            json={"key": "telegram.owner_chat_id", "value": "123"},
        )

    assert resp.status_code == 400


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
    from unittest.mock import AsyncMock, MagicMock, patch

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
# Tests: A.1 Ghost key deletion — deleted keys return 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ghost_key_paper_max_daily_returns_400(_app):
    """PUT /api/config/paper.max_daily returns 400 (key removed from allow-list)."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/paper.max_daily",
            json={"key": "paper.max_daily", "value": 10},
        )

    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ghost_key_paper_auto_generate_cards_returns_400(_app):
    """PUT /api/config/paper.auto_generate_cards returns 400 (key removed from allow-list)."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/paper.auto_generate_cards",
            json={"key": "paper.auto_generate_cards", "value": True},
        )

    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ghost_key_ui_page_size_returns_400(_app):
    """PUT /api/config/ui.page_size returns 400 (key removed from allow-list)."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/ui.page_size",
            json={"key": "ui.page_size", "value": 20},
        )

    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ghost_key_ingestion_max_papers_returns_400(_app):
    """PUT /api/config/ingestion.max_papers_per_run returns 400 (key removed from allow-list)."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/ingestion.max_papers_per_run",
            json={"key": "ingestion.max_papers_per_run", "value": 50},
        )

    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ghost_key_ingestion_chunk_size_returns_400(_app):
    """PUT /api/config/ingestion.chunk_size returns 400 (key removed from allow-list)."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/ingestion.chunk_size",
            json={"key": "ingestion.chunk_size", "value": 512},
        )

    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Tests: A.2 FSRS validators
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fsrs_desired_retention_valid_accepted(_app):
    """PUT /api/config/fsrs.desired_retention accepts a valid value in (0, 1)."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/fsrs.desired_retention",
            json={"key": "fsrs.desired_retention", "value": 0.85},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "fsrs.desired_retention"
    assert body["value"] == 0.85


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [0.0, 1.0, -0.1, 1.5, "high", True])
async def test_fsrs_desired_retention_invalid_rejected(_app, bad_value):
    """PUT /api/config/fsrs.desired_retention rejects out-of-range or wrong-type values."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/fsrs.desired_retention",
            json={"key": "fsrs.desired_retention", "value": bad_value},
        )

    assert resp.status_code == 400


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
