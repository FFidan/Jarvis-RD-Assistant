"""Tests for OpenAlexSource.

TDD — written before the implementation was added.
Uses respx to mock the OpenAlex Works API.
Fixtures: tests/fixtures/openalex_search.json,
          tests/fixtures/openalex_single_work.json,
          tests/fixtures/openalex_new_since_2026_04.json
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
from app.models import PaperSourceConfig, SourceType, TopicRef
from app.sources.openalex_source import (
    OPENALEX_API_URL,
    OpenAlexSource,
    _reconstruct_abstract,
)

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_FIXTURE = json.loads((FIXTURES / "openalex_search.json").read_text())
SINGLE_FIXTURE = json.loads((FIXTURES / "openalex_single_work.json").read_text())
NEW_SINCE_FIXTURE = json.loads((FIXTURES / "openalex_new_since_2026_04.json").read_text())


def _make_source(api_key: str | None = "test-oa-key") -> OpenAlexSource:
    config = PaperSourceConfig(
        id=3,
        source_type=SourceType.OPENALEX,
        enabled=True,
        config={"api_key": api_key} if api_key else {},
    )
    client = httpx.AsyncClient()
    return OpenAlexSource(config, client)


# ---------------------------------------------------------------------------
# Abstract inverted index reconstruction (standalone helper tests)
# ---------------------------------------------------------------------------


def test_reconstruct_abstract_simple():
    """Simple inverted index is correctly reconstructed."""
    idx = {"Hello": [0], "world": [1]}
    assert _reconstruct_abstract(idx) == "Hello world"


def test_reconstruct_abstract_multi_position():
    """Words appearing at multiple positions are placed correctly."""
    idx = {"the": [0, 4], "cat": [1], "sat": [2], "on": [3], "mat": [5]}
    result = _reconstruct_abstract(idx)
    tokens = result.split()
    assert tokens[0] == "the"
    assert tokens[1] == "cat"
    assert tokens[4] == "the"
    assert tokens[5] == "mat"


def test_reconstruct_abstract_none_returns_none():
    """None input returns None."""
    assert _reconstruct_abstract(None) is None


def test_reconstruct_abstract_empty_dict_returns_none():
    """Empty dict returns None."""
    assert _reconstruct_abstract({}) is None


def test_reconstruct_abstract_from_fixture():
    """Fixture entry with abstract_inverted_index is correctly reconstructed."""
    work = SEARCH_FIXTURE["results"][0]
    result = _reconstruct_abstract(work["abstract_inverted_index"])
    assert result is not None
    assert "Deep" in result
    assert "natural" in result
    assert "language" in result


# ---------------------------------------------------------------------------
# search() parses fixture correctly
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_parses_fixture():
    """search() parses 5-paper OpenAlex fixture into PaperCreate list."""
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(200, json=SEARCH_FIXTURE))

    source = _make_source()
    papers = await source.search("deep learning", max_results=5)

    assert len(papers) == 5
    assert all(p.source_type == SourceType.OPENALEX for p in papers)
    p0 = papers[0]
    assert p0.title == "Deep Learning for Natural Language Processing"
    assert p0.external_id == "openalex:W3001001"
    assert p0.metadata.get("doi") == "10.1145/3001001"
    assert "Alice Johnson" in p0.authors
    assert p0.abstract is not None
    assert "Deep" in p0.abstract
    assert p0.pdf_url == "https://example.com/papers/nlp_deep.pdf"
    assert p0.published_date is not None
    assert p0.published_date.year == 2025


@respx.mock
async def test_search_handles_null_abstract():
    """Works with null abstract_inverted_index return None abstract."""
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(200, json=SEARCH_FIXTURE))

    source = _make_source()
    papers = await source.search("contrastive", max_results=5)
    # Paper at index 1 has null abstract_inverted_index
    assert papers[1].abstract is None


@respx.mock
async def test_search_handles_null_primary_location():
    """Works with null primary_location (pdf_url → None)."""
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(200, json=SEARCH_FIXTURE))

    source = _make_source()
    papers = await source.search("federated", max_results=5)
    # Paper at index 3 has null primary_location
    assert papers[3].pdf_url is None


# ---------------------------------------------------------------------------
# fetch_by_id() parses single work fixture
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_by_id_parses_fixture():
    """fetch_by_id correctly parses the single-work fixture."""
    respx.get(f"{OPENALEX_API_URL}/W9999999").mock(
        return_value=httpx.Response(200, json=SINGLE_FIXTURE)
    )

    source = _make_source()
    paper = await source.fetch_by_id("W9999999")

    assert paper is not None
    assert paper.title == "Attention Is All You Need: A Revisit"
    assert paper.external_id == "openalex:W9999999"
    assert "Ashish Vaswani" in paper.authors
    assert paper.abstract is not None
    assert "transformer" in paper.abstract
    assert paper.pdf_url == "https://arxiv.org/pdf/1706.03762"
    assert paper.published_date is not None
    assert paper.published_date.year == 2025


@respx.mock
async def test_fetch_by_id_returns_none_on_404():
    """fetch_by_id returns None when the API returns 404."""
    respx.get(f"{OPENALEX_API_URL}/W_MISSING").mock(return_value=httpx.Response(404))

    source = _make_source()
    paper = await source.fetch_by_id("W_MISSING")
    assert paper is None


@respx.mock
async def test_fetch_by_id_doi_format():
    """fetch_by_id with DOI-style ID constructs the correct URL."""
    route = respx.get(f"{OPENALEX_API_URL}/doi:10.9999/test").mock(
        return_value=httpx.Response(200, json=SINGLE_FIXTURE)
    )

    source = _make_source()
    await source.fetch_by_id("10.9999/test")
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# fetch_new_since() uses date filter in URL
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_uses_date_filter():
    """fetch_new_since includes from_publication_date in the filter param."""
    route = respx.get(OPENALEX_API_URL).mock(
        return_value=httpx.Response(200, json=NEW_SINCE_FIXTURE)
    )

    source = _make_source()
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [TopicRef(id=1, name="Mamba", query_terms=["state space models"])]

    papers = await source.fetch_new_since(since=since, topics=topics, limit=10)

    called_params = dict(route.calls[0].request.url.params)
    assert "from_publication_date:2026-04-01" in called_params.get("filter", "")
    assert len(papers) == 5


@respx.mock
async def test_fetch_new_since_empty_topics_single_request():
    """Empty topics list results in exactly one API call."""
    route = respx.get(OPENALEX_API_URL).mock(
        return_value=httpx.Response(200, json=NEW_SINCE_FIXTURE)
    )

    source = _make_source()
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    await source.fetch_new_since(since=since, topics=[], limit=50)

    assert route.call_count == 1


@respx.mock
async def test_fetch_new_since_deduplication():
    """Papers returned by multiple topic queries are deduplicated."""
    # Both topic queries return the same fixture (same IDs)
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(200, json=NEW_SINCE_FIXTURE))

    source = _make_source()
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    topics = [
        TopicRef(id=1, name="A", query_terms=["a"]),
        TopicRef(id=2, name="B", query_terms=["b"]),
    ]

    papers = await source.fetch_new_since(since=since, topics=topics, limit=100)
    ids = [p.external_id for p in papers]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Missing API key → empty list + log warning
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_missing_api_key_returns_empty(caplog):
    """Missing API key returns [] and logs at INFO level (exactly once)."""
    source = _make_source(api_key=None)

    with caplog.at_level(logging.INFO, logger="app.sources.openalex_source"):
        papers = await source.search("neural networks")

    assert papers == []
    assert any("OPENALEX_API_KEY" in r.message for r in caplog.records)


async def test_fetch_by_id_missing_api_key_returns_none():
    """fetch_by_id with missing key returns None without raising."""
    source = _make_source(api_key=None)
    result = await source.fetch_by_id("W12345")
    assert result is None


async def test_fetch_new_since_missing_api_key_returns_empty():
    """fetch_new_since with missing key returns [] without raising."""
    source = _make_source(api_key=None)
    since = datetime(2026, 4, 1, tzinfo=UTC)
    result = await source.fetch_new_since(since=since, topics=[], limit=10)
    assert result == []


async def test_missing_key_logged_only_once(caplog):
    """The missing-key INFO log is only emitted once per source instance."""
    source = _make_source(api_key=None)

    with caplog.at_level(logging.INFO, logger="app.sources.openalex_source"):
        await source.search("a")
        await source.search("b")
        await source.fetch_new_since(since=datetime(2026, 1, 1, tzinfo=UTC), topics=[], limit=5)

    key_msgs = [r for r in caplog.records if "OPENALEX_API_KEY" in r.message]
    assert len(key_msgs) == 1


# ---------------------------------------------------------------------------
# 429 → empty list
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_429_returns_empty():
    """HTTP 429 from OpenAlex search returns []."""
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(429))

    source = _make_source()
    papers = await source.search("test query")
    assert papers == []


@respx.mock
async def test_fetch_new_since_429_returns_empty():
    """HTTP 429 during fetch_new_since returns []."""
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(429))

    source = _make_source()
    since = datetime(2026, 4, 1, tzinfo=UTC)
    papers = await source.fetch_new_since(since=since, topics=[], limit=10)
    assert papers == []
