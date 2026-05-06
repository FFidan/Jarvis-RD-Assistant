"""Tests for ArxivSource.fetch_new_since().

TDD — written before the implementation was added.
Uses respx to mock the arXiv Atom API; fixture XML is loaded from
tests/fixtures/arxiv_new_since.xml.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.arxiv_source import ARXIV_API_URL, ArxivSource

FIXTURES = Path(__file__).parent / "fixtures"


def _make_source(config_extra: dict | None = None) -> ArxivSource:
    """Create an ArxivSource with a minimal PaperSourceConfig."""
    config = PaperSourceConfig(
        id=1,
        source_type=SourceType.ARXIV,
        enabled=True,
        config=config_extra or {},
    )
    client = httpx.AsyncClient()
    return ArxivSource(config, client)


# ---------------------------------------------------------------------------
# Happy path: fixture with 5 entries is parsed correctly
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_happy_path():
    """fetch_new_since returns list[PaperCreate] parsed from arXiv Atom fixture."""
    fixture = (FIXTURES / "arxiv_new_since.xml").read_bytes()

    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    source = _make_source()
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [TopicRef(id=1, name="neural ODE", query_terms=["neural ODE"])]

    papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert len(papers) == 5
    assert all(p.source_type == SourceType.ARXIV for p in papers)
    # First entry in fixture
    assert papers[0].title == "Neural ODEs for Continuous-Time Dynamics"
    assert papers[0].external_id == "arxiv:2404.01001"
    assert papers[0].published_date is not None
    assert papers[0].published_date.year == 2026
    assert papers[0].pdf_url is not None


# ---------------------------------------------------------------------------
# Empty topics → single date-only query (no topic filter)
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_empty_topics():
    """Empty topics list results in a single date-only API call."""
    fixture = (FIXTURES / "arxiv_new_since.xml").read_bytes()
    route = respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    source = _make_source()
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)

    papers = await source.fetch_new_since(since=since, topics=[], limit=50)

    assert route.call_count == 1
    # Verify the date filter is in the search_query param
    called_params = dict(route.calls[0].request.url.params)
    assert "submittedDate:[" in called_params.get("search_query", "")
    assert len(papers) == 5


# ---------------------------------------------------------------------------
# HTTP error → returns []
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_http_error_returns_empty():
    """HTTP errors during fetch_new_since are caught; returns []."""
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(503))

    source = _make_source()
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [TopicRef(id=1, name="ML", query_terms=["machine learning"])]

    papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []


@respx.mock
async def test_fetch_new_since_retries_429_with_retry_after(monkeypatch):
    """arXiv 429 should retry after Retry-After before degrading to []."""
    fixture = (FIXTURES / "arxiv_new_since.xml").read_bytes()
    route = respx.get(ARXIV_API_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, content=fixture),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("paper_ingestion.sources.arxiv_source.asyncio.sleep", fake_sleep)

    source = _make_source()
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [TopicRef(id=1, name="ML", query_terms=["machine learning"])]

    papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert route.call_count == 2
    assert 2 in sleeps
    assert len(papers) == 5


@respx.mock
async def test_fetch_new_since_final_429_records_rate_limit_diagnostic(monkeypatch):
    """Final arXiv 429 remains non-throwing but is diagnosable by Pulse."""
    route = respx.get(ARXIV_API_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "11"}),
            httpx.Response(429, headers={"Retry-After": "11"}),
            httpx.Response(429, headers={"Retry-After": "11"}),
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("paper_ingestion.sources.arxiv_source.asyncio.sleep", fake_sleep)

    source = _make_source()
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [TopicRef(id=1, name="ML", query_terms=["machine learning"])]

    papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert route.call_count == 3
    assert papers == []
    assert source.last_poll_diagnostic is not None
    assert source.last_poll_diagnostic["status"] == "rate_limit"
    assert source.last_poll_diagnostic["status_code"] == 429
    assert source.last_poll_diagnostic["retry_after_s"] == 11


@respx.mock
async def test_fetch_new_since_malformed_xml_records_api_error():
    """A 200 response with malformed XML is diagnosable by Pulse."""
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, text="<feed><entry>"))

    source = _make_source()
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [TopicRef(id=1, name="ML", query_terms=["machine learning"])]

    papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    assert source.last_poll_diagnostic is not None
    assert source.last_poll_diagnostic["status"] == "error"
    assert "malformed XML" in source.last_poll_diagnostic["message"]
    assert source.last_poll_diagnostic["status_code"] == 200


# ---------------------------------------------------------------------------
# Rate limiter is called for each request
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_calls_rate_limiter():
    """_rate_limit is called once per topic query issued."""
    fixture = (FIXTURES / "arxiv_new_since.xml").read_bytes()
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    source = _make_source()
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [
        TopicRef(id=1, name="Topic A", query_terms=["topic a"]),
        TopicRef(id=2, name="Topic B", query_terms=["topic b"]),
    ]

    call_count = 0
    original_rate_limit = source._rate_limit

    async def spy_rate_limit():
        nonlocal call_count
        call_count += 1
        # Skip actual sleep to keep tests fast
        source._last_request_time = 0.0
        await original_rate_limit()

    source._rate_limit = spy_rate_limit  # type: ignore[method-assign]

    await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert call_count == 2  # one call per topic


# ---------------------------------------------------------------------------
# Multi-topic deduplication
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_deduplication():
    """Papers returned by multiple topic queries are deduplicated by external_id."""
    fixture = (FIXTURES / "arxiv_new_since.xml").read_bytes()
    # Both topic queries return the same fixture (same IDs)
    respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    source = _make_source()
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
    topics = [
        TopicRef(id=1, name="A", query_terms=["a"]),
        TopicRef(id=2, name="B", query_terms=["b"]),
    ]

    papers = await source.fetch_new_since(since=since, topics=topics, limit=100)

    ids = [p.external_id for p in papers]
    assert len(ids) == len(set(ids)), "Duplicate external_ids found"


# ---------------------------------------------------------------------------
# Date format in search_query
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_date_format():
    """submittedDate filter is formatted as YYYYMMDDHHMM on both bounds."""
    fixture = (FIXTURES / "arxiv_new_since.xml").read_bytes()
    route = respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    source = _make_source()
    since = datetime(2026, 4, 9, 13, 30, 0, tzinfo=UTC)

    await source.fetch_new_since(since=since, topics=[], limit=5)

    called_params = dict(route.calls[0].request.url.params)
    sq = called_params.get("search_query", "")
    assert "202604091330" in sq
    assert "202604091330" in sq
    assert "299912312359" in sq
    assert "29991231]" not in sq
    assert "99999999" not in sq


@respx.mock
async def test_arxiv_no_magic_ceiling_in_query_url():
    """PI-EDGE-014: fetch_new_since must use a full minute-precision ceiling.

    The magic number 99999999 is an invalid arXiv date that may be silently
    rejected by the API.  The implementation should use a far-future sentinel
    with the same YYYYMMDDHHMM precision as the lower bound.
    """
    fixture = (FIXTURES / "arxiv_new_since.xml").read_bytes()
    route = respx.get(ARXIV_API_URL).mock(return_value=httpx.Response(200, content=fixture))

    source = _make_source()
    since = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

    await source.fetch_new_since(since=since, topics=[], limit=5)

    called_params = dict(route.calls[0].request.url.params)
    sq = called_params.get("search_query", "")
    assert "99999999" not in sq, (
        "Magic ceiling 99999999 must not appear in arXiv submittedDate query — "
        "use a full YYYYMMDDHHMM sentinel instead"
    )
    assert "299912312359" in sq, "Expected far-future minute sentinel in submittedDate range"
    assert "29991231]" not in sq


@respx.mock
async def test_fetch_new_since_timeout_records_informative_diagnostic():
    """arXiv timeouts should not degrade into a blank error diagnostic."""
    respx.get(ARXIV_API_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    source = _make_source()
    since = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)

    papers = await source.fetch_new_since(since=since, topics=[], limit=5)

    assert papers == []
    assert source.last_poll_diagnostic is not None
    assert source.last_poll_diagnostic["status"] == "error"
    assert "timed out" in source.last_poll_diagnostic["message"].lower()
