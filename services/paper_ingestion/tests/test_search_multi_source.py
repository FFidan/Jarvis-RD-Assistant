"""Tests for multi-source fan-out in POST /api/search-preview and /api/search.

Covers:
- Multi-source merge with dedup
- Per-source error isolation (one source failing → degraded_sources, others OK)
- Legacy ``source: "both"`` validator migration
- Budget splitting (max_results divided across sources)
- Round-robin merge vs date sort
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from paper_ingestion.models import PaperCreate, SearchRequest, SourceType
from paper_ingestion.routers import search
from paper_ingestion.routers.search import (
    MultiSourceSearchResponse,
    _dedup_papers,
    _normalize_title,
    _round_robin_merge,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


def test_normalize_title_strips_punctuation():
    # Punctuation is replaced with spaces then whitespace is collapsed.
    assert _normalize_title("Hello, World!") == "hello world"


def test_normalize_title_collapses_whitespace():
    # Leading/trailing/internal whitespace is collapsed to single spaces.
    assert _normalize_title("  neural   ODE  ") == "neural ode"


def test_dedup_by_doi():
    papers = [
        _make_paper("arxiv:1", "Title A", doi="10.1234/abc"),
        _make_paper("s2:1", "Title A duplicate", doi="10.1234/abc"),  # dup
        _make_paper("arxiv:2", "Title B", doi="10.1234/def"),
    ]
    result = _dedup_papers(papers)
    assert len(result) == 2
    assert result[0].external_id == "arxiv:1"


def test_dedup_by_arxiv_id():
    papers = [
        _make_paper("arxiv:1", "Title A", arxiv_id="2301.00001"),
        _make_paper("s2:1", "Title A copy", arxiv_id="2301.00001"),  # dup
    ]
    result = _dedup_papers(papers)
    assert len(result) == 1


def test_dedup_by_title_year():
    papers = [
        _make_paper("arxiv:1", "Neural Ordinary Differential Equations", pub_year=2018),
        _make_paper("s2:1", "Neural Ordinary Differential Equations", pub_year=2018),  # dup
        _make_paper("s2:2", "Neural Ordinary Differential Equations", pub_year=2019),  # diff year
    ]
    result = _dedup_papers(papers)
    assert len(result) == 2


def test_round_robin_merge_interleaves():
    per_source = {
        "arxiv": [_make_paper(f"a:{i}", f"A{i}") for i in range(3)],
        "pubmed": [_make_paper(f"p:{i}", f"P{i}") for i in range(2)],
    }
    merged = _round_robin_merge(per_source)
    # Should alternate: a:0, p:0, a:1, p:1, a:2
    assert len(merged) == 5
    assert merged[0].external_id == "a:0"
    assert merged[1].external_id == "p:0"
    assert merged[2].external_id == "a:1"
    assert merged[3].external_id == "p:1"
    assert merged[4].external_id == "a:2"


# ---------------------------------------------------------------------------
# SearchRequest model: legacy migration
# ---------------------------------------------------------------------------


def test_legacy_source_single_migrated():
    """``source: "arxiv"`` migrates to ``source_types: ["arxiv"]``."""
    req = SearchRequest(query="test", source=SourceType.ARXIV)
    assert SourceType.ARXIV in req.source_types


def test_legacy_source_both_migrated():
    """``source: "both"`` migrates to ``source_types: ["arxiv", "semantic_scholar"]``."""

    req = SearchRequest.model_validate({"query": "test", "source": "both"})
    assert SourceType.ARXIV in req.source_types
    assert SourceType.SEMANTIC_SCHOLAR in req.source_types


def test_source_types_passthrough():
    """``source_types: [...]`` is passed through unchanged."""
    req = SearchRequest(query="test", source_types=[SourceType.PUBMED, SourceType.OPENALEX])
    assert req.source_types == [SourceType.PUBMED, SourceType.OPENALEX]


def test_default_source_types_is_arxiv():
    """Default source_types when nothing is specified is [arxiv]."""
    req = SearchRequest(query="test")
    assert req.source_types == [SourceType.ARXIV]


# ---------------------------------------------------------------------------
# Integration-style tests using monkeypatch
# ---------------------------------------------------------------------------


def _make_paper(
    external_id: str,
    title: str,
    source_type: SourceType = SourceType.ARXIV,
    doi: str | None = None,
    arxiv_id: str | None = None,
    pub_year: int | None = None,
) -> PaperCreate:
    metadata: dict = {}
    if doi:
        metadata["doi"] = doi
    if arxiv_id:
        metadata["arxiv_id"] = arxiv_id
    published_date = date(pub_year, 1, 1) if pub_year else None
    return PaperCreate(
        external_id=external_id,
        source_type=source_type,
        title=title,
        authors=["Test Author"],
        abstract="Abstract",
        published_date=published_date,
        url=f"https://example.com/{external_id}",
        pdf_url=None,
        citation_count=0,
        metadata=metadata,
    )


def _make_plugin_source(
    source_type: SourceType,
    results: list[PaperCreate],
    *,
    raises: Exception | None = None,
) -> SimpleNamespace:
    mock_search = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=results)
    return SimpleNamespace(
        source_type=source_type.value,
        config=SimpleNamespace(config={}),
        search=mock_search,
    )


@pytest.mark.asyncio
async def test_preview_multi_source_merge_and_dedup(monkeypatch):
    """Multi-source preview returns merged, deduplicated results."""
    arxiv_papers = [
        _make_paper("arxiv:1", "Paper One", SourceType.ARXIV, doi="10.1/1"),
        _make_paper("arxiv:2", "Paper Two", SourceType.ARXIV),
    ]
    pubmed_papers = [
        _make_paper("pubmed:1", "PubMed Paper", SourceType.PUBMED),
        _make_paper("pubmed:dup", "Paper One Dup", SourceType.PUBMED, doi="10.1/1"),  # dup
    ]

    arxiv_source = _make_plugin_source(SourceType.ARXIV, arxiv_papers)
    pubmed_source = _make_plugin_source(SourceType.PUBMED, pubmed_papers)

    async def fake_get_source(st, db_pool, http_client, request=None):
        if st == SourceType.ARXIV:
            return arxiv_source
        return pubmed_source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(
        query="test",
        source_types=[SourceType.ARXIV, SourceType.PUBMED],
        max_results=10,
    )
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(),
        body=body,
        db_pool=MagicMock(),
        http_client=MagicMock(),
    )

    assert isinstance(result, MultiSourceSearchResponse)
    assert result.total == 3  # 4 papers minus 1 dup
    assert len(result.results) == 3
    assert result.degraded_sources == []
    # per_source_counts should reflect the two sources
    assert "arxiv" in result.per_source_counts
    assert "pubmed" in result.per_source_counts


@pytest.mark.asyncio
async def test_preview_per_source_counts_accurate(monkeypatch):
    """per_source_counts reflects actual deduplicated counts per source."""
    arxiv_papers = [_make_paper(f"arxiv:{i}", f"A{i}", SourceType.ARXIV) for i in range(3)]
    pubmed_papers = [_make_paper(f"pubmed:{i}", f"P{i}", SourceType.PUBMED) for i in range(2)]

    arxiv_source = _make_plugin_source(SourceType.ARXIV, arxiv_papers)
    pubmed_source = _make_plugin_source(SourceType.PUBMED, pubmed_papers)

    async def fake_get_source(st, db_pool, http_client, request=None):
        return arxiv_source if st == SourceType.ARXIV else pubmed_source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(
        query="test",
        source_types=[SourceType.ARXIV, SourceType.PUBMED],
        max_results=10,
    )
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=MagicMock(), http_client=MagicMock()
    )

    assert result.per_source_counts["arxiv"] == 3
    assert result.per_source_counts["pubmed"] == 2
    assert result.total == 5


@pytest.mark.asyncio
async def test_preview_source_error_isolated(monkeypatch):
    """If one source raises RuntimeError, its papers are absent but degraded_sources is set."""
    arxiv_papers = [_make_paper("arxiv:1", "Good Paper", SourceType.ARXIV)]
    arxiv_source = _make_plugin_source(SourceType.ARXIV, arxiv_papers)
    broken_source = _make_plugin_source(
        SourceType.PUBMED, [], raises=RuntimeError("network failure")
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return arxiv_source if st == SourceType.ARXIV else broken_source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(
        query="test",
        source_types=[SourceType.ARXIV, SourceType.PUBMED],
        max_results=10,
    )
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=MagicMock(), http_client=MagicMock()
    )

    # arxiv results still returned
    assert result.total == 1
    assert result.results[0].external_id == "arxiv:1"
    # pubmed in degraded_sources
    assert "pubmed" in result.degraded_sources


@pytest.mark.asyncio
async def test_preview_legacy_source_both_still_works(monkeypatch):
    """Legacy ``source='both'`` is migrated to arxiv+s2 and returns results."""
    arxiv_papers = [_make_paper("a:1", "Arxiv Paper", SourceType.ARXIV)]
    s2_papers = [_make_paper("s2:1", "S2 Paper", SourceType.SEMANTIC_SCHOLAR)]

    arxiv_source = _make_plugin_source(SourceType.ARXIV, arxiv_papers)
    s2_source = _make_plugin_source(SourceType.SEMANTIC_SCHOLAR, s2_papers)

    async def fake_get_source(st, db_pool, http_client, request=None):
        return arxiv_source if st == SourceType.ARXIV else s2_source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    # Create request using legacy "both" via dict (as an HTTP client would send)
    body = SearchRequest(**{"query": "test", "source": "both", "max_results": 10})
    assert SourceType.ARXIV in body.source_types
    assert SourceType.SEMANTIC_SCHOLAR in body.source_types

    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=MagicMock(), http_client=MagicMock()
    )

    assert result.total == 2
    assert result.degraded_sources == []


@pytest.mark.asyncio
async def test_preview_date_sort_orders_by_published_date(monkeypatch):
    """sort_by='date' merges all papers then sorts by published_date DESC."""
    papers = [
        _make_paper("a:old", "Old Paper", SourceType.ARXIV, pub_year=2020),
        _make_paper("p:new", "New Paper", SourceType.PUBMED, pub_year=2024),
        _make_paper("a:mid", "Mid Paper", SourceType.ARXIV, pub_year=2022),
    ]
    # Split across two sources
    arxiv_source = _make_plugin_source(SourceType.ARXIV, [papers[0], papers[2]])
    pubmed_source = _make_plugin_source(SourceType.PUBMED, [papers[1]])

    async def fake_get_source(st, db_pool, http_client, request=None):
        return arxiv_source if st == SourceType.ARXIV else pubmed_source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(
        query="test",
        source_types=[SourceType.ARXIV, SourceType.PUBMED],
        max_results=10,
        sort_by="date",
    )
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=MagicMock(), http_client=MagicMock()
    )

    assert result.results[0].published_date.year == 2024  # newest first
    assert result.results[1].published_date.year == 2022
    assert result.results[2].published_date.year == 2020


@pytest.mark.asyncio
async def test_preview_budget_split_respects_max_results(monkeypatch):
    """Budget is split across sources; each source receives at most ceil(max/n) results."""
    call_budgets: dict[str, int] = {}

    async def fake_get_source(st, db_pool, http_client, request=None):
        async def _search(query, max_results, **kwargs):
            call_budgets[st.value] = max_results
            return []

        return SimpleNamespace(
            source_type=st.value,
            config=SimpleNamespace(config={}),
            search=_search,
        )

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(
        query="test",
        source_types=[SourceType.ARXIV, SourceType.PUBMED, SourceType.OPENALEX],
        max_results=10,
    )
    await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=MagicMock(), http_client=MagicMock()
    )

    # 10 / 3 = 3 remainder 1; first source gets 4, rest get 3
    assert sum(call_budgets.values()) == 10
    assert max(call_budgets.values()) <= 4
    assert min(call_budgets.values()) >= 3


# ---------------------------------------------------------------------------
# Empty source_types guard (fixes ZeroDivisionError 500 on empty list)
# ---------------------------------------------------------------------------


def test_empty_source_types_rejected_by_pydantic():
    """Pydantic's min_length=1 rejects empty source_types lists at validation time.

    This is the primary defense — user payloads with source_types=[] should
    never reach the router's budget-split math (which would ZeroDivisionError).
    """
    with pytest.raises(ValidationError) as exc_info:
        SearchRequest(query="test", source_types=[])
    # The error should mention the source_types field and a min-length-ish message.
    errors = exc_info.value.errors()
    assert any(
        "source_types" in (err.get("loc") or ()) and "at least 1" in str(err.get("msg", "")).lower()
        for err in errors
    ), f"Expected min_length validation error on source_types, got: {errors}"


@pytest.mark.asyncio
async def test_empty_source_types_defensive_guard_preview():
    """Defensive guard in search_papers_preview raises HTTPException 400.

    Pydantic normally blocks empty source_types before the handler runs, but
    the guard protects against future payload changes or direct internal calls
    that bypass the Pydantic layer (belt-and-suspenders).
    """
    body = SearchRequest.model_construct(
        query="test",
        source=None,
        source_types=[],
        max_results=10,
        year_from=None,
        year_to=None,
        sort_by="relevance",
        author=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        await search.search_papers_preview.__wrapped__(
            MagicMock(), body=body, db_pool=MagicMock(), http_client=MagicMock()
        )
    assert exc_info.value.status_code == 400
    assert "at least one source" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_empty_source_types_defensive_guard_search():
    """Defensive guard in search_papers (non-preview) raises HTTPException 400."""
    body = SearchRequest.model_construct(
        query="test",
        source=None,
        source_types=[],
        max_results=10,
        year_from=None,
        year_to=None,
        sort_by="relevance",
        author=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        await search.search_papers.__wrapped__(
            MagicMock(), body=body, db_pool=MagicMock(), http_client=MagicMock()
        )
    assert exc_info.value.status_code == 400
    assert "at least one source" in exc_info.value.detail.lower()
