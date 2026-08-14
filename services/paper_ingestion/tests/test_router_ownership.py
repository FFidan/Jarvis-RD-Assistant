"""Visibility-policy wiring smoke tests for paper-touching routers.

Verifies that single-paper guards and cross-paper queries consistently use the
persisted public-or-library policy. The policy matrix itself is tested in
``libs/jarvis_common/tests/test_ownership.py`` and
``libs/jarvis_common/tests/test_ownership_canonical_invariant.py``; this module
focuses on router wiring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from tests._embedder_fakes import _make_embedder
from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Minimal app instance with mocked DB pool and disabled auth/limiter."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
    from paper_ingestion.deps import (
        get_db_pool,
        get_embedder,
        get_http_client,
        get_verifier,
        limiter,
    )
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    mock_http = AsyncMock()
    with patch_pi_test_app(
        mock_pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            state_overrides={"http_client": mock_http},
            # Test bodies add per-test overrides for these seams; declaring
            # them here guarantees any in-test write is removed again on exit.
            dependency_absent=(get_embedder, get_http_client, get_verifier),
            dependency_overrides={
                verify_api_key: lambda: None,
                # Depends-wired routes (extract_paper, find_similar_papers)
                # resolve the caller via current_user_id_strict; pin it.
                # Still-imperative routes in this file resolve via the autouse
                # module-symbol monkeypatch.
                current_user_id_strict: lambda: 1,
            },
        ),
    ):
        yield app, conn


# ---------------------------------------------------------------------------
# rag.py — single-paper summarize endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rag_summarize_paper_passes_for_visible_paper(_app, monkeypatch):
    """POST /api/summarize/{paper_id} returns 202 for a visible paper."""
    app, conn = _app

    # Stub out paper.summarize task so we don't need a real DB / procrastinate
    from unittest.mock import MagicMock

    import jarvis_common.task_registry as task_registry

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()
    monkeypatch.setitem(task_registry._TASK_MAP, "paper.summarize", mock_task)

    conn.fetchrow.return_value = {"id": 42, "is_visible": True}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/summarize/42")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    # Visibility is enforced before enqueueing.
    conn.fetchrow.assert_awaited()


# ---------------------------------------------------------------------------
# extractions.py — single-paper extract endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractions_extract_paper_passes_for_visible_paper(_app, monkeypatch):
    """POST /api/papers/{paper_id}/extract returns 202 for a visible paper."""
    from unittest.mock import AsyncMock, MagicMock

    import jarvis_common.task_registry as task_registry

    app, conn = _app

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()
    monkeypatch.setitem(task_registry._TASK_MAP, "extraction.single", mock_task)

    conn.fetchrow.return_value = {"id": 42, "is_visible": True}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/papers/42/extract",
            json={"template_id": 1},
        )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    conn.fetchrow.assert_awaited()


# ---------------------------------------------------------------------------
# search.py — single-paper relevance-score endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_relevance_score_passes_for_visible_paper(_app):
    """POST /api/relevance-score returns 200 for a visible paper and healthy embedder."""
    app, conn = _app

    conn.fetchrow.side_effect = [
        {"id": 7, "is_visible": True},
        FakeRecord(title="Test Paper", abstract="An abstract."),
        FakeRecord(query_terms=["agents"]),
    ]
    conn.execute.return_value = "INSERT 0 1"

    embedder = MagicMock()
    embedder.qdrant = object()  # truthy
    embedder.compute_relevance = AsyncMock(return_value=0.42)

    from paper_ingestion.deps import get_embedder

    app.dependency_overrides[get_embedder] = lambda: embedder

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/relevance-score?paper_id=7&topic_id=3")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paper_id"] == 7
    assert body["topic_id"] == 3
    assert body["relevance_score"] == 0.42


# ---------------------------------------------------------------------------
# feed.py — cross-paper feed endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_list_papers_includes_user_filter_in_query(_app):
    """GET /api/papers/feed renders the user-scoping predicate into the SQL."""
    app, conn = _app

    captured_queries: list[str] = []

    async def _capture_fetch(query, *args):
        captured_queries.append(query)
        return []

    async def _capture_fetchval(query, *args):
        captured_queries.append(query)
        return 0

    conn.fetch.side_effect = _capture_fetch
    conn.fetchval.side_effect = _capture_fetchval

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/feed")

    assert resp.status_code == 200, resp.text
    # The rendered SQL must not use legacy paper ownership fields.
    feed_queries = [q for q in captured_queries if " FROM papers p" in q]
    assert feed_queries, f"feed query never issued: {captured_queries}"
    for q in feed_queries:
        assert "p.user_id" not in q, f"legacy p.user_id leaked into query: {q}"
        assert "p.discovered_by" not in q, f"audit column must not scope feed: {q}"


# ---------------------------------------------------------------------------
# discovery.py — single-paper similar/{id} endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_similar_papers_passes_for_visible_seed(_app):
    """GET /api/similar/{paper_id} returns 200 for a visible seed paper."""
    app, conn = _app

    conn.fetchrow.return_value = FakeRecord(
        id=42,
        title="Seed Paper",
        abstract="Seed abstract.",
        is_visible=True,
    )
    conn.fetch.return_value = []  # no enrichment rows

    embedder = MagicMock()
    embedder.qdrant = object()
    embedder.search_similar = AsyncMock(return_value=[])

    from paper_ingestion.deps import get_embedder

    app.dependency_overrides[get_embedder] = lambda: embedder

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/similar/42")

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# Extra: confirm the discovery enrichment SQL embeds the user filter
@pytest.mark.asyncio
async def test_discovery_similar_enrichment_query_includes_user_filter(_app):
    """The metadata-enrichment SELECT must scope by user_id."""
    app, conn = _app

    conn.fetchrow.return_value = FakeRecord(
        id=42,
        title="Seed",
        abstract="Abstract.",
        is_visible=True,
    )

    captured_queries: list[str] = []

    async def _capture_fetch(query, *args):
        captured_queries.append(query)
        return []

    conn.fetch.side_effect = _capture_fetch

    embedder = MagicMock()
    embedder.qdrant = object()
    embedder.search_similar = AsyncMock(
        return_value=[
            {"paper_id": 1, "score": 0.9, "content": "snippet"},
        ]
    )

    from paper_ingestion.deps import get_embedder

    app.dependency_overrides[get_embedder] = lambda: embedder

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/similar/42")

    assert resp.status_code == 200, resp.text
    # Enrichment uses the same persisted public-or-library predicate.
    enrich_queries = [q for q in captured_queries if "FROM papers" in q]
    assert enrich_queries, f"enrichment never queried: {captured_queries}"
    for q in enrich_queries:
        assert "visibility_scope = 'public'" in q
        assert "user_library" in q
        assert "discovered_by" not in q, f"audit column must not gate access: {q}"


# ---------------------------------------------------------------------------
# Defensive: confirm cross-paper batch_summarize SQL has the filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rag_batch_summarize_query_includes_user_filter(_app, monkeypatch):
    """POST /api/papers/batch-summarize SQL embeds the user-scoping predicate."""
    app, conn = _app

    captured_queries: list[str] = []

    async def _capture_fetch(query, *args):
        captured_queries.append(query)
        return []

    conn.fetch.side_effect = _capture_fetch

    # Patch http_client + verifier deps to no-ops
    from paper_ingestion.deps import get_http_client, get_verifier

    app.dependency_overrides[get_http_client] = lambda: AsyncMock()
    app.dependency_overrides[get_verifier] = lambda: MagicMock()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/papers/batch-summarize")

    assert resp.status_code == 202, resp.text
    # The batch selection is rooted on papers and must not use legacy owner or
    # audit columns as an authorization shortcut.
    bs_queries = [q for q in captured_queries if "FROM papers p" in q]
    assert bs_queries, f"batch_summarize never queried: {captured_queries}"
    for q in bs_queries:
        assert "p.user_id" not in q, f"legacy predicate leaked: {q}"
        assert "p.discovered_by" not in q, f"audit column must not gate access: {q}"


# ---------------------------------------------------------------------------
# PI-LIB-03: process_batch and batch_extract_papers use assert_papers_ownership
# (batch call) instead of a per-paper loop.  Tests verify:
#   (a) any private paper outside the library is rejected before enqueue;
#   (b) a batch whose rows all satisfy the central predicate is queued.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_batch_rejects_private_paper_outside_library(_app, monkeypatch):
    """POST /api/papers/process_batch rejects an invisible private paper.

    # Verified: papers_bulk.py:141 — assert_papers_ownership (batch) called once.
    """
    import jarvis_common.task_registry as task_registry

    app, conn = _app

    # Override get_current_user_id so process_batch sees user_id=1.
    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 1

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()
    monkeypatch.setitem(task_registry._TASK_MAP, "papers.batch_process", mock_task)

    conn.fetch.return_value = [{"id": 99, "is_visible": False}]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/papers/process_batch",
            json={"paper_ids": [99]},
        )

    assert resp.status_code == 403, (
        f"Expected 403 for non-owned paper in process_batch; got {resp.status_code}: {resp.text}"
    )
    mock_task.defer_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_batch_accepts_visible_paper(_app, monkeypatch):
    """POST /api/papers/process_batch queues a batch whose paper is visible."""
    import jarvis_common.task_registry as task_registry

    app, conn = _app

    from jarvis_common.auth import get_current_user_id

    app.dependency_overrides[get_current_user_id] = lambda: 1

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()
    monkeypatch.setitem(task_registry._TASK_MAP, "papers.batch_process", mock_task)

    conn.fetch.return_value = [{"id": 42, "is_visible": True}]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/papers/process_batch",
            json={"paper_ids": [42]},
        )

    assert resp.status_code == 202, (
        f"Expected 202 for caller-owned paper in process_batch; got {resp.status_code}: {resp.text}"
    )
    assert "job_id" in resp.json(), f"Response must contain job_id; got: {resp.json()}"
    mock_task.defer_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_extract_papers_rejects_private_paper_outside_library(_app, monkeypatch):
    """POST /extractions/batch rejects an invisible private paper.

    # Verified: extractions.py:308 — assert_papers_ownership (batch) called once.
    """
    import jarvis_common.task_registry as task_registry

    app, conn = _app

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()
    monkeypatch.setitem(task_registry._TASK_MAP, "extraction.batch", mock_task)

    conn.fetch.return_value = [{"id": 77, "is_visible": False}]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/extractions/batch",
            json={"paper_ids": [77], "template_id": 1},
        )

    assert resp.status_code == 403, (
        f"Expected 403 for non-owned paper in batch_extract; got {resp.status_code}: {resp.text}"
    )
    mock_task.defer_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_extract_papers_accepts_visible_paper(_app, monkeypatch):
    """POST /extractions/batch queues a batch whose paper is visible."""
    import jarvis_common.task_registry as task_registry

    app, conn = _app

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()
    monkeypatch.setitem(task_registry._TASK_MAP, "extraction.batch", mock_task)

    conn.fetch.return_value = [{"id": 55, "is_visible": True}]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/extractions/batch",
            json={"paper_ids": [55], "template_id": 1},
        )

    assert resp.status_code == 200, (
        f"Expected 200 for caller-owned paper in batch_extract; got {resp.status_code}: {resp.text}"
    )
    assert "job_id" in resp.json(), f"Response must contain job_id; got: {resp.json()}"
    mock_task.defer_async.assert_awaited_once()


# ---------------------------------------------------------------------------
# Ranked retrieval endpoints must spend their result budget on results the
# caller may see: the visibility and chunk-backing filters run over the whole
# candidate pool, and the page is cut afterwards.
#
# Verified: routers/discovery.py:153 (GET /api/similar page cut after filtering)
# Verified: routers/discovery.py:254 (POST /api/discover page cut after filtering)
# Verified: ingestion/search.py:687 (hybrid_search page cut after the recheck)
# ---------------------------------------------------------------------------

_CANDIDATE_IDS = [101, 102, 103, 104, 105, 106]
_VISIBLE_CANDIDATE_IDS = [103, 104, 105, 106]


def _ranked_hits(paper_ids: list[int]) -> list[dict]:
    """Vector hits for *paper_ids*, best score first, each backed by chunk 0."""
    return [
        {
            "paper_id": paper_id,
            "score": 0.99 - index * 0.01,
            "chunk_index": 0,
            "content": f"snippet for {paper_id}",
        }
        for index, paper_id in enumerate(paper_ids)
    ]


def _meta_record(paper_id: int) -> FakeRecord:
    """One paper-metadata row, carrying every column both enrichment paths read."""
    return FakeRecord(
        {
            "id": paper_id,
            "title": f"Paper {paper_id}",
            "authors": ["A. Author"],
            "url": f"https://example.test/{paper_id}",
            "abstract": f"abstract {paper_id}",
            "published_date": None,
        }
    )


def _wire_discovery_conn(conn, *, visible_ids: list[int], seed_paper_id: int = 42) -> None:
    """Answer each SELECT the discovery endpoints issue, dispatched by shape."""
    conn.fetchrow.return_value = FakeRecord(
        {
            "id": seed_paper_id,
            "title": "Seed Paper",
            "abstract": "Seed abstract.",
            "is_visible": True,
        }
    )

    async def _fetch(query, *args):
        if "FROM paper_chunks" in query:
            return [FakeRecord({"paper_id": pid, "chunk_index": 0}) for pid in args[0]]
        if "FROM papers p" in query:
            return [_meta_record(pid) for pid in args[0] if pid in visible_ids]
        if "FROM papers WHERE id" in query:
            return [FakeRecord({"id": seed_paper_id})]
        return []  # caller-library scope lookup

    conn.fetch.side_effect = _fetch


def _wire_hybrid_conn(conn, *, visible_ids: list[int], bm25_ids: list[int]) -> None:
    """Answer hybrid search's BM25 leg and its metadata visibility recheck."""

    async def _fetch(query, *args):
        if "search_vector" in query:
            candidate_limit = args[1]
            return [_meta_record(pid) for pid in bm25_ids[:candidate_limit]]
        if "FROM papers p" in query:
            return [_meta_record(pid) for pid in args[0] if pid in visible_ids]
        return []  # caller-library scope lookup

    conn.fetch.side_effect = _fetch


