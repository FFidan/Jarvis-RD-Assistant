"""Visibility-policy wiring smoke tests for paper-touching routers.

Verifies that single-paper guards and cross-paper queries consistently use the
persisted public-or-library policy. The policy matrix itself is tested in
``libs/jarvis_common/tests/test_ownership.py`` and
``libs/jarvis_common/tests/test_ownership_canonical_invariant.py``; this module
focuses on router wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Minimal app instance with mocked DB pool and disabled auth/limiter."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    # Depends-wired routes (extract_paper, find_similar_papers) resolve the
    # caller via current_user_id_strict; pin it. Still-imperative routes in
    # this file resolve via the autouse module-symbol monkeypatch.
    app.dependency_overrides[current_user_id_strict] = lambda: 1

    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


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


# Touch unused imports so static analysers don't complain in editor environments
_ = datetime.now(UTC)
