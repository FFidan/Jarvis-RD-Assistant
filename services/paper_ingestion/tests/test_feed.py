"""Tests for the What's New paper feed endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_paper_record as _make_paper_record
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

from tests.conftest import FakeRecord

# ---------------------------------------------------------------------------
# Helpers — fake asyncpg records
# ---------------------------------------------------------------------------


def _to_record(d: dict) -> FakeRecord:
    return FakeRecord(d)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Create a test client with mocked DB pool and disabled auth."""
    with patch.dict(
        "os.environ",
        {
            "DATABASE_URL": "postgresql://test:test@localhost/test",
            "QDRANT_URL": "http://localhost:6333",
            "DEV_MODE": "true",
        },
    ):
        # Patch lifespan to avoid real resource init
        from fastapi.testclient import TestClient
        from jarvis_common import verify_api_key
        from paper_ingestion.deps import get_db_pool, limiter
        from paper_ingestion.main import app

        # MagicMock so that pool.acquire() returns a synchronous
        # context-manager stub (matching asyncpg's PoolAcquireContext
        # behaviour). The autouse ``_default_authenticated_user`` override
        # stays in place: the helper touches only the seams named here.
        mock_pool = MagicMock()
        with patch_pi_test_app(
            mock_pool,
            app=app,
            get_db_pool=get_db_pool,
            limiter=limiter,
            options=PITestAppOptions(
                remove_owner_override=False,
                override_db_dependency=True,
                disable_limiter=True,
                dependency_overrides={verify_api_key: lambda: None},
            ),
        ):
            yield TestClient(app, raise_server_exceptions=False), mock_pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListFeedPapers:
    """Tests for GET /api/papers/feed."""

    def test_feed_returns_papers_with_correct_structure(self, client):
        """Feed endpoint returns papers list and total count."""
        test_client, mock_pool = client
        conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        records = [_to_record(_make_paper_record(paper_id=i)) for i in range(1, 4)]
        conn.fetch.return_value = records
        conn.fetchval.return_value = 3

        resp = test_client.get("/api/papers/feed")
        assert resp.status_code == 200

        body = resp.json()
        assert "papers" in body
        assert "total" in body
        assert body["total"] == 3
        assert len(body["papers"]) == 3

        paper = body["papers"][0]
        assert "id" in paper
        assert "title" in paper
        assert "summary_brief" in paper
        assert "confidence" in paper
        assert "discovered_at" in paper

    def test_unread_only_filters_correctly(self, client):
        """Feed with unread_only=true returns only unread papers."""
        test_client, mock_pool = client
        conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        unread_records = [_to_record(_make_paper_record(paper_id=1, user_status="new"))]
        conn.fetch.return_value = unread_records
        conn.fetchval.return_value = 1

        resp = test_client.get("/api/papers/feed", params={"unread_only": "true"})
        assert resp.status_code == 200

        body = resp.json()
        assert body["total"] == 1
        assert len(body["papers"]) == 1

        # Verify SQL uses VIEW_PREDICATES['active'] for unread filtering (Phase-A redesign)
        fetch_call = conn.fetch.call_args
        sql = fetch_call[0][0]
        assert "COALESCE(pus.state, 'inbox') IN ('inbox','to_read','reading')" in sql

    def test_empty_feed(self, client):
        """Feed returns empty list when no papers exist."""
        test_client, mock_pool = client
        conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        conn.fetch.return_value = []
        conn.fetchval.return_value = 0

        resp = test_client.get("/api/papers/feed")
        assert resp.status_code == 200

        body = resp.json()
        assert body["total"] == 0
        assert body["papers"] == []

    def test_feed_rejects_unknown_scope(self, client):
        """Feed scope is an explicit library/corpus enum."""
        test_client, _mock_pool = client

        resp = test_client.get("/api/papers/feed", params={"scope": "everything"})

        assert resp.status_code == 422
        assert "Unknown scope" in resp.text

    def test_feed_search_can_include_zotero_notes_without_duplicate_rows(self, client):
        """include_zotero_notes searches notes through EXISTS and exposes note metadata."""
        test_client, mock_pool = client
        conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        record = _make_paper_record(paper_id=1)
        record["note_match_count"] = 2
        record["note_snippet"] = "Important Zotero highlight"
        conn.fetch.return_value = [_to_record(record)]
        conn.fetchval.return_value = 1

        resp = test_client.get(
            "/api/papers/feed",
            params={"q": "highlight", "include_zotero_notes": "true"},
        )
        assert resp.status_code == 200

        sql = conn.fetch.call_args.args[0]
        count_sql = conn.fetchval.call_args.args[0]
        assert "EXISTS (SELECT 1 FROM paper_notes pn" in sql
        assert "EXISTS (SELECT 1 FROM paper_notes pn" in count_sql
        assert "pn.user_id IS NOT DISTINCT FROM $1" in sql
        assert "pn.user_id IS NOT DISTINCT FROM $1" in count_sql
        assert "JOIN paper_notes" not in count_sql
        assert resp.json()["papers"][0]["note_match_count"] == 2


def test_feed_route_served_from_papers_feed_module():
    """After consolidation, GET /api/papers/feed is owned by papers_feed (feed.py gone)."""
    import pytest
    from fastapi.routing import APIRoute

    try:  # FastAPI >=0.137 flattens the route tree through this public iterator.
        from fastapi.routing import (
            iter_route_contexts as _iter_route_contexts,  # type: ignore[attr-defined]
        )
    except ImportError:  # FastAPI <0.137 keeps app.routes a flat APIRoute list.
        _iter_route_contexts = None

    from paper_ingestion.main import app

    if _iter_route_contexts is not None:
        route = next(
            context.route
            for context in _iter_route_contexts(app.routes)
            if isinstance(context.route, APIRoute)
            and context.path == "/api/papers/feed"
            and "GET" in context.methods
        )
    else:
        route = next(
            r
            for r in app.routes
            if isinstance(r, APIRoute) and r.path == "/api/papers/feed" and "GET" in r.methods
        )
    assert route.endpoint.__module__.endswith("routers.papers_feed"), (
        f"/api/papers/feed must be defined in papers_feed; got {route.endpoint.__module__}"
    )
    with pytest.raises(ModuleNotFoundError):
        __import__("paper_ingestion.routers.feed")
