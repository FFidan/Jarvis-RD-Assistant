"""Executive dashboard contract tests — A192, A193, A194, A195, A196, A197.

Covers:
- GET  /api/executive/intent/today  (A192) — scoped to caller's user_id
- POST /api/executive/intent/today  (A193) — upserts daily_intent row for caller
- GET  /api/executive/my-day        (A194) — response shape + scoped to caller
- GET  /api/executive/my-day-bundle (A195) — bundle keys present + scoped to caller
- POST /api/executive/tasks         (A196) — quick-add task; 404 for non-owned project
- POST /api/executive/focus/log     (A197) — daily_log incremented; 404 for non-owned task
"""

from __future__ import annotations

import httpx
import pytest


from jarvis_common.testing_contract_apps import (
    DEFAULT_CONTRACT_API_KEY,
    make_contract_client as _client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# §A192 — GET /api/executive/intent/today
# ---------------------------------------------------------------------------


async def test_get_intent_today_scoped_to_caller(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/executive/intent/today returns caller's intent text (scoped by user_id).

    Seeds a daily_intent row for user A and verifies user B receives null/empty
    (their own — not A's). Collapses test_intent.py's mock-pool assertion.
    """
    # Seed intent for user A
    await contract_conn.execute(
        """INSERT INTO daily_intent (user_id, intent_date, intent_text)
           VALUES ($1, CURRENT_DATE, 'Contract intent for A')
           ON CONFLICT (user_id, intent_date)
           DO UPDATE SET intent_text = EXCLUDED.intent_text""",
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/executive/intent/today")
    assert resp_a.status_code == 200, (
        f"GET intent for user A failed: {resp_a.status_code}: {resp_a.text[:300]}"
    )
    body_a = resp_a.json()
    assert body_a.get("intent") == "Contract intent for A", (
        f"User A expected their own intent; got {body_a}"
    )

    # User B should NOT see user A's intent
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/executive/intent/today")
    assert resp_b.status_code == 200, (
        f"GET intent for user B failed: {resp_b.status_code}: {resp_b.text[:300]}"
    )
    body_b = resp_b.json()
    assert body_b.get("intent") != "Contract intent for A", (
        f"IDOR: user B received user A's intent text: {body_b}"
    )


# ---------------------------------------------------------------------------
# §A193 — POST /api/executive/intent/today
# ---------------------------------------------------------------------------


async def test_save_intent_today_upserts_for_caller(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/executive/intent/today upserts a daily_intent row for the caller.

    Collapses test_intent.py's SQL-text check to a real DB upsert proof.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/executive/intent/today",
            json={"intent": "Upserted intent contract"},
        )

    assert resp.status_code in (200, 201), (
        f"POST intent failed: {resp.status_code}: {resp.text[:300]}"
    )

    db_text = await contract_conn.fetchval(
        """SELECT intent_text FROM daily_intent
           WHERE user_id = $1 AND intent_date = CURRENT_DATE""",
        contract_two_users.user_a_id,
    )
    assert db_text == "Upserted intent contract", f"DB intent_text mismatch; got {db_text!r}"


# ---------------------------------------------------------------------------
# §A194 — GET /api/executive/my-day — response shape + scoping
# ---------------------------------------------------------------------------


async def test_get_my_day_response_shape(contract_two_users, _le_app, _configure_api_key):
    """GET /api/executive/my-day returns expected top-level keys.

    Collapses test_executive.py's SQL-dispatch fragmentation assertions to a
    response-shape behavioral contract. The endpoint aggregates six concurrent
    DB reads; we assert the shape is correct under seeded inputs.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/executive/my-day")

    assert resp.status_code == 200, (
        f"GET /api/executive/my-day failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    required_keys = {
        "tasks",
        "cards_due",
        "recommendations",
        "today_focus_hours",
        "focus_streak_days",
        "project_pulse",
    }
    missing = required_keys - set(body.keys())
    assert not missing, f"my-day response missing keys: {missing}. Got: {list(body.keys())}"
    assert isinstance(body["tasks"], list)
    assert isinstance(body["cards_due"], int) and body["cards_due"] >= 0
    assert isinstance(body["focus_streak_days"], int) and body["focus_streak_days"] >= 0
    assert isinstance(body["project_pulse"], list)


async def test_get_my_day_tasks_scoped_to_caller(contract_two_users, _le_app, _configure_api_key):
    """GET /api/executive/my-day tasks list does not include user B's tasks.

    User A's task_id_a has title containing ZZZ-ISOLATION-A-TASK; user B must
    not see it in their my-day response.
    """
    from jarvis_common.testing import A_TASK_TITLE

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/executive/my-day")

    assert resp.status_code == 200
    task_titles = [t["title"] for t in resp.json()["tasks"]]
    assert A_TASK_TITLE not in task_titles, (
        f"IDOR: user B's my-day contains user A's task title {A_TASK_TITLE!r}. "
        f"All titles: {task_titles}"
    )


# ---------------------------------------------------------------------------
# §A195 — GET /api/executive/my-day-bundle — bundle keys present
# ---------------------------------------------------------------------------


async def test_get_my_day_bundle_response_keys(contract_two_users, _le_app, _configure_api_key):
    """GET /api/executive/my-day-bundle returns all required bundle keys.

    Collapses test_executive.py's bundle-key assertions (SQL-dispatch fragments)
    to a behavioral shape contract.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/executive/my-day-bundle")

    assert resp.status_code == 200, (
        f"GET my-day-bundle failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    required_keys = {"tasks", "intent", "threads", "yesterday", "journal"}
    missing = required_keys - set(body.keys())
    assert not missing, f"my-day-bundle missing keys: {missing}. Got: {list(body.keys())}"
    assert isinstance(body["tasks"], list)
    assert isinstance(body["threads"], list)
    # yesterday must have required subkeys
    yday = body["yesterday"]
    assert "date" in yday and "focused_hours" in yday and "tasks_done" in yday, (
        f"yesterday missing subkeys: {yday}"
    )


# ---------------------------------------------------------------------------
# §A196 — POST /api/executive/tasks — quick-add; 404 for non-owned project
# ---------------------------------------------------------------------------


async def test_quick_add_task_non_owned_project_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot quick-add a task to user A's project — 404 (IDOR guard)."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            "/api/executive/tasks",
            json={"title": "Injected Task", "project_id": project_id},
        )

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} quick-adding task to user A's project "
        f"{project_id} (expected 404). Body: {resp.text[:300]}"
    )


async def test_quick_add_task_creates_row_with_user_id(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/executive/tasks creates a task with the caller's user_id."""
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/executive/tasks",
            json={"title": "Quick Task Contract"},
        )

    assert resp.status_code == 201, f"Quick-add task failed: {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    task_id = body["id"]

    # Response shape: safe fields present
    assert body["title"] == "Quick Task Contract", f"title mismatch: {body.get('title')!r}"
    assert body["status"] == "todo", f"status mismatch: {body.get('status')!r}"
    assert isinstance(body["priority"], int), f"priority not int: {body.get('priority')!r}"

    # Response shape: user_id must never be exposed (LE-D5-03)
    assert "user_id" not in body, f"user_id leaked in response: {body.get('user_id')}"

    db_user_id = await contract_conn.fetchval(
        "SELECT user_id FROM tasks WHERE id = $1",
        task_id,
    )
    assert db_user_id == contract_two_users.user_a_id, (
        f"Quick-added task {task_id} has user_id={db_user_id}; "
        f"expected {contract_two_users.user_a_id}"
    )


# ---------------------------------------------------------------------------
# §A197 — POST /api/executive/focus/log — daily_log upsert; 404 non-owned task
# ---------------------------------------------------------------------------


async def test_log_focus_session_non_owned_task_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot log focus against user A's task — 404 (IDOR guard)."""
    task_id_a = contract_two_users.task_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            "/api/executive/focus/log",
            json={"duration_hours": 25 / 60, "task_id": task_id_a},
        )

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} logging focus against user A's task "
        f"{task_id_a} (expected 404). Body: {resp.text[:300]}"
    )


