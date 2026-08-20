"""Direct tests for source-backed search router behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from paper_ingestion.models import PaperSourceConfig, SearchRequest, SourceType
from paper_ingestion.routers.search import _search_preview_source_papers, _search_source_papers
from paper_ingestion.sources.openalex_source import OpenAlexSource
from paper_ingestion.sources.pubmed_source import PubMedSource
from paper_ingestion.sources.semantic_scholar_source import SemanticScholarSource


def _make_source(*, api_key: str | None = None, side_effect=None):
    source = SimpleNamespace(
        source_type="semantic_scholar",
        config=SimpleNamespace(config={"api_key": api_key} if api_key else {}),
        search=AsyncMock(side_effect=side_effect),
    )
    return source


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


def _source_config(source_type: SourceType, config: dict | None = None) -> PaperSourceConfig:
    return PaperSourceConfig(
        id=1,
        source_type=source_type,
        enabled=True,
        config=config or {},
    )


@pytest.mark.asyncio
async def test_missing_openalex_key_marks_interactive_search_failed(monkeypatch):
    source = OpenAlexSource(_source_config(SourceType.OPENALEX), MagicMock())
    monkeypatch.setattr(source, "_api_key", None)
    body = SearchRequest(query="graph neural networks", source_types=[SourceType.OPENALEX])

    source_name, papers, failed = await _search_source_papers("openalex", source, 5, body)

    assert source_name == "openalex"
    assert papers == []
    assert failed is True


@pytest.mark.asyncio
async def test_openalex_background_key_checks_keep_empty_results(monkeypatch):
    source = OpenAlexSource(_source_config(SourceType.OPENALEX), MagicMock())
    monkeypatch.setattr(source, "_api_key", None)

    assert await source.fetch_by_id("W1") is None
    assert await source.fetch_new_since(datetime(2025, 1, 1, tzinfo=UTC), [], limit=5) == []


@pytest.mark.asyncio
async def test_quoted_query_uses_each_source_search_syntax(monkeypatch):
    requests: dict[str, httpx.Request] = {}

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests[request.url.host] = request
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": []})
        if request.url.host == "api.semanticscholar.org":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, content=b"<eSearchResult><IdList/></eSearchResult>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        openalex = OpenAlexSource(_source_config(SourceType.OPENALEX), client)
        semantic_scholar = SemanticScholarSource(
            _source_config(SourceType.SEMANTIC_SCHOLAR), client
        )
        pubmed = PubMedSource(_source_config(SourceType.PUBMED), client)
        monkeypatch.setattr(openalex, "_api_key", "configured")
        monkeypatch.setattr(openalex, "_rate_limit", AsyncMock())
        monkeypatch.setattr(semantic_scholar, "_rate_limit", AsyncMock())
        monkeypatch.setattr(pubmed, "_rate_limit", AsyncMock())

        query = '"graph neural networks"'
        await openalex.search(query)
        await semantic_scholar.search(query)
        await pubmed.search(query)

    assert requests["api.openalex.org"].url.params["search"] == query
    assert requests["api.semanticscholar.org"].url.params["query"] == "graph neural networks"
    assert requests["eutils.ncbi.nlm.nih.gov"].url.params["term"] == query


@pytest.mark.asyncio
async def test_openalex_preview_reports_actionable_missing_key(monkeypatch):
    source = OpenAlexSource(_source_config(SourceType.OPENALEX), MagicMock())
    monkeypatch.setattr(source, "_api_key", None)
    body = SearchRequest(query="graph neural networks", source_types=[SourceType.OPENALEX])

    source_name, papers, error = await _search_preview_source_papers("openalex", source, 5, body)

    assert source_name == "openalex"
    assert papers == []
    assert error is not None
    assert error.message == (
        "OpenAlex search was skipped because no API key is configured. "
        "Add an OpenAlex API key in Settings > Sources."
    )


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).
