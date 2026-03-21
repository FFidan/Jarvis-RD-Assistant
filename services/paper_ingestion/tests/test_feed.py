"""Tests for the What's New paper feed endpoints."""

import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

# Stub heavy native modules unavailable outside Docker.
for _mod_name in ("fitz",):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

import pytest

# ---------------------------------------------------------------------------
# Helpers — fake asyncpg records
# ---------------------------------------------------------------------------


def _make_paper_record(
    paper_id: int = 1,
    is_read: bool = False,
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
        "is_read": is_read,
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
        from app.main import app, get_db_pool
        from fastapi.testclient import TestClient

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
        assert "is_read" in paper
        assert "discovered_at" in paper

    def test_unread_only_filters_correctly(self, client):
        """Feed with unread_only=true returns only unread papers."""
        test_client, mock_pool = client
        conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        unread_records = [_to_record(_make_paper_record(paper_id=1, is_read=False))]
        conn.fetch.return_value = unread_records
        conn.fetchval.return_value = 1

        resp = test_client.get("/api/papers/feed", params={"unread_only": "true"})
        assert resp.status_code == 200

        body = resp.json()
        assert body["total"] == 1
        assert len(body["papers"]) == 1
        assert body["papers"][0]["is_read"] is False

        # Verify SQL contained the WHERE clause for is_read
        fetch_call = conn.fetch.call_args
        sql = fetch_call[0][0]
        assert "is_read = FALSE" in sql

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


class TestMarkPaperRead:
    """Tests for PUT /api/papers/{paper_id}/read."""

    def test_mark_paper_read_success(self, client):
        """Marking an existing paper as read returns ok."""
        test_client, mock_pool = client
        conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        conn.fetchrow.return_value = _to_record({"id": 42})

        resp = test_client.put("/api/papers/42/read")
        assert resp.status_code == 200

        body = resp.json()
        assert body["status"] == "ok"
        assert body["paper_id"] == 42

        # Verify the UPDATE SQL was called with the right paper_id
        fetchrow_call = conn.fetchrow.call_args
        sql = fetchrow_call[0][0]
        assert "UPDATE papers SET is_read = TRUE" in sql
        assert fetchrow_call[0][1] == 42

    def test_mark_paper_read_writes_paper_user_state(self, client):
        """PUT /api/papers/{id}/read also upserts paper_user_state."""
        test_client, mock_pool = client
        conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        conn.fetchrow.return_value = _to_record({"id": 42})

        resp = test_client.put("/api/papers/42/read")
        assert resp.status_code == 200

        # Verify paper_user_state INSERT was executed
        execute_calls = conn.execute.call_args_list
        assert len(execute_calls) >= 1
        user_state_sql = execute_calls[0][0][0]
        assert "paper_user_state" in user_state_sql
        assert "read" in user_state_sql
        # Verify paper_id was passed as argument
        assert execute_calls[0][0][1] == 42

    def test_mark_nonexistent_paper_returns_404(self, client):
        """Marking a nonexistent paper as read returns 404."""
        test_client, mock_pool = client
        conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        conn.fetchrow.return_value = None

        resp = test_client.put("/api/papers/99999/read")
        assert resp.status_code == 404

        body = resp.json()
        assert "not found" in body["detail"].lower()
