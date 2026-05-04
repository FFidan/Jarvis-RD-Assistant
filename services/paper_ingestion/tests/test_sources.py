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
        result, source_counts = await discover_candidates(
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
# SEC-002: SourceResponse must redact secret config keys
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
