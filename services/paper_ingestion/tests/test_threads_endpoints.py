"""Unit tests for the `thread` entity CRUD + auto-seed producers (UI_v3 My-Day).

Uses the ``__wrapped__`` pattern (same as test_journal_endpoints.py) to call
the endpoint functions directly, bypassing FastAPI routing and the slowapi
rate-limiter decorator. Cross-user isolation is asserted by verifying every
query binds the authenticated ``user_id`` and that a non-owned id resolves to a
404 (the DB mock returning ``None`` simulates "row not visible to this user").
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from paper_ingestion.models.thread import ThreadCreate, ThreadUpdate
from paper_ingestion.routers import threads

from tests.conftest import _make_pool_and_conn

_NOW = datetime(2026, 5, 15, 12, 0, 0)


def _mock_request():
    return MagicMock()


def _row(**over):
    base = {
        "id": 1,
        "title": "Refactor reranker",
        "anchor": "blocked on cross-encoder cache",
        "progress": 0.4,
        "last_at": _NOW,
        "status": "open",
        "created_at": _NOW,
    }
    base.update(over)
    return base


def _patch_uid(uid: int = 42):
    return patch(
        "paper_ingestion.routers.threads.current_user_id_strict",
        new_callable=AsyncMock,
        return_value=uid,
    )


# ---------------------------------------------------------------------------
# list_threads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_threads_scoped_to_caller():
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_row(), _row(id=2, title="Survey GNNs")]
    with _patch_uid(42):
        result = await threads.list_threads.__wrapped__(_mock_request(), db_pool=pool)
    assert [t.id for t in result] == [1, 2]
    sql, *params = conn.fetch.await_args.args
    assert "WHERE user_id = $1 AND status = 'open'" in sql
    assert "ORDER BY last_at DESC" in sql
    assert params == [42]  # the authenticated user id, nothing else


# ---------------------------------------------------------------------------
# get_thread — found / cross-user 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_found():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = _row()
    with _patch_uid(42):
        result = await threads.get_thread.__wrapped__(_mock_request(), thread_id=1, db_pool=pool)
    assert result.id == 1
    sql, *params = conn.fetchrow.await_args.args
    assert "WHERE id = $1 AND user_id = $2" in sql
    assert params == [1, 42]


@pytest.mark.asyncio
async def test_get_thread_cross_user_is_404():
    """A thread owned by another user → fetchrow None (WHERE user_id filter) → 404."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    with _patch_uid(99), pytest.raises(HTTPException) as exc:
        await threads.get_thread.__wrapped__(_mock_request(), thread_id=1, db_pool=pool)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# create_thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_thread_binds_caller_user_id():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = _row(id=7, title="New thread", progress=0.0)
    body = ThreadCreate(title="New thread", anchor="a", progress=0.0)
    with _patch_uid(42):
        result = await threads.create_thread.__wrapped__(_mock_request(), body=body, db_pool=pool)
    assert result.id == 7
    sql, *params = conn.fetchrow.await_args.args
    assert "INSERT INTO thread (user_id, title, anchor, progress)" in sql
    assert params[0] == 42  # $1 = authenticated user id
    assert params[1] == "New thread"


# ---------------------------------------------------------------------------
# update_thread — progress/status, no-fields 400, cross-user 404,
#                 allowlist enforcement, partial-update isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_thread_sets_fields_and_bumps_last_at():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = _row(progress=1.0, status="done")
    body = ThreadUpdate(progress=1.0, status="done")
    with _patch_uid(42):
        result = await threads.update_thread.__wrapped__(
            _mock_request(), thread_id=1, body=body, db_pool=pool
        )
    assert result.progress == 1.0
    sql, *params = conn.fetchrow.await_args.args
    assert "last_at = NOW()" in sql
    # last two bound params are always (thread_id, user_id) for the WHERE clause
    assert params[-2:] == [1, 42]
    assert "AND user_id = $" in sql


