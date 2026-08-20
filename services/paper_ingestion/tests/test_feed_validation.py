"""Tests for list/string param caps on feed and discovery endpoints.

Verifies that:
- GET /api/papers/feed rejects CSV string params exceeding 500 characters (422).
- POST /api/discover rejects paper_ids lists exceeding 200 items (400 or 422).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: F401

import pytest
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

# ---------------------------------------------------------------------------
# Shared client fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client():
    """FastAPI TestClient with mocked DB, auth disabled, and rate-limiter off."""
    with patch.dict(
        "os.environ",
        {
            "DATABASE_URL": "postgresql://test:test@localhost/test",
            "QDRANT_URL": "http://localhost:6333",
            "DEV_MODE": "true",
        },
    ):
        from fastapi.testclient import TestClient
        from jarvis_common import verify_api_key
        from jarvis_common.auth import current_user_id_strict
        from paper_ingestion.deps import get_db_pool, get_embedder, limiter
        from paper_ingestion.main import app

        mock_pool = MagicMock()
        mock_embedder = MagicMock()
        with patch_pi_test_app(
            mock_pool,
            app=app,
            get_db_pool=get_db_pool,
            limiter=limiter,
            options=PITestAppOptions(
                remove_identity_overrides=False,
                override_db_dependency=True,
                disable_limiter=True,
                dependency_overrides={
                    verify_api_key: lambda: None,
                    get_embedder: lambda: mock_embedder,
                    # PR5-T8: /api/discover resolves identity via
                    # ``Depends(current_user_id_strict)``, which the autouse
                    # module-symbol stub does not reach. Override it so this
                    # "auth disabled" fixture keeps exercising the paper_ids
                    # cap (422) rather than the auth gate (401).
                    current_user_id_strict: lambda: 1,
                },
            ),
        ):
            yield TestClient(app, raise_server_exceptions=False), mock_pool


# ---------------------------------------------------------------------------
# feed CSV param caps
# ---------------------------------------------------------------------------


class TestFeedStringParamCaps:
    """GET /api/papers/feed CSV params must be rejected when > 500 chars."""

    def test_feed_rejects_topic_names_too_long(self, api_client):
        """topic_names > 500 chars → 422 Unprocessable Entity."""
        client, _ = api_client
        long_value = "neural-odes," * 50  # 600+ chars
        resp = client.get(f"/api/papers/feed?topic_names={long_value}")
        assert resp.status_code == 422, (
            f"Expected 422 for overlong topic_names, got {resp.status_code}: {resp.text}"
        )

    def test_feed_rejects_statuses_too_long(self, api_client):
        """statuses > 500 chars → 422 Unprocessable Entity."""
        client, _ = api_client
        long_value = "new," * 130  # 520 chars
        resp = client.get(f"/api/papers/feed?statuses={long_value}")
        assert resp.status_code == 422, (
            f"Expected 422 for overlong statuses, got {resp.status_code}: {resp.text}"
        )

    def test_feed_rejects_source_types_too_long(self, api_client):
        """source_types > 500 chars → 422 Unprocessable Entity."""
        client, _ = api_client
        long_value = "arxiv," * 90  # 540 chars
        resp = client.get(f"/api/papers/feed?source_types={long_value}")
        assert resp.status_code == 422, (
            f"Expected 422 for overlong source_types, got {resp.status_code}: {resp.text}"
        )

    def test_feed_accepts_topic_names_within_limit(self, api_client):
        """topic_names <= 500 chars passes validation (does not 422)."""
        client, mock_pool = api_client
        conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        conn.fetch.return_value = []
        conn.fetchval.return_value = 0

        short_value = "neural-odes,transformers"
        resp = client.get(f"/api/papers/feed?topic_names={short_value}")
        assert resp.status_code != 422, (
            f"Valid topic_names should not be rejected, got {resp.status_code}: {resp.text}"
        )

    def test_feed_rejects_q_too_long(self, api_client):
        """GET /api/papers/feed: q > 500 chars → 422 Unprocessable Entity."""
        client, _ = api_client
        long_q = "a" * 501
        resp = client.get(f"/api/papers/feed?q={long_q}")
        assert resp.status_code == 422, (
            f"Expected 422 for overlong q on /api/papers/feed, got {resp.status_code}: {resp.text}"
        )

    def test_papers_rejects_q_too_long(self, api_client):
        """GET /api/papers: q > 500 chars → 422 Unprocessable Entity."""
        client, _ = api_client
        long_q = "a" * 501
        resp = client.get(f"/api/papers?q={long_q}")
        assert resp.status_code == 422, (
            f"Expected 422 for overlong q on /api/papers, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# discover paper_ids list cap
# ---------------------------------------------------------------------------


class TestDiscoverPaperIdsCap:
    """POST /api/discover with > 200 paper_ids must be rejected."""

    def test_feed_rejects_too_many_paper_ids(self, api_client):
        """paper_ids with 250 items → 400 or 422."""
        client, _ = api_client
        paper_ids = list(range(250))
        resp = client.post("/api/discover", json={"paper_ids": paper_ids})
        assert resp.status_code in (400, 422), (
            f"Expected 400 or 422 for oversized paper_ids, got {resp.status_code}: {resp.text}"
        )

    def test_discover_rejects_exactly_201_paper_ids(self, api_client):
        """paper_ids with 201 items → 400 or 422 (boundary check)."""
        client, _ = api_client
        paper_ids = list(range(201))
        resp = client.post("/api/discover", json={"paper_ids": paper_ids})
        assert resp.status_code in (400, 422), (
            f"Expected 400 or 422 for 201 paper_ids, got {resp.status_code}: {resp.text}"
        )
