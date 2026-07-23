"""Tests for ``PubMedSource``.

NCBI E-utilities are mocked with respx and use the shared esearch and efetch XML
fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from jarvis_common.maintenance import OutboundEgressBlockedError
from lxml import etree
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.sources.pubmed_source import (
    EFETCH_URL,
    ESEARCH_URL,
    PubMedSource,
    _parse_abstract,
    _parse_authors,
    _parse_doi,
    _parse_pub_date,
)

FIXTURES = Path(__file__).parent / "fixtures"
ESEARCH_XML = (FIXTURES / "pubmed_esearch.xml").read_bytes()
EFETCH_XML = (FIXTURES / "pubmed_efetch.xml").read_bytes()


def _make_source(api_key: str | None = None) -> PubMedSource:
    config = PaperSourceConfig(
        id=4,
        source_type=SourceType.PUBMED,
        enabled=True,
        config={"api_key": api_key} if api_key else {},
    )
    client = httpx.AsyncClient()
    return PubMedSource(config, client)


# ---------------------------------------------------------------------------
# XML helper unit tests
# ---------------------------------------------------------------------------


def _article_xml(xml_str: str) -> etree._Element:
    """Wrap xml_str in a MedlineCitation shell for helper tests."""
    medline_xml = f"""<MedlineCitation>
  <PMID>99999</PMID>
  <Article>{xml_str}</Article>
