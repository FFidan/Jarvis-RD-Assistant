"""Direct tests for search-preview helper seams."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from paper_ingestion.models import PaperCreate, SourceType
from paper_ingestion.routers.search_helpers import (
    _build_preview_source_error,
    _load_local_library_matches,
    _preview_match_keys,
    _retry_after_seconds,
)


def _paper(**overrides) -> PaperCreate:
    data = {
        "external_id": "arxiv:1234.5678",
        "source_type": SourceType.ARXIV,
        "title": "Neural ODEs",
        "authors": ["Ada Lovelace"],
        "abstract": "A paper.",
        "published_date": date(2024, 1, 2),
        "url": "https://arxiv.org/abs/1234.5678?utm=1#frag",
        "pdf_url": None,
        "citation_count": 0,
        "metadata": {"doi": "10.1000/ABC", "arxiv_id": "1234.5678"},
    }
    data.update(overrides)
    return PaperCreate(**data)


def test_preview_match_keys_normalizes_every_supported_lookup_key() -> None:
    """Preview papers should produce deterministic local-library lookup keys."""
    keys = _preview_match_keys([_paper()])

    assert keys.dois == {"10.1000/abc"}
    assert keys.arxiv_ids == {"1234.5678"}
    assert keys.urls == {"https://arxiv.org/abs/1234.5678"}
    assert keys.external_ids == {"arxiv:1234.5678"}
    assert keys.normalized_titles == {"neural odes"}
    assert keys.years == {2024}
    assert keys.has_keys()


def test_retry_after_seconds_parses_integral_and_decimal_headers() -> None:
    """Retry-After headers should tolerate integer-like decimal strings."""
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(429, headers={"Retry-After": "2.0"}, request=request)
    exc = httpx.HTTPStatusError("rate limited", request=request, response=response)

    assert _retry_after_seconds(exc) == 2


def test_retry_after_seconds_returns_none_for_missing_or_invalid_header() -> None:
    """Missing or unparsable retry hints should not raise."""
    request = httpx.Request("GET", "https://example.test")
    response = httpx.Response(429, headers={"Retry-After": "later"}, request=request)
    exc = httpx.HTTPStatusError("rate limited", request=request, response=response)

    assert _retry_after_seconds(exc) is None
    assert _retry_after_seconds(RuntimeError("boom")) is None


def test_build_preview_source_error_maps_semantic_scholar_rate_limit_without_key() -> None:
    """Semantic Scholar 429s without an API key should include the settings hint."""
    request = httpx.Request("GET", "https://api.semanticscholar.org")
    response = httpx.Response(429, headers={"Retry-After": "5"}, request=request)
    exc = httpx.HTTPStatusError("rate limited", request=request, response=response)
    plugin = MagicMock()
    plugin.config.config = {}

    error = _build_preview_source_error("semantic_scholar", exc, plugin=plugin)

    assert error.kind == "rate_limit"
    assert error.status_code == 429
    assert error.retry_after_s == 5
    assert error.settings_hint == "Configure a Semantic Scholar API key in Settings > Sources."


def test_build_preview_source_error_maps_unavailable_http_exception() -> None:
    """Expected source bootstrap failures should become unavailable errors."""
    error = _build_preview_source_error(
        "pubmed",
        HTTPException(status_code=503, detail="PubMed disabled"),
        unavailable=True,
    )

    assert error.kind == "unavailable"
    assert error.status_code == 503
    assert error.settings_hint == "Enable the source in Settings > Sources."


@pytest.mark.asyncio
async def test_load_local_library_matches_short_circuits_empty_preview_list() -> None:
    """An explicitly empty preview result set should not scan the local library."""
    pool = MagicMock()

    indexes, title_year = await _load_local_library_matches(pool, preview_papers=[])

    assert indexes == {}
    assert title_year == {}
    pool.acquire.assert_not_called()
