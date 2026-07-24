"""OpenAlex source parsing, transport, scheduling, and failure-path tests."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from jarvis_common.maintenance import OutboundEgressBlockedError
from paper_ingestion.models import PaperSourceConfig, SourceType, TopicRef
from paper_ingestion.pdf_processor import ALLOWED_PDF_DOMAINS
from paper_ingestion.sources.openalex_source import (
    OPENALEX_API_URL,
    OpenAlexSource,
    _reconstruct_abstract,
)
from pydantic import SecretStr

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


def _make_source_with_keys(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_key: str | None,
    settings_key: str | None,
) -> OpenAlexSource:
    import paper_ingestion.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_paper_ingestion_settings",
        lambda: SimpleNamespace(
            openalex_api_key=SecretStr(settings_key) if settings_key is not None else None
        ),
    )
    config = PaperSourceConfig(
        id=3,
        source_type=SourceType.OPENALEX,
        enabled=True,
        config={"api_key": database_key},
    )
    return OpenAlexSource(config, httpx.AsyncClient())


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
    # example.com is not in ALLOWED_PDF_DOMAINS → filtered out by domain-allowlist validation
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
# Missing API key → no request and an empty return shape
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_missing_api_key_returns_empty(caplog):
    """Missing API key returns [] and logs at INFO level (exactly once)."""
    route = respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(200, json=SEARCH_FIXTURE))
    source = _make_source(api_key=None)

    with caplog.at_level(logging.INFO, logger="paper_ingestion.sources.openalex_source"):
        papers = await source.search("neural networks")

    assert papers == []
    assert route.call_count == 0
    assert any("OPENALEX_API_KEY" in r.message for r in caplog.records)


@respx.mock
async def test_fetch_by_id_missing_api_key_returns_none_without_http_request():
    """fetch_by_id with missing key returns None without raising."""
    route = respx.get(f"{OPENALEX_API_URL}/W12345").mock(
        return_value=httpx.Response(200, json=SINGLE_FIXTURE)
    )
    source = _make_source(api_key=None)
    result = await source.fetch_by_id("W12345")
    assert result is None
    assert route.call_count == 0


@respx.mock
async def test_fetch_new_since_missing_api_key_returns_empty_without_http_request():
    """fetch_new_since with missing key returns [] without raising."""
    route = respx.get(OPENALEX_API_URL).mock(
        return_value=httpx.Response(200, json=NEW_SINCE_FIXTURE)
    )
    source = _make_source(api_key=None)
    since = datetime(2026, 4, 1, tzinfo=UTC)
    result = await source.fetch_new_since(since=since, topics=[], limit=10)
    assert result == []
    assert route.call_count == 0


async def test_missing_key_logged_only_once(caplog):
    """The missing-key INFO log is only emitted once per source instance."""
    source = _make_source(api_key=None)

    with caplog.at_level(logging.INFO, logger="paper_ingestion.sources.openalex_source"):
        await source.search("a")
        await source.search("b")
        await source.fetch_new_since(since=datetime(2026, 1, 1, tzinfo=UTC), topics=[], limit=5)

    key_msgs = [r for r in caplog.records if "OPENALEX_API_KEY" in r.message]
    assert len(key_msgs) == 1


@pytest.mark.parametrize(
    ("database_key", "settings_key", "expected"),
    [
        ("  database-key  ", "settings-key", "database-key"),
        ("   ", "  settings-key  ", "settings-key"),
        ("   ", "\t ", None),
    ],
)
def test_api_key_resolution_strips_values_and_falls_back_from_blank_database_key(
    monkeypatch, database_key, settings_key, expected
):
    source = _make_source_with_keys(
        monkeypatch,
        database_key=database_key,
        settings_key=settings_key,
    )
    assert source._api_key == expected


@respx.mock
async def test_whitespace_database_and_settings_keys_never_open_http(monkeypatch):
    source = _make_source_with_keys(
        monkeypatch,
        database_key="  ",
        settings_key="\t ",
    )
    try:
        assert await source.search("test") == []
        assert await source.fetch_by_id("W12345") is None
        assert (
            await source.fetch_new_since(
                since=datetime(2026, 1, 1, tzinfo=UTC),
                topics=[],
                limit=5,
            )
            == []
        )
        assert not respx.calls
    finally:
        await source.http_client.aclose()


@pytest.mark.parametrize("operation", ["search", "fetch_by_id", "fetch_new_since"])
async def test_http_error_never_exposes_query_key_in_logs_or_diagnostics(
    monkeypatch, caplog, operation
):
    secret = "openalex-negative-proof-secret"
    source = _make_source(secret)
    attempted_urls: list[str] = []

    async def fail_request(url, *, params, timeout):
        del timeout
        request = httpx.Request("GET", url, params=params)
        attempted_urls.append(str(request.url))
        raise httpx.ConnectError(
            f"connection failed for {request.url}",
            request=request,
        )

    monkeypatch.setattr(source.http_client, "get", AsyncMock(side_effect=fail_request))
    monkeypatch.setattr(source, "_rate_limit", AsyncMock())
    monkeypatch.setattr(source, "apply_startup_grace", AsyncMock())
    record_outcome = AsyncMock()
    monkeypatch.setattr(source, "_record_fetch_outcome", record_outcome)

    try:
        with caplog.at_level(logging.INFO):
            if operation == "search":
                assert await source.search("test") == []
            elif operation == "fetch_by_id":
                assert await source.fetch_by_id("W12345") is None
            else:
                assert (
                    await source.fetch_new_since(
                        since=datetime(2026, 1, 1, tzinfo=UTC),
                        topics=[],
                        limit=5,
                    )
                    == []
                )

        assert attempted_urls and secret in attempted_urls[0]
        emitted = (
            caplog.text + repr(source.last_poll_diagnostic) + repr(record_outcome.await_args_list)
        )
        assert secret not in emitted
        if operation == "fetch_new_since":
            assert source.last_poll_diagnostic == {
                "status": "error",
                "message": "OpenAlex request failed. It will retry automatically later.",
                "status_code": None,
                "retry_after_s": None,
                "settings_hint": None,
            }
            assert record_outcome.await_args.kwargs["log_context"] == {
                "http_status": None,
                "exception": "ConnectError",
            }
        else:
            assert source.last_poll_diagnostic is None
            record_outcome.assert_not_awaited()
    finally:
        await source.http_client.aclose()


async def test_fetch_new_since_rechecks_quarantine_after_rate_limit(monkeypatch, tmp_path):
    """A restore beginning during rate-limit wait prevents the outbound request."""
    source = _make_source()
    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))

    class ActivatingLimiter:
        async def acquire(self) -> None:
            quarantine.touch()

    monkeypatch.setattr(
        source,
        "make_persistent_rate_limiter",
        lambda **_kwargs: ActivatingLimiter(),
    )

    try:
        with pytest.raises(OutboundEgressBlockedError, match="credential review"):
            await source.fetch_new_since(
                since=datetime(2026, 1, 1, tzinfo=UTC),
                topics=[],
                limit=5,
            )
    finally:
        await source.http_client.aclose()


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
# pdf_url validated against ALLOWED_PDF_DOMAINS allowlist
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


# ---------------------------------------------------------------------------
# MED-PI-EXT-02: HTTP 503 + Retry-After → rate_limit (not server_error)
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_new_since_503_with_retry_after_classified_as_rate_limit():
    """503 + Retry-After header is classified as rate_limit, not error.

    When OpenAlex returns HTTP 503 with a Retry-After header the error should
    be classified as ``rate_limit`` (matching 429 behaviour) so the Pulse
    scheduler applies a backoff rather than an immediate retry.
    """
    respx.get(OPENALEX_API_URL).mock(
        return_value=httpx.Response(503, headers={"Retry-After": "30"})
    )

    source = _make_source()
    since = datetime(2026, 4, 1, tzinfo=UTC)
    papers = await source.fetch_new_since(since=since, topics=[], limit=10)

    assert papers == []
    assert source.last_poll_diagnostic is not None
    assert source.last_poll_diagnostic["status"] == "rate_limit", (
        f"Expected 'rate_limit', got {source.last_poll_diagnostic['status']!r}"
    )
    assert source.last_poll_diagnostic["status_code"] == 503


@respx.mock
async def test_fetch_new_since_503_without_retry_after_classified_as_error():
    """503 WITHOUT Retry-After stays classified as error (not rate_limit)."""
    respx.get(OPENALEX_API_URL).mock(return_value=httpx.Response(503))

    source = _make_source()
    since = datetime(2026, 4, 1, tzinfo=UTC)
    papers = await source.fetch_new_since(since=since, topics=[], limit=10)

    assert papers == []
    assert source.last_poll_diagnostic is not None
    assert source.last_poll_diagnostic["status"] != "rate_limit", (
        "503 without Retry-After must NOT be classified as rate_limit"
    )