</MedlineCitation>"""
    root = etree.fromstring(medline_xml.encode())
    return root.find("Article")


def test_parse_abstract_structured():
    """Structured abstract (multiple AbstractText sections) is concatenated."""
    article_el = _article_xml("""
    <Abstract>
      <AbstractText Label="BACKGROUND">Some background here.</AbstractText>
      <AbstractText Label="METHODS">Methods described here.</AbstractText>
      <AbstractText Label="RESULTS">Results reported here.</AbstractText>
    </Abstract>
    """)
    result = _parse_abstract(article_el)
    assert result is not None
    assert "BACKGROUND: Some background here." in result
    assert "METHODS: Methods described here." in result
    assert "RESULTS: Results reported here." in result


def test_parse_abstract_simple():
    """Single AbstractText (no Label) is returned as-is."""
    article_el = _article_xml("""
    <Abstract>
      <AbstractText>Simple abstract text without labels.</AbstractText>
    </Abstract>
    """)
    result = _parse_abstract(article_el)
    assert result == "Simple abstract text without labels."


def test_parse_abstract_missing_returns_none():
    """Missing Abstract element returns None."""
    article_el = _article_xml("<ArticleTitle>No abstract here</ArticleTitle>")
    assert _parse_abstract(article_el) is None


def test_parse_authors_full_name():
    """Authors with LastName + ForeName are combined as 'ForeName LastName'."""
    article_el = _article_xml("""
    <AuthorList>
      <Author><LastName>Smith</LastName><ForeName>Alice</ForeName></Author>
      <Author><LastName>Jones</LastName><ForeName>Bob</ForeName></Author>
    </AuthorList>
    """)
    authors = _parse_authors(article_el)
    assert authors == ["Alice Smith", "Bob Jones"]


def test_parse_authors_last_only():
    """Author with only LastName (no ForeName) uses just the last name."""
    article_el = _article_xml("""
    <AuthorList>
      <Author><LastName>Doe</LastName></Author>
    </AuthorList>
    """)
    authors = _parse_authors(article_el)
    assert authors == ["Doe"]


def test_parse_doi_extracts_doi():
    """DOI is extracted from ArticleIdList when IdType='doi'."""
    medline_xml = b"""<MedlineCitation>
      <PMID>12345</PMID>
      <ArticleIdList>
        <ArticleId IdType="pubmed">12345</ArticleId>
        <ArticleId IdType="doi">10.1016/j.test.2026.001</ArticleId>
      </ArticleIdList>
    </MedlineCitation>"""
    root = etree.fromstring(medline_xml)
    doi = _parse_doi(root)
    assert doi == "10.1016/j.test.2026.001"


def test_parse_doi_missing_returns_none():
    """Returns None when no doi ArticleId is present."""
    medline_xml = b"""<MedlineCitation>
      <PMID>12345</PMID>
      <ArticleIdList>
        <ArticleId IdType="pubmed">12345</ArticleId>
      </ArticleIdList>
    </MedlineCitation>"""
    root = etree.fromstring(medline_xml)
    assert _parse_doi(root) is None


def test_parse_pub_date_article_date():
    """ArticleDate is preferred when present."""
    article_el = _article_xml("""
    <ArticleDate DateType="Electronic">
      <Year>2026</Year><Month>03</Month><Day>15</Day>
    </ArticleDate>
    """)
    result = _parse_pub_date(article_el)
    assert result is not None
    assert result.year == 2026
    assert result.month == 3
    assert result.day == 15


def test_parse_pub_date_pubdate_fallback():
    """Falls back to PubDate when ArticleDate is absent."""
    article_el = _article_xml("""
    <Journal><JournalIssue><PubDate>
      <Year>2025</Year><Month>Jan</Month>
    </PubDate></JournalIssue></Journal>
    """)
    result = _parse_pub_date(article_el)
    assert result is not None
    assert result.year == 2025
    assert result.month == 1


def test_parse_pub_date_year_only():
    """Year-only PubDate defaults to January 1."""
    article_el = _article_xml("""
    <Journal><JournalIssue><PubDate>
      <Year>2025</Year>
    </PubDate></JournalIssue></Journal>
    """)
    result = _parse_pub_date(article_el)
    assert result is not None
    assert result.year == 2025
    assert result.month == 1
    assert result.day == 1


def test_parse_pub_date_missing_returns_none():
    """Returns None when neither ArticleDate nor PubDate is present."""
    article_el = _article_xml("<ArticleTitle>Test</ArticleTitle>")
    assert _parse_pub_date(article_el) is None


# ---------------------------------------------------------------------------
# search() pipeline: esearch → efetch
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_esearch_then_efetch():
    """search() calls esearch then efetch and returns parsed papers."""
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    papers = await source.search("neural networks", max_results=5)

    assert len(papers) == 5
    assert all(p.source_type == SourceType.PUBMED for p in papers)
    assert papers[0].title == "Deep Neural Networks for Medical Image Analysis"
    assert papers[0].external_id == "pubmed:38000001"


@respx.mock
async def test_search_structured_abstract_concatenated():
    """First paper from efetch fixture has structured abstract correctly concatenated."""
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    papers = await source.search("deep learning", max_results=5)

    abstract = papers[0].abstract
    assert abstract is not None
    assert "BACKGROUND:" in abstract
    assert "METHODS:" in abstract
    assert "RESULTS:" in abstract
    assert "CONCLUSIONS:" in abstract


@respx.mock
async def test_search_doi_extracted():
    """DOI is extracted from ArticleIdList into metadata."""
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    papers = await source.search("neural networks", max_results=5)

    # First paper has DOI in fixture
    assert papers[0].metadata.get("doi") == "10.1016/j.media.2026.001"
    # Third paper (PMID 38000003) has no DOI
    assert papers[2].metadata.get("doi") is None


@respx.mock
async def test_search_authors_parsed():
    """Authors are parsed with ForeName + LastName format."""
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    papers = await source.search("test", max_results=5)

    assert "Wei Zhang" in papers[0].authors
    assert "Raj Patel" in papers[0].authors


# ---------------------------------------------------------------------------
# fetch_by_id() with single PMID
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_by_id_single_pmid():
    """fetch_by_id fetches a single PMID and returns one paper."""
    # efetch with single PMID returns full fixture (we use first paper)
    single_paper_xml = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>38000001</PMID>
      <Article>
        <ArticleTitle>Single Paper Test</ArticleTitle>
        <Abstract><AbstractText>Test abstract.</AbstractText></Abstract>
        <AuthorList><Author><LastName>Test</LastName><ForeName>Author</ForeName></Author></AuthorList>
        <ArticleDate DateType="Electronic">
          <Year>2026</Year><Month>01</Month><Day>10</Day>
        </ArticleDate>
      </Article>
      <ArticleIdList>
        <ArticleId IdType="pubmed">38000001</ArticleId>
      </ArticleIdList>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=single_paper_xml))

    source = _make_source()
    paper = await source.fetch_by_id("38000001")

    assert paper is not None
    assert paper.title == "Single Paper Test"
    assert paper.external_id == "pubmed:38000001"
    assert paper.metadata["pmid"] == "38000001"


@respx.mock
async def test_fetch_by_id_strips_prefix():
    """fetch_by_id accepts 'pubmed:PMID' format."""
    single_xml = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345</PMID>
      <Article>
        <ArticleTitle>Title</ArticleTitle>
        <AuthorList/>
      </Article>
      <ArticleIdList><ArticleId IdType="pubmed">12345</ArticleId></ArticleIdList>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=single_xml))

    source = _make_source()
    paper = await source.fetch_by_id("pubmed:12345")
    assert paper is not None
    assert paper.external_id == "pubmed:12345"


# ---------------------------------------------------------------------------
# Empty esearch result → no efetch call
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_empty_esearch_no_efetch():
    """Empty esearch result skips efetch and returns []."""
    empty_esearch = b"""<?xml version="1.0"?>
