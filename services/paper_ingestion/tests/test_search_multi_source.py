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
from paper_ingestion.models import PaperCreate, SearchRequest, SourceType
from paper_ingestion.routers import search
from paper_ingestion.routers.search import (
    _dedup_papers,
    _load_local_library_matches,
    _normalize_title,
    _normalize_url,
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


def test_normalize_title_is_ascii_only():
    # B6: SQL uses POSIX [:alnum:] which is ASCII-only on the default locale.
    # Python normalization must match so SQL-returned candidates and the
    # in-memory index agree on the same canonical form.
    assert _normalize_title("Neural ODÉs") == "neural od s"
    assert _normalize_title("Café Latté") == "caf latt"
    assert _normalize_title("深度学习") == ""
    # ASCII letters/digits/underscore stay; everything else collapses to space.
    assert _normalize_title("transformer_v2") == "transformer_v2"


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


# Keep local: multi-source-specific kwargs (doi/arxiv_id/pub_year/source_type) not in pulse_helpers.make_pulse_paper.
def _make_paper(
    external_id: str,
    title: str,
    source_type: SourceType = SourceType.ARXIV,
    doi: str | None = None,
    arxiv_id: str | None = None,
    pub_year: int | None = None,
    authors: list[str] | None = None,
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
        authors=authors or ["Test Author"],
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


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, rows: list[dict] | None = None, fetch_side_effect=None):
        self.rows = rows or []
        self.fetch_side_effect = fetch_side_effect
        self.fetch_calls: list[tuple] = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, *args))
        if self.fetch_side_effect is not None:
            if callable(self.fetch_side_effect):
                return self.fetch_side_effect(query, *args)
            return self.fetch_side_effect.pop(0)
        return self.rows


class _FakePool:
    def __init__(self, rows: list[dict] | None = None, fetch_side_effect=None):
        self._conn = _FakeConn(rows, fetch_side_effect=fetch_side_effect)

    def acquire(self):
        return _FakeAcquire(self._conn)

    @property
    def fetch_calls(self) -> list[tuple]:
        return self._conn.fetch_calls


def _make_preview_pool(rows: list[dict] | None = None, fetch_side_effect=None) -> _FakePool:
    return _FakePool(rows, fetch_side_effect=fetch_side_effect)


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


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
        MagicMock(), body=body, db_pool=_make_preview_pool(), http_client=MagicMock()
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
        MagicMock(), body=body, db_pool=_make_preview_pool(), http_client=MagicMock()
    )

    # 10 / 3 = 3 remainder 1; first source gets 4, rest get 3
    assert sum(call_budgets.values()) == 10
    assert max(call_budgets.values()) <= 4
    assert min(call_budgets.values()) >= 3


@pytest.mark.asyncio
async def test_preview_library_match_by_doi_arxiv_and_title_year(monkeypatch):
    """Preview rows carry local-library linkage metadata in match-precedence order."""
    local_rows = [
        {
            "id": 11,
            "external_id": "local:doi",
            "title": "DOI Match Paper",
            "authors": ["Local Author"],
            "url": "https://library.example/doi",
            "published_date": date(2024, 1, 1),
            "metadata": {"doi": "10.1000/DOI-MATCH"},
            "zotero_item_key": "ZOTERO-DOI",
            "has_project_links": False,
        },
        {
            "id": 12,
            "external_id": "local:arxiv",
            "title": "ArXiv Match Paper",
            "authors": ["Local Author"],
            "url": "https://library.example/arxiv",
            "published_date": date(2023, 1, 1),
            "metadata": {"arxiv_id": "2301.12345"},
            "zotero_item_key": None,
            "has_project_links": True,
        },
        {
            "id": 13,
            "external_id": "local:title",
            "title": "Title Match Paper",
            "authors": ["Local Author"],
            "url": "https://library.example/title",
            "published_date": date(2022, 1, 1),
            "metadata": {},
            "zotero_item_key": "ZOTERO-TITLE",
            "has_project_links": True,
        },
    ]
    source = _make_plugin_source(
        SourceType.ARXIV,
        [
            _make_paper(
                "preview:doi",
                "DOI Match Paper",
                SourceType.ARXIV,
                doi="10.1000/doi-match",
            ),
            _make_paper(
                "preview:arxiv",
                "ArXiv Match Paper",
                SourceType.ARXIV,
                arxiv_id="2301.12345",
            ),
            _make_paper(
                "preview:title",
                "Title Match Paper",
                SourceType.ARXIV,
                pub_year=2022,
                authors=["Local Author"],
            ),
        ],
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(query="test", source_types=[SourceType.ARXIV], max_results=10)
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=_make_preview_pool(local_rows), http_client=MagicMock()
    )

    assert [row.library_match.paper_id for row in result.results] == [11, 12, 13]
    assert [row.library_match.has_project_links for row in result.results] == [False, True, True]
    assert [row.library_match.zotero_item_key for row in result.results] == [
        "ZOTERO-DOI",
        None,
        "ZOTERO-TITLE",
    ]


