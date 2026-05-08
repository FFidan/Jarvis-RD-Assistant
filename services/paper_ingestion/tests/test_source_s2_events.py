"""Tests for LG-B3: SemanticScholarSource emits category='source' log_event rows."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import respx
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.semantic_scholar_source import S2_API_URL, SemanticScholarSource

_S2_PAPER_ITEM = {
    "paperId": "p1",
    "title": "Neural CDE Paper",
    "authors": [{"name": "Author A", "authorId": "1"}],
    "abstract": "Abstract text",
    "year": 2026,
    "publicationDate": "2026-04-15",
    "url": "https://www.semanticscholar.org/paper/p1",
    "citationCount": 3,
    "externalIds": {"ArXiv": "2604.00001"},
    "openAccessPdf": None,
    "tldr": None,
}


def _make_source(db_pool=None) -> SemanticScholarSource:
    config = PaperSourceConfig(
        id=2,
        source_type=SourceType.SEMANTIC_SCHOLAR,
        enabled=True,
        config={},
    )
    client = httpx.AsyncClient()
    return SemanticScholarSource(config, client, db_pool=db_pool)


def _make_topic(name: str, terms: list[str] | None = None) -> TopicRef:
    return TopicRef(id=1, name=name, query_terms=terms or [name])


def _mock_pool() -> MagicMock:
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


@respx.mock
async def test_s2_emits_source_event_on_success():
    """fetch_new_since emits a 'source' log_event with message='fetch_succeeded' on success."""
    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(200, json={"data": [_S2_PAPER_ITEM]})
    )

    pool = _mock_pool()
    source = _make_source(db_pool=pool)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("neural ODE")]

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_calls: list[dict] = []

    async def _capture_log_event(**kwargs):
        log_calls.append(kwargs)

    with (
        patch(
            "paper_ingestion.sources.semantic_scholar_source.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch(
            "paper_ingestion.sources.semantic_scholar_source.log_event",
            side_effect=_capture_log_event,
        ),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert len(papers) > 0

    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    success_events = [c for c in source_events if c.get("message") == "fetch_succeeded"]
    assert success_events, "Expected a log_event with message='fetch_succeeded'"
    ev = success_events[0]
    assert ev["source"] == "semantic_scholar"
    assert ev["level"] == "info"
    assert ev["context"]["http_status"] == 200
    assert "papers_fetched" in ev["context"]


@respx.mock
async def test_s2_emits_source_event_on_rate_limit():
    """fetch_new_since emits a 'source' log_event with message='rate_limited' on 429."""
    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "10"})
    )

    pool = _mock_pool()
    source = _make_source(db_pool=pool)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("ML")]

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_calls: list[dict] = []

    async def _capture_log_event(**kwargs):
        log_calls.append(kwargs)

    with (
        patch(
            "paper_ingestion.sources.semantic_scholar_source.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch(
            "paper_ingestion.sources.semantic_scholar_source.log_event",
            side_effect=_capture_log_event,
        ),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    rate_events = [c for c in source_events if c.get("message") == "rate_limited"]
    assert rate_events, "Expected a log_event with message='rate_limited'"
    ev = rate_events[0]
    assert ev["source"] == "semantic_scholar"
    assert ev["level"] == "warning"


@respx.mock
async def test_s2_emits_source_event_on_http_error():
    """fetch_new_since emits a 'source' log_event with message='fetch_failed' on network error."""
    respx.get(f"{S2_API_URL}/paper/search").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    pool = _mock_pool()
    source = _make_source(db_pool=pool)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("ML")]

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_calls: list[dict] = []

    async def _capture_log_event(**kwargs):
        log_calls.append(kwargs)

    with (
        patch(
            "paper_ingestion.sources.semantic_scholar_source.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch(
            "paper_ingestion.sources.semantic_scholar_source.log_event",
            side_effect=_capture_log_event,
        ),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    fail_events = [c for c in source_events if c.get("message") == "fetch_failed"]
    assert fail_events, "Expected a log_event with message='fetch_failed'"
    ev = fail_events[0]
    assert ev["source"] == "semantic_scholar"
    assert ev["level"] == "error"


@respx.mock
async def test_s2_no_log_event_without_pool():
    """fetch_new_since does NOT call log_event when db_pool is None."""
    respx.get(f"{S2_API_URL}/paper/search").mock(
        return_value=httpx.Response(200, json={"data": [_S2_PAPER_ITEM]})
    )

    source = _make_source(db_pool=None)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("neural ODE")]

    with patch(
        "paper_ingestion.sources.semantic_scholar_source.log_event", new_callable=AsyncMock
    ) as mock_log:
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    mock_log.assert_not_called()
    assert isinstance(papers, list)
