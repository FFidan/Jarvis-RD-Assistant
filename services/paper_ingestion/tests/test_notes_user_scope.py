"""Unit tests: note mutations are scoped by user_id at the database layer.

Multi-tenant mode: the UPDATE and DELETE statements carry AND user_id = $N so that
a race between the ownership SELECT and the mutation cannot target another user's row.
Single-tenant mode (user_id=None): original single-predicate SQL is preserved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn


def _note_row() -> FakeRecord:
    return FakeRecord({"paper_id": 1, "source": "user"})


def _paper_row() -> FakeRecord:
    """Represent a paper accepted by the central visibility predicate."""
    return FakeRecord({"id": 1, "is_visible": True})


def _full_note_row() -> FakeRecord:
    return FakeRecord(
        {
            "id": 5,
            "paper_id": 1,
            "user_note": "updated text",
            "highlight_text": None,
            "page_number": None,
            "source": "user",
            "zotero_annotation_key": None,
            "verification_status": "unverified",
            "verified_quote": None,
            "verified_page_number": None,
            "promoted_at": None,
            "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        }
    )


def _setup_app(user_id: int | None, fetchrow_side_effects: list) -> tuple:
    from jarvis_common import verify_api_key
    from jarvis_common.auth import get_current_user_id
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn(fetchrow_side_effects=fetchrow_side_effects)
    conn.execute = AsyncMock(return_value="DELETE 1")

    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_current_user_id] = lambda: user_id

    return app, conn


def _teardown_app(app) -> None:
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_update_note_multi_tenant_includes_user_id_in_update():
    """PUT /api/notes/{id}: multi-tenant UPDATE WHERE clause must contain AND user_id.

    The dynamic UPDATE must remain owner-scoped even though the response is
    reselected with its generation-derived stale flag.
    """
    app, conn = _setup_app(
        user_id=42,
        fetchrow_side_effects=[
            _note_row(),
            _paper_row(),
            _full_note_row(),
            _full_note_row(),
        ],
    )
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put("/api/notes/5", json={"user_note": "updated text"})
    finally:
        _teardown_app(app)

    assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text[:200]}"
    update_call = next(
        call
        for call in conn.fetchrow.await_args_list
        if "UPDATE" in call.args[0] and "paper_notes" in call.args[0]
    )
    update_params = update_call.args[1:]
    # dynamic_update binds (record_id, set_value, user_id); the trailing owner id
    # is the extra_where predicate that single-tenant mode omits.
    assert update_params == (5, "updated text", 42), (
        f"multi-tenant UPDATE must bind note_id, value, and owner user_id; got: {update_params}"
    )


@pytest.mark.asyncio
async def test_delete_note_multi_tenant_includes_user_id_in_delete():
    """DELETE /api/notes/{id}: multi-tenant DELETE SQL must contain AND user_id = $2."""
    app, conn = _setup_app(
        user_id=42,
        fetchrow_side_effects=[_note_row(), _paper_row()],
    )
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/notes/5")
    finally:
        _teardown_app(app)

    assert resp.status_code == 204, f"Expected 204; got {resp.status_code}: {resp.text[:200]}"
    delete_params = conn.execute.await_args.args[1:]
    # Multi-tenant DELETE binds (note_id, user_id); the owner param is exactly what
    # single-tenant mode omits.
    assert delete_params == (5, 42), (
        f"multi-tenant DELETE must bind note_id and owner user_id; got: {delete_params}"
    )


@pytest.mark.asyncio
async def test_delete_note_single_tenant_no_user_predicate():
    """DELETE /api/notes/{id}: single-tenant (user_id=None) uses simple WHERE id = $1."""
    app, conn = _setup_app(
        user_id=None,
        fetchrow_side_effects=[_note_row()],
    )
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete("/api/notes/5")
    finally:
        _teardown_app(app)

    assert resp.status_code == 204, f"Expected 204; got {resp.status_code}: {resp.text[:200]}"
    delete_params = conn.execute.await_args.args[1:]
    # Single-tenant DELETE binds only note_id — no owner predicate param.
    assert delete_params == (5,), (
        f"single-tenant DELETE must bind only note_id; got: {delete_params}"
    )
