"""Direct tests for source-backed search router behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "libs" / "jarvis_common"))
sys.modules.setdefault("fitz", MagicMock())
sys.modules.setdefault("tiktoken", MagicMock(get_encoding=MagicMock(return_value=MagicMock())))
sys.modules.setdefault("qdrant_client", MagicMock(AsyncQdrantClient=MagicMock()))
sys.modules.setdefault(
    "qdrant_client.models",
    MagicMock(
        Distance=MagicMock(),
        PointIdsList=MagicMock(),
        PointStruct=MagicMock(),
        VectorParams=MagicMock(),
    ),
)

from fastapi import HTTPException

from app.models import PaperCreate, SearchRequest, SourceType
from app.routers import search


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

    assert len(result) == 1
    assert result[0].title == "Neural ODE"
    db_pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_search_preview_maps_semantic_scholar_rate_limit_without_api_key(monkeypatch):
    """Preview search should preserve S2 rate limits as actionable 429s."""
    request = httpx.Request("GET", "https://api.semanticscholar.org/graph/v1/paper/search")
    response = httpx.Response(429, request=request)
    source = _make_source(
        side_effect=httpx.HTTPStatusError("rate limited", request=request, response=response)
    )
    monkeypatch.setattr(search, "get_source_for_type", AsyncMock(return_value=source))

    with pytest.raises(HTTPException) as exc_info:
        await search.search_papers_preview.__wrapped__(
            MagicMock(),
            body=SearchRequest(query="Neural ODE", source=SourceType.SEMANTIC_SCHOLAR, max_results=10),
            db_pool=MagicMock(),
            http_client=MagicMock(),
        )

    assert exc_info.value.status_code == 429
    assert "Semantic Scholar rate limit reached" in exc_info.value.detail
    assert "configure an API key" in exc_info.value.detail


@pytest.mark.asyncio
async def test_search_preview_maps_semantic_scholar_rate_limit_with_api_key(monkeypatch):
    """Preview search should omit the config hint when an S2 API key exists."""
    request = httpx.Request("GET", "https://api.semanticscholar.org/graph/v1/paper/search")
    response = httpx.Response(429, request=request)
    source = _make_source(
        api_key="configured",
        side_effect=httpx.HTTPStatusError("rate limited", request=request, response=response),
    )
    monkeypatch.setattr(search, "get_source_for_type", AsyncMock(return_value=source))

    with pytest.raises(HTTPException) as exc_info:
        await search.search_papers_preview.__wrapped__(
            MagicMock(),
            body=SearchRequest(query="Neural ODE", source=SourceType.SEMANTIC_SCHOLAR, max_results=10),
            db_pool=MagicMock(),
            http_client=MagicMock(),
        )

    assert exc_info.value.status_code == 429
    assert "Semantic Scholar rate limit reached" in exc_info.value.detail
    assert "configure an API key" not in exc_info.value.detail
