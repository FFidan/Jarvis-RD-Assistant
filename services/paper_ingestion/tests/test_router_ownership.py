"""WS-6B-β: ownership wiring smoke tests for paper-touching routers.

Verifies (a) single-user mode (caller user_id=None) is unchanged — endpoints
return their normal status; (b) the WHERE filter for cross-paper SQL is
present in the rendered query and evaluates to TRUE in single-user mode (so
all rows are returned).

The 4-quadrant ownership matrix (NULL caller, NULL paper, owner match,
owner mismatch) is exhaustively tested in
``libs/jarvis_common/tests/test_ownership.py`` and
``services/paper_ingestion/tests/test_jobs_sse_ownership.py`` —
this module focuses only on the new router wiring sites.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """asyncpg.Record substitute that supports dict[key], .keys(), .get()."""

    def keys(self):
        return super().keys()


def _make_pool_and_conn():
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def _app():
    """Minimal app instance with mocked DB pool and disabled auth/limiter."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None

    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# rag.py — single-paper summarize endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rag_summarize_paper_passes_in_single_user_mode(_app, monkeypatch):
    """POST /api/summarize/{paper_id} returns 202 in single-user mode."""
    app, conn = _app

    # Stub out paper_summarize.defer_async so we don't need a real DB / procrastinate
    from jarvis_common import task_registry

    monkeypatch.setattr(task_registry.paper_summarize, "defer_async", AsyncMock())

    # In single-user mode (current_user_id_or_none returns None) the
    # ownership helper short-circuits and never calls fetchrow.
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/summarize/42")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    # Ownership check skipped entirely in single-user mode → no fetchrow call
    conn.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# extractions.py — single-paper extract endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractions_extract_paper_passes_in_single_user_mode(_app, monkeypatch):
    """POST /api/papers/{paper_id}/extract returns 202 in single-user mode."""
    from unittest.mock import AsyncMock

    from jarvis_common import task_registry

    app, conn = _app

    monkeypatch.setattr(task_registry.extraction_single, "defer_async", AsyncMock())

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
    conn.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# search.py — single-paper relevance-score endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_relevance_score_passes_in_single_user_mode(_app):
    """POST /api/relevance-score returns 200 in single-user mode + healthy embedder."""
    app, conn = _app

    # fetchrow is called for paper + topic lookups (NOT for the ownership
    # check, which short-circuits in single-user mode).
    conn.fetchrow.side_effect = [
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
    # Both data + count queries should embed the user-scoping predicate
    assert any(
        "($1::int IS NULL OR p.user_id IS NULL OR p.user_id = $1)" in q for q in captured_queries
    ), f"user filter missing from queries: {captured_queries}"


# ---------------------------------------------------------------------------
# discovery.py — single-paper similar/{id} endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_similar_papers_passes_in_single_user_mode(_app):
    """GET /api/similar/{paper_id} returns 200 in single-user mode."""
    app, conn = _app

    # fetchrow returns paper metadata (ownership check is short-circuited)
    conn.fetchrow.return_value = FakeRecord(id=42, title="Seed Paper", abstract="Seed abstract.")
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

    conn.fetchrow.return_value = FakeRecord(id=42, title="Seed", abstract="Abstract.")

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
    assert any(
        "($2::int IS NULL OR user_id IS NULL OR user_id = $2)" in q for q in captured_queries
    ), f"enrichment user filter missing: {captured_queries}"


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

    assert resp.status_code == 200, resp.text
    assert any(
        "($2::int IS NULL OR p.user_id IS NULL OR p.user_id = $2)" in q for q in captured_queries
    ), f"batch_summarize user filter missing: {captured_queries}"


# Touch unused imports so static analysers don't complain in editor environments
_ = datetime.now(UTC)
