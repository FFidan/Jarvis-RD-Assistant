"""Tests for the What's New paper feed endpoints."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — fake asyncpg records
# ---------------------------------------------------------------------------


def _make_paper_record(
    paper_id: int = 1,
    discovered_at: datetime | None = None,
    summary_brief: str | None = "Brief summary",
    confidence: str | None = "HIGH",
    user_status: str | None = "new",
    rating: int | None = None,
) -> dict:
    """Return a dict mimicking an asyncpg Record for a joined feed row."""
    now = discovered_at or datetime.now(UTC)
    return {
        "id": paper_id,
        "external_id": f"arxiv:{paper_id}",
        "source_type": "arxiv",
        "title": f"Paper {paper_id}",
        "authors": ["Author A"],
        "abstract": "Abstract text",
        "published_date": None,
        "url": f"https://arxiv.org/abs/{paper_id}",
        "pdf_url": None,
        "pdf_local_path": None,
        "pdf_downloaded": False,
        "citation_count": 0,
        "metadata": {},
        "discovered_at": now,
        "created_at": now,
        "summary_brief": summary_brief,
        "confidence": confidence,
        "user_status": user_status,
        "rating": rating,
    }


class FakeRecord(dict):
    """Dict subclass that supports both dict[key] and .get()."""

    def __getitem__(self, key):
        return super().__getitem__(key)

    def get(self, key, default=None):
        return super().get(key, default)


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
        from paper_ingestion.deps import get_db_pool
        from paper_ingestion.main import app

        # Override the db_pool dependency — use MagicMock so that
        # pool.acquire() returns a synchronous context-manager stub
        # (matching asyncpg's PoolAcquireContext behaviour).
        mock_pool = MagicMock()
        app.dependency_overrides = {}

        app.dependency_overrides[get_db_pool] = lambda: mock_pool
        app.state.limiter.enabled = False

        # Disable auth
        from jarvis_common import verify_api_key

        app.dependency_overrides[verify_api_key] = lambda: None

        yield TestClient(app, raise_server_exceptions=False), mock_pool

        app.dependency_overrides.clear()
    app.state.limiter.enabled = True


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
