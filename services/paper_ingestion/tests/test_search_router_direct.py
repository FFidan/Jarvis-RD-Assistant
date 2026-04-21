"""Direct tests for source-backed search router behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from paper_ingestion.models import PaperCreate, SearchRequest, SourceType
from paper_ingestion.routers import search


def _make_source(*, api_key: str | None = None, side_effect=None):
    source = SimpleNamespace(
        source_type="semantic_scholar",
        config=SimpleNamespace(config={"api_key": api_key} if api_key else {}),
        search=AsyncMock(side_effect=side_effect),
    )
    return source


@pytest.mark.asyncio
async def test_search_preview_returns_results_without_db_writes(monkeypatch):
    """Preview search should return source results without upserting them."""
    db_pool = MagicMock()
    http_client = MagicMock()
    source = _make_source(side_effect=None)
    source.search.return_value = [
        PaperCreate(
            external_id="s2:1",
            source_type=SourceType.SEMANTIC_SCHOLAR,
            title="Neural ODE",
            authors=["Ada"],
            abstract="preview",
            published_date=None,
            url="https://www.semanticscholar.org/paper/1",
            pdf_url=None,
            citation_count=0,
            metadata={},
        )
    ]
    monkeypatch.setattr(search, "get_source_for_type", AsyncMock(return_value=source))

    result = await search.search_papers_preview.__wrapped__(
        MagicMock(),
        body=SearchRequest(query="Neural ODE", source=SourceType.SEMANTIC_SCHOLAR, max_results=10),
        db_pool=db_pool,
        http_client=http_client,
    )

    assert len(result.results) == 1
    assert result.results[0].title == "Neural ODE"
    db_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_search_preview_maps_semantic_scholar_rate_limit_without_api_key(monkeypatch):
    """Preview search degrades gracefully on S2 rate limit, reporting the source as degraded."""
    request = httpx.Request("GET", "https://api.semanticscholar.org/graph/v1/paper/search")
    response = httpx.Response(429, request=request)
    source = _make_source(
        side_effect=httpx.HTTPStatusError("rate limited", request=request, response=response)
    )
    monkeypatch.setattr(search, "get_source_for_type", AsyncMock(return_value=source))

    result = await search.search_papers_preview.__wrapped__(
        MagicMock(),
        body=SearchRequest(query="Neural ODE", source=SourceType.SEMANTIC_SCHOLAR, max_results=10),
        db_pool=MagicMock(),
        http_client=MagicMock(),
    )

    # Source failed — reported as degraded, not raised as HTTP exception
    assert result.results == []
    assert "semantic_scholar" in result.degraded_sources


@pytest.mark.asyncio
async def test_search_preview_maps_semantic_scholar_rate_limit_with_api_key(monkeypatch):
    """Preview search degrades gracefully on S2 rate limit even when an API key is configured."""
    request = httpx.Request("GET", "https://api.semanticscholar.org/graph/v1/paper/search")
    response = httpx.Response(429, request=request)
    source = _make_source(
        api_key="configured",
        side_effect=httpx.HTTPStatusError("rate limited", request=request, response=response),
    )
    monkeypatch.setattr(search, "get_source_for_type", AsyncMock(return_value=source))

    result = await search.search_papers_preview.__wrapped__(
        MagicMock(),
        body=SearchRequest(query="Neural ODE", source=SourceType.SEMANTIC_SCHOLAR, max_results=10),
        db_pool=MagicMock(),
        http_client=MagicMock(),
    )

    # Source failed — reported as degraded, not raised as HTTP exception
    assert result.results == []
    assert "semantic_scholar" in result.degraded_sources
