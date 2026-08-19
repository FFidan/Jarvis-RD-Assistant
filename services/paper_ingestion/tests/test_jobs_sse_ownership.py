"""Integration-style tests for Platform SSE ownership filtering.

Verifies that GET /api/jobs/{id}/stream returns 404 when the job's user_id
doesn't match the caller's user_id, and allows access when user_id is NULL
(single-tenant / no-ownership mode).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

# conftest.py has already installed qdrant_client / qdrant_client.models / tiktoken /
# rapidfuzz stubs.
import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JOB_UUID = "00000000-0000-0000-0000-000000000001"


# Keep local: wires fetchrow as an async function with custom side_effect — not covered by canonical make_pool_and_conn.
def _make_pool_with_job(user_id: int | None, *, terminal: bool = True) -> MagicMock:
    """Return a mock asyncpg pool whose get_procrastinate_job_for_jarvis_id() returns a row.

    Parameters
    ----------
    user_id:
        Value to set on the job row's ``user_id`` key inside ``args``.
    terminal:
        When True (default) the row has ``status='succeeded'`` so the SSE
        generator loop exits immediately after the initial ownership check.

    Implementation note
    -------------------
    ``get_unified`` now calls only ``get_procrastinate_job_for_jarvis_id``,
    which issues a ``fetchrow``.  ``stream_job_events`` also calls
    ``get_procrastinate_job_for_jarvis_id`` on each poll cycle.  We return
    the procrastinate-shaped row on every ``fetchrow`` call so both the
    ownership check and the SSE loop work correctly (the terminal status
    causes the SSE loop to exit after one cycle).
    """
    prow = {
        "id": 1,
        "queue_name": "paper_ingestion",
        "task_name": "test.noop",
        # Use a terminal status so the SSE generator exits without blocking forever.
        "status": "succeeded" if terminal else "todo",
        "args": {
            "job_id": _JOB_UUID,
            **({"user_id": str(user_id)} if user_id is not None else {}),
        },
        "attempts": 1,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }

    async def _fetchrow_side_effect(*_args, **_kwargs):
        return prow

    conn = AsyncMock()
    conn.fetchrow = _fetchrow_side_effect
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


# ---------------------------------------------------------------------------
# Fixture: Platform facade with auth + rate-limiting bypassed
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app_with_pool():
    """Yield a factory that creates a ready-to-test app for a given job user_id."""
    from contextlib import ExitStack

    from fastapi import HTTPException

    from jarvis_common import current_user_id_strict, verify_api_key
    from platform_api.deps import get_db_pool, limiter, verify_platform_request
    from platform_api.main import app

    with ExitStack() as stack:

        def _make(job_user_id: int | None, caller_user_id: int | None = None):
            pool = _make_pool_with_job(job_user_id)

            # Stub current_user_id_strict: return caller id when set, raise 401
            # when None. stream_job uses Depends(current_user_id_strict) which
            # raises 401 on None.
            if caller_user_id is not None:
                caller = caller_user_id

                def _caller_override(caller=caller):
                    return caller
            else:

                async def _caller_override():
                    raise HTTPException(status_code=401, detail="Not authenticated")

            stack.enter_context(
                patch_pi_test_app(
                    pool,
                    app=app,
                    get_db_pool=get_db_pool,
                    limiter=limiter,
                    options=PITestAppOptions(
                        remove_identity_overrides=False,
                        disable_limiter=True,
                        dependency_overrides={
                            verify_api_key: lambda: None,
                            verify_platform_request: lambda: None,
                            current_user_id_strict: _caller_override,
                        },
                    ),
                )
            )
            return app, pool

        yield _make


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_job_with_user_id_and_no_caller_returns_401(_app_with_pool) -> None:
    """Job seeded with user_id=42, caller is unauthenticated → 401.

    stream_job uses current_user_id_strict which raises 401 before the
    ownership check runs, so an anonymous caller is rejected at the auth
    layer regardless of the job's user_id.
    """
    app, _pool = _app_with_pool(job_user_id=42, caller_user_id=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/jobs/{_JOB_UUID}/stream")

    assert resp.status_code == 401, (
        f"Expected 401 for unauthenticated caller on job with user_id=42, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_stream_job_with_null_user_id_and_no_caller_returns_401(_app_with_pool) -> None:
    """Job seeded with user_id=NULL, caller is unauthenticated → 401.

    current_user_id_strict raises 401 before ownership is checked.
    System-only (NULL-row) jobs require an authenticated caller; even then
    _owner_matches returns False for NULL row_user_id.
    """
    app, _pool = _app_with_pool(job_user_id=None, caller_user_id=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/jobs/{_JOB_UUID}/stream")

    assert resp.status_code == 401, (
        f"Expected 401 for unauthenticated caller on NULL-row job, got {resp.status_code}"
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