@pytest.mark.asyncio
async def test_preview_duplicate_local_rows_prefer_most_actionable_match(monkeypatch):
    """Duplicate local rows keep the most actionable/current row for each key."""
    local_rows = [
        {
            "id": 31,
            "external_id": "local:old",
            "title": "Duplicate Title",
            "authors": ["Local Author"],
            "url": "https://library.example/duplicate-old",
            "published_date": date(2024, 1, 1),
            "metadata": {"doi": "10.1000/dup"},
            "zotero_item_key": None,
            "has_project_links": False,
        },
        {
            "id": 44,
            "external_id": "local:newer-key",
            "title": "Duplicate Title",
            "authors": ["Local Author"],
            "url": "https://library.example/duplicate-newer-key",
            "published_date": date(2024, 1, 1),
            "metadata": {"doi": "10.1000/dup"},
            "zotero_item_key": "ZOTERO-DUP",
            "has_project_links": False,
        },
        {
            "id": 52,
            "external_id": "local:project",
            "title": "Duplicate Title",
            "authors": ["Local Author"],
            "url": "https://library.example/duplicate-project",
            "published_date": date(2024, 1, 1),
            "metadata": {"doi": "10.1000/dup"},
            "zotero_item_key": "ZOTERO-DUP-PROJECT",
            "has_project_links": True,
        },
    ]
    source = _make_plugin_source(
        SourceType.ARXIV,
        [_make_paper("preview:dup", "Duplicate Title", SourceType.ARXIV, doi="10.1000/dup")],
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(query="test", source_types=[SourceType.ARXIV], max_results=10)
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=_make_preview_pool(local_rows), http_client=MagicMock()
    )

    assert result.results[0].library_match.paper_id == 52
    assert result.results[0].library_match.has_project_links is True
    assert result.results[0].library_match.zotero_item_key == "ZOTERO-DUP-PROJECT"


@pytest.mark.asyncio
async def test_preview_title_year_match_requires_preview_year(monkeypatch):
    """Title fallback does not match when the preview row has no year."""
    local_rows = [
        {
            "id": 21,
            "external_id": "local:title-year",
            "title": "Yeared Title",
            "authors": ["Local Author"],
            "url": "https://library.example/yeared",
            "published_date": date(2024, 1, 1),
            "metadata": {},
            "zotero_item_key": None,
            "has_project_links": False,
        }
    ]
    source = _make_plugin_source(
        SourceType.ARXIV,
        [_make_paper("preview:title-year", "Yeared Title", SourceType.ARXIV)],
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(query="test", source_types=[SourceType.ARXIV], max_results=10)
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=_make_preview_pool(local_rows), http_client=MagicMock()
    )

    assert result.results[0].library_match is None


@pytest.mark.asyncio
async def test_preview_title_year_match_requires_local_year(monkeypatch):
    """Title fallback does not match when the local row has no year."""
    local_rows = [
        {
            "id": 22,
            "external_id": "local:title-no-year",
            "title": "No Year Title",
            "authors": ["Local Author"],
            "url": "https://library.example/no-year",
            "published_date": None,
            "metadata": {},
            "zotero_item_key": None,
            "has_project_links": False,
        }
    ]
    source = _make_plugin_source(
        SourceType.ARXIV,
        [_make_paper("preview:no-year", "No Year Title", SourceType.ARXIV, pub_year=2024)],
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(query="test", source_types=[SourceType.ARXIV], max_results=10)
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=_make_preview_pool(local_rows), http_client=MagicMock()
    )

    assert result.results[0].library_match is None