<eSearchResult>
  <Count>0</Count>
  <IdList/>
</eSearchResult>"""
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=empty_esearch))
    efetch_route = respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    papers = await source.search("xyzzy quantum foam", max_results=10)

    assert papers == []
    assert efetch_route.call_count == 0


# ---------------------------------------------------------------------------
# HTTP errors → return []
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_esearch_http_error_returns_empty():
    """HTTP error from esearch returns []."""
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(500))

    source = _make_source()
    papers = await source.search("test", max_results=5)
    assert papers == []


@respx.mock
async def test_search_efetch_http_error_returns_empty():
    """HTTP error from efetch returns []."""
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(503))

    source = _make_source()
    papers = await source.search("test", max_results=5)
    assert papers == []


# ---------------------------------------------------------------------------
# fetch_new_since()
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_uses_mindate():
    """fetch_new_since passes mindate param to esearch."""
    route = respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [TopicRef(id=1, name="AI", query_terms=["artificial intelligence"])]

    papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    called_params = dict(route.calls[0].request.url.params)
    assert called_params.get("mindate") == "2026/04/01"
    assert called_params.get("datetype") == "pdat"
    assert len(papers) == 5


@respx.mock
async def test_fetch_new_since_empty_topics():
    """Empty topics uses a fallback term for date-only search."""
    route = respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)

    await source.fetch_new_since(since=since, topics=[], limit=10)

    assert route.call_count == 1


@respx.mock
async def test_fetch_new_since_429_records_rate_limit_diagnostic():
    """PubMed Pulse polling records transient upstream rate limits."""
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}))

    source = _make_source()
    papers = await source.fetch_new_since(
        since=datetime(2026, 5, 1, tzinfo=UTC),
        topics=[TopicRef(id=1, name="oncology", query_terms=["oncology"])],
        limit=10,
    )

    assert papers == []
    assert source.last_poll_diagnostic is not None
    assert source.last_poll_diagnostic["status"] == "rate_limit"
    assert source.last_poll_diagnostic["status_code"] == 429
    assert source.last_poll_diagnostic["retry_after_s"] == 7


async def test_fetch_new_since_propagates_outbound_quarantine(monkeypatch):
    """A quarantine race remains retryable instead of looking like no data."""
    source = _make_source()

    async def blocked_search(*_args, **_kwargs):
        raise OutboundEgressBlockedError("outbound work is quarantined")

    monkeypatch.setattr(source, "_esearch", blocked_search)

    with pytest.raises(OutboundEgressBlockedError, match="quarantined"):
        await source.fetch_new_since(
            since=datetime(2026, 5, 1, tzinfo=UTC),
            topics=[TopicRef(id=1, name="oncology", query_terms=["oncology"])],
            limit=10,
        )


# ---------------------------------------------------------------------------
# Missing ArticleDate → year-only fallback (integration via fixture)
# ---------------------------------------------------------------------------


@respx.mock
async def test_missing_article_date_falls_back_to_pubdate():
    """Paper with no ArticleDate falls back to PubDate (year-only → Jan 1)."""
    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    papers = await source.search("graph neural", max_results=5)

    # PMID 38000004 (index 3) has only <PubDate><Year>2025</Year></PubDate>
    paper = next(p for p in papers if p.metadata.get("pmid") == "38000004")
    assert paper.published_date is not None
    assert paper.published_date.year == 2025
    assert paper.published_date.month == 1
    assert paper.published_date.day == 1


# ---------------------------------------------------------------------------
# sort_by parameter: pub_date vs relevance
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_sort_by_date_sends_pub_date_param():
    """sort_by='date' must send sort=pub_date (underscore) to NCBI, never pub+date."""
    route = respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    await source.search("neural networks", max_results=5, sort_by="date")

    called_params = dict(route.calls[0].request.url.params)
    assert called_params.get("sort") == "pub_date", (
        f"Expected sort=pub_date, got sort={called_params.get('sort')!r}"
    )
    assert "pub+date" not in str(route.calls[0].request.url), (
        "sort param must use underscore form 'pub_date', not 'pub+date'"
    )


@respx.mock
async def test_search_sort_by_relevance_omits_sort_param():
    """sort_by='relevance' must NOT include a sort param — NCBI defaults to relevance."""
    route = respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    await source.search("neural networks", max_results=5, sort_by="relevance")

    called_params = dict(route.calls[0].request.url.params)
    assert "sort" not in called_params, (
        f"sort param should be absent for relevance sort, got: {called_params.get('sort')!r}"
    )


@respx.mock
async def test_search_default_sort_omits_sort_param():
    """Default search() call (no sort_by) must NOT include a sort param."""
    route = respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    await source.search("neural networks", max_results=5)

    called_params = dict(route.calls[0].request.url.params)
    assert "sort" not in called_params, (
        f"Default search should not include sort param, got: {called_params.get('sort')!r}"
    )


@respx.mock
async def test_pub_plus_date_never_appears_in_request():
    """Confirm 'pub+date' (the incorrect form) never appears in any request URL."""
    route = respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    # Test both sort modes
    for sort_by in ("relevance", "date"):
        await source.search("test", max_results=5, sort_by=sort_by)

    for call in route.calls:
        url_str = str(call.request.url)
        assert "pub+date" not in url_str, f"'pub+date' found in URL: {url_str}"
        assert "pub%2Bdate" not in url_str, f"encoded 'pub+date' found in URL: {url_str}"


# ---------------------------------------------------------------------------
# DRY-S2: PubMedSource symmetry — db_pool + grace + persistent limiter + run history
# ---------------------------------------------------------------------------


def _make_source_with_pool(mock_pool) -> PubMedSource:
    """PubMedSource constructed with a fake db_pool."""
    config = PaperSourceConfig(id=4, source_type=SourceType.PUBMED, enabled=True, config={})
    client = httpx.AsyncClient()
    return PubMedSource(config, client, db_pool=mock_pool)


def _make_mock_pool():
    """Return (pool, conn) mocks wired for asyncpg-style acquire()."""
    from unittest.mock import AsyncMock, MagicMock

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_pool, mock_conn


@respx.mock
async def test_fetch_new_since_calls_enforce_startup_grace():
    """fetch_new_since respects _enforce_startup_grace (called at entry)."""
    from unittest.mock import AsyncMock, patch

    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    grace_mock = AsyncMock()
    with patch("paper_ingestion.sources.base._enforce_startup_grace", grace_mock):
        await source.fetch_new_since(
            since=datetime(2026, 4, 1, tzinfo=UTC),
            topics=[TopicRef(id=1, name="AI", query_terms=["AI"])],
            limit=10,
        )

    grace_mock.assert_called_once()


@respx.mock
async def test_fetch_new_since_uses_persistent_rate_limiter_when_pool_set():
    """PersistentSourceRateLimiter is instantiated and acquire() called when db_pool is set."""
    from unittest.mock import AsyncMock, patch

    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    mock_pool, _ = _make_mock_pool()
    source = _make_source_with_pool(mock_pool)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    with patch(
        "paper_ingestion.sources.base.PersistentSourceRateLimiter",
        return_value=mock_limiter,
    ):
        await source.fetch_new_since(
            since=datetime(2026, 4, 1, tzinfo=UTC),
            topics=[TopicRef(id=1, name="AI", query_terms=["AI"])],
            limit=10,
        )

    mock_limiter.acquire.assert_called_once()


@respx.mock
async def test_fetch_new_since_inserts_run_history_on_success():
    """fetch_new_since inserts source_run_history row with status='ok' when pool set."""
    from unittest.mock import AsyncMock, patch

    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    mock_pool, mock_conn = _make_mock_pool()
    source = _make_source_with_pool(mock_pool)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    with patch(
        "paper_ingestion.sources.base.PersistentSourceRateLimiter",
        return_value=mock_limiter,
    ):
        await source.fetch_new_since(
            since=datetime(2026, 4, 1, tzinfo=UTC),
            topics=[TopicRef(id=1, name="AI", query_terms=["AI"])],
            limit=10,
        )

    all_calls = mock_conn.execute.call_args_list
    run_history_calls = [c for c in all_calls if "source_run_history" in c.args[0]]
    assert run_history_calls, "Expected source_run_history insert"
    sql, *args = run_history_calls[0].args
    assert "ok" in args
    assert "pubmed" in args


@respx.mock
async def test_fetch_new_since_calls_log_event_on_success():
    """fetch_new_since calls log_event with fetch_succeeded after a successful run."""
    from unittest.mock import AsyncMock, patch

    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    mock_pool, mock_conn = _make_mock_pool()
    source = _make_source_with_pool(mock_pool)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_event_mock = AsyncMock()
    with (
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.base.log_event", log_event_mock),
    ):
        await source.fetch_new_since(
            since=datetime(2026, 4, 1, tzinfo=UTC),
            topics=[TopicRef(id=1, name="AI", query_terms=["AI"])],
            limit=10,
        )

    log_event_mock.assert_called_once()
    call_kwargs = log_event_mock.call_args.kwargs
    assert call_kwargs.get("message") == "fetch_succeeded"
    assert call_kwargs.get("source") == "pubmed"


@respx.mock
async def test_fetch_new_since_records_run_history_status_error_on_exception():
    """fetch_new_since inserts source_run_history with status='error' and returns [] (no re-raise)."""
    from unittest.mock import AsyncMock, patch

    respx.get(ESEARCH_URL).mock(side_effect=RuntimeError("unexpected upstream failure"))

    mock_pool, mock_conn = _make_mock_pool()
    source = _make_source_with_pool(mock_pool)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    log_event_mock = AsyncMock()
    with (
        patch(
            "paper_ingestion.sources.base.PersistentSourceRateLimiter",
            return_value=mock_limiter,
        ),
        patch("paper_ingestion.sources.base.log_event", log_event_mock),
    ):
        result = await source.fetch_new_since(
            since=datetime(2026, 4, 1, tzinfo=UTC),
            topics=[TopicRef(id=1, name="AI", query_terms=["AI"])],
            limit=10,
            user_id=7,
        )

    # Exception is caught and partial results returned; no re-raise.
    assert result == []

    all_calls = mock_conn.execute.call_args_list
    run_history_calls = [c for c in all_calls if "source_run_history" in c.args[0]]
    assert run_history_calls, "Expected source_run_history insert on error"

    log_event_mock.assert_called()
    call_kwargs = log_event_mock.call_args.kwargs
    assert call_kwargs.get("message") == "fetch_failed"
    assert call_kwargs.get("source") == "pubmed"


# ---------------------------------------------------------------------------
# per-term failure returns partial results, no exception propagated
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_partial_results_on_per_term_failure():
    """Second term raising must not abort; term-1 results are returned."""
    term1_esearch = b"""<?xml version="1.0"?>
