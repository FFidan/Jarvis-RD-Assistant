"""Regression tests — GET /api/papers/feed never returns 500 across limit values.

BATCH-A: regression test only. No production code changes.

The feed handler declares: ``limit: int = Query(default=20, ge=1, le=100)``
- limit < 1    → 422 (FastAPI Query validation)
- limit 1..100 → 200
- limit > 100  → 422 (FastAPI Query validation)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_paper_record as _make_paper_record

from tests.conftest import FakeRecord

# ---------------------------------------------------------------------------
# Helpers — reuse the same fake-record pattern from test_feed.py
# ---------------------------------------------------------------------------


def _to_record(d: dict) -> FakeRecord:
    return FakeRecord(d)


# ---------------------------------------------------------------------------
# Fixture — matches test_feed.py exactly
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Test client with mocked DB pool and disabled auth."""
    with patch.dict(
        "os.environ",
        {
            "DATABASE_URL": "postgresql://test:test@localhost/test",
            "QDRANT_URL": "http://localhost:6333",
            "DEV_MODE": "true",
        },
    ):
        from fastapi.testclient import TestClient
        from paper_ingestion.deps import get_db_pool
        from paper_ingestion.main import app

        mock_pool = MagicMock()
        app.dependency_overrides = {}
        app.dependency_overrides[get_db_pool] = lambda: mock_pool
        app.state.limiter.enabled = False

        from jarvis_common import get_current_user_id, verify_api_key

        app.dependency_overrides[verify_api_key] = lambda: None
        # CC-03: this fixture resets ``dependency_overrides`` wholesale, which
        # wipes the autouse ``_default_authenticated_user`` override. Re-add it
        # so the converted ``Depends(get_current_user_id)`` routes still default
        # to user 1 (identical to the pre-conversion symbol-stub behaviour).
        app.dependency_overrides[get_current_user_id] = lambda: 1

        yield TestClient(app, raise_server_exceptions=False), mock_pool

        app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Helper — wire a mock pool connection that returns n papers
# ---------------------------------------------------------------------------


def _setup_conn(mock_pool: MagicMock, n: int = 3):
    conn = AsyncMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    conn.fetch.return_value = [_to_record(_make_paper_record(i)) for i in range(1, n + 1)]
    conn.fetchval.return_value = n
    return conn


# ---------------------------------------------------------------------------
# Parametrized status-code regression
# ---------------------------------------------------------------------------

_LIMIT_CASES = [
    # (limit_param,  n_papers_in_db,  expected_status,  test_id)
    (None, 3, 200, "default"),
    (1, 1, 200, "min_allowed"),
    (100, 5, 200, "max_allowed"),
    (50, 3, 200, "mid_range"),
    (101, 0, 422, "over_max"),
    (0, 0, 422, "zero"),
    (-1, 0, 422, "negative"),
]


@pytest.mark.parametrize(
    "limit_param,n_papers,expected_status",
    [(lp, n, s) for lp, n, s, _ in _LIMIT_CASES],
    ids=[tid for *_, tid in _LIMIT_CASES],
)
def test_feed_limit_status_code(client, limit_param, n_papers, expected_status):
    """GET /api/papers/feed returns correct HTTP status for each limit value."""
    test_client, mock_pool = client
    if expected_status == 200:
        _setup_conn(mock_pool, n=n_papers)
    params = {} if limit_param is None else {"limit": limit_param}
    resp = test_client.get("/api/papers/feed", params=params)
    assert resp.status_code == expected_status, (
        f"limit={limit_param!r}: expected {expected_status}, got {resp.status_code}: {resp.text}"
    )
    if expected_status != 200:
        assert resp.status_code != 500


# ---------------------------------------------------------------------------
# Structure test (kept separate — different assertion domain)
# ---------------------------------------------------------------------------


def test_feed_response_structure_with_limit(client):
    """Feed returns well-formed FeedResponse for limit=5."""
    test_client, mock_pool = client
    _setup_conn(mock_pool, n=3)
    resp = test_client.get("/api/papers/feed", params={"limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert "papers" in body
    assert "total" in body
    assert isinstance(body["papers"], list)
    assert isinstance(body["total"], int)