def _semantic_chunks(paper_ids: list[int]) -> list[dict]:
    """Global chunk hits for *paper_ids*, best score first."""
    return [
        {
            "paper_id": paper_id,
            "chunk_index": 0,
            "content": "",
            "page_number": None,
            "score": 0.99 - index * 0.01,
        }
        for index, paper_id in enumerate(paper_ids)
    ]


def _override_embedder(app, embedder) -> None:
    """Point the get_embedder dependency at *embedder* for this request."""
    from paper_ingestion.deps import get_embedder

    app.dependency_overrides[get_embedder] = lambda: embedder


def _asgi_client(app) -> httpx.AsyncClient:
    """Return the standard in-process ASGI client used across this module."""
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_similar_page_is_filled_with_visible_lower_ranked_papers(_app):
    """GET /api/similar/{id} returns a full page when the top candidates are invisible."""
    app, conn = _app
    _wire_discovery_conn(conn, visible_ids=_VISIBLE_CANDIDATE_IDS)

    embedder = MagicMock()
    embedder.qdrant = object()
    embedder.search_similar = AsyncMock(return_value=_ranked_hits(_CANDIDATE_IDS))
    _override_embedder(app, embedder)

    async with _asgi_client(app) as client:
        resp = await client.get("/api/similar/42?limit=2")

    assert resp.status_code == 200, resp.text
    assert [item["paper_id"] for item in resp.json()] == [103, 104], (
        "GET /api/similar must fill the requested page with the highest-ranked visible "
        f"candidates instead of losing the slots the invisible ones took; got {resp.json()}"
    )
    assert embedder.search_similar.await_args.kwargs["limit"] > 2, (
        "the candidate pool must exceed the page size, or filtering has nothing to fall back on"
    )


