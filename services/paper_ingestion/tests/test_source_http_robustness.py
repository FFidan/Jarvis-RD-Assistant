"""Source plugin robustness tests.

Covers:
- H13: SemanticScholarSource._fetch_json returns {} on 429/5xx (no exception)
- H21: OpenAlexSource rejects pdf_url with non-http(s) scheme even if hostname is in allowlist
- M4: OpenAlexSource sends api_key as ?api_key= query param, NOT Authorization Bearer header
"""

from __future__ import annotations

import httpx
import pytest
import respx
from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.pdf_processor import ALLOWED_PDF_DOMAINS
from paper_ingestion.sources.openalex_source import OPENALEX_API_URL, OpenAlexSource
from paper_ingestion.sources.semantic_scholar_source import S2_API_URL, SemanticScholarSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_s2_source(api_key: str | None = None) -> SemanticScholarSource:
    config = PaperSourceConfig(
        id=2,
        source_type=SourceType.SEMANTIC_SCHOLAR,
        enabled=True,
        config={"api_key": api_key} if api_key else {},
    )
    return SemanticScholarSource(config, httpx.AsyncClient())


def _make_oa_source(api_key: str | None = "test-oa-key") -> OpenAlexSource:
    config = PaperSourceConfig(
        id=3,
        source_type=SourceType.OPENALEX,
        enabled=True,
        config={"api_key": api_key} if api_key else {},
    )
    return OpenAlexSource(config, httpx.AsyncClient())


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
# H13: S2 _fetch_json returns {} on 429 (no exception raised)
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


# ---------------------------------------------------------------------------
# H21: OpenAlex pdf_url scheme check — file:// rejected even if hostname in allowlist
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
# M4: OpenAlex api_key sent as ?api_key= query param, NOT Authorization Bearer header
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
