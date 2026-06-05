"""Tests for LG-B3: all three source types emit category='source' log_event rows.

B1-05: Consolidated from test_source_arxiv_events.py, test_source_openalex_events.py,
and test_source_s2_events.py — 3 files × 4 behaviors → parametrized over sources.
The sibling files (openalex / s2) import nothing; this module is the sole container.

Covered behaviors (12 cases = 3 sources × 4):
  1. emits_source_event_on_success
  2. emits_source_event_on_rate_limit
  3. emits_source_event_on_http_error
  4. no_log_event_without_pool
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.arxiv_source import ARXIV_API_URL, ArxivSource
from paper_ingestion.sources.openalex_source import OPENALEX_API_URL, OpenAlexSource
from paper_ingestion.sources.semantic_scholar_source import S2_API_URL, SemanticScholarSource

from tests._source_fakes import mock_log_event_pool

FIXTURES = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Per-source fixture data
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Source factories
# ---------------------------------------------------------------------------


def _make_arxiv_source(db_pool=None) -> ArxivSource:
    config = PaperSourceConfig(
        id=1,
        source_type=SourceType.ARXIV,
        enabled=True,
        config={},
    )
    return ArxivSource(config, httpx.AsyncClient(), db_pool=db_pool)


def _make_openalex_source(db_pool=None) -> OpenAlexSource:
    config = PaperSourceConfig(
        id=3,
        source_type=SourceType.OPENALEX,
        enabled=True,
        config={"api_key": "test-oa-key"},
    )
    return OpenAlexSource(config, httpx.AsyncClient(), db_pool=db_pool)


def _make_s2_source(db_pool=None) -> SemanticScholarSource:
    config = PaperSourceConfig(
        id=2,
        source_type=SourceType.SEMANTIC_SCHOLAR,
        enabled=True,
        config={},
    )
    return SemanticScholarSource(config, httpx.AsyncClient(), db_pool=db_pool)


def _make_topic(name: str, terms: list[str] | None = None) -> TopicRef:
    return TopicRef(id=1, name=name, query_terms=terms or [name])


# ---------------------------------------------------------------------------
# Per-source parametrize table — "on_success" (4 assertions each)
# ---------------------------------------------------------------------------

_SUCCESS_PARAMS = [
    pytest.param(
        "arxiv",
        lambda pool: _make_arxiv_source(db_pool=pool),
        "paper_ingestion.sources.arxiv_source",
        lambda: respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(
                200, content=(FIXTURES / "arxiv_new_since.xml").read_bytes()
            )
        ),
        id="arxiv",
    ),
    pytest.param(
        "openalex",
        lambda pool: _make_openalex_source(db_pool=pool),
        "paper_ingestion.sources.openalex_source",
        lambda: respx.get(OPENALEX_API_URL).mock(
            return_value=httpx.Response(200, json=_OA_RESPONSE)
        ),
        id="openalex",
    ),
    pytest.param(
        "semantic_scholar",
        lambda pool: _make_s2_source(db_pool=pool),
        "paper_ingestion.sources.semantic_scholar_source",
        lambda: respx.get(f"{S2_API_URL}/paper/search").mock(
            return_value=httpx.Response(200, json={"data": [_S2_PAPER_ITEM]})
        ),
        id="semantic_scholar",
    ),
]


@pytest.mark.parametrize("source_name,make_source,module,setup_mock", _SUCCESS_PARAMS)
@respx.mock
async def test_source_emits_event_on_success(source_name, make_source, module, setup_mock):
    """fetch_new_since emits category='source' / message='fetch_succeeded' on HTTP 200."""
    setup_mock()

    pool = mock_log_event_pool()
    source = make_source(pool)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("neural ODE")]

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_calls: list[dict] = []

    async def _capture(**kwargs):
        log_calls.append(kwargs)

    with (
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter", return_value=mock_limiter
        ),
        patch("paper_ingestion.sources.base.log_event", side_effect=_capture),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert len(papers) > 0
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    success_events = [c for c in source_events if c.get("message") == "fetch_succeeded"]
    assert success_events, "Expected a log_event with message='fetch_succeeded'"
    ev = success_events[0]
    assert ev["source"] == source_name
    assert ev["level"] == "info"
    assert ev["context"]["http_status"] == 200
    assert "papers_fetched" in ev["context"]


# ---------------------------------------------------------------------------
# on_rate_limit — arxiv diverges (3×429 + asyncio.sleep patch)
# ---------------------------------------------------------------------------


@respx.mock
async def test_arxiv_emits_source_event_on_rate_limit(monkeypatch):
    """ArxivSource: fetch_new_since emits message='rate_limited' on 429 (exhausts 3 retries)."""
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("paper_ingestion.sources.arxiv_source.asyncio.sleep", fake_sleep)
    respx.get(ARXIV_API_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(429, headers={"Retry-After": "5"}),
        ]
    )

    pool = mock_log_event_pool()
    source = _make_arxiv_source(db_pool=pool)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("ML")]

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_calls: list[dict] = []

    async def _capture(**kwargs):
        log_calls.append(kwargs)

    with (
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.base.log_event", side_effect=_capture),
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


_RATE_LIMIT_PARAMS = [
    pytest.param(
        "openalex",
        lambda pool: _make_openalex_source(db_pool=pool),
        "paper_ingestion.sources.openalex_source",
        lambda: respx.get(OPENALEX_API_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"})
        ),
        True,  # assert context["http_status"] == 429
        id="openalex",
    ),
    pytest.param(
        "semantic_scholar",
        lambda pool: _make_s2_source(db_pool=pool),
        "paper_ingestion.sources.semantic_scholar_source",
        lambda: respx.get(f"{S2_API_URL}/paper/search").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "10"})
        ),
        False,  # s2 original test doesn't assert context["http_status"]
        id="semantic_scholar",
    ),
]


@pytest.mark.parametrize(
    "source_name,make_source,module,setup_mock,check_status_ctx", _RATE_LIMIT_PARAMS
)
@respx.mock
async def test_source_emits_event_on_rate_limit(
    source_name, make_source, module, setup_mock, check_status_ctx
):
    """fetch_new_since emits message='rate_limited' / level='warning' on 429."""
    setup_mock()

    pool = mock_log_event_pool()
    source = make_source(pool)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("ML")]

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_calls: list[dict] = []

    async def _capture(**kwargs):
        log_calls.append(kwargs)

    with (
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter", return_value=mock_limiter
        ),
        patch("paper_ingestion.sources.base.log_event", side_effect=_capture),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    rate_events = [c for c in source_events if c.get("message") == "rate_limited"]
    assert rate_events, "Expected a log_event with message='rate_limited'"
    ev = rate_events[0]
    assert ev["source"] == source_name
    assert ev["level"] == "warning"
    if check_status_ctx:
        assert ev["context"]["http_status"] == 429


# ---------------------------------------------------------------------------
# on_http_error — arxiv has a weaker assertion (may be rate_limited or fetch_failed)
# ---------------------------------------------------------------------------


@respx.mock
async def test_arxiv_emits_source_event_on_http_error():
    """ArxivSource: pure ConnectError (no prior 429) emits fetch_failed, not rate_limited."""
    respx.get(ARXIV_API_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    pool = mock_log_event_pool()
    source = _make_arxiv_source(db_pool=pool)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("ML")]

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_calls: list[dict] = []

    async def _capture(**kwargs):
        log_calls.append(kwargs)

    with (
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.base.log_event", side_effect=_capture),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    assert all(c["source"] == "arxiv" for c in source_events)
    # Pure connection error (no preceding 429) must not be misclassified as rate_limited.
    assert not any(c.get("message") == "rate_limited" for c in source_events)


@respx.mock
async def test_arxiv_emits_rate_limited_event_on_429_then_connect_error(monkeypatch):
    """ArxivSource: 429 on early attempts + ConnectError on final attempt → rate_limited event.

    arXiv may drop the connection after repeated throttling. The self-healing
    cooldown must fire (rate_limited event) so the source is not stuck in error state.
    """
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("paper_ingestion.sources.arxiv_source.asyncio.sleep", fake_sleep)
    respx.get(ARXIV_API_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.ConnectError(""),
        ]
    )

    pool = mock_log_event_pool()
    source = _make_arxiv_source(db_pool=pool)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("ML")]

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_calls: list[dict] = []

    async def _capture(**kwargs):
        log_calls.append(kwargs)

    with (
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.base.log_event", side_effect=_capture),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    rate_events = [c for c in source_events if c.get("message") == "rate_limited"]
    assert rate_events, "Expected a log_event with message='rate_limited' after 429+ConnectError"
    ev = rate_events[0]
    assert ev["source"] == "arxiv"
    assert ev["level"] == "warning"


_HTTP_ERROR_PARAMS = [
    pytest.param(
        "openalex",
        lambda pool: _make_openalex_source(db_pool=pool),
        "paper_ingestion.sources.openalex_source",
        lambda: respx.get(OPENALEX_API_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        ),
        id="openalex",
    ),
    pytest.param(
        "semantic_scholar",
        lambda pool: _make_s2_source(db_pool=pool),
        "paper_ingestion.sources.semantic_scholar_source",
        lambda: respx.get(f"{S2_API_URL}/paper/search").mock(
            side_effect=httpx.ConnectError("connection refused")
        ),
        id="semantic_scholar",
    ),
]


@pytest.mark.parametrize("source_name,make_source,module,setup_mock", _HTTP_ERROR_PARAMS)
@respx.mock
async def test_source_emits_event_on_http_error(source_name, make_source, module, setup_mock):
    """fetch_new_since emits message='fetch_failed' / level='error' on ConnectError."""
    setup_mock()

    pool = mock_log_event_pool()
    source = make_source(pool)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("ML")]

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_calls: list[dict] = []

    async def _capture(**kwargs):
        log_calls.append(kwargs)

    with (
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter", return_value=mock_limiter
        ),
        patch("paper_ingestion.sources.base.log_event", side_effect=_capture),
    ):
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    assert papers == []
    source_events = [c for c in log_calls if c.get("category") == "source"]
    assert source_events, "Expected at least one log_event with category='source'"
    fail_events = [c for c in source_events if c.get("message") == "fetch_failed"]
    assert fail_events, "Expected a log_event with message='fetch_failed'"
    ev = fail_events[0]
    assert ev["source"] == source_name
    assert ev["level"] == "error"


# ---------------------------------------------------------------------------
# no_log_event_without_pool — fully parametrized across all 3 sources
# ---------------------------------------------------------------------------

_NO_POOL_PARAMS = [
    pytest.param(
        lambda: _make_arxiv_source(db_pool=None),
        "paper_ingestion.sources.arxiv_source",
        lambda: respx.get(ARXIV_API_URL).mock(
            return_value=httpx.Response(
                200, content=(FIXTURES / "arxiv_new_since.xml").read_bytes()
            )
        ),
        id="arxiv",
    ),
    pytest.param(
        lambda: _make_openalex_source(db_pool=None),
        "paper_ingestion.sources.openalex_source",
        lambda: respx.get(OPENALEX_API_URL).mock(
            return_value=httpx.Response(200, json=_OA_RESPONSE)
        ),
        id="openalex",
    ),
    pytest.param(
        lambda: _make_s2_source(db_pool=None),
        "paper_ingestion.sources.semantic_scholar_source",
        lambda: respx.get(f"{S2_API_URL}/paper/search").mock(
            return_value=httpx.Response(200, json={"data": [_S2_PAPER_ITEM]})
        ),
        id="semantic_scholar",
    ),
]


@pytest.mark.parametrize("make_source,module,setup_mock", _NO_POOL_PARAMS)
@respx.mock
async def test_source_no_log_event_without_pool(make_source, module, setup_mock):
    """fetch_new_since does NOT call log_event when db_pool is None."""
    setup_mock()
    source = make_source()
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [_make_topic("neural ODE")]

    with patch("paper_ingestion.sources.base.log_event", new_callable=AsyncMock) as mock_log:
        papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    mock_log.assert_not_called()
    assert isinstance(papers, list)
