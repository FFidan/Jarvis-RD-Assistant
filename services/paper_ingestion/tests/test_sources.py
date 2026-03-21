"""Tests for paper source plugins."""

import httpx
import respx

from app.models import PaperSourceConfig
from app.sources.semantic_scholar_source import SemanticScholarSource


@respx.mock
async def test_semantic_scholar_search():
    """SemanticScholarSource.search returns parsed papers from S2 API."""
    mock_response = {
        "data": [
            {
                "paperId": "abc123",
                "title": "Attention Is All You Need",
                "authors": [{"name": "Ashish Vaswani"}],
                "abstract": "The dominant sequence transduction models...",
                "year": 2017,
                "publicationDate": "2017-06-12",
                "url": "https://www.semanticscholar.org/paper/abc123",
                "citationCount": 100000,
                "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762"},
                "externalIds": {"ArXiv": "1706.03762", "DOI": "10.5555/3295222.3295349"},
            }
        ]
    }
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    config = PaperSourceConfig(id=2, source_type="semantic_scholar", enabled=True, config={})
    async with httpx.AsyncClient() as client:
        source = SemanticScholarSource(config, client)
        papers = await source.search("attention transformer", max_results=1)

    assert len(papers) == 1
    assert papers[0].title == "Attention Is All You Need"
    assert papers[0].external_id == "s2:abc123"
    assert papers[0].citation_count == 100000
    assert papers[0].metadata["arxiv_id"] == "1706.03762"


@respx.mock
async def test_semantic_scholar_fetch_by_id():
    """SemanticScholarSource.fetch_by_id returns a single paper."""
    mock_response = {
        "paperId": "abc123",
        "title": "Test Paper",
        "authors": [{"name": "Test Author"}],
        "abstract": "Test abstract",
        "year": 2024,
        "publicationDate": None,
        "url": "https://www.semanticscholar.org/paper/abc123",
        "citationCount": 5,
        "openAccessPdf": None,
        "externalIds": {},
    }
    respx.get("https://api.semanticscholar.org/graph/v1/paper/abc123").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    config = PaperSourceConfig(id=2, source_type="semantic_scholar", enabled=True, config={})
    async with httpx.AsyncClient() as client:
        source = SemanticScholarSource(config, client)
        paper = await source.fetch_by_id("abc123")

    assert paper is not None
    assert paper.title == "Test Paper"
    assert paper.published_date.year == 2024


@respx.mock
async def test_semantic_scholar_fetch_not_found():
    """SemanticScholarSource.fetch_by_id returns None for 404."""
    respx.get("https://api.semanticscholar.org/graph/v1/paper/nonexistent").mock(
        return_value=httpx.Response(404)
    )

    config = PaperSourceConfig(id=2, source_type="semantic_scholar", enabled=True, config={})
    async with httpx.AsyncClient() as client:
        source = SemanticScholarSource(config, client)
        paper = await source.fetch_by_id("nonexistent")

    assert paper is None
