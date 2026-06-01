"""Contract tests for source-layer DB writes.

Collapses these mock-unit DB-pool tests to real asyncpg:
  - test_source_config_router.py: update_source_config upsert path, JSONB merge,
    clear_cooldown UPDATE
  - test_arxiv_source.py: _insert_run_history on success and on 429

Idiomatic-mock carve-out: respx/httpx mocks for external HTTP (arXiv API,
external source APIs) are KEPT in all tests below where they appear.

All tests use the session-scoped contract_conn fixture (asyncpg connection
wrapped in a per-test rollback transaction).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _sources_pool(contract_conn):
    """SharedConnPool wrapping the per-test contract connection."""
    from jarvis_common.testing import SharedConnPool

    return SharedConnPool(contract_conn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_admin_request(user_id: int = 1):
    """Minimal fake FastAPI Request with admin state."""
    from types import SimpleNamespace

    state = SimpleNamespace(user_id=user_id, user_role="admin")
    return SimpleNamespace(state=state)


# ---------------------------------------------------------------------------
# paper_sources JSONB upsert — update_source_config contract tests
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_source_config_update_inserts_new_row(contract_conn, _sources_pool):
    """update_source_config inserts a new paper_sources row when none exists.

    Collapses: test_update_source_config_upserts_when_row_absent (mock SQL
    assertion → real INSERT with DB read-back).
    """
    from unittest.mock import patch

    import paper_ingestion.routers.source_config as sc_router

    with patch.object(sc_router, "get_source_class", return_value=object()):
        body = sc_router.SourceConfigBody(api_key="contract-key-001")
        result = await sc_router.update_source_config(
            "arxiv_contract_test_001", body, db_pool=_sources_pool
        )

    assert result == {"ok": True}
    row = await contract_conn.fetchrow(
        "SELECT config FROM paper_sources WHERE source_type = $1",
        "arxiv_contract_test_001",
    )
    assert row is not None, "Row must be inserted when UPDATE affects 0 rows"
    assert row["config"]["api_key"] == "contract-key-001"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_source_config_update_merges_existing_row(contract_conn, _sources_pool):
    """update_source_config merges into an existing paper_sources row.

    The JSONB || operator must preserve pre-existing keys not mentioned in
    the request body.
    Collapses: test_update_source_config_merges_api_key (mock JSONB arg
    shape → real DB JSONB merge + read-back).
    """
    from unittest.mock import patch

    import paper_ingestion.routers.source_config as sc_router

    # Seed a row with a pre-existing key that must survive the merge.
    # Pass config as a native Python dict — asyncpg's JSONB codec auto-encodes it.
    # Avoid $2::jsonb cast (stmt-cache DataError in SharedConnPool; see plan §D4 concern 1).
    await contract_conn.execute(
        """
        INSERT INTO paper_sources (source_type, enabled, config)
        VALUES ($1, FALSE, $2)
        """,
        "s2_contract_test_002",
        {"email": "pre@example.com", "extra_key": "preserved"},
    )

    with patch.object(sc_router, "get_source_class", return_value=object()):
        body = sc_router.SourceConfigBody(api_key="new-s2-key")
        result = await sc_router.update_source_config(
            "s2_contract_test_002", body, db_pool=_sources_pool
        )

    assert result == {"ok": True}
    row = await contract_conn.fetchrow(
        "SELECT config FROM paper_sources WHERE source_type = $1",
        "s2_contract_test_002",
    )
    assert row is not None
    cfg = row["config"]
    # New key was added
    assert cfg["api_key"] == "new-s2-key"
    # Pre-existing key NOT in request body must be preserved (JSONB merge)
    assert cfg["email"] == "pre@example.com"
    assert cfg["extra_key"] == "preserved"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_source_config_jsonb_is_stored_as_object_not_string(contract_conn, _sources_pool):
    """B-1b regression: JSONB arg must be a native dict to prevent double-encoding.

    Passes a real UPDATE through asyncpg. If the router were passing a
    pre-serialised JSON string, the stored value would be a JSON string scalar
    instead of an object — causing this read-back to fail with a type error or
    incorrect value.
    Collapses: test_update_source_config_jsonb_arg_is_dict_not_str (mock
    isinstance check → real DB round-trip).
    """
    from unittest.mock import patch

    import paper_ingestion.routers.source_config as sc_router

    # Seed a row first so the UPDATE path runs (not the INSERT fallback).
    await contract_conn.execute(
        "INSERT INTO paper_sources (source_type, enabled, config) VALUES ($1, FALSE, '{}'::jsonb)",
        "oa_contract_test_003",
    )

    with patch.object(sc_router, "get_source_class", return_value=object()):
        body = sc_router.SourceConfigBody(api_key="double-encode-check", email="b@example.com")
        await sc_router.update_source_config("oa_contract_test_003", body, db_pool=_sources_pool)

    row = await contract_conn.fetchrow(
        "SELECT config FROM paper_sources WHERE source_type = $1",
        "oa_contract_test_003",
    )
    assert row is not None
    cfg = row["config"]
    # asyncpg auto-decodes JSONB → if double-encoded we'd get a string, not a dict
    assert isinstance(cfg, dict), (
        f"config must decode as dict (not {type(cfg).__name__!r}); "
        "a pre-serialised string arg would double-encode and return a string scalar"
    )
    assert cfg["api_key"] == "double-encode-check"
    assert cfg["email"] == "b@example.com"


# ---------------------------------------------------------------------------
# source_health cooldown reset — clear_source_cooldown contract test
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_clear_cooldown_resets_source_health_row(contract_conn, _sources_pool):
    """clear_source_cooldown UPDATE sets last_status=ok and clears cooldown_until.

    Collapses: test_clear_cooldown_resets_source_health (mock SQL assertion
    → real UPDATE + read-back).
    """
    from unittest.mock import patch

    import paper_ingestion.routers.source_config as sc_router

    # Seed a source_health row in "rate_limit" state with a future cooldown.
    await contract_conn.execute(
        """
        INSERT INTO source_health (source_type, last_status, cooldown_until, consecutive_failures)
        VALUES ($1, 'rate_limit', NOW() + INTERVAL '1 hour', 3)
        """,
        "arxiv_contract_cooldown_004",
    )

    request = _build_admin_request(user_id=1)
    with patch.object(sc_router, "get_source_class", return_value=object()):
        result = await sc_router.clear_source_cooldown(
            "arxiv_contract_cooldown_004", request, db_pool=_sources_pool
        )

    assert result == {"ok": True}
    row = await contract_conn.fetchrow(
        "SELECT last_status, cooldown_until, consecutive_failures "
        "FROM source_health WHERE source_type = $1",
        "arxiv_contract_cooldown_004",
    )
    assert row is not None
    assert row["last_status"] == "ok"
    assert row["cooldown_until"] is None
    assert row["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# source_run_history write — ArxivSource._insert_run_history contract tests
#
# These collapse the mock-pool tests in test_arxiv_source.py that assert SQL
# argument presence via mock_conn.execute.call_args_list examination. The
# contract tests exercise the real INSERT and read the row back.
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_arxiv_run_history_inserted_on_success(contract_conn, _sources_pool):
    """ArxivSource._insert_run_history writes a status='ok' row to source_run_history.

    Collapses: test_fetch_new_since_writes_run_history_on_success
    (mock pool + SQL substring check → real INSERT + row read-back).

    External HTTP (arXiv Atom API) is still mocked via respx.
    """
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    import httpx
    import respx
    from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
    from paper_ingestion.sources.arxiv_source import ARXIV_API_URL, ArxivSource

    fixture = (
        __import__("pathlib").Path(__file__).parent.parent / "fixtures" / "arxiv_new_since.xml"
    ).read_bytes()

    config = PaperSourceConfig(id=1, source_type=SourceType.ARXIV, enabled=True, config={})
    client = httpx.AsyncClient()
    source = ArxivSource(config, client, db_pool=_sources_pool)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    with (
        respx.mock,
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
    ):
        respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))
        papers = await source.fetch_new_since(
            since=datetime(2026, 4, 9, tzinfo=UTC),
            topics=[TopicRef(id=1, name="neural ODE", query_terms=["neural ODE"])],
            limit=10,
        )

    assert len(papers) > 0, "Fixture must yield at least one parsed paper"

    row = await contract_conn.fetchrow(
        "SELECT status, candidate_count FROM source_run_history"
        " WHERE source_type = 'arxiv' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None, "source_run_history row must be inserted on success"
    assert row["status"] == "ok"
    assert row["candidate_count"] >= 0


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_arxiv_run_history_inserted_on_rate_limit(contract_conn, _sources_pool):
    """ArxivSource._insert_run_history writes a status='rate_limit' row on 429.

    Collapses: test_fetch_new_since_writes_run_history_on_429_with_cooldown
    (mock pool + SQL assertion → real INSERT + read-back).

    External HTTP is mocked to return 429 three times (exhausting retries).
    asyncio.sleep is patched to skip actual waits.
    """
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, patch

    import httpx
    import respx
    from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
    from paper_ingestion.sources.arxiv_source import ARXIV_API_URL, ArxivSource

    config = PaperSourceConfig(id=1, source_type=SourceType.ARXIV, enabled=True, config={})
    client = httpx.AsyncClient()
    source = ArxivSource(config, client, db_pool=_sources_pool)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    async def _noop_sleep(_: float) -> None:
        pass

    with (
        respx.mock,
        patch("paper_ingestion.sources.arxiv_source.asyncio.sleep", _noop_sleep),
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
    ):
        respx.get(ARXIV_API_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(429, headers={"Retry-After": "1"}),
            ]
        )
        papers = await source.fetch_new_since(
            since=datetime(2026, 4, 9, tzinfo=UTC),
            topics=[TopicRef(id=1, name="ML", query_terms=["machine learning"])],
            limit=10,
        )

    assert papers == []

    row = await contract_conn.fetchrow(
        "SELECT status FROM source_run_history WHERE source_type = 'arxiv' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None, "source_run_history row must be inserted on rate-limit"
    assert row["status"] == "rate_limit"