@pytest.mark.asyncio
async def test_preview_title_year_collision_requires_author_overlap(monkeypatch):
    """Same-title/same-year collisions do not match without author overlap."""
    local_rows = [
        {
            "id": 23,
            "external_id": "local:collision-a",
            "title": "Collision Paper",
            "authors": ["Alice Example"],
            "url": "https://library.example/collision-a",
            "published_date": date(2024, 1, 1),
            "metadata": {},
            "zotero_item_key": None,
            "has_project_links": False,
        },
        {
            "id": 24,
            "external_id": "local:collision-b",
            "title": "Collision Paper",
            "authors": ["Bob Example"],
            "url": "https://library.example/collision-b",
            "published_date": date(2024, 1, 1),
            "metadata": {},
            "zotero_item_key": None,
            "has_project_links": False,
        },
    ]
    source = _make_plugin_source(
        SourceType.ARXIV,
        [
            _make_paper(
                "preview:collision",
                "Collision Paper",
                SourceType.ARXIV,
                pub_year=2024,
                authors=["Carol Example"],
            )
        ],
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(query="test", source_types=[SourceType.ARXIV], max_results=10)
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=_make_preview_pool(local_rows), http_client=MagicMock()
    )

    assert result.results[0].library_match is None


@pytest.mark.asyncio
async def test_preview_unmatched_rows_have_null_library_match(monkeypatch):
    """Preview rows without a local-library hit keep ``library_match`` null."""
    source = _make_plugin_source(
        SourceType.PUBMED,
        [_make_paper("preview:1", "Unmatched Paper", SourceType.PUBMED, pub_year=2025)],
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)

    body = SearchRequest(query="test", source_types=[SourceType.PUBMED], max_results=10)
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=_make_preview_pool(), http_client=MagicMock()
    )

    assert result.results[0].library_match is None


@pytest.mark.asyncio
async def test_preview_without_match_keys_does_not_scan_library(monkeypatch):
    """Preview matching should avoid local-library fetches when no candidate keys exist."""
    source = _make_plugin_source(
        SourceType.PUBMED,
        [],
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)
    pool = _make_preview_pool(
        [
            {
                "id": 99,
                "external_id": "unrelated",
                "title": "Unrelated",
                "authors": ["Nobody"],
                "url": "https://library.example/unrelated",
                "published_date": date(2024, 1, 1),
                "metadata": {},
                "zotero_item_key": None,
                "has_project_links": False,
            }
        ]
    )

    body = SearchRequest(query="test", source_types=[SourceType.PUBMED], max_results=10)
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=pool, http_client=MagicMock()
    )

    assert result.results == []
    assert pool.fetch_calls == []


@pytest.mark.asyncio
async def test_preview_library_match_query_uses_candidate_keys(monkeypatch):
    """Local matching query should be key-bounded instead of scanning all papers."""
    source = _make_plugin_source(
        SourceType.ARXIV,
        [_make_paper("preview:doi", "DOI Match Paper", SourceType.ARXIV, doi="10.1000/key")],
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)
    pool = _make_preview_pool(
        [
            {
                "id": 11,
                "external_id": "local:doi",
                "title": "DOI Match Paper",
                "authors": ["Local Author"],
                "url": "https://library.example/doi",
                "published_date": date(2024, 1, 1),
                "metadata": {"doi": "10.1000/key"},
                "zotero_item_key": None,
                "has_project_links": False,
            }
        ]
    )

    body = SearchRequest(query="test", source_types=[SourceType.ARXIV], max_results=10)
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=pool, http_client=MagicMock()
    )

    assert result.results[0].library_match.paper_id == 11
    query, *args = pool.fetch_calls[0]
    assert "metadata->>'doi'" in query
    assert args[1] == ["10.1000/key"]


