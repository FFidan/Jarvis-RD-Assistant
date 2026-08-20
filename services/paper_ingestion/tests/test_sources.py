"""Tests for paper source plugins and display_order behavior."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from paper_ingestion.models import PaperSourceConfig, SourceResponse, SourceType
from paper_ingestion.sources.semantic_scholar_source import SemanticScholarSource


@respx.mock
async def test_semantic_scholar_search():
    """SemanticScholarSource.search returns parsed papers from S2 API."""
    mock_response = {
        "data": [
            {
                "paperId": "abc123",
                "title": "Attention Is All You Need",
                "authors": [{"name": "Ashish Vaswani"}],
                "abstract": "The dominant sequence transduction models...",
                "year": 2017,
                "publicationDate": "2017-06-12",
                "url": "https://www.semanticscholar.org/paper/abc123",
                "citationCount": 100000,
                "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762"},
                "externalIds": {"ArXiv": "1706.03762", "DOI": "10.5555/3295222.3295349"},
            }
        ]
    }
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    config = PaperSourceConfig(
        id=2, source_type=SourceType.SEMANTIC_SCHOLAR, enabled=True, config={}
    )
    async with httpx.AsyncClient() as client:
        source = SemanticScholarSource(config, client)
        papers = await source.search("attention transformer", max_results=1)

    assert len(papers) == 1
    assert papers[0].title == "Attention Is All You Need"
    assert papers[0].external_id == "s2:abc123"
    assert papers[0].citation_count == 100000
    assert papers[0].metadata["arxiv_id"] == "1706.03762"


@respx.mock
async def test_semantic_scholar_fetch_by_id():
    """SemanticScholarSource.fetch_by_id returns a single paper."""
    mock_response = {
        "paperId": "abc123",
        "title": "Test Paper",
        "authors": [{"name": "Test Author"}],
        "abstract": "Test abstract",
        "year": 2024,
        "publicationDate": None,
        "url": "https://www.semanticscholar.org/paper/abc123",
        "citationCount": 5,
        "openAccessPdf": None,
        "externalIds": {},
    }
    respx.get("https://api.semanticscholar.org/graph/v1/paper/abc123").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    config = PaperSourceConfig(
        id=2, source_type=SourceType.SEMANTIC_SCHOLAR, enabled=True, config={}
    )
    async with httpx.AsyncClient() as client:
        source = SemanticScholarSource(config, client)
        paper = await source.fetch_by_id("abc123")

    assert paper is not None
    assert paper.title == "Test Paper"
    assert paper.published_date is not None
    assert paper.published_date.year == 2024


@respx.mock
async def test_semantic_scholar_fetch_not_found():
    """SemanticScholarSource.fetch_by_id returns None for 404."""
    respx.get("https://api.semanticscholar.org/graph/v1/paper/nonexistent").mock(
        return_value=httpx.Response(404)
    )

    config = PaperSourceConfig(
        id=2, source_type=SourceType.SEMANTIC_SCHOLAR, enabled=True, config={}
    )
    async with httpx.AsyncClient() as client:
        source = SemanticScholarSource(config, client)
        paper = await source.fetch_by_id("nonexistent")

    assert paper is None


# ---------------------------------------------------------------------------
# display_order: discovery.py SQL assertion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_fetch_sources_uses_display_order_ordering():
    """discover_candidates issues ORDER BY display_order when fetching sources.

    We mock the asyncpg pool and assert the SQL passed to conn.fetch contains
    the correct ORDER BY clause.  We short-circuit after the source fetch by
    returning an empty list (no enabled sources), so the test is lightweight.
    """
    from datetime import datetime

    from paper_ingestion.pulse.discovery import discover_candidates
    from paper_ingestion.pulse.profile import UserProfile

    conn = AsyncMock()
    conn.fetch.return_value = []
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    profile = UserProfile(
        topics=[],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=None,
        weights={},
        deck_size=10,
        stage2_top_k=40,
        recent_positive_titles=[],
        recent_negative_titles=[],
    )
    async with httpx.AsyncClient() as http_client:
        result, source_counts, _ = await discover_candidates(
            db_pool=pool,
            http_client=http_client,
            profile=profile,
            since=datetime(2026, 1, 1, tzinfo=UTC),
        )
    assert result == []
    assert source_counts == {}

    fetch_sql = conn.fetch.call_args[0][0]
    assert "display_order" in fetch_sql.lower(), (
        f"Expected ORDER BY display_order in discovery SQL; got: {fetch_sql!r}"
    )


# ---------------------------------------------------------------------------
# SourceResponse must redact secret config keys
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _make_source_response(**config_overrides: object) -> SourceResponse:
    """Helper: build a SourceResponse with the given config dict."""
    return SourceResponse(
        id=1,
        source_type=SourceType.SEMANTIC_SCHOLAR,
        enabled=True,
        config=dict(config_overrides),
        priority=1,
        display_order=0,
        created_at=_FIXED_TS,
    )


def test_source_response_redacts_api_key():
    """SourceResponse must mask the api_key field in config on construction."""
    resp = _make_source_response(api_key="REAL_SECRET_VALUE", other_setting="visible")

    assert resp.config["api_key"] != "REAL_SECRET_VALUE", (
        "api_key must not be returned in plaintext"
    )
    # H.1: mask_secret returns "****" + last 4 chars (was first 4 + "****")
    # "REAL_SECRET_VALUE" → "****ALUE"
    assert resp.config["api_key"] == "****ALUE"
    # Non-secret keys are unaffected
    assert resp.config["other_setting"] == "visible"


def test_source_response_redacts_all_known_secret_keys():
    """Every key in _SECRET_KEY_NAMES is masked; non-secret keys pass through."""
    secret_keys = ["api_key", "client_secret", "token", "password", "secret", "bearer"]
    config = {k: f"value_for_{k}" for k in secret_keys}
    config["safe_key"] = "not-a-secret"

    resp = _make_source_response(**config)

    for k in secret_keys:
        assert resp.config[k] != f"value_for_{k}", f"Key {k!r} must be redacted"
        assert "****" in resp.config[k], f"Key {k!r} must contain mask marker"
    assert resp.config["safe_key"] == "not-a-secret"


def test_source_response_empty_secret_value_not_masked():
    """Empty/falsy secret values are left as-is (no mask applied to falsy values)."""
    resp = _make_source_response(api_key="", password=None)

    # Falsy → not masked (the validator guards with `if k.lower() in _SECRET_KEY_NAMES and v`)
    assert resp.config["api_key"] == ""
    assert resp.config["password"] is None


def test_source_response_empty_config_passes():
    """SourceResponse with no config dict works without errors."""
    resp = _make_source_response()
    assert resp.config == {}


def test_source_update_carries_no_redaction():
    """SourceUpdate (input model) must NOT redact — it carries real values to the DB layer."""
    from paper_ingestion.models import SourceUpdate

    update = SourceUpdate(config={"api_key": "REAL_KEY"})
    # Input model must preserve the real value so it can be written to the DB
    assert update.config is not None
    assert update.config["api_key"] == "REAL_KEY"


# ---------------------------------------------------------------------------
# Recommendations rate-limit (migrated from test_recommendations_rate_limit.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _recommendations_app(monkeypatch):
    """App fixture with rate limiter ENABLED for rate-limit testing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    conn = AsyncMock()
    conn.fetch.return_value = []
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = ctx

    # disable_limiter stays False: this fixture exists to test rate limiting.
    with patch_pi_test_app(
        mock_pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=False,
            override_db_dependency=True,
            dependency_overrides={verify_api_key: lambda: None},
        ),
    ):
        yield app