<eSearchResult>
  <Count>1</Count>
  <IdList><Id>11111111</Id></IdList>
</eSearchResult>"""
    single_paper_xml = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>11111111</PMID>
      <Article>
        <ArticleTitle>Term One Paper</ArticleTitle>
        <AuthorList/>
      </Article>
      <ArticleIdList><ArticleId IdType="pubmed">11111111</ArticleId></ArticleIdList>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""

    # First call to esearch (term-1): succeeds.  Second call (term-2): raises.
    esearch_route = respx.get(ESEARCH_URL)
    call_count = {"n": 0}

    def esearch_side_effect(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, content=term1_esearch)
        raise RuntimeError("simulated upstream failure on term-2")

    esearch_route.mock(side_effect=esearch_side_effect)
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=single_paper_xml))

    source = _make_source()
    papers = await source.fetch_new_since(
        since=datetime(2026, 4, 1, tzinfo=UTC),
        topics=[
            TopicRef(id=1, name="Topic A", query_terms=["topic_a"]),
            TopicRef(id=2, name="Topic B", query_terms=["topic_b"]),
        ],
        limit=50,
    )

    # Term-1 paper is returned; no exception propagated.
    assert len(papers) == 1
    assert papers[0].external_id == "pubmed:11111111"


# ---------------------------------------------------------------------------
# M10a: error path forwards retry_after_s from last_poll_diagnostic
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_error_path_forwards_retry_after_s():
    """M10a: when last_poll_diagnostic carries retry_after_s, the error branch
    forwards it to _record_fetch_outcome → update_last_request."""
    from unittest.mock import AsyncMock, patch

    # esearch returns 429 (sets last_poll_diagnostic with retry_after_s=42),
    # then the second call raises to trigger the outer exception branch.
    call_count = {"n": 0}

    def esearch_side_effect(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "42"})
        raise RuntimeError("unexpected failure after rate-limit")

    respx.get(ESEARCH_URL).mock(side_effect=esearch_side_effect)

    mock_pool, mock_conn = _make_mock_pool()
    source = _make_source_with_pool(mock_pool)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    with patch(
        "paper_ingestion.sources.base.PersistentSourceRateLimiter",
        return_value=mock_limiter,
    ):
        await source.fetch_new_since(
            since=datetime(2026, 4, 1, tzinfo=UTC),
            topics=[
                TopicRef(id=1, name="A", query_terms=["alpha"]),
                TopicRef(id=2, name="B", query_terms=["beta"]),
            ],
            limit=10,
            user_id=3,
        )

    # update_last_request must have been called; the retry_after_s from the
    # 429 diagnostic (42 s) must be forwarded as a keyword argument.
    mock_limiter.update_last_request.assert_called_once()
    _call = mock_limiter.update_last_request.call_args
    assert _call.kwargs.get("retry_after_s") == 42, (
        f"Expected retry_after_s=42 forwarded to update_last_request, got: {_call}"
    )


# ---------------------------------------------------------------------------
# rate limiter acquire called once per term in the loop
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_rate_limiter_acquired_per_term():
    """p_limiter.acquire() must fire once per term, not once per call."""
    from unittest.mock import AsyncMock, patch

    respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    mock_pool, _ = _make_mock_pool()
    source = _make_source_with_pool(mock_pool)

    mock_limiter = AsyncMock()
    mock_limiter.acquire = AsyncMock()
    mock_limiter.update_last_request = AsyncMock()

    with patch(
        "paper_ingestion.sources.base.PersistentSourceRateLimiter",
        return_value=mock_limiter,
    ):
        await source.fetch_new_since(
            since=datetime(2026, 4, 1, tzinfo=UTC),
            topics=[
                TopicRef(id=1, name="A", query_terms=["alpha"]),
                TopicRef(id=2, name="B", query_terms=["beta"]),
            ],
            limit=100,
        )

    # Two terms → acquire must have been called exactly twice.
    assert mock_limiter.acquire.call_count == 2, (
        f"Expected 2 acquire() calls (one per term), got {mock_limiter.acquire.call_count}"
    )


# ---------------------------------------------------------------------------
# NCBI client identification headers and query parameters
# ---------------------------------------------------------------------------


@respx.mock
async def test_esearch_carries_ncbi_identification_headers_and_params():
    """An esearch request identifies the client in headers and query parameters."""
    route = respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    source = _make_source()
    # Inject a known email so we can assert it is forwarded.
    source._ncbi_email = "test@example.com"
    source._ncbi_tool = "TestTool"
    source._ncbi_user_agent = "TestTool/1.0 (tool=TestTool; contact=test@example.com)"

    await source.search("neural networks", max_results=5)

    assert route.call_count >= 1
    request = route.calls[0].request

    # User-Agent header must be set.
    ua = request.headers.get("user-agent", "")
    assert "TestTool" in ua, f"Expected 'TestTool' in User-Agent, got: {ua!r}"

    # tool= and email= query params must be present.
    params = dict(request.url.params)
    assert params.get("tool") == "TestTool", f"Expected tool=TestTool, got: {params.get('tool')!r}"
    assert params.get("email") == "test@example.com", (
        f"Expected email=test@example.com, got: {params.get('email')!r}"
    )


@respx.mock
async def test_esearch_omits_email_param_when_ncbi_email_empty():
    """When ncbi_email is empty, email param must be absent; tool param must always be present."""
    route = respx.get(ESEARCH_URL).mock(return_value=httpx.Response(200, content=ESEARCH_XML))
    respx.get(EFETCH_URL).mock(return_value=httpx.Response(200, content=EFETCH_XML))

    # _make_source() constructs with default ncbi_email="" (empty)
    source = _make_source()
    assert source._ncbi_email == "", "Expected default ncbi_email to be empty string"

    await source.search("neural networks", max_results=5)

    assert route.call_count >= 1
    request = route.calls[0].request

    # Verify email param is absent and tool param is present with default value.
    params = dict(request.url.params)
    assert "email" not in params, (
        f"email param should be absent when ncbi_email is empty, got: {params.get('email')!r}"
    )
    assert params.get("tool") == "JARVIS-RD", (
        f"Expected default tool=JARVIS-RD, got: {params.get('tool')!r}"
    )
