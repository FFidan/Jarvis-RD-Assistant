"""Integration-style tests for SSE stream endpoint user_id ownership filter.

Verifies that GET /api/jobs/{id}/stream returns 404 when the job's user_id
doesn't match the caller's user_id, and allows access when user_id is NULL
(single-tenant / no-ownership mode).
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stubs for Docker-only dependencies (must precede any app.* import)
# Conftest already stubs most, but we ensure idempotent guards here for
# clarity and standalone-run safety.
# ---------------------------------------------------------------------------

if "qdrant_client" not in sys.modules:
    _fake_qdrant = types.ModuleType("qdrant_client")
    _fake_qdrant.AsyncQdrantClient = MagicMock()
    sys.modules["qdrant_client"] = _fake_qdrant

if "qdrant_client.models" not in sys.modules:
    _fake_qm = types.ModuleType("qdrant_client.models")
    for _attr in ("Distance", "PointIdsList", "PointStruct", "VectorParams"):
        setattr(_fake_qm, _attr, MagicMock())
    sys.modules["qdrant_client.models"] = _fake_qm

if "fitz" not in sys.modules:
    sys.modules["fitz"] = MagicMock()

if "tiktoken" not in sys.modules:
    _fake_tiktoken = types.ModuleType("tiktoken")
    _fake_tiktoken.get_encoding = MagicMock(return_value=MagicMock())
    sys.modules["tiktoken"] = _fake_tiktoken

if "rapidfuzz" not in sys.modules:
    _fake_rapidfuzz = types.ModuleType("rapidfuzz")
    _fake_rapidfuzz.fuzz = MagicMock()
    sys.modules["rapidfuzz"] = _fake_rapidfuzz

import httpx
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JOB_UUID = "00000000-0000-0000-0000-000000000001"


def _make_pool_with_job(user_id: int | None, *, terminal: bool = True) -> MagicMock:
    """Return a mock asyncpg pool whose jobs.get() returns a row with given user_id.

    Parameters
    ----------
    user_id:
        Value to set on the job row's ``user_id`` column.
    terminal:
        When True (default) the row has ``status='succeeded'`` so the SSE
        generator loop exits immediately after the initial ownership check.
    """
    job_row = {
        "id": _JOB_UUID,
        "kind": "test.noop",
        # Use a terminal status so the SSE generator exits without blocking forever.
        "status": "succeeded" if terminal else "queued",
        "progress": 1.0 if terminal else None,
        "progress_message": "done" if terminal else None,
        "result": {"ok": True} if terminal else None,
        "error": None,
        "user_id": user_id,
        "created_at": None,
        "updated_at": None,
        "payload": {},
        "cancel_requested": False,
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=job_row)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


# ---------------------------------------------------------------------------
# Fixture: paper_ingestion app with auth + rate-limiting bypassed
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app_with_pool():
    """Yield a factory that creates a ready-to-test app for a given job user_id."""
    from app.main import app
    from jarvis_common import current_user_id, verify_api_key

    def _make(job_user_id: int | None, caller_user_id: int | None = None):
        pool = _make_pool_with_job(job_user_id)
        app.state.db_pool = pool

        try:
            app.state.limiter.enabled = False
        except AttributeError:
            pass

        # Bypass API key auth
        app.dependency_overrides[verify_api_key] = lambda: None
        # Stub current_user_id to return the specified caller identity
        app.dependency_overrides[current_user_id] = lambda: caller_user_id

        return app, pool

    yield _make
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_job_with_user_id_and_no_caller_returns_404(_app_with_pool) -> None:
    """Job seeded with user_id=42, caller is None (single-tenant) → 404."""
    app, _pool = _app_with_pool(job_user_id=42, caller_user_id=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/jobs/{_JOB_UUID}/stream")

    assert resp.status_code == 404, (
        f"Expected 404 for job with user_id=42 accessed by caller None, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_stream_job_with_null_user_id_and_no_caller_is_allowed(_app_with_pool) -> None:
    """Job seeded with user_id=NULL, caller is None → SSE stream starts (200)."""
    app, _pool = _app_with_pool(job_user_id=None, caller_user_id=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Use stream=True to avoid waiting for the generator to close
        async with client.stream("GET", f"/api/jobs/{_JOB_UUID}/stream") as resp:
            assert resp.status_code == 200, (
                f"Expected 200 for job with user_id=NULL, got {resp.status_code}"
            )


@pytest.mark.asyncio
async def test_stream_job_with_matching_user_id_is_allowed(_app_with_pool) -> None:
    """Job seeded with user_id=42, caller is 42 → SSE stream starts (200)."""
    app, _pool = _app_with_pool(job_user_id=42, caller_user_id=42)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with client.stream("GET", f"/api/jobs/{_JOB_UUID}/stream") as resp:
            assert resp.status_code == 200, (
                f"Expected 200 for matching user_id=42, got {resp.status_code}"
            )


@pytest.mark.asyncio
async def test_stream_job_with_mismatched_user_id_returns_404(_app_with_pool) -> None:
    """Job seeded with user_id=42, caller is 99 → 404 (no existence leak)."""
    app, _pool = _app_with_pool(job_user_id=42, caller_user_id=99)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/jobs/{_JOB_UUID}/stream")

    assert resp.status_code == 404, (
        f"Expected 404 for user_id mismatch (42 vs 99), got {resp.status_code}"
    )
