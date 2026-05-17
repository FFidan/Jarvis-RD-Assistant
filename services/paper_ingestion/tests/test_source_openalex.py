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
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.pdf_processor import ALLOWED_PDF_DOMAINS
from paper_ingestion.sources.openalex_source import (
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


def test_reconstruct_abstract_gap_positions_no_double_spaces():
    """Gapped inverted index (positions 1 and 3 assigned, position 2 empty)
    must not produce double spaces, and word order must be preserved.

    OpenAlex can omit positions for punctuation or formatting tokens.
    The reconstructed abstract must be single-spaced clean text.
    """
    # Position 0="Neural", 1="networks", pos 2 is a gap, 3="learn", 4="representations"
    idx = {
        "Neural": [0],
        "networks": [1],
        "learn": [3],
        "representations": [4],
    }
    result = _reconstruct_abstract(idx)
    assert result is not None
    assert "  " not in result, f"Double space found in: {result!r}"
    # Words appear in position order
    assert result.index("Neural") < result.index("networks")
    assert result.index("networks") < result.index("learn")
    assert result.index("learn") < result.index("representations")


def test_reconstruct_abstract_multiple_gaps_no_double_spaces():
    """Multiple consecutive gap positions also produce single-spaced output."""
    # Positions 0,1 filled; 2,3,4 gap; 5 filled
    idx = {"first": [0], "second": [1], "sixth": [5]}
    result = _reconstruct_abstract(idx)
    assert result is not None
    assert "  " not in result, f"Double space found in: {result!r}"
    assert result == "first second sixth"


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
    # example.com is not in ALLOWED_PDF_DOMAINS → filtered out by PI-EDGE-007 validation
    assert p0.pdf_url is None
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

    with caplog.at_level(logging.INFO, logger="paper_ingestion.sources.openalex_source"):
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

    with caplog.at_level(logging.INFO, logger="paper_ingestion.sources.openalex_source"):
        await source.search("a")
        await source.search("b")
        await source.fetch_new_since(since=datetime(2026, 1, 1, tzinfo=UTC), topics=[], limit=5)

    key_msgs = [r for r in caplog.records if "OPENALEX_API_KEY" in r.message]
    assert len(key_msgs) == 1


# ---------------------------------------------------------------------------
# ISSUE-OOM-1: _reconstruct_abstract allocation cap (_MAX_ABSTRACT_TOKENS)
# ---------------------------------------------------------------------------


def test_reconstruct_abstract_giant_position_no_oom(caplog):
    """A crafted inverted index with a huge position must NOT raise MemoryError.

    An adversarial/malformed OpenAlex work with a position like 1_000_000_000
    previously triggered list("") * (max_pos + 1) → multi-GB allocation →
    MemoryError (a BaseException, escaping the except Exception guards around
    _parse_work callers → worker crash for all users).

    After the fix:
    - No MemoryError is raised.
    - The return value is a non-empty string containing the in-range token ("b").
    - A WARNING is logged at the openalex_source logger.
    """
    giant_idx = {"a": [1_000_000_000], "b": [0]}

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.sources.openalex_source"):
        result = _reconstruct_abstract(giant_idx)

    # Must not raise; must return a bounded non-empty string
    assert result is not None
    assert isinstance(result, str)
    assert len(result) > 0
    # "b" is at position 0, well within the cap; it must appear in the result
    assert "b" in result
    # "a" is at position 1_000_000_000, beyond the cap; it is silently dropped
    assert "a" not in result
    # A WARNING must have been emitted about the oversized index
    warn_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warn_records, "Expected a WARNING log for oversized abstract index"


def test_reconstruct_abstract_normal_input_unchanged():
    """Normal small inverted index is completely unaffected by the cap.

    Behaviour must be byte-identical to the pre-fix implementation for all
    valid inputs.
    """
    idx = {"Hello": [0], "world": [1]}
    assert _reconstruct_abstract(idx) == "Hello world"


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
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "9"}))

    source = _make_source()
    since = datetime(2026, 4, 1, tzinfo=UTC)
    papers = await source.fetch_new_since(since=since, topics=[], limit=10)
    assert papers == []
    assert source.last_poll_diagnostic is not None
    assert source.last_poll_diagnostic["status"] == "rate_limit"
    assert source.last_poll_diagnostic["status_code"] == 429
    assert source.last_poll_diagnostic["retry_after_s"] == 9


# ---------------------------------------------------------------------------
# PI-EDGE-007: pdf_url validated against ALLOWED_PDF_DOMAINS allowlist
# ---------------------------------------------------------------------------


@respx.mock
async def test_openalex_pdf_url_validated_against_allowlist(caplog):
    """_parse_work: pdf_url with a hostname not in ALLOWED_PDF_DOMAINS is set to None.

    Scenario A: unrecognised domain → pdf_url discarded, INFO logged.
    Scenario B: arxiv.org (in allowlist) → pdf_url preserved.
    """
    allowed_host = next(iter(ALLOWED_PDF_DOMAINS))  # e.g. "arxiv.org"
    allowed_url = f"https://{allowed_host}/pdf/1234.5678"
    blocked_url = "https://evil.example.com/paper.pdf"

    def _work(pdf_url_value: str | None) -> dict:
        return {
            "id": "https://openalex.org/W9000001",
            "title": "Test Paper",
            "display_name": "Test Paper",
            "doi": None,
            "publication_date": "2026-01-01",
            "authorships": [],
            "abstract_inverted_index": None,
            "primary_location": {"pdf_url": pdf_url_value},
        }

    source = _make_source()

    # Scenario A: blocked domain → None, INFO log emitted
    with caplog.at_level(logging.INFO, logger="paper_ingestion.sources.openalex_source"):
        respx.get(OPENALEX_API_URL).mock(
            return_value=httpx.Response(200, json={"meta": {}, "results": [_work(blocked_url)]})
        )
        papers_blocked = await source.search("test")

    assert papers_blocked[0].pdf_url is None
    assert any("ALLOWED_PDF_DOMAINS" in r.message for r in caplog.records)

    caplog.clear()

    # Scenario B: allowed domain → pdf_url kept
    with caplog.at_level(logging.INFO, logger="paper_ingestion.sources.openalex_source"):
        respx.get(OPENALEX_API_URL).mock(
            return_value=httpx.Response(200, json={"meta": {}, "results": [_work(allowed_url)]})
        )
        papers_allowed = await source.search("test")

    assert papers_allowed[0].pdf_url == allowed_url
    # No ALLOWED_PDF_DOMAINS log for an accepted URL
    assert not any("ALLOWED_PDF_DOMAINS" in r.message for r in caplog.records)
