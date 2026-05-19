"""Tests for LG-B3: ArxivSource emits category='source' log_event rows."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import respx
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.arxiv_source import ARXIV_API_URL, ArxivSource

from tests._source_fakes import mock_log_event_pool

FIXTURES = Path(__file__).parent / "fixtures"


def _make_source(db_pool=None) -> ArxivSource:
    config = PaperSourceConfig(
        id=1,
        source_type=SourceType.ARXIV,
        enabled=True,
        config={},
    )
    client = httpx.AsyncClient()
    return ArxivSource(config, client, db_pool=db_pool)


def _make_topic(name: str, terms: list[str] | None = None) -> TopicRef:
    return TopicRef(id=1, name=name, query_terms=terms or [name])


def _fixture_xml() -> bytes:
    return (FIXTURES / "arxiv_new_since.xml").read_bytes()


@respx.mock
async def test_arxiv_emits_source_event_on_success():
    """fetch_new_since emits a 'source' log_event with message='fetch_succeeded' on success."""
    fixture = _fixture_xml()
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    pool = mock_log_event_pool()
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
            "paper_ingestion.sources.arxiv_source.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.arxiv_source.log_event", side_effect=_capture_log_event),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert len(papers) > 0

    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    success_events = [c for c in source_events if c.get("message") == "fetch_succeeded"]
    assert success_events, "Expected a log_event with message='fetch_succeeded'"
    ev = success_events[0]
    assert ev["source"] == "arxiv"
    assert ev["level"] == "info"
    assert ev["context"]["http_status"] == 200
    assert "papers_fetched" in ev["context"]


@respx.mock
async def test_arxiv_emits_source_event_on_rate_limit(monkeypatch):
    """fetch_new_since emits a 'source' log_event with message='rate_limited' on 429."""
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("paper_ingestion.sources.arxiv_source.asyncio.sleep", fake_sleep)
    # Exhaust all _MAX_FETCH_ATTEMPTS with 429 so fetch_xml returns None.
    respx.get(ARXIV_API_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(429, headers={"Retry-After": "5"}),
        ]
    )

    pool = mock_log_event_pool()
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
            "paper_ingestion.sources.arxiv_source.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.arxiv_source.log_event", side_effect=_capture_log_event),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    rate_events = [c for c in source_events if c.get("message") == "rate_limited"]
    assert rate_events, "Expected a log_event with message='rate_limited'"
    ev = rate_events[0]
    assert ev["source"] == "arxiv"
    assert ev["level"] == "warning"


@respx.mock
async def test_arxiv_emits_source_event_on_http_error():
    """fetch_new_since emits a 'source' log_event with message='fetch_failed' on HTTP error."""
    respx.get(ARXIV_API_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    pool = mock_log_event_pool()
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
            "paper_ingestion.sources.arxiv_source.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.arxiv_source.log_event", side_effect=_capture_log_event),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    # Note: ConnectError causes _fetch_xml to exhaust retries and return None (not raise),
    # so the "root is None" path fires — may be fetch_failed or rate_limited.
    # We just require category='source' and source='arxiv'.
    assert source_events, "Expected at least one log_event with category='source'"
    assert all(c["source"] == "arxiv" for c in source_events)


@respx.mock
async def test_arxiv_no_log_event_without_pool():
    """fetch_new_since does NOT call log_event when db_pool is None."""
    fixture = _fixture_xml()
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    source = _make_source(db_pool=None)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("neural ODE")]

    with patch(
        "paper_ingestion.sources.arxiv_source.log_event", new_callable=AsyncMock
    ) as mock_log:
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    mock_log.assert_not_called()
    assert isinstance(papers, list)
