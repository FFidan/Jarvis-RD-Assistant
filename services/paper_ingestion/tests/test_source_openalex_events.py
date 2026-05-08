"""Tests for LG-B3: OpenAlexSource emits category='source' log_event rows."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import respx
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.openalex_source import OPENALEX_API_URL, OpenAlexSource

_OA_WORK_ITEM = {
    "id": "https://openalex.org/W1234567890",
    "title": "Neural ODE Paper",
    "display_name": "Neural ODE Paper",
    "publication_date": "2026-04-15",
    "doi": "https://doi.org/10.1234/test",
    "authorships": [
        {
            "author": {"id": "https://openalex.org/A1", "display_name": "Author A"},
            "institutions": [],
        }
    ],
    "abstract_inverted_index": {"Neural": [0], "ODE": [1]},
    "primary_location": {"landing_page_url": "https://doi.org/10.1234/test", "pdf_url": None},
    "open_access": {"oa_url": None},
    "cited_by_count": 2,
    "concepts": [],
    "topics": [],
}

_OA_RESPONSE = {"results": [_OA_WORK_ITEM], "meta": {"count": 1, "per_page": 25}}


def _make_source(db_pool=None, api_key: str = "test-oa-key") -> OpenAlexSource:
    config = PaperSourceConfig(
        id=3,
        source_type=SourceType.OPENALEX,
        enabled=True,
        config={"api_key": api_key},
    )
    client = httpx.AsyncClient()
    return OpenAlexSource(config, client, db_pool=db_pool)


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
async def test_openalex_emits_source_event_on_success():
    """fetch_new_since emits a 'source' log_event with message='fetch_succeeded' on success."""
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(200, json=_OA_RESPONSE))

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
            "paper_ingestion.sources.openalex_source.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.openalex_source.log_event", side_effect=_capture_log_event),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert len(papers) > 0

    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    success_events = [c for c in source_events if c.get("message") == "fetch_succeeded"]
    assert success_events, "Expected a log_event with message='fetch_succeeded'"
    ev = success_events[0]
    assert ev["source"] == "openalex"
    assert ev["level"] == "info"
    assert ev["context"]["http_status"] == 200
    assert "papers_fetched" in ev["context"]


@respx.mock
async def test_openalex_emits_source_event_on_rate_limit():
    """fetch_new_since emits a 'source' log_event with message='rate_limited' on 429."""
    respx.get(OPENALEX_API_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"})
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
            "paper_ingestion.sources.openalex_source.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.openalex_source.log_event", side_effect=_capture_log_event),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    rate_events = [c for c in source_events if c.get("message") == "rate_limited"]
    assert rate_events, "Expected a log_event with message='rate_limited'"
    ev = rate_events[0]
    assert ev["source"] == "openalex"
    assert ev["level"] == "warning"
    assert ev["context"]["http_status"] == 429


@respx.mock
async def test_openalex_emits_source_event_on_http_error():
    """fetch_new_since emits a 'source' log_event with message='fetch_failed' on network error."""
    respx.get(OPENALEX_API_URL).mock(side_effect=httpx.ConnectError("connection refused"))

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
            "paper_ingestion.sources.openalex_source.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.openalex_source.log_event", side_effect=_capture_log_event),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    fail_events = [c for c in source_events if c.get("message") == "fetch_failed"]
    assert fail_events, "Expected a log_event with message='fetch_failed'"
    ev = fail_events[0]
    assert ev["source"] == "openalex"
    assert ev["level"] == "error"


@respx.mock
async def test_openalex_no_log_event_without_pool():
    """fetch_new_since does NOT call log_event when db_pool is None."""
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(200, json=_OA_RESPONSE))

    source = _make_source(db_pool=None)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("neural ODE")]

    with patch(
        "paper_ingestion.sources.openalex_source.log_event", new_callable=AsyncMock
    ) as mock_log:
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    mock_log.assert_not_called()
    assert isinstance(papers, list)
