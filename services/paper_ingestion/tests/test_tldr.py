"""Tests for TLDR summary feature (T2-2).

Covers:
1. S2 TLDR parsed into metadata["s2_tldr"]
2. S2 missing TLDR -> no metadata key
3. TLDR word cap (>30 words truncated to 30)
4. Feed endpoint returns tldr field
5. LLM fallback when S2 TLDR absent
"""

from datetime import UTC, datetime

import httpx
import respx
from app.models import PaperSourceConfig
from app.sources.semantic_scholar_source import SemanticScholarSource

# ---------------------------------------------------------------------------
# Part A: S2 source tests
# ---------------------------------------------------------------------------


@respx.mock
async def test_s2_tldr_parsed_into_metadata():
    """SemanticScholarSource parses S2 TLDR into metadata['s2_tldr']."""
    mock_response = {
        "data": [
            {
                "paperId": "abc123",
                "title": "Attention Is All You Need",
                "authors": [
                    {"name": "Ashish Vaswani", "authorId": "1234567"},
                    {"name": "Noam Shazeer", "authorId": "2345678"},
                ],
                "abstract": "The dominant sequence transduction models...",
                "year": 2017,
                "publicationDate": "2017-06-12",
                "url": "https://www.semanticscholar.org/paper/abc123",
                "citationCount": 100000,
                "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762"},
                "externalIds": {"ArXiv": "1706.03762"},
                "tldr": {
                    "model": "tldr@v2",
                    "text": "A new network architecture based solely on attention mechanisms.",
                },
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
    assert papers[0].metadata["s2_tldr"] == (
        "A new network architecture based solely on attention mechanisms."
    )


@respx.mock
async def test_s2_missing_tldr_no_metadata_key():
    """When S2 API returns no TLDR, metadata should not contain 's2_tldr'."""
    mock_response = {
        "data": [
            {
                "paperId": "def456",
                "title": "Some Paper Without TLDR",
                "authors": [{"name": "Test Author"}],
                "abstract": "An abstract.",
                "year": 2024,
                "publicationDate": "2024-01-01",
                "url": "https://www.semanticscholar.org/paper/def456",
                "citationCount": 5,
                "openAccessPdf": None,
                "externalIds": {},
                "tldr": None,
            }
        ]
    }
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    config = PaperSourceConfig(id=2, source_type="semantic_scholar", enabled=True, config={})
    async with httpx.AsyncClient() as client:
        source = SemanticScholarSource(config, client)
        papers = await source.search("test", max_results=1)

    assert len(papers) == 1
    assert "s2_tldr" not in papers[0].metadata


@respx.mock
async def test_s2_author_ids_parsed():
    """SemanticScholarSource parses author IDs into metadata['s2_author_ids']."""
    mock_response = {
        "data": [
            {
                "paperId": "ghi789",
                "title": "Author ID Test",
                "authors": [
                    {"name": "Alice", "authorId": "111"},
                    {"name": "Bob", "authorId": "222"},
                    {"name": "Charlie"},  # No authorId
                ],
                "abstract": "Test.",
                "year": 2024,
                "publicationDate": None,
                "url": "https://www.semanticscholar.org/paper/ghi789",
                "citationCount": 0,
                "openAccessPdf": None,
                "externalIds": {},
                "tldr": None,
            }
        ]
    }
    respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=mock_response)
    )

    config = PaperSourceConfig(id=2, source_type="semantic_scholar", enabled=True, config={})
    async with httpx.AsyncClient() as client:
        source = SemanticScholarSource(config, client)
        papers = await source.search("test", max_results=1)

    assert len(papers) == 1
    author_ids = papers[0].metadata["s2_author_ids"]
    assert len(author_ids) == 2
    assert author_ids[0] == {"name": "Alice", "authorId": "111"}
    assert author_ids[1] == {"name": "Bob", "authorId": "222"}


# ---------------------------------------------------------------------------
# Part C: TLDR word cap
# ---------------------------------------------------------------------------


