"""Source HTTP failure, URL validation, credential, and quarantine tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import respx
from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.pdf_processor import ALLOWED_PDF_DOMAINS
from jarvis_common.maintenance import OutboundEgressBlockedError
from paper_ingestion.sources.arxiv_source import ArxivSource
from paper_ingestion.sources.openalex_source import OPENALEX_API_URL, OpenAlexSource
from paper_ingestion.sources.pubmed_source import PubMedSource
from paper_ingestion.sources.semantic_scholar_source import S2_API_URL, SemanticScholarSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_s2_source(
    api_key: str | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> SemanticScholarSource:
    config = PaperSourceConfig(
        id=2,
        source_type=SourceType.SEMANTIC_SCHOLAR,
        enabled=True,
        config={"api_key": api_key} if api_key else {},
    )
    return SemanticScholarSource(config, http_client or httpx.AsyncClient())


def _make_oa_source(
    api_key: str | None = "test-oa-key",
    *,
    http_client: httpx.AsyncClient | None = None,
) -> OpenAlexSource:
    config = PaperSourceConfig(
        id=3,
        source_type=SourceType.OPENALEX,
        enabled=True,
        config={"api_key": api_key} if api_key else {},
    )
    return OpenAlexSource(config, http_client or httpx.AsyncClient())


def _make_source(source_type: SourceType, source_class, http_client=None):
    config = PaperSourceConfig(id=9, source_type=source_type, enabled=True, config={})
    return source_class(config, http_client or httpx.AsyncClient())


def _oa_work(pdf_url_value: str | None) -> dict:
    """Minimal valid OpenAlex Work dict with the given pdf_url."""
    return {
        "id": "https://openalex.org/W1234567",
        "title": "Test Paper",
        "display_name": "Test Paper",
        "doi": None,
        "publication_date": "2026-01-01",
        "authorships": [],
        "abstract_inverted_index": None,
        "primary_location": {"pdf_url": pdf_url_value},
    }


# ---------------------------------------------------------------------------
# Semantic Scholar transient responses
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
async def test_s2_fetch_json_returns_empty_on_429(status_code: int) -> None:
    """_fetch_json returns {} on transient errors instead of raising HTTPStatusError."""
    respx.get(url__startswith=S2_API_URL).mock(return_value=httpx.Response(status_code))

    source = _make_s2_source()
    # _fetch_json should NOT raise; it should return {}
    result = await source._fetch_json("/paper/search", {"query": "test"})

    assert result == {}, f"Expected {{}} for status {status_code}, got {result!r}"


@respx.mock
@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
async def test_s2_fetch_by_id_returns_none_on_transient_empty_payload(status_code: int) -> None:
    """fetch_by_id must not parse transient S2 failures into empty paper shells."""
    respx.get(f"{S2_API_URL}/paper/abc123").mock(return_value=httpx.Response(status_code))

    source = _make_s2_source()
    paper = await source.fetch_by_id("abc123")

    assert paper is None


@respx.mock
async def test_s2_fetch_by_id_returns_none_for_malformed_payload() -> None:
    """fetch_by_id rejects payloads that cannot identify a real paper."""
    respx.get(f"{S2_API_URL}/paper/abc123").mock(
        return_value=httpx.Response(200, json={"paperId": "", "title": ""})
    )

    source = _make_s2_source()
    paper = await source.fetch_by_id("abc123")

    assert paper is None


# ---------------------------------------------------------------------------
# OpenAlex PDF URL scheme validation
# ---------------------------------------------------------------------------


@respx.mock
async def test_openalex_rejects_file_scheme_pdf_url() -> None:
    """pdf_url with file:// scheme is rejected even if hostname is in ALLOWED_PDF_DOMAINS.

    The hostname-only check was insufficient; an attacker could
    craft file://arxiv.org/etc/passwd.  a scheme guard was added.
    """
    # Pick a hostname that IS in the allowlist
    allowed_host = next(iter(ALLOWED_PDF_DOMAINS))
    # Craft a URL with file:// scheme but an allowed hostname
    evil_url = f"file://{allowed_host}/etc/passwd"

    respx.get(OPENALEX_API_URL).mock(
        return_value=httpx.Response(200, json={"meta": {}, "results": [_oa_work(evil_url)]})
    )

    source = _make_oa_source()
    papers = await source.search("test")

    assert len(papers) == 1
    assert papers[0].pdf_url is None, (
        f"Expected pdf_url=None for file:// URL with allowed hostname, got {papers[0].pdf_url!r}"
    )


# ---------------------------------------------------------------------------
# OpenAlex API-key transport
# ---------------------------------------------------------------------------


@respx.mock
async def test_openalex_api_key_sent_as_query_param() -> None:
    """OpenAlexSource sends api_key as a query param, not an Authorization Bearer header."""
    route = respx.get(OPENALEX_API_URL).mock(
        return_value=httpx.Response(200, json={"meta": {}, "results": []})
    )

    source = _make_oa_source(api_key="my-secret-key")
    await source.search("neural networks")

    assert route.call_count == 1
    request = route.calls[0].request
    params = dict(request.url.params)

    # api_key must appear in query params
    assert params.get("api_key") == "my-secret-key", (
        f"Expected api_key in query params, got params={params!r}"
    )

    # Authorization header must NOT be present
    assert "authorization" not in {k.lower() for k in request.headers}, (
        f"Unexpected Authorization header: {dict(request.headers)!r}"
    )


@pytest.mark.parametrize(
    ("source_factory", "invoke"),
    [
        (_make_s2_source, lambda source: source._fetch_json("/paper/search")),
        (_make_oa_source, lambda source: source.search("quarantine")),
        (
            lambda: _make_source(SourceType.ARXIV, ArxivSource),
            lambda source: source._fetch_xml({"search_query": "all:test"}),
        ),
        (
            lambda: _make_source(SourceType.PUBMED, PubMedSource),
            lambda source: source._esearch("quarantine", 1),
        ),
    ],
    ids=["semantic-scholar", "openalex", "arxiv", "pubmed"],
)
async def test_source_sinks_refuse_quarantine_before_http(
    monkeypatch, tmp_path, source_factory, invoke
) -> None:
    """Quarantine prevents each scholarly source from opening an HTTP request."""
    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    source = source_factory()

    with pytest.raises(OutboundEgressBlockedError, match="credential review"):
        await invoke(source)

    await source.http_client.aclose()


class _BarrierLimiter:
    """Pause one rate-limit acquisition until a test releases it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def acquire(self) -> None:
        """Signal entry and wait for the test to activate quarantine."""
        self.entered.set()
        await self.release.wait()


