"""Tests for SemanticScholarSource.get_recommendations().

TDD — written before the implementation was added.
Uses respx to mock the S2 Recommendations API.
Fixture: tests/fixtures/s2_recommendations_multi.json
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx
from app.models import PaperSourceConfig, SourceType
from app.sources.semantic_scholar_source import (
    S2_RECOMMENDATIONS_URL,
    SemanticScholarSource,
)

FIXTURES = Path(__file__).parent / "fixtures"
REC_FIXTURE = json.loads((FIXTURES / "s2_recommendations_multi.json").read_text())


def _make_source(api_key: str | None = None) -> SemanticScholarSource:
    config = PaperSourceConfig(
        id=2,
        source_type=SourceType.SEMANTIC_SCHOLAR,
        enabled=True,
        config={"api_key": api_key} if api_key else {},
    )
    client = httpx.AsyncClient()
    return SemanticScholarSource(config, client)


# ---------------------------------------------------------------------------
# With API key → uses POST multi-seed endpoint
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_recommendations_with_api_key_uses_post():
    """When api_key is set, get_recommendations POSTs to /recommendations/v1/papers."""
    route = respx.post(f"{S2_RECOMMENDATIONS_URL}/papers").mock(
        return_value=httpx.Response(200, json=REC_FIXTURE)
    )

    source = _make_source(api_key="test-key-abc")
    papers = await source.get_recommendations(
        positive_seeds=["pid1", "pid2"],
        negative_seeds=["pid3"],
        limit=10,
    )

    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert "pid1" in body["positivePaperIds"]
    assert "pid2" in body["positivePaperIds"]
    assert "pid3" in body["negativePaperIds"]
    assert len(papers) == 10  # fixture has 10, limit is 10
    assert all(p.source_type == SourceType.SEMANTIC_SCHOLAR for p in papers)


@respx.mock
async def test_get_recommendations_with_api_key_parses_papers():
    """Papers returned via POST path are correctly parsed."""
    respx.post(f"{S2_RECOMMENDATIONS_URL}/papers").mock(
        return_value=httpx.Response(200, json=REC_FIXTURE)
    )

    source = _make_source(api_key="test-key-abc")
    papers = await source.get_recommendations(positive_seeds=["pid1"], limit=5)

    assert len(papers) == 5
    assert papers[0].title == "Efficient Transformer Self-Attention"
    assert papers[0].external_id == "s2:rec001"
    assert papers[0].citation_count == 42
    assert papers[2].metadata.get("doi") == "10.1234/ssm.2025"


# ---------------------------------------------------------------------------
# Without API key → falls back to forpaper GET loop (top-3 seeds)
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_recommendations_without_api_key_uses_forpaper_get():
    """Without api_key, get_recommendations uses GET forpaper/{id} for each seed.

    All three seeds return the same paper (rec001).  After dedup only 1 result
    remains, but we verify all 3 seed URLs were called.
    """
    single_rec = {"recommendedPapers": [REC_FIXTURE["recommendedPapers"][0]]}
    route = respx.get(url__regex=rf"{S2_RECOMMENDATIONS_URL}/papers/forpaper/.*").mock(
        return_value=httpx.Response(200, json=single_rec)
    )

    source = _make_source(api_key=None)
    papers = await source.get_recommendations(
        positive_seeds=["seed1", "seed2", "seed3"],
        limit=50,
    )

    # All 3 seed URLs called; dedup reduces identical papers to 1
    assert route.call_count == 3
    assert len(papers) == 1  # all seeds returned rec001 — deduplicated to one


@respx.mock
async def test_get_recommendations_without_key_deduplication():
    """Forpaper loop deduplicates papers by S2 paper ID across seed queries."""
    # All three seeds return the same paper
    same_paper = REC_FIXTURE["recommendedPapers"][0]
    respx.get(url__regex=rf"{S2_RECOMMENDATIONS_URL}/papers/forpaper/.*").mock(
        return_value=httpx.Response(200, json={"recommendedPapers": [same_paper]})
    )

    source = _make_source(api_key=None)
    papers = await source.get_recommendations(
        positive_seeds=["s1", "s2", "s3"],
        limit=50,
    )

    # Only one unique paper despite 3 seed queries
    assert len(papers) == 1
    assert papers[0].external_id == "s2:rec001"


# ---------------------------------------------------------------------------
# 429 response → returns []
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_recommendations_429_with_key_returns_empty():
    """POST 429 is handled gracefully; returns []."""
    respx.post(f"{S2_RECOMMENDATIONS_URL}/papers").mock(return_value=httpx.Response(429))

    source = _make_source(api_key="my-key")
    papers = await source.get_recommendations(positive_seeds=["pid1"], limit=10)

    assert papers == []


@respx.mock
async def test_get_recommendations_429_without_key_skips_seed():
    """GET forpaper 429 causes that seed to be skipped; others still processed."""
    # First seed: 429, second seed: success
    good_paper = REC_FIXTURE["recommendedPapers"][1]
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"recommendedPapers": [good_paper]})

    respx.get(url__regex=rf"{S2_RECOMMENDATIONS_URL}/papers/forpaper/.*").mock(
        side_effect=side_effect
    )

    source = _make_source(api_key=None)
    papers = await source.get_recommendations(
        positive_seeds=["bad_seed", "good_seed"],
        limit=50,
    )

    assert len(papers) == 1
    assert papers[0].external_id == "s2:rec002"


# ---------------------------------------------------------------------------
# Empty seeds → returns []
# ---------------------------------------------------------------------------


async def test_get_recommendations_empty_seeds_returns_empty():
    """Empty positive_seeds list returns [] without making any HTTP calls."""
    source = _make_source(api_key="key")
    papers = await source.get_recommendations(positive_seeds=[], limit=10)
    assert papers == []


# ---------------------------------------------------------------------------
# Limit is respected
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_recommendations_limit_respected():
    """Result is trimmed to the requested limit."""
    respx.post(f"{S2_RECOMMENDATIONS_URL}/papers").mock(
        return_value=httpx.Response(200, json=REC_FIXTURE)
    )

    source = _make_source(api_key="key")
    papers = await source.get_recommendations(positive_seeds=["pid1"], limit=3)

    assert len(papers) == 3
