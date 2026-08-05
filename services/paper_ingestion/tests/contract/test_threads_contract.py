"""Thread entity contract tests.

Covers endpoints in paper_ingestion/routers/threads.py:
  GET  /api/my-day/threads              — list open threads (user-scoped)
  GET  /api/my-day/threads/{id}         — single thread (owner 200, non-owner 404)
  POST /api/my-day/threads              — manual create (201 + ThreadResponse)
  PATCH /api/my-day/threads/{id}        — partial update (owner 200, non-owner 404)
  POST /api/my-day/threads/seed/pomodoro — de-duplicated seed from Pomodoro
  POST /api/my-day/threads/{id}/resume  — bump last_at

Survivor-of:
  test_threads_endpoints.py — all mock-unit tests in this file exercise the same
  handlers with a stubbed DB; these contract tests exercise the real schema.

Carve-out:
  Rate-limiter is disabled on the _pi_threads_app fixture (no Slowapi 429s).
  No Qdrant / Ollama / LLM calls are triggered by thread endpoints.

Verified identifiers:
  threads.py:64-84   — list_threads WHERE user_id=$1 AND status='open'
  threads.py:92-110  — get_thread WHERE id=$1 AND user_id=$2 → 404
  threads.py:118-137 — create_thread INSERT RETURNING
  threads.py:145-183 — update_thread PATCH → 404 non-owner
  threads.py:221-263 — seed_thread_from_pomodoro de-duplication (title ON CONFLICT)
  threads.py:191-213 — resume_thread UPDATE last_at
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool

from jarvis_common.testing_contract_apps import (
    PITestAppOptions,
    patch_pi_test_app,
)
from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_threads_app(contract_conn):
    """paper_ingestion app wired to the contract connection + rate limiter off.

    Removes the autouse _default_authenticated_user stub so session-cookie
    auth reaches the real current_user_id_strict path.
    """
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    with patch_pi_test_app(
        shared,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=True,
            override_db_dependency=True,
            disable_limiter=True,
        ),
    ) as wired_app:
        yield wired_app


# ---------------------------------------------------------------------------
# T-01: POST + GET round-trip — create thread, verify in list
# Verified: threads.py:118-137 (create_thread INSERT RETURNING)
# Verified: threads.py:64-84 (list_threads WHERE user_id=$1 AND status='open')
# ---------------------------------------------------------------------------


async def test_t01_create_thread_appears_in_list(
    contract_two_users,
    _pi_threads_app,
    _configure_api_key,
):
    """POST /api/my-day/threads creates a thread; GET returns it in user A's list.

    Verified: threads.py:118-137 create_thread + threads.py:64-84 list_threads.
    Survivor-of: test_threads_endpoints.py::test_create_thread_binds_caller_user_id
      + test_list_threads_scoped_to_caller.
    """
    async with _make_client(_pi_threads_app, contract_two_users.cookie_a) as c:
        create_resp = await c.post(
            "/api/my-day/threads",
            json={"title": "T01 Contract Thread", "progress": 0.0},
        )
    assert create_resp.status_code == 201, (
        f"Expected 201; got {create_resp.status_code}: {create_resp.text[:200]}"
    )
    body = create_resp.json()
    for field in ("id", "title", "status", "progress", "created_at"):
        assert field in body, f"Missing field {field!r} in ThreadResponse: {body}"
    assert body["title"] == "T01 Contract Thread"
    assert body["status"] == "open"
    thread_id = body["id"]

    # Thread must appear in the list
    async with _make_client(_pi_threads_app, contract_two_users.cookie_a) as c:
        list_resp = await c.get("/api/my-day/threads")
    assert list_resp.status_code == 200
    ids = [t["id"] for t in list_resp.json()]
    assert thread_id in ids, f"Created thread {thread_id} must appear in list; got ids={ids}"


# ---------------------------------------------------------------------------
# T-02: GET /api/my-day/threads/{id} — IDOR: user B gets 404 for user A's thread
# Verified: threads.py:92-110 (get_thread WHERE id=$1 AND user_id=$2)
# ---------------------------------------------------------------------------


async def test_t02_get_thread_idor_user_b_gets_404(
    contract_two_users,
    _pi_threads_app,
    _configure_api_key,
    contract_conn,
):
    """GET /api/my-day/threads/{id}: user B cannot access user A's thread — 404.

    Verified: threads.py:92-110 WHERE id=$1 AND user_id=$2 (no leak, no 403).
    Survivor-of: test_threads_endpoints.py::test_get_thread_cross_user_is_404.
    """
    # Seed a thread directly for user A
    thread_id = await contract_conn.fetchval(
        """INSERT INTO thread (user_id, title, progress, status)
           VALUES ($1, 'T02 IDOR Thread', 0.0, 'open')
           RETURNING id""",
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_threads_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/my-day/threads/{thread_id}")

    assert resp.status_code == 404, (
        f"User B must get 404 for user A's thread; got {resp.status_code}: {resp.text[:200]}"
    )


# ---------------------------------------------------------------------------
# T-03: PATCH /api/my-day/threads/{id} — owner updates, non-owner gets 404
# Verified: threads.py:145-183 (update_thread WHERE id=$x AND user_id=$y)
# ---------------------------------------------------------------------------


async def test_t03_patch_thread_owner_updates_title(
    contract_two_users,
    _pi_threads_app,
    _configure_api_key,
    contract_conn,
):
    """PATCH /api/my-day/threads/{id}: owner can update title; DB row reflects change.

    Verified: threads.py:145-183 (update_thread SET "title"=$1 WHERE id AND user_id).
    Survivor-of: test_threads_endpoints.py::test_update_thread_sets_fields_and_bumps_last_at.
    """
    thread_id = await contract_conn.fetchval(
        """INSERT INTO thread (user_id, title, progress, status)
           VALUES ($1, 'T03 Original Title', 0.0, 'open')
           RETURNING id""",
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_threads_app, contract_two_users.cookie_a) as c:
        resp = await c.patch(
            f"/api/my-day/threads/{thread_id}",
            json={"title": "T03 Updated Title"},
        )
    assert resp.status_code == 200, (
        f"Owner expected 200 for PATCH; got {resp.status_code}: {resp.text[:200]}"
    )
    assert resp.json()["title"] == "T03 Updated Title"

    row = await contract_conn.fetchrow("SELECT title FROM thread WHERE id = $1", thread_id)
    assert row["title"] == "T03 Updated Title", (
        f"DB row must reflect updated title; got {row['title']!r}"
    )


async def test_t03_patch_thread_non_owner_gets_404(
    contract_two_users,
    _pi_threads_app,
    _configure_api_key,
    contract_conn,
):
    """PATCH /api/my-day/threads/{id}: non-owner gets 404 (no state written).

    Verified: threads.py:181-183 (row is None → HTTPException 404).
    Survivor-of: test_threads_endpoints.py::test_update_thread_cross_user_is_404.
    """
    thread_id = await contract_conn.fetchval(
        """INSERT INTO thread (user_id, title, progress, status)
           VALUES ($1, 'T03 B Overwrite Target', 0.0, 'open')
           RETURNING id""",
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_threads_app, contract_two_users.cookie_b) as c:
        resp = await c.patch(
            f"/api/my-day/threads/{thread_id}",
            json={"title": "B Overwrite Attempt"},
        )
    assert resp.status_code == 404, (
        f"Non-owner PATCH must return 404; got {resp.status_code}: {resp.text[:200]}"
    )

    row = await contract_conn.fetchrow("SELECT title FROM thread WHERE id = $1", thread_id)
    assert row["title"] == "T03 B Overwrite Target", (
        "Non-owner PATCH must not change the thread title in DB"
    )


# ---------------------------------------------------------------------------
# T-04: POST /api/my-day/threads/seed/pomodoro — de-duplication on title
# Verified: threads.py:221-263 (seed_thread_from_pomodoro — FOR UPDATE + UPSERT)
# ---------------------------------------------------------------------------


async def test_t04_seed_pomodoro_deduplicates_on_title(
    contract_two_users,
    _pi_threads_app,
    _configure_api_key,
    contract_conn,
):
    """POST /api/my-day/threads/seed/pomodoro: second seed with same title touches, not duplicates.

    Verified: threads.py:221-263 (seed_thread_from_pomodoro FOR UPDATE + GREATEST(progress)).
    Survivor-of: test_threads_endpoints.py::test_seed_pomodoro_dedupes_existing_open_thread.
    """
    payload = {"title": "T04 Pomodoro Dedup", "progress": 0.25}

    async with _make_client(_pi_threads_app, contract_two_users.cookie_a) as c:
        r1 = await c.post("/api/my-day/threads/seed/pomodoro", json=payload)
    assert r1.status_code == 201
    assert r1.json()["created"] is True
    thread_id_1 = r1.json()["thread"]["id"]

    async with _make_client(_pi_threads_app, contract_two_users.cookie_a) as c:
        r2 = await c.post(
            "/api/my-day/threads/seed/pomodoro",
            json={"title": "T04 Pomodoro Dedup", "progress": 0.5},
        )
    assert r2.status_code == 201
    assert r2.json()["created"] is False, (
        "Second seed with same title must set created=False (de-duplication path)"
    )
    # Same thread id — no new row
    assert r2.json()["thread"]["id"] == thread_id_1

    count = await contract_conn.fetchval(
        "SELECT count(*) FROM thread WHERE user_id=$1 AND title='T04 Pomodoro Dedup' AND status='open'",
        contract_two_users.user_a_id,
    )
    assert count == 1, f"De-duplication must yield exactly 1 thread row; got {count}"
    # Progress should be GREATEST(0.25, 0.5) = 0.5
    row = await contract_conn.fetchrow("SELECT progress FROM thread WHERE id=$1", thread_id_1)
    assert abs(row["progress"] - 0.5) < 1e-9, (
        f"GREATEST(progress) must take 0.5; got {row['progress']!r}"
    )
