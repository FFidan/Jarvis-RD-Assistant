"""Contradictions router: auth, cross-tenant scoping, and scan preflight.

(a) No session → 401.
(b) User B does not receive user A's contradictions.
(c) POST /contradictions/scan skips (does not enqueue) when the caller has
    no scannable findings.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

# D3-08: use canonical pool builder instead of duplicated local _mock_pool.
from tests.conftest import FakeRecord, _make_pool_and_conn


@contextmanager
def _wired_app(pool, *, user_id=None):
    """Wire the app; when *user_id* is given, current_user_id_strict resolves to it."""
    from jarvis_common.auth import current_user_id_strict, verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    overrides = {verify_api_key: lambda: None}
    if user_id is not None:
        overrides[current_user_id_strict] = lambda: user_id

    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides=overrides,
        ),
    ):
        yield app


def _contradiction_row(*, paper_a_id: int = 10, paper_b_id: int = 11) -> FakeRecord:
    return FakeRecord(
        {
            "id": 1,
            "paper_a_id": paper_a_id,
            "paper_b_id": paper_b_id,
            "paper_a_title": "Paper A",
            "paper_b_title": "Paper B",
            "finding_a": "A",
            "finding_b": "B",
            "quote_a": "quote A",
            "quote_b": "quote B",
            "page_a": 1,
            "page_b": 2,
            "contradiction_type": "result",
            "explanation": "Conflict",
            "confidence": 0.85,
            "status": "verified",
            "created_at": datetime.now(UTC),
            "total_count": 1,
        }
    )


# ---------------------------------------------------------------------------
# (a) No session → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.real_auth
async def test_get_contradictions_no_session_returns_401() -> None:
    """GET /api/contradictions without a session must return 401."""
    pool, _conn = _make_pool_and_conn()
    with _wired_app(pool) as app:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/contradictions")
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# (b) Cross-tenant isolation: user B does not see user A's rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_contradictions_cross_tenant_isolation() -> None:
    """User B must not receive contradictions that belong only to user A.

    The SQL predicate scopes via user_library: only rows where at least one
    paper side is in the caller's library are returned. This test verifies
    that user_id=2 (user B) gets an empty result when the DB returns nothing
    (simulating that neither paper_a nor paper_b is in user B's library).
    """
    pool, conn = _make_pool_and_conn()
    # DB returns empty — user B has no contradictions in their library.
    conn.fetch.return_value = []

    # current_user_id_strict (Depends-wired) resolves to user_id=2.
    with _wired_app(pool, user_id=2) as app:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/contradictions")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 0
    assert body["contradictions"] == []

    # Confirm user_id=2 was forwarded to the DB query (first positional param).
    call_args = conn.fetch.await_args
    assert call_args is not None
    query_params = call_args.args[1:]  # first arg is SQL string
    assert 2 in query_params, f"user_id=2 not found in DB params: {query_params}"


@pytest.mark.asyncio
async def test_get_contradictions_user_id_threaded_to_sql() -> None:
    """Verify the user_id is included in the SQL query params."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_contradiction_row()]

    # current_user_id_strict (Depends-wired) resolves to 1 (user A).
    with _wired_app(pool, user_id=1) as app:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/contradictions")

    assert resp.status_code == 200, resp.text
    # user_id=1 must appear in the SQL params forwarded by list_contradictions.
    call_args = conn.fetch.await_args
    assert call_args is not None
    query_params = call_args.args[1:]
    assert 1 in query_params, f"user_id=1 not found in DB params: {query_params}"
    # Also confirm user_library scoping clause is present in the SQL.
    sql = call_args.args[0]
    assert "user_library" in sql, "user_library JOIN/subquery missing from generated SQL"


# ---------------------------------------------------------------------------
# (c) POST /contradictions/scan preflight: skip when there is nothing to scan
# ---------------------------------------------------------------------------


async def _post_library_scan(scannable_count: int, monkeypatch) -> tuple[Any, Any, Any]:
    """POST /api/contradictions/scan with the preflight COUNT returning *scannable_count*.

    Returns ``(response, mock_task, conn)``.
    """
    import jarvis_common.task_registry as task_registry

    pool, conn = _make_pool_and_conn(fetchval_return=scannable_count)

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()
    monkeypatch.setitem(task_registry._TASK_MAP, "contradictions.scan", mock_task)

    with _wired_app(pool, user_id=7) as app:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/contradictions/scan", json={})

    return resp, mock_task, conn


@pytest.mark.asyncio
async def test_scan_preflight_skips_without_enqueuing_when_no_findings(monkeypatch) -> None:
    """Zero scannable findings → 202 skipped with a reason and NO deferred job."""
    resp, mock_task, conn = await _post_library_scan(0, monkeypatch)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "skipped"
    assert body["job_id"] is None
    assert body["reason"] == "no_findings"
    mock_task.defer_async.assert_not_awaited()
    # The preflight count must be scoped to the caller.
    count_params = conn.fetchval.await_args.args[1:]
    assert 7 in count_params, f"user_id=7 not bound in preflight COUNT params: {count_params}"


@pytest.mark.asyncio
async def test_scan_preflight_enqueues_when_findings_exist(monkeypatch) -> None:
    """A caller with scannable findings keeps the 202 + queued job contract."""
    resp, mock_task, _conn = await _post_library_scan(3, monkeypatch)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"], f"expected a job_id for the queued scan, got: {body}"
    mock_task.defer_async.assert_awaited_once()
    assert mock_task.defer_async.await_args.kwargs["user_id"] == 7