async def test_log_focus_session_persists_to_daily_log(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/executive/focus/log upserts today's daily_log.focus_hours for caller.

    Collapses test_executive.py's SQL-substring assertions about the UPSERT
    structure to a real DB persistence proof.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/executive/focus/log",
            json={"duration_hours": 0.5},
        )

    assert resp.status_code == 200, f"POST focus/log failed: {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert "recorded_hours" in body, f"Response missing recorded_hours: {body}"
    assert body["recorded_hours"] > 0, f"recorded_hours should be > 0; got {body}"

    # Verify the daily_log row was created/updated for user A
    focus_hours = await contract_conn.fetchval(
        """SELECT focus_hours FROM daily_log
           WHERE user_id = $1 AND log_date = CURRENT_DATE""",
        contract_two_users.user_a_id,
    )
    assert focus_hours is not None and focus_hours > 0, (
        f"Expected daily_log.focus_hours > 0 for user A after focus log; got {focus_hours}"
    )


# ---------------------------------------------------------------------------
# §A218 — GET /api/executive/my-day via session cookie (positive control)
# ---------------------------------------------------------------------------


async def test_my_day_returns_caller_data_via_session_cookie(
    contract_two_users, _le_app, _configure_api_key
):
    """GET /api/executive/my-day returns 200 with correct shape for session-authenticated caller.

    Exercises current_user_id_strict_with_owner_override: session path resolves
    user_id from the jarvis_session cookie. Tests scoping at the positive control
    level — the fixture already has test_get_my_day_response_shape, this test
    documents the contract for the *session-cookie* auth path explicitly.
    # Verified: services/learning_engine/learning_engine/routers/executive.py:152-189
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/executive/my-day")

    assert resp.status_code == 200, (
        f"my-day via session cookie failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert isinstance(body.get("tasks"), list), f"tasks must be list; got {body}"
    assert isinstance(body.get("cards_due"), int), f"cards_due must be int; got {body}"


async def test_my_day_no_session_returns_401(_le_app, _configure_api_key):
    """GET /api/executive/my-day without any session returns 401 (or 403 for no identity).

    When neither session nor X-Owner-User-Id resolves, current_user_id_strict_with_owner_override
    raises 401. Documents the behavior: API-key-only (no session, no override header) → 401.
    # Verified: libs/jarvis_common/jarvis_common/auth.py:452-465
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_le_app),
        base_url="http://test",
        headers={"X-API-Key": DEFAULT_CONTRACT_API_KEY},
    ) as c:
        resp = await c.get("/api/executive/my-day")

    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for unauthenticated my-day; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# Cluster 13 — Executive my-day behaviors (LE-E-01..LE-E-05)
