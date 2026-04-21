"""Tests for settings, nudges, sources, and analytics endpoints.

Covers:
- Config: GET /api/config, GET /api/config/{key}, PUT /api/config/{key}
- Nudges: GET /api/nudges, PUT /api/nudges/{id}
- Sources: GET /api/sources, PUT /api/sources/{id}
- Analytics: GET /api/analytics/papers-by-source, GET /api/analytics/papers-by-status
"""

from datetime import UTC, datetime
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
async def test_set_config_allowed_key(_app):
    """PUT /api/config/{key} sets a config value for an allowed key."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/llm.smart_model",
            json={"key": "llm.smart_model", "value": "new-model"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "llm.smart_model"
    assert body["value"] == "new-model"
    conn.execute.assert_awaited_once()


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
    _call_args = conn.execute.call_args
    positional_args = _call_args.args if _call_args.args else _call_args[0]
    # positional_args: (sql, key, value)
    stored_value = positional_args[2]
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