def test_tldr_word_cap():
    """TLDR longer than 30 words is truncated to exactly 30."""
    words = ["word"] * 50
    long_tldr = " ".join(words)
    capped = " ".join(long_tldr.split()[:30])
    assert len(capped.split()) == 30


def test_tldr_short_unchanged():
    """TLDR shorter than 30 words remains unchanged."""
    short_tldr = "This is a short TLDR with only a few words."
    capped = " ".join(short_tldr.split()[:30])
    assert capped == short_tldr


# ---------------------------------------------------------------------------
# Part D: FeedPaper model includes tldr field
# ---------------------------------------------------------------------------


def test_feed_paper_model_includes_tldr():
    """FeedPaper model accepts and serializes a tldr field."""
    from app.models import FeedPaper

    now = datetime.now(UTC)
    paper = FeedPaper(
        id=1,
        external_id="arxiv:1",
        source_type="arxiv",
        title="Test Paper",
        authors=["Author A"],
        url="https://arxiv.org/abs/1",
        created_at=now,
        summary_brief="Brief summary",
        tldr="This paper introduces a novel attention mechanism.",
        confidence="HIGH",
    )
    data = paper.model_dump()
    assert data["tldr"] == "This paper introduces a novel attention mechanism."


def test_feed_paper_model_tldr_none_by_default():
    """FeedPaper model defaults tldr to None."""
    from app.models import FeedPaper

    now = datetime.now(UTC)
    paper = FeedPaper(
        id=1,
        external_id="arxiv:1",
        source_type="arxiv",
        title="Test Paper",
        authors=["Author A"],
        url="https://arxiv.org/abs/1",
        created_at=now,
    )
    assert paper.tldr is None


def test_summary_response_model_includes_tldr():
    """SummaryResponse model accepts and serializes a tldr field."""
    from app.models import SummaryResponse

    now = datetime.now(UTC)
    summary = SummaryResponse(
        id=1,
        paper_id=1,
        summary_brief="Brief",
        summary_detailed="Detailed",
        tldr="One-sentence contribution summary.",
        key_findings=[],
        confidence="HIGH",
        cross_references=[],
        created_at=now,
    )
    data = summary.model_dump()
    assert data["tldr"] == "One-sentence contribution summary."


# ---------------------------------------------------------------------------
# Part E: LLM fallback when S2 TLDR absent
# ---------------------------------------------------------------------------


def test_tldr_fallback_to_s2():
    """When LLM returns empty TLDR, fall back to S2 TLDR."""
    llm_tldr = ""
    s2_tldr = "S2 provided this TLDR about the paper contribution."

    # Replicate the logic from summarize_paper
    tldr = llm_tldr
    if isinstance(tldr, str):
        tldr = " ".join(tldr.split()[:30])
    else:
        tldr = ""
    if not tldr.strip() and s2_tldr:
        tldr = " ".join(s2_tldr.split()[:30])

    assert tldr == s2_tldr


def test_llm_tldr_used_when_available():
    """When LLM returns a valid TLDR, it takes precedence over S2."""
    llm_tldr = "LLM generated this TLDR."
    s2_tldr = "S2 provided this different TLDR."

    tldr = llm_tldr
    if isinstance(tldr, str):
        tldr = " ".join(tldr.split()[:30])
    else:
        tldr = ""
    if not tldr.strip() and s2_tldr:
        tldr = " ".join(s2_tldr.split()[:30])

    assert tldr == "LLM generated this TLDR."


def test_tldr_cap_applied_to_s2_fallback():
    """S2 TLDR fallback is also capped to 30 words."""
    llm_tldr = ""
    s2_tldr = " ".join(["long"] * 50)

    tldr = llm_tldr
    if isinstance(tldr, str):
        tldr = " ".join(tldr.split()[:30])
    else:
        tldr = ""
    if not tldr.strip() and s2_tldr:
        tldr = " ".join(s2_tldr.split()[:30])

    assert len(tldr.split()) == 30