@pytest.mark.asyncio
async def test_preview_library_match_query_normalizes_local_candidate_keys(monkeypatch):
    """Candidate SQL should normalize local DOI/arXiv/URL before filtering rows."""
    source = _make_plugin_source(
        SourceType.ARXIV,
        [
            _make_paper(
                "preview:doi",
                "DOI Match Paper",
                SourceType.ARXIV,
                doi="10.1000/key",
                arxiv_id="2401.00001",
            )
        ],
    )

    async def fake_get_source(st, db_pool, http_client, request=None):
        return source

    monkeypatch.setattr(search, "get_source_for_type", fake_get_source)
    pool = _make_preview_pool(
        [
            {
                "id": 11,
                "external_id": " local:doi ",
                "title": "DOI Match Paper",
                "authors": ["Test Author"],
                "url": "https://example.com/preview:doi/?utm_source=test#fragment",
                "published_date": date(2024, 1, 1),
                "metadata": {"doi": " 10.1000/key ", "arxiv_id": " 2401.00001 "},
                "zotero_item_key": None,
                "has_project_links": False,
            }
        ]
    )

    body = SearchRequest(query="test", source_types=[SourceType.ARXIV], max_results=10)
    result = await search.search_papers_preview.__wrapped__(
        MagicMock(), body=body, db_pool=pool, http_client=MagicMock()
    )

    assert result.results[0].library_match.paper_id == 11
    query, *args = pool.fetch_calls[0]
    assert "lower(btrim(coalesce(p.metadata->>'doi', '')))" in query
    assert "lower(btrim(coalesce(p.metadata->>'arxiv_id', '')))" in query
    assert "split_part(lower(coalesce(p.url, '')), '#', 1)" in query
    assert _normalize_url("https://example.com/preview:doi/?utm_source=test#fragment") in args[3]


@pytest.mark.asyncio
async def test_load_local_library_matches_filters_candidate_rows_by_args():
    """The preview-match helper should only index rows matching supplied candidate keys."""
    preview = [
        _make_paper(
            "preview:doi",
            "DOI Match Paper",
            SourceType.ARXIV,
            doi="10.1000/key",
            arxiv_id="2401.00001",
            pub_year=2024,
        )
    ]
    matching_row = {
        "id": 11,
        "external_id": "local:doi",
        "title": "DOI Match Paper",
        "authors": ["Test Author"],
        "url": "https://example.com/preview:doi/?utm_source=test#fragment",
        "published_date": date(2024, 1, 1),
        "metadata": {"doi": " 10.1000/key ", "arxiv_id": " 2401.00001 "},
        "zotero_item_key": None,
        "has_project_links": False,
    }
    unrelated_row = {
        "id": 12,
        "external_id": "local:other",
        "title": "Other Paper",
        "authors": ["Other Author"],
        "url": "https://example.com/other",
        "published_date": date(2024, 1, 1),
        "metadata": {"doi": "10.1000/other"},
        "zotero_item_key": None,
        "has_project_links": False,
    }

    def filter_rows(_query, _user_id, dois, arxiv_ids, urls, external_ids, titles, years):
        selected = []
        for row in [matching_row, unrelated_row]:
            metadata = row["metadata"]
            if (
                str(metadata.get("doi", "")).strip().lower() in dois
                or str(metadata.get("arxiv_id", "")).strip().lower() in arxiv_ids
                or _normalize_url(row["url"]) in urls
                or str(row["external_id"]).strip().lower() in external_ids
                or (
                    _normalize_title(row["title"]) in titles and row["published_date"].year in years
                )
            ):
                selected.append(row)
        return selected

    indexes, title_year_candidates = await _load_local_library_matches(
        _make_preview_pool(fetch_side_effect=filter_rows),
        preview,
        user_id=None,
    )

    assert indexes[("doi", "10.1000/key")].paper_id == 11
    assert ("doi", "10.1000/other") not in indexes
    assert title_year_candidates[("doi match paper", 2024)][0].match.paper_id == 11


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


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


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).


# Cluster 2 deletion (2026-05-22): superseded by test_pi_search_contract.py (SR-01..SR-05).
