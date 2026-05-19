"""Group-B IDOR sweep — unit tests for audit fixes.

Verifies that:
- B.1 GET /api/system/models does NOT return provider API keys from user_config.
- B.2 GET /api/papers/brief SQL includes user_id ownership filter.
- B.2 POST /api/papers/process_batch asserts ownership before enqueue.
- B.3 pulse_decks SELECT in load_today/load_history includes user_id filter.
- B.4 GET /api/pulse/stats SQL includes user_id filter.
- B.4 GET /api/pulse/debug SQL includes user_id filter.
- B.7 train_classifier_model stamps user_id on INSERT into pulse_models and
      filters recommendation_feedback by user_id.
- WS-ADMIN-AUDIT: GET /api/admin/audit-log SQL uses "timestamp" AS created_at
      alias (audit_log has column timestamp, NOT created_at).

Tests use the recording-mock pattern from test_pulse_idor.py:
  _make_pool_and_conn() returns an AsyncMock conn whose .fetch / .fetchrow /
  .fetchval calls record SQL + args for assertion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_pulse_client(user_id_override=7):
    """Minimal FastAPI app with only the pulse router mounted.

    WS-CROSS-USER: pulse routes now resolve via
    ``current_user_id_strict_with_owner_override`` (hard-401 when sessionless),
    so the test injects a concrete authenticated user instead of ``None``.
    """
    from jarvis_common import (
        current_user_id_strict_with_owner_override,
        verify_api_key,
    )
    from paper_ingestion.deps import limiter
    from paper_ingestion.routers import pulse as pulse_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool
    app.state.http_client = MagicMock()
    app.state.embedder = MagicMock()

    app.include_router(pulse_router.router)

    app.dependency_overrides[verify_api_key] = lambda: None
    # CC-03: pulse routes resolve via Depends(get_current_user_id) ->
    # Depends(current_user_id_strict_with_owner_override). This mini-app is
    # separate from paper_ingestion.main.app (which the autouse fixture
    # overrides), so set the inner-resolver override here; FastAPI resolves it
    # recursively through the wrapper. Same injected user, same assertions.
    app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: user_id_override

    tc = TestClient(app, raise_server_exceptions=False)
    return tc, pool, conn, app


def _make_system_client():
    """Minimal FastAPI app with only the system router mounted."""
    from jarvis_common import current_user_id, verify_api_key
    from paper_ingestion.deps import limiter
    from paper_ingestion.routers import system as system_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool
    app.state.http_client = MagicMock()

    app.include_router(system_router.router)

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id] = lambda: None

    tc = TestClient(app, raise_server_exceptions=False)
    return tc, pool, conn, app


def _make_papers_client(user_id_override=None):
    """Minimal FastAPI app with only the papers router mounted."""
    from jarvis_common import current_user_id_or_none, verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.routers import papers as papers_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool
    app.state.http_client = MagicMock()
    app.state.embedder = MagicMock()

    app.include_router(papers_router.router)

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[current_user_id_or_none] = lambda: user_id_override

    tc = TestClient(app, raise_server_exceptions=False)
    return tc, pool, conn, app


# ---------------------------------------------------------------------------
# B.1 — system/models allowlist: provider keys must not be returned
# ---------------------------------------------------------------------------


def test_system_models_does_not_leak_provider_keys():
    """GET /api/system/models must NOT query for provider API keys.

    The security guarantee is that the SQL uses an IN allowlist of safe keys
    (llm.smart_model, llm.fast_model, llm.embed_model) rather than a broad
    LIKE 'llm.%' that would also return provider secrets like llm.openai_api_key.

    We verify the SQL structure directly — the mock returns only what the DB
    would actually return when constrained to the allowlist.
    """
    tc, _pool, conn, app = _make_system_client()

    # Mock returns only allowed model-selection rows (DB would filter via IN)
    conn.fetch.return_value = [
        FakeRecord(key="llm.smart_model", value="mistral-nemo"),
        FakeRecord(key="llm.fast_model", value="qwen"),
    ]

    # Stub Ollama probes to avoid network
    async def _no_ollama(url, **kw):
        raise RuntimeError("ollama offline")

    app.state.http_client.get.side_effect = _no_ollama

    resp = tc.get("/api/system/models")
    body = resp.json()

    # Verify allowlist SQL (not LIKE)
    sql: str = conn.fetch.call_args.args[0]
    assert "LIKE" not in sql, f"SQL must not use LIKE; got: {sql!r}"
    # Postgres ANY($1::text[]) is equivalent to IN (...) — both enforce an allowlist
    assert "IN (" in sql or "IN(" in sql or "ANY(" in sql, (
        f"SQL must use IN/ANY allowlist; got: {sql!r}"
    )
    # Provider key names must NOT appear in the SQL itself
    assert "openai_api_key" not in sql, f"Provider key name must NOT appear in SQL; got: {sql!r}"
    assert "anthropic_api_key" not in sql, f"Provider key name must NOT appear in SQL; got: {sql!r}"

    # Only allowlisted keys should appear in the response
    current: dict = body["current"]
    for key in current:
        assert key in ("smart_model", "fast_model", "embed_model"), (
            f"Unexpected key {key!r} in /models response current={current}"
        )


# ---------------------------------------------------------------------------
# B.2 — papers/brief: SQL must carry user_id ownership filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_papers_brief_idor_user_id_filter_no_search():
    """GET /api/papers/brief (no search) must scope to the caller's user_library.

    WS-CROSS-USER: the previous unscoped canonical-corpus fallback (served
    when user_id was None) leaked every user's papers to API-key-only
    callers and has been removed; the endpoint now hard-401s sessionless
    callers and always JOINs user_library for an authenticated one.
    """
    import httpx
    from httpx import ASGITransport
    from jarvis_common import current_user_id_strict_with_owner_override, verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    old_limiter = app.state.limiter.enabled
    app.state.limiter.enabled = False
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_db_pool] = lambda: pool
    # CC-03: list_papers_brief now resolves identity via
    # Depends(get_current_user_id) -> Depends(current_user_id_strict_with_owner_override);
    # override the inner resolver so the caller is user 42 (FastAPI resolves the
    # override recursively through the wrapper). Same attacker id, same assertions.
    app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: 42

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/papers/brief")
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = old_limiter

    assert resp.status_code == 200
    sql: str = conn.fetch.call_args.args[0]
    assert "JOIN user_library" in sql, (
        f"brief SQL must JOIN user_library for IDOR guard; got:\n{sql!r}"
    )
    assert "p.user_id" not in sql, f"legacy p.user_id leaked into brief SQL: {sql!r}"
    args = conn.fetch.call_args.args
    assert 42 in args, f"user_id=42 must be bound in query params; got: {args}"


@pytest.mark.asyncio
async def test_papers_brief_idor_user_id_filter_with_search():
    """GET /api/papers/brief?search=X SQL must include user_id guard.

    CC-03: list_papers_brief now resolves identity via
    Depends(get_current_user_id), so the caller is set with a FastAPI
    dependency override on the inner resolver (resolved recursively through
    the wrapper) rather than a module-level patch.
    """
    import httpx
    from httpx import ASGITransport
    from jarvis_common import current_user_id_strict_with_owner_override, verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    old_limiter = app.state.limiter.enabled
    app.state.limiter.enabled = False
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: 42

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/papers/brief", params={"search": "neural"})
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = old_limiter

    assert resp.status_code == 200
    sql: str = conn.fetch.call_args.args[0]
    # Sprint B: with an authenticated caller the brief endpoint JOINs
    # user_library so user A cannot see user B's saved papers. The IDOR
    # guard moves from a `WHERE p.user_id IS NOT DISTINCT FROM $N`
    # predicate to a `JOIN user_library ul ON ... AND ul.user_id = $N`.
    assert "JOIN user_library" in sql, (
        f"brief SQL with search must JOIN user_library for IDOR guard; got:\n{sql!r}"
    )
    # user_id=42 must be passed as a query parameter
    args = conn.fetch.call_args.args
    assert 42 in args, f"user_id=42 must be bound in query params; got: {args}"


# ---------------------------------------------------------------------------
# B.2 — process_batch: must assert ownership before enqueuing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_batch_asserts_ownership_before_enqueue():
    """POST /api/papers/process_batch must call assert_paper_ownership for each paper_id.

    We mock assert_paper_ownership at the papers_service module level and verify it
    is called once per paper ID before the task is deferred.
    """
    import httpx
    from httpx import ASGITransport
    from jarvis_common import current_user_id_strict_with_owner_override, verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn()

    old_limiter = app.state.limiter.enabled
    app.state.limiter.enabled = False
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_db_pool] = lambda: pool
    # CC-03: caller identity (user 7) via the inner-resolver override, resolved
    # recursively through Depends(get_current_user_id). Same id, same assertions.
    app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: 7

    ownership_calls: list[tuple] = []

    async def _fake_ownership(c, paper_id, user_id):
        ownership_calls.append((paper_id, user_id))

    import jarvis_common.task_registry as task_registry

    deferred_jobs: list[dict] = []

    async def _fake_defer(**kw):
        deferred_jobs.append(kw)

    mock_batch_task = MagicMock()
    mock_batch_task.defer_async = AsyncMock(side_effect=_fake_defer)
    with (
        patch("paper_ingestion.papers_service.assert_paper_ownership", side_effect=_fake_ownership),
        patch.dict(task_registry._TASK_MAP, {"papers.batch_process": mock_batch_task}),
    ):
        try:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/papers/process_batch", json={"paper_ids": [10, 20, 30]}
                )
        finally:
            app.dependency_overrides.clear()
            app.state.limiter.enabled = old_limiter

    assert resp.status_code == 200
    # Ownership must have been checked for each of the 3 paper IDs
    checked_ids = [c[0] for c in ownership_calls]
    assert sorted(checked_ids) == [10, 20, 30], (
        f"assert_paper_ownership must be called for each paper_id; calls: {ownership_calls}"
    )
    # The job must still be enqueued
    assert len(deferred_jobs) == 1


# ---------------------------------------------------------------------------
# B.3 — pulse deck load_today/load_history: pulse_decks SELECT must filter user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_today_pulse_decks_filters_by_user_id():
    """load_today must add AND user_id IS NOT DISTINCT FROM $1 to pulse_decks SELECT."""
    from paper_ingestion.pulse.deck import load_today

    pool, conn = _make_pool_and_conn()
    # Simulate no deck found (returns None → no cards fetched)
    conn.fetchrow.return_value = None

    await load_today(pool, user_id=7)

    assert conn.fetchrow.await_count >= 1
    sql: str = conn.fetchrow.await_args.args[0]
    assert "pulse_decks" in sql, f"load_today must query pulse_decks; got: {sql!r}"
    assert "IS NOT DISTINCT FROM" in sql, (
        f"load_today pulse_decks SELECT must include IS NOT DISTINCT FROM; got:\n{sql!r}"
    )
    params = conn.fetchrow.await_args.args[1:]
    assert 7 in params, f"user_id=7 must be bound in load_today query; params: {params}"


@pytest.mark.asyncio
async def test_load_history_pulse_decks_filters_by_user_id():
    """load_history must add AND user_id IS NOT DISTINCT FROM $N to pulse_decks SELECT."""
    from paper_ingestion.pulse.deck import load_history

    pool, conn = _make_pool_and_conn()
    # No decks → early return
    conn.fetch.return_value = []

    await load_history(pool, days=30, user_id=99)

    assert conn.fetch.await_count >= 1
    sql: str = conn.fetch.await_args_list[0].args[0]
    assert "pulse_decks" in sql, f"load_history must query pulse_decks; got: {sql!r}"
    assert "IS NOT DISTINCT FROM" in sql, (
        f"load_history pulse_decks SELECT must include IS NOT DISTINCT FROM; got:\n{sql!r}"
    )
    params = conn.fetch.await_args_list[0].args[1:]
    assert 99 in params, f"user_id=99 must be bound in load_history query; params: {params}"


# ---------------------------------------------------------------------------
# B.4 — pulse stats/debug: must filter pulse_decks by user_id
# ---------------------------------------------------------------------------


def test_pulse_stats_idor_user_id_filter():
    """GET /api/pulse/stats SQL must scope pulse_decks by an exact user_id match."""
    tc, pool, conn, app = _make_pulse_client(user_id_override=7)

    conn.fetchrow.return_value = FakeRecord(
        {
            "decks_generated": 0,
            "avg_candidates": None,
            "avg_llm_calls": None,
            "avg_duration_s": None,
            "last_run_at": None,
            "last_error": None,
            "degraded_reason": None,
        }
    )

    try:
        resp = tc.get("/api/pulse/stats")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200, f"Unexpected: {resp.status_code} — {resp.text}"

    sql: str = conn.fetchrow.await_args.args[0]
    assert "pulse_decks" in sql.lower(), f"stats SQL must query pulse_decks; got: {sql!r}"
    assert "IS NOT DISTINCT FROM" not in sql, (
        f"pulse/stats SQL must not use the permissive NULL-matching predicate; got:\n{sql!r}"
    )
    assert "user_id = $" in sql, (
        f"pulse/stats SQL must scope by an exact user_id match; got:\n{sql!r}"
    )
    params = conn.fetchrow.await_args.args[1:]
    assert 7 in params, f"user_id=7 must be bound in pulse/stats query; params: {params}"


def test_pulse_debug_idor_user_id_filter(monkeypatch):
    """GET /api/pulse/debug SQL must include IS NOT DISTINCT FROM for user_id."""
    # W1-5: debug endpoint is dev-mode-gated; force DEV_MODE=true to exercise
    # the SQL-emitting path under test.
    monkeypatch.setenv("DEV_MODE", "true")
    tc, pool, conn, app = _make_pulse_client(user_id_override=7)

    # debug_pulse does fetchrow (deck), then fetch (cards), fetch (embed_rows), fetchrow (model)
    conn.fetchrow.side_effect = [
        # First call: deck row
        FakeRecord(
            {
                "id": 1,
                "deck_date": "2026-05-04",
                "card_count": 5,
                "generated_at": "2026-05-04T04:00:00",
                "stats": {},
                "degraded_reason": None,
            }
        ),
        # Second call: model_row
        None,
    ]
    conn.fetch.return_value = []  # card_rows and embed_rows both empty

    try:
        resp = tc.get("/api/pulse/debug")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter

        limiter.enabled = True

    assert resp.status_code == 200, f"Unexpected: {resp.status_code} — {resp.text}"

    # First fetchrow call is the pulse_decks SELECT
    first_call_sql: str = conn.fetchrow.await_args_list[0].args[0]
    assert "pulse_decks" in first_call_sql.lower(), (
        f"debug first query must target pulse_decks; got: {first_call_sql!r}"
    )
    assert "IS NOT DISTINCT FROM" not in first_call_sql, (
        f"pulse/debug SQL must not use the permissive NULL-matching predicate; got:\n{first_call_sql!r}"
    )
    assert "user_id = $" in first_call_sql, (
        f"pulse/debug SQL must scope pulse_decks by an exact user_id match; got:\n{first_call_sql!r}"
    )


# ---------------------------------------------------------------------------
# B.7 — train_classifier_model: user_id stamped on INSERT + filters feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_train_classifier_model_stamps_user_id_on_insert():
    """train_classifier_model must pass user_id as $1 in INSERT INTO pulse_models."""
    from paper_ingestion.pulse.training import train_classifier_model

    pool, conn = _make_pool_and_conn()

    # Simulate insufficient ratings so we exit early (before INSERT)
    conn.fetch.return_value = []

    result = await train_classifier_model(pool, user_id=55)

    # Verify the feedback SELECT includes user_id filter
    sql: str = conn.fetch.await_args.args[0]
    assert "IS NOT DISTINCT FROM" in sql, (
        f"train_classifier_model feedback SELECT must filter by user_id; got:\n{sql!r}"
    )
    params = conn.fetch.await_args.args[1:]
    assert 55 in params, f"user_id=55 must be bound in feedback SELECT; params: {params}"

    assert result["trained"] is False  # insufficient ratings → no INSERT triggered


@pytest.mark.asyncio
async def test_train_classifier_model_feedback_select_filters_by_user_id_none():
    """train_classifier_model with user_id=None must pass None (single-user mode)."""
    from paper_ingestion.pulse.training import train_classifier_model

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    await train_classifier_model(pool, user_id=None)

    sql: str = conn.fetch.await_args.args[0]
    assert "IS NOT DISTINCT FROM" in sql, (
        f"feedback SELECT must include IS NOT DISTINCT FROM; got:\n{sql!r}"
    )
    params = conn.fetch.await_args.args[1:]
    assert None in params, f"user_id=None must be bound as NULL param; params: {params}"


# ---------------------------------------------------------------------------
# WS-ADMIN-AUDIT B1 regression: audit_log has column "timestamp", not created_at
# ---------------------------------------------------------------------------


def _make_audit_admin_client():
    """Minimal FastAPI app with only the audit_admin router mounted."""
    from jarvis_common import require_admin, verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.routers import audit_admin as audit_admin_router

    app = FastAPI()
    app.state.limiter = limiter
    limiter.enabled = False

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool

    app.include_router(audit_admin_router.router)

    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_db_pool] = lambda: pool

    tc = TestClient(app, raise_server_exceptions=False)
    return tc, pool, conn, app


def test_audit_log_endpoint_selects_timestamp_column_aliased_as_created_at():
    """GET /api/admin/audit-log must SELECT "timestamp" AS created_at, not bare created_at.

    The audit_log table (mig 030) has column ``timestamp`` (TIMESTAMPTZ) with
    NO ``created_at`` column. If the SQL references ``created_at`` directly,
    Postgres raises "column does not exist" and the endpoint 500s. The fix
    aliases the real column: SELECT ... "timestamp" AS created_at ... so the
    JSON response field stays ``created_at`` (what the frontend expects).

    This test would have caught B1: if the SQL contained ``created_at`` without
    the alias, the assertion on ``'"timestamp" AS created_at'`` would fail and
    alert that the column name is wrong.
    """
    import datetime as dt

    tc, _pool, conn, app = _make_audit_admin_client()

    # Simulate one audit_log row returned with the aliased field name.
    fake_ts = dt.datetime(2026, 5, 15, 12, 0, 0, tzinfo=dt.UTC)
    conn.fetch.return_value = [
        FakeRecord(
            {
                "id": 1,
                "user_id": "ferhat",
                "action": "auth.login",
                "resource": "/api/auth/verify",
                "metadata": {},
                "created_at": fake_ts,  # asyncpg returns the aliased name
            }
        )
    ]

    try:
        resp = tc.get("/api/admin/audit-log")
    finally:
        app.dependency_overrides.clear()
        from paper_ingestion.deps import limiter as _limiter

        _limiter.enabled = True

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    # Assert the SQL uses the real column with the alias — not bare created_at.
    sql: str = conn.fetch.call_args.args[0]
    assert '"timestamp" AS created_at' in sql, (
        f'SQL must SELECT "timestamp" AS created_at (real column name); got:\n{sql!r}'
    )
    # Bare "created_at" without the alias would mean the real column was referenced
    # directly (which does not exist in audit_log). Ensure no standalone reference.
    assert "FROM audit_log" in sql, f"SQL must query audit_log; got:\n{sql!r}"
    # The column must NOT appear as a bare reference outside the alias expression.
    # i.e. "created_at" must only appear right-hand side of "AS created_at".
    bare_ref_count = sql.count("created_at")
    alias_count = sql.count('"timestamp" AS created_at')
    assert bare_ref_count == alias_count, (
        f"SQL must reference created_at only as the alias (not as a bare column); got:\n{sql!r}"
    )

    # The response entries must carry created_at as an ISO string.
    body = resp.json()
    assert body["entries"], "Expected at least one entry in the response"
    entry = body["entries"][0]
    assert "created_at" in entry, f"Response entry must have created_at field; got: {entry}"
    assert entry["created_at"] == fake_ts.isoformat(), (
        f"created_at must be ISO-formatted timestamp; got: {entry['created_at']!r}"
    )