@pytest.mark.asyncio
async def test_discover_page_is_filled_with_visible_lower_ranked_papers(_app):
    """POST /api/discover returns a full page when the top candidates are invisible."""
    app, conn = _app
    _wire_discovery_conn(conn, visible_ids=_VISIBLE_CANDIDATE_IDS)

    candidates = _ranked_hits(_CANDIDATE_IDS)

    async def _discover_from_seeds(*, limit, **_kwargs):
        # Mirrors discover_from_seeds: best score first, at most `limit` papers.
        return candidates[:limit]

    embedder = MagicMock()
    embedder.qdrant = object()
    embedder.discover_from_seeds = AsyncMock(side_effect=_discover_from_seeds)
    _override_embedder(app, embedder)

    async with _asgi_client(app) as client:
        resp = await client.post("/api/discover", json={"paper_ids": [42], "limit": 2})

    assert resp.status_code == 200, resp.text
    assert [item["paper_id"] for item in resp.json()] == [103, 104], (
        "POST /api/discover must fill the requested page with the highest-ranked visible "
        f"candidates instead of losing the slots the invisible ones took; got {resp.json()}"
    )


@pytest.mark.asyncio
async def test_search_hybrid_page_is_filled_with_visible_lower_ranked_papers(_app):
    """POST /api/papers/search-hybrid fills its page after the metadata recheck."""
    from jarvis_common.auth import get_current_user_id

    app, conn = _app
    app.dependency_overrides[get_current_user_id] = lambda: 1
    _wire_hybrid_conn(conn, visible_ids=_VISIBLE_CANDIDATE_IDS, bm25_ids=[])

    candidates = _semantic_chunks(_CANDIDATE_IDS)

    async def _bounded_semantic_search(_query, *, limit, **_kwargs):
        return candidates[:limit]

    embedder = _make_embedder()
    embedder.search_chunks_global = AsyncMock(side_effect=_bounded_semantic_search)
    _override_embedder(app, embedder)

    async with _asgi_client(app) as client:
        resp = await client.post(
            "/api/papers/search-hybrid", json={"query": "test", "max_results": 2}
        )

    assert resp.status_code == 200, resp.text
    assert [item["id"] for item in resp.json()] == [103, 104], (
        "POST /api/papers/search-hybrid must fill the requested page with the highest-ranked "
        f"papers that survive the visibility recheck; got {resp.json()}"
    )
    assert embedder.search_chunks_global.await_args.kwargs["limit"] > 2, (
        "the semantic request must exceed the page size, or invisible leading "
        "candidates consume every slot before the metadata recheck"
    )