# ---------------------------------------------------------------------------
# BUG-02: source_run_history INSERT failure must not propagate out of discover_candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_handles_source_run_history_insert_failure(caplog):
    """discover_candidates must not raise when the source_run_history INSERT fails.

    The INSERT is observability-only (run-history); a DB failure there must
    never surface as a pipeline error.  The warning must be logged so the
    failure is still observable.
    """
    import logging
    from unittest.mock import AsyncMock, MagicMock, patch

    from paper_ingestion.pulse.discovery import discover_candidates
    from paper_ingestion.pulse.profile import UserProfile

    # Conn whose execute raises (INSERT path) but fetch works (source list).
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"id": 1, "source_type": "arxiv", "enabled": True, "config": {}},
    ]
    conn.execute.side_effect = RuntimeError("db INSERT exploded")

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    profile = UserProfile(
        topics=[],
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=None,
        weights={},
        deck_size=10,
        stage2_top_k=40,
        recent_positive_titles=[],
        recent_negative_titles=[],
        user_id=1,
    )

    # A mock source whose source_type matches the DB row so cache lookup finds it.
    mock_src = MagicMock()
    mock_src.source_type = "arxiv"
    source_cache = {"arxiv": mock_src}

    # Make the rate limiter report in_cooldown=True for the arxiv source.
    mock_snapshot = {
        "in_cooldown": True,
        "cooldown_until": "2099-01-01T00:00:00+00:00",
        "stale": False,
    }
    mock_limiter = AsyncMock()
    mock_limiter.health_snapshot = AsyncMock(return_value=mock_snapshot)

    since = datetime(2026, 1, 1, tzinfo=UTC)

    with (
        caplog.at_level(logging.WARNING, logger="paper_ingestion.pulse.discovery"),
        patch(
            "paper_ingestion.pulse.discovery.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.pulse.discovery.get_source_class", return_value=None),
    ):
        async with httpx.AsyncClient() as http_client:
            result = await discover_candidates(
                db_pool=pool,
                http_client=http_client,
                profile=profile,
                since=since,
                source_cache=source_cache,
            )

    # Must not raise; returns empty candidates because the only source was in cooldown.
    papers, source_counts, diagnostics = result
    assert papers == []

    # The INSERT failure must be logged as a warning.
    assert any("source_run_history INSERT failed" in record.message for record in caplog.records), (
        f"Expected 'source_run_history INSERT failed' warning; got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# BUG-03: run_pulse early-return (load_profile failure) must include all 12 keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_pulse_early_return_stats_has_all_required_keys():
    """Stats dict returned on load_profile failure must include all 12 contract keys.

    The stats dict must carry deck_date, card_count,
    source_counts, and classifier even when the pipeline aborts at step 1.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from paper_ingestion.pulse.job import run_pulse

    pool = MagicMock()

    with patch(
        "paper_ingestion.pulse.job.load_profile",
        AsyncMock(side_effect=RuntimeError("db down")),
    ):
        stats = await run_pulse(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
            user_id=1,
        )

    required_keys = {
        "candidate_count",
        "stage1_survivors",
        "stage2_scored",
        "llm_calls",
        "duration_s",
        "last_error",
        "degraded_reason",
        "source_diagnostics",
        "deck_date",
        "card_count",
        "source_counts",
        "classifier",
    }
    assert set(stats.keys()) >= required_keys, (
        f"Stats dict missing keys: {required_keys - set(stats.keys())}"
    )
    # Sentinel defaults on the early-return path
    assert stats["deck_date"] is None
    assert stats["card_count"] == 0
    assert stats["source_counts"] == {}
    assert stats["classifier"] is None
    assert "load_profile" in stats["last_error"]


@pytest.mark.asyncio
async def test_list_recommendations_rate_limit(_recommendations_app):
    """6 rapid calls to GET /api/recommendations should yield at least one 429."""
    import httpx
    from httpx import ASGITransport

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_recommendations_app), base_url="http://test"
    ) as client:
        statuses = []
        for _ in range(6):
            resp = await client.get("/api/recommendations")
            statuses.append(resp.status_code)

    assert 429 in statuses, f"Expected at least one 429 in {statuses}"