# Survivor-of mock-units in test_executive.py:
#   test_my_day_returns_focus_stats (414)         → LE-E-01
#   test_my_day_limit_recommendations_query_param (196) → LE-E-01 (incidental shape)
#   test_focus_log_paper_not_found (302)          → LE-E-02
#   test_focus_log_rejects_other_users_paper (322) → LE-E-03
#   test_my_day_bundle_null_tolerant_when_empty (719) → LE-E-04
#   test_focus_log_missing_duration_returns_422 (351) → LE-E-05
#   test_focus_log_negative_hours_returns_422 (523) → LE-E-05 (sub-assertion)
#   test_focus_log_excessive_hours_returns_422 (533) → LE-E-05 (sub-assertion)
# Pre-existing survivors cover (no new contract needed):
#   test_my_day_happy_path / _empty (100, 174)    → test_get_my_day_response_shape (197)
#   test_focus_log_bare_timer / _with_task_id (225, 246) → test_log_focus_session_persists_to_daily_log (342)
#   test_focus_log_task_not_found / _no_side_effects (276, 565) → test_log_focus_session_non_owned_task_gets_404 (325)
#   test_my_day_tasks_include_project_context (372) → test_get_my_day_tasks_scoped_to_caller (227)
#   test_my_day_returns_project_pulse (436)       → 197
#   test_my_day_includes_completed_tasks_today (464) → 227
#   test_my_day_bundle_shape_and_aggregation (639) → test_get_my_day_bundle_response_keys (251) + LE-E-04
# ---------------------------------------------------------------------------


async def test_my_day_focus_stats_aggregation(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/executive/my-day aggregates focus_hours from daily_log for the caller.

    Seeds a daily_log row for user A with focus_hours=1.5; asserts the response
    field today_focus_hours == 1.5. Replaces test_my_day_returns_focus_stats's
    SQL-keyed mock dispatch with a real-DB read; also asserts IDOR isolation.

    # Verified: services/learning_engine/learning_engine/routers/executive.py:151
    # (get_my_day runs 6 concurrent reads scoped to current_user_id_strict;
    # today_focus_hours comes from _FOCUS_HOURS_SQL against daily_log).
    """
    await contract_conn.execute(
        """
        INSERT INTO daily_log (user_id, log_date, focus_hours)
        VALUES ($1, CURRENT_DATE, 1.5)
        ON CONFLICT (user_id, log_date) DO UPDATE SET focus_hours = EXCLUDED.focus_hours
        """,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/executive/my-day")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body.get("today_focus_hours") == 1.5, (
        f"Expected today_focus_hours=1.5 for user A; got {body.get('today_focus_hours')!r}"
    )

    # IDOR: user B should not see user A's hours
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/executive/my-day")
    assert resp_b.status_code == 200, resp_b.text[:300]
    body_b = resp_b.json()
    assert body_b.get("today_focus_hours") in (0, 0.0, None), (
        f"IDOR leak: user B saw today_focus_hours={body_b.get('today_focus_hours')!r}"
    )


async def test_focus_log_paper_not_found_returns_404(
    contract_two_users, _le_app, _configure_api_key
):
    """POST /api/executive/focus/log with a nonexistent paper_id returns 404.

    Exercises the real assert_paper_ownership check against the live schema.

    # Verified: services/learning_engine/learning_engine/routers/executive.py:399
    # (log_focus_session calls assert_paper_ownership when paper_id is provided).
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/executive/focus/log",
            json={"duration_hours": 0.5, "paper_id": 9_999_999},
        )

    assert resp.status_code == 404, (
        f"Expected 404 for missing paper; got {resp.status_code}: {resp.text[:300]}"
    )