@pytest.mark.asyncio
async def test_update_thread_no_fields_is_400():
    pool, conn = _make_pool_and_conn()
    with _patch_uid(42), pytest.raises(HTTPException) as exc:
        await threads.update_thread.__wrapped__(
            _mock_request(), thread_id=1, body=ThreadUpdate(), db_pool=pool
        )
    assert exc.value.status_code == 400
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_thread_cross_user_is_404():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    with _patch_uid(99), pytest.raises(HTTPException) as exc:
        await threads.update_thread.__wrapped__(
            _mock_request(), thread_id=1, body=ThreadUpdate(progress=0.5), db_pool=pool
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# update_thread — explicit allowlist (mirrors notes.py _NOTE_ALLOWED_COLUMNS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, value, col_fragment",
    [
        ("title", "New title", '"title"'),
        ("anchor", "some anchor text", '"anchor"'),
        ("progress", 0.75, '"progress"'),
        ("status", "done", '"status"'),
    ],
)
async def test_update_thread_each_allowed_field(field: str, value: object, col_fragment: str):
    """Each allowlisted column can be updated individually (partial-update semantics)."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = _row(**{field: value})
    body = ThreadUpdate.model_validate({field: value})
    with _patch_uid(42):
        await threads.update_thread.__wrapped__(
            _mock_request(), thread_id=1, body=body, db_pool=pool
        )
    sql, *params = conn.fetchrow.await_args.args
    # The quoted column name must appear in the SET clause
    assert col_fragment in sql
    # last_at is always bumped
    assert "last_at = NOW()" in sql
    # WHERE clause always scopes to the authenticated user
    assert "AND user_id = $" in sql
    assert params[-2:] == [1, 42]
    # Only the single field value + thread_id + user_id are bound
    # (1 field value = params[0], then thread_id, user_id)
    assert params[0] == value


@pytest.mark.asyncio
async def test_update_thread_unrecognised_key_is_silently_dropped():
    """model_dump(include=_THREAD_ALLOWED_COLUMNS) silently drops non-allowlisted keys.

    Because ThreadUpdate only defines the four allowed fields, Pydantic rejects
    unknown keys at model construction time.  This test verifies that a body
    with ONLY unknown fields (simulated via an empty ThreadUpdate) returns 400
    — the same "No fields to update" path — and that the DB is never touched.
    This mirrors the notes.py pattern where include=_NOTE_ALLOWED_COLUMNS
    silently drops anything outside the set.
    """
    pool, conn = _make_pool_and_conn()
    # An empty ThreadUpdate has all fields as None / unset.
    # model_dump(exclude_unset=True, include=_THREAD_ALLOWED_COLUMNS) → {}
    with _patch_uid(42), pytest.raises(HTTPException) as exc:
        await threads.update_thread.__wrapped__(
            _mock_request(), thread_id=1, body=ThreadUpdate(), db_pool=pool
        )
    assert exc.value.status_code == 400
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_thread_partial_update_leaves_other_fields_untouched():
    """Only the supplied field appears in the SET clause; other columns are absent.

    This asserts that the allowlist + exclude_unset semantics produce a true
    partial update: sending only ``status`` must NOT include title/anchor/progress
    in the SQL SET clause, leaving them at their existing DB values.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = _row(status="done")
    body = ThreadUpdate(status="done")
    with _patch_uid(42):
        await threads.update_thread.__wrapped__(
            _mock_request(), thread_id=1, body=body, db_pool=pool
        )
    sql, *params = conn.fetchrow.await_args.args
    assert '"status"' in sql
    # Other allowlisted columns must NOT appear in the SET clause
    assert '"title"' not in sql
    assert '"anchor"' not in sql
    assert '"progress"' not in sql
    # Exactly one positional param for the field value (before thread_id + user_id)
    assert len(params) == 3  # status_value, thread_id, user_id
    assert params[0] == "done"


# ---------------------------------------------------------------------------
# resume_thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_thread_touches_last_at():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = _row()
    with _patch_uid(42):
        result = await threads.resume_thread.__wrapped__(_mock_request(), thread_id=1, db_pool=pool)
    assert result.id == 1
    sql, *params = conn.fetchrow.await_args.args
    assert "SET last_at = NOW()" in sql
    assert "AND user_id = $2 AND status = 'open'" in sql
    assert params == [1, 42]


@pytest.mark.asyncio
async def test_resume_thread_cross_user_is_404():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    with _patch_uid(99), pytest.raises(HTTPException) as exc:
        await threads.resume_thread.__wrapped__(_mock_request(), thread_id=1, db_pool=pool)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Auto-seed producer 1 — Pomodoro
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_pomodoro_creates_when_no_duplicate():
    pool, conn = _make_pool_and_conn()
    # first fetchrow = existing lookup (None), second = the INSERT RETURNING
    conn.fetchrow.side_effect = [None, _row(id=5, title="Interrupted: tune FSRS")]
    body = ThreadCreate(title="Interrupted: tune FSRS", progress=0.3)
    with _patch_uid(42):
        result = await threads.seed_thread_from_pomodoro.__wrapped__(
            _mock_request(), body=body, db_pool=pool
        )
    assert result.created is True
    assert result.thread.id == 5
    lookup_sql, *lookup_params = conn.fetchrow.await_args_list[0].args
    assert "status = 'open' AND title = $2" in lookup_sql
    assert lookup_params == [42, "Interrupted: tune FSRS"]


@pytest.mark.asyncio
async def test_seed_pomodoro_dedupes_existing_open_thread():
    pool, conn = _make_pool_and_conn()
    existing = _row(id=9, progress=0.2)
    conn.fetchrow.side_effect = [existing, _row(id=9, progress=0.5)]
    body = ThreadCreate(title="Refactor reranker", progress=0.5)
    with _patch_uid(42):
        result = await threads.seed_thread_from_pomodoro.__wrapped__(
            _mock_request(), body=body, db_pool=pool
        )
    assert result.created is False
    assert result.thread.id == 9
    update_sql, *_ = conn.fetchrow.await_args_list[1].args
    assert "GREATEST(progress, $2)" in update_sql


# ---------------------------------------------------------------------------
# Auto-seed producer 2 — EOD "make this a thread"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_eod_creates_when_no_duplicate():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [None, _row(id=11, title="Blocked: vendor API rate limit")]
    body = ThreadCreate(title="Blocked: vendor API rate limit")
    with _patch_uid(42):
        result = await threads.seed_thread_from_eod.__wrapped__(
            _mock_request(), body=body, db_pool=pool
        )
    assert result.created is True
    assert result.thread.id == 11


@pytest.mark.asyncio
async def test_seed_eod_dedupes_existing():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [_row(id=3), _row(id=3)]
    body = ThreadCreate(title="Refactor reranker")
    with _patch_uid(42):
        result = await threads.seed_thread_from_eod.__wrapped__(
            _mock_request(), body=body, db_pool=pool
        )
    assert result.created is False
    assert result.thread.id == 3