@pytest.mark.asyncio
async def test_search_hybrid_recheck_changes_composition_not_fused_order(_app):
    """Dropping papers the caller may not see leaves the surviving fused order intact."""
    from jarvis_common.auth import get_current_user_id

    app, conn = _app
    app.dependency_overrides[get_current_user_id] = lambda: 1

    # Both legs contribute, so fusion — not one leg's ranking — decides the order.
    bm25_ids = [106, 104, 102]
    chunks = _semantic_chunks(_CANDIDATE_IDS)
    embedder = _make_embedder()
    embedder.search_chunks_global = AsyncMock(return_value=chunks)
    _override_embedder(app, embedder)

    body = {"query": "test", "max_results": 10}
    _wire_hybrid_conn(conn, visible_ids=_CANDIDATE_IDS, bm25_ids=bm25_ids)
    async with _asgi_client(app) as client:
        everything_visible = await client.post("/api/papers/search-hybrid", json=body)

    assert everything_visible.status_code == 200, everything_visible.text
    fused_order = [item["id"] for item in everything_visible.json()]
    assert fused_order == [102, 104, 106, 101, 103, 105], (
        f"reciprocal-rank fusion must interleave both legs; got {fused_order}"
    )

    # 101 and 105 reach the ranking through the semantic leg alone, so the
    # metadata recheck is what removes them.
    survivors = [pid for pid in _CANDIDATE_IDS if pid not in (101, 105)]
    _wire_hybrid_conn(conn, visible_ids=survivors, bm25_ids=bm25_ids)
    async with _asgi_client(app) as client:
        filtered = await client.post("/api/papers/search-hybrid", json=body)

    assert filtered.status_code == 200, filtered.text
    assert [item["id"] for item in filtered.json()] == [
        pid for pid in fused_order if pid in survivors
    ], (
        "the recheck must change which papers are returned, not the order of the ones "
        f"that survive it; got {[item['id'] for item in filtered.json()]} from {fused_order}"
    )