@pytest.mark.parametrize(
    ("source_factory", "invoke", "persistent_limiter"),
    [
        (
            lambda client: _make_s2_source(http_client=client),
            lambda source: source._fetch_json("/paper/search"),
            False,
        ),
        (
            lambda client: _make_source(SourceType.PUBMED, PubMedSource, client),
            lambda source: source._esearch("quarantine", 1),
            False,
        ),
        (
            lambda client: _make_source(SourceType.PUBMED, PubMedSource, client),
            lambda source: source._efetch(["1"]),
            False,
        ),
        (
            lambda client: _make_source(SourceType.ARXIV, ArxivSource, client),
            lambda source: source._fetch_xml({"search_query": "all:test"}),
            False,
        ),
        (
            lambda client: _make_oa_source(http_client=client),
            lambda source: source.search("quarantine"),
            False,
        ),
        (
            lambda client: _make_oa_source(http_client=client),
            lambda source: source.fetch_by_id("W123"),
            False,
        ),
        (
            lambda client: _make_oa_source(http_client=client),
            lambda source: source.fetch_new_since(
                since=datetime(2026, 1, 1, tzinfo=UTC),
                topics=[],
                limit=1,
            ),
            True,
        ),
    ],
    ids=[
        "semantic-scholar",
        "pubmed-search",
        "pubmed-fetch",
        "arxiv",
        "openalex-search",
        "openalex-fetch",
        "openalex-scheduled-fetch",
    ],
)
async def test_source_sinks_recheck_quarantine_after_rate_limit(
    monkeypatch,
    tmp_path,
    source_factory,
    invoke,
    persistent_limiter,
) -> None:
    """A restore beginning during a limiter wait prevents every source request."""
    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    requests = 0

    def handle_request(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={})

    barrier = _BarrierLimiter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        source = source_factory(client)
        if persistent_limiter:
            monkeypatch.setattr(
                source,
                "make_persistent_rate_limiter",
                lambda **_kwargs: barrier,
            )
        else:
            monkeypatch.setattr(source, "_rate_limit", barrier.acquire)

        task = asyncio.create_task(invoke(source))
        await asyncio.wait_for(barrier.entered.wait(), timeout=1.0)
        quarantine.touch()
        barrier.release.set()

        with pytest.raises(OutboundEgressBlockedError, match="credential review"):
            await task

    assert requests == 0