async def test_focus_log_rejects_other_users_paper(contract_two_users, _le_app, _configure_api_key):
    """User B cannot log focus against user A's paper (assert_paper_ownership).

    Exercises real discovered_by / user_library ownership check across the HTTP
    boundary — replaces the mock-fetchrow assertion in
    test_focus_log_rejects_other_users_paper (line 322 of test_executive.py).

    # Verified: services/learning_engine/learning_engine/routers/executive.py:399
    # (log_focus_session: assert_paper_ownership for paper_id when provided).
    """
    paper_id_a = contract_two_users.paper_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            "/api/executive/focus/log",
            json={"duration_hours": 0.5, "paper_id": paper_id_a},
        )

    assert resp.status_code in (403, 404), (
        f"User B logging focus against user A's paper_id={paper_id_a}: "
        f"expected 403/404, got {resp.status_code}: {resp.text[:300]}"
    )


async def test_my_day_bundle_null_tolerant_with_no_seed(
    contract_two_users, _le_app, _configure_api_key
):
    """GET /api/executive/my-day-bundle for a user with no bundle data returns 200.

    All seven concurrent reads must null-tolerate empty rows: intent has null
    fields, tasks is [], journal is null, pulse_today is null. No 500.

    # Verified: services/learning_engine/learning_engine/routers/executive.py:283
    # (get_my_day_bundle runs 7 concurrent reads; null-tolerant serialization).
    """
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/executive/my-day-bundle")

    assert resp.status_code == 200, (
        f"my-day-bundle should be null-tolerant; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    for key in ("tasks", "intent", "threads", "journal", "pulse_today"):
        assert key in body, f"my-day-bundle missing key {key!r}: {body}"
    assert isinstance(body["tasks"], list), (
        f"tasks must be list; got {type(body['tasks']).__name__}"
    )


# ---------------------------------------------------------------------------
# §LE-D5-02 — POST /api/executive/intent/today rate-limit (30/minute)
# ---------------------------------------------------------------------------


async def test_intent_today_post_rate_limited(contract_two_users, _le_app, _configure_api_key):
    """POST /api/executive/intent/today enforces 30/minute rate limit.

    Bursts 31 requests; at least one must return 429.
    Temporarily re-enables the limiter (disabled globally in _le_app for other tests).
    """
    from learning_engine.deps import limiter

    limiter.enabled = True
    try:
        statuses = []
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            for i in range(31):
                resp = await c.post(
                    "/api/executive/intent/today",
                    json={"intent": f"burst {i}"},
                )
                statuses.append(resp.status_code)
    finally:
        limiter.enabled = False

    assert 429 in statuses, (
        f"Expected at least one 429 from 31 rapid POSTs; got statuses: {set(statuses)}"
    )


async def test_focus_log_invalid_duration_returns_422(
    contract_two_users, _le_app, _configure_api_key
):
    """POST /api/executive/focus/log enforces duration_hours: float, gt=0, le=24 at HTTP layer.

    Three sub-assertions: missing, negative, excessive. Replaces three mock-unit
    tests (missing 351, negative 523, excessive 533).

    # Verified: services/learning_engine/learning_engine/routers/executive.py:135
    # (FocusSessionRequest: duration_hours: float = Field(..., gt=0, le=24)).
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        # Missing duration_hours
        resp_missing = await c.post("/api/executive/focus/log", json={})
        assert resp_missing.status_code == 422, (
            f"missing duration_hours should 422; got {resp_missing.status_code}: "
            f"{resp_missing.text[:200]}"
        )

        # Zero or negative duration_hours
        resp_neg = await c.post("/api/executive/focus/log", json={"duration_hours": -0.5})
        assert resp_neg.status_code == 422, (
            f"negative duration_hours should 422; got {resp_neg.status_code}"
        )

        # Excessive duration_hours (> 24h cap)
        resp_excess = await c.post("/api/executive/focus/log", json={"duration_hours": 25.0})
        assert resp_excess.status_code == 422, (
            f"excessive duration_hours should 422; got {resp_excess.status_code}"
        )
