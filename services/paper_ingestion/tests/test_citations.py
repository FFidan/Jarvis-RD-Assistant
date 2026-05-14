"""Tests for citation graph feature."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import asyncpg
import pytest
from fastapi import HTTPException
from paper_ingestion.models import (
    CitationFetchResponse,
    CitationGraphResponse,
    CitationRelation,
    GraphEdge,
    GraphNode,
)

# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


def test_citation_relation_valid():
    """CitationRelation accepts valid data."""
    rel = CitationRelation(
        source_paper_id=1,
        cited_paper_id=2,
        citation_context="This paper extends...",
        is_influential=True,
        intent=["methodology"],
    )
    assert rel.source_paper_id == 1
    assert rel.cited_paper_id == 2
    assert rel.is_influential is True


def test_citation_relation_defaults():
    """CitationRelation has correct defaults."""
    rel = CitationRelation(source_paper_id=1, cited_paper_id=2)
    assert rel.citation_context is None
    assert rel.is_influential is None
    assert rel.intent == []


def test_citation_fetch_response():
    """CitationFetchResponse validates correctly."""
    resp = CitationFetchResponse(citations_added=5, references_added=10, stubs_created=8)
    assert resp.citations_added == 5
    assert resp.stubs_created == 8


def test_graph_node_valid():
    """GraphNode accepts valid data."""
    node = GraphNode(id=1, title="Test Paper", citation_count=42, is_stub=False)
    assert node.title == "Test Paper"
    assert node.is_stub is False


def test_graph_node_defaults():
    """GraphNode has correct defaults."""
    node = GraphNode(id=1, title="Test")
    assert node.citation_count == 0
    assert node.published_date is None
    assert node.is_stub is False


def test_graph_node_with_date():
    """GraphNode handles published_date."""
    node = GraphNode(id=1, title="Test", published_date=date(2024, 1, 15))
    assert node.published_date == date(2024, 1, 15)


def test_graph_edge_valid():
    """GraphEdge accepts valid data."""
    edge = GraphEdge(source=1, target=2, is_influential=True, context="extends")
    assert edge.source == 1
    assert edge.target == 2


def test_graph_edge_defaults():
    """GraphEdge has correct defaults."""
    edge = GraphEdge(source=1, target=2)
    assert edge.is_influential is None
    assert edge.context is None


def test_citation_graph_response():
    """CitationGraphResponse composes nodes and edges."""
    graph = CitationGraphResponse(
        nodes=[GraphNode(id=1, title="A"), GraphNode(id=2, title="B")],
        edges=[GraphEdge(source=1, target=2)],
    )
    assert len(graph.nodes) == 2
    assert len(graph.edges) == 1


def test_citation_graph_response_empty():
    """CitationGraphResponse handles empty graph."""
    graph = CitationGraphResponse(nodes=[], edges=[])
    assert graph.nodes == []
    assert graph.edges == []


# ---------------------------------------------------------------------------
# Stub creation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_stub_new():
    """Creates a new stub paper from S2 data."""
    from paper_ingestion.citations import get_or_create_stub_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [
        None,  # SELECT check -- not exists
        {"id": 42},  # INSERT RETURNING
    ]

    s2_data = {
        "citingPaper": {
            "paperId": "abc123",
            "title": "Test Paper",
            "authors": [{"name": "John Smith"}],
            "year": 2024,
            "citationCount": 10,
        }
    }
    result = await get_or_create_stub_paper(mock_conn, s2_data)
    assert result == 42


@pytest.mark.asyncio
async def test_get_or_create_stub_existing():
    """Returns existing paper if external_id matches."""
    from paper_ingestion.citations import get_or_create_stub_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 99}

    s2_data = {
        "citingPaper": {
            "paperId": "abc123",
            "title": "Existing Paper",
            "authors": [],
        }
    }
    result = await get_or_create_stub_paper(mock_conn, s2_data)
    assert result == 99


@pytest.mark.asyncio
async def test_get_or_create_stub_minimal_data():
    """Returns None when S2 data lacks paperId or title."""
    from paper_ingestion.citations import get_or_create_stub_paper

    mock_conn = AsyncMock()

    # No paperId
    result = await get_or_create_stub_paper(mock_conn, {"citingPaper": {"title": "X"}})
    assert result is None

    # No title
    result = await get_or_create_stub_paper(
        mock_conn,
        {"citingPaper": {"paperId": "x", "title": ""}},
    )
    assert result is None


# ---------------------------------------------------------------------------
# Citation sync tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_citations_for_paper_raises_for_missing_paper():
    """sync_citations_for_paper raises ValueError when the seed paper is missing."""
    from paper_ingestion.citations import sync_citations_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None
    mock_source = AsyncMock()

    with pytest.raises(ValueError, match="Paper 7 not found"):
        await sync_citations_for_paper(mock_conn, mock_source, 7)


@pytest.mark.asyncio
async def test_sync_citations_for_paper_returns_zeroes_when_table_missing():
    """UndefinedTableError degrades to an empty CitationFetchResponse."""
    from paper_ingestion.citations import sync_citations_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "external_id": "s2:seed123",
        "metadata": {},
    }
    mock_source = AsyncMock()
    mock_source.fetch_citations.return_value = [
        {
            "citingPaper": {
                "paperId": "stub1",
                "title": "Stub Citation",
                "authors": [],
                "year": 2024,
            }
        }
    ]
    mock_source.fetch_references.return_value = []

    mock_conn.fetchrow.side_effect = [
        mock_conn.fetchrow.return_value,
        None,
        {"id": 11},
    ]
    mock_conn.fetchval.side_effect = asyncpg.exceptions.UndefinedTableError(
        SimpleNamespace(message="paper_citations missing")
    )

    result = await sync_citations_for_paper(mock_conn, mock_source, 5)

    assert result == CitationFetchResponse(
        citations_added=0,
        references_added=0,
        stubs_created=0,
    )
    mock_conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_citations_for_paper_processes_references_when_citations_fail():
    """Reference ingestion still proceeds when citation fetching fails."""
    from paper_ingestion.citations import sync_citations_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "external_id": "s2:seed123",
        "metadata": {},
    }
    mock_conn.fetchrow.side_effect = [
        mock_conn.fetchrow.return_value,
        None,
        {"id": 21},
    ]
    mock_conn.fetchval.side_effect = [1, "true"]
    mock_conn.execute.side_effect = [
        "UPDATE 1",
    ]

    mock_source = AsyncMock()
    mock_source.fetch_citations.side_effect = RuntimeError("citations unavailable")
    mock_source.fetch_references.return_value = [
        {
            "citedPaper": {
                "paperId": "ref-1",
                "title": "Referenced Work",
                "authors": [],
                "year": 2023,
            },
            "contexts": ["supports prior work"],
            "isInfluential": True,
            "intents": ["background"],
        }
    ]

    result = await sync_citations_for_paper(mock_conn, mock_source, 8)

    assert result.citations_added == 0
    assert result.references_added == 1
    assert result.stubs_created == 1
    assert mock_conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_sync_citations_for_paper_processes_both_directions_with_expected_edges(
    monkeypatch: pytest.MonkeyPatch,
):
    """Citation sync preserves edge orientation for citing and referenced papers."""
    import paper_ingestion.citations as citations_module

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "external_id": "s2:seed123",
        "metadata": {},
    }
    mock_conn.fetchval.side_effect = [1, "true", 1, "true"]
    mock_conn.execute.side_effect = [
        "UPDATE 1",
    ]

    stub_lookup = AsyncMock(side_effect=[21, 31])
    monkeypatch.setattr(citations_module, "get_or_create_stub_paper", stub_lookup)

    mock_source = AsyncMock()
    mock_source.fetch_citations.return_value = [
        {
            "citingPaper": {
                "paperId": "cite-1",
                "title": "Citing Work",
            },
            "contexts": ["extends the seed"],
            "isInfluential": True,
            "intents": ["methodology"],
        }
    ]
    mock_source.fetch_references.return_value = [
        {
            "citedPaper": {
                "paperId": "ref-1",
                "title": "Referenced Work",
            },
            "contexts": ["builds on prior work"],
            "isInfluential": False,
            "intents": ["background"],
        }
    ]

    result = await citations_module.sync_citations_for_paper(mock_conn, mock_source, 8)

    assert result == CitationFetchResponse(
        citations_added=1,
        references_added=1,
        stubs_created=2,
    )
    fetchval_calls = mock_conn.fetchval.await_args_list
    assert fetchval_calls[0] == call(
        citations_module._INSERT_CITATION_SQL,
        21,
        8,
        "extends the seed",
        True,
        ["methodology"],
    )
    assert fetchval_calls[2] == call(
        citations_module._INSERT_CITATION_SQL,
        8,
        31,
        "builds on prior work",
        False,
        ["background"],
    )


# ---------------------------------------------------------------------------
# Graph building tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_graph_empty():
    """Empty paper_ids returns empty graph."""
    from paper_ingestion.citations import build_citation_graph

    mock_conn = AsyncMock()
    result = await build_citation_graph(mock_conn, [])
    assert result.nodes == []
    assert result.edges == []


@pytest.mark.asyncio
async def test_build_graph_single_paper():
    """Graph with a single paper and its citations."""
    from paper_ingestion.citations import build_citation_graph

    mock_conn = AsyncMock()
    # First fetch: expansion query
    mock_conn.fetch.side_effect = [
        # Depth 1 expansion
        [{"source_paper_id": 1, "cited_paper_id": 2}, {"source_paper_id": 3, "cited_paper_id": 1}],
        # Node data
        [
            {
                "id": 1,
                "title": "Paper A",
                "citation_count": 10,
                "published_date": None,
                "metadata": {},
            },
            {
                "id": 2,
                "title": "Paper B",
                "citation_count": 5,
                "published_date": None,
                "metadata": {"stub": "true"},
            },
            {
                "id": 3,
                "title": "Paper C",
                "citation_count": 20,
                "published_date": None,
                "metadata": {},
            },
        ],
        # Edge data
        [
            {
                "source_paper_id": 1,
                "cited_paper_id": 2,
                "is_influential": True,
                "citation_context": "extends",
            },
            {
                "source_paper_id": 3,
                "cited_paper_id": 1,
                "is_influential": False,
                "citation_context": None,
            },
        ],
    ]

    result = await build_citation_graph(mock_conn, [1])
    assert len(result.nodes) == 3
    assert len(result.edges) == 2
    stub_nodes = [n for n in result.nodes if n.is_stub]
    assert len(stub_nodes) == 1
    assert stub_nodes[0].title == "Paper B"


@pytest.mark.asyncio
async def test_build_graph_depth_2():
    """Graph expands to depth 2."""
    from paper_ingestion.citations import build_citation_graph

    mock_conn = AsyncMock()
    mock_conn.fetch.side_effect = [
        # Depth 1: paper 1 connects to 2
        [{"source_paper_id": 1, "cited_paper_id": 2}],
        # Depth 2: paper 2 connects to 3
        [{"source_paper_id": 2, "cited_paper_id": 3}],
        # Node data
        [
            {"id": 1, "title": "A", "citation_count": 0, "published_date": None, "metadata": {}},
            {"id": 2, "title": "B", "citation_count": 0, "published_date": None, "metadata": {}},
            {"id": 3, "title": "C", "citation_count": 0, "published_date": None, "metadata": {}},
        ],
        # Edges
        [
            {
                "source_paper_id": 1,
                "cited_paper_id": 2,
                "is_influential": None,
                "citation_context": None,
            },
            {
                "source_paper_id": 2,
                "cited_paper_id": 3,
                "is_influential": None,
                "citation_context": None,
            },
        ],
    ]

    result = await build_citation_graph(mock_conn, [1], depth=2)
    assert len(result.nodes) == 3
    assert len(result.edges) == 2


# ---------------------------------------------------------------------------
# PI-007: batch_fetch_citations endpoint enqueues a durable job
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Create a mock asyncpg Pool whose acquire() returns an async context manager."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.mark.asyncio
async def test_batch_fetch_citations_enqueues_job():
    """POST /api/citations/batch-fetch enqueues a citations.batch_fetch job.

    PI-007: endpoint must no longer use BackgroundTasks — it must call
    citations_batch_fetch.defer_async and return a job_id in the response.
    """
    from paper_ingestion.routers import citations as citations_router

    pool, _conn = _make_pool_and_conn()

    import jarvis_common.task_registry as task_registry

    mock_task = MagicMock()
    mock_defer = AsyncMock(return_value=None)
    mock_task.defer_async = mock_defer
    with patch.dict(task_registry.KIND_TO_TASK, {"citations.batch_fetch": mock_task}):
        result = await citations_router.batch_fetch_citations.__wrapped__(
            MagicMock(),
            db_pool=pool,
        )

    mock_defer.assert_awaited_once()
    call_kwargs = mock_defer.call_args.kwargs
    assert "job_id" in call_kwargs
    assert call_kwargs["user_id"] is None
    assert result.message == f"Job {call_kwargs['job_id']} queued"
    assert result.queued == 1


@pytest.mark.asyncio
async def test_batch_fetch_citations_response_shape():
    """batch_fetch_citations response has job_id embedded in message field."""
    import jarvis_common.task_registry as task_registry
    from paper_ingestion.routers import citations as citations_router

    pool, _conn = _make_pool_and_conn()

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(return_value=None)
    with patch.dict(task_registry.KIND_TO_TASK, {"citations.batch_fetch": mock_task}):
        result = await citations_router.batch_fetch_citations.__wrapped__(
            MagicMock(),
            db_pool=pool,
        )

    assert result.message.startswith("Job ")
    assert "queued" in result.message
    assert result.queued >= 1


# ---------------------------------------------------------------------------
# H11: ownership enforcement on citation endpoints
# ---------------------------------------------------------------------------
# assert_paper_ownership semantics (from db_helpers.py):
#   - user_id=None  → single-user mode, all access allowed (no DB hit)
#   - user_id set   → fetchrow("SELECT discovered_by FROM papers WHERE id=$1")
#       None        → raises HTTPException(404)
#       row present, caller in library or canonical (discovered_by=None) → OK
#       row present, not in library, different discoverer → raises HTTPException(403)
#
# These tests exercise the "user B cannot access user A's paper" path for each
# of the three endpoints added in H11.


class FakeRecord(dict):
    """asyncpg.Record substitute that supports dict[key] and .keys()."""

    def keys(self):
        return super().keys()


async def _user_b(_request):
    """Simulate a caller with user_id=2 (does not own paper owned by user 1)."""
    del _request
    return 2


@pytest.mark.parametrize(
    "endpoint_name,call_kwargs",
    [
        (
            "get_paper_citations",
            {"paper_id": 7},
        ),
        (
            "fetch_citations_for_paper",
            {"paper_id": 7},
        ),
    ],
)
@pytest.mark.asyncio
async def test_single_paper_citation_endpoints_403_for_other_user(
    monkeypatch, endpoint_name, call_kwargs
):
    """Single-paper citation endpoints raise 403 when caller does not own the paper.

    The paper exists (discovered_by=1) but the caller is user 2 and is not in
    the paper's user_library entry → assert_paper_ownership raises 403.
    """
    from paper_ingestion.routers import citations as citations_router

    monkeypatch.setattr(
        "paper_ingestion.routers.citations.current_user_id_or_none",
        _user_b,
    )

    pool, conn = _make_pool_and_conn()
    # assert_paper_ownership probes discovered_by; user 1 owns this paper
    conn.fetchrow.return_value = FakeRecord(discovered_by=1)
    # user_library miss → caller is not in library → 403
    conn.fetchval = AsyncMock(return_value=None)

    endpoint = getattr(citations_router, endpoint_name).__wrapped__

    # fetch_citations_for_paper also needs an s2_source dependency but we
    # never reach it because ownership check fires first.
    extra = {}
    if endpoint_name == "fetch_citations_for_paper":
        extra["s2_source"] = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await endpoint(MagicMock(), db_pool=pool, **call_kwargs, **extra)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_get_citation_graph_403_for_unowned_paper(monkeypatch):
    """GET /api/citations/graph raises 403 for the first unowned paper_id.

    Semantics: strict-fail on first unauthorized id (per-id loop in the router).
    paper_id=7 is owned by user 1; caller is user 2 and is not in the library.
    """
    from paper_ingestion.routers import citations as citations_router

    monkeypatch.setattr(
        "paper_ingestion.routers.citations.current_user_id_or_none",
        _user_b,
    )

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(discovered_by=1)
    conn.fetchval = AsyncMock(return_value=None)  # not in library

    with pytest.raises(HTTPException) as exc_info:
        await citations_router.get_citation_graph.__wrapped__(
            MagicMock(),
            paper_ids=[7],
            depth=1,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_get_citation_graph_filters_unauthorized_ids(monkeypatch):
    """Mixed list [owned_id, unowned_id] → 403 on the first unowned id.

    Chosen semantics: strict-fail (mirrors assert_paper_ownership default).
    The endpoint aborts at the first unauthorized paper rather than returning
    a partial subgraph.  This is the simpler, safer contract: callers learn
    explicitly which paper they lack access to.
    """
    from paper_ingestion.routers import citations as citations_router

    monkeypatch.setattr(
        "paper_ingestion.routers.citations.current_user_id_or_none",
        _user_b,
    )

    pool, conn = _make_pool_and_conn()

    async def _fetchrow(sql, paper_id):
        # Paper 1: caller is the discoverer (ownership granted)
        # Paper 99: discovered by user 1 (not the caller)
        if paper_id == 1:
            return FakeRecord(discovered_by=2)
        return FakeRecord(discovered_by=1)

    conn.fetchrow.side_effect = _fetchrow
    # For paper 99: user_library miss → not in library
    conn.fetchval = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc_info:
        await citations_router.get_citation_graph.__wrapped__(
            MagicMock(),
            paper_ids=[1, 99],
            depth=1,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 403
