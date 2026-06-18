"""Unit tests for the daily expired-user hard-purge job (Qdrant scoping)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paper_ingestion.jobs import data_purge


@pytest.mark.asyncio
async def test_purge_qdrant_excludes_papers_still_held_by_survivors():
    """The delete filter must exclude paper_ids in `protected_paper_ids` so a
    surviving user's shared papers keep their vectors."""
    qdrant = AsyncMock()
    qdrant.count.return_value = type("C", (), {"count": 7})()

    await data_purge._purge_qdrant_for_user(qdrant, uid=42, protected_paper_ids=[101, 202])

    delete_kwargs = qdrant.delete.await_args.kwargs
    flt = delete_kwargs["points_selector"]
    # must clause keeps the user_id match; must_not excludes protected paper_ids.
    assert any(c.match.value == 42 for c in flt.must), "user_id match dropped"
    excluded = {pid for c in (flt.must_not or []) for pid in c.match.any}
    assert excluded == {101, 202}, f"protected paper_ids not excluded: {excluded}"


@pytest.mark.asyncio
async def test_purge_qdrant_no_protected_filters_only_by_user():
    """With no surviving holders the filter is the legacy user_id-only delete."""
    qdrant = AsyncMock()
    qdrant.count.return_value = type("C", (), {"count": 3})()

    await data_purge._purge_qdrant_for_user(qdrant, uid=9, protected_paper_ids=[])

    flt = qdrant.delete.await_args.kwargs["points_selector"]
    assert any(c.match.value == 9 for c in flt.must)
    assert not flt.must_not, "must_not must be empty when nothing is protected"


@pytest.mark.asyncio
async def test_data_purge_task_protects_shared_paper_of_survivor(monkeypatch):
    """End-to-end orchestration: when an expired user and a surviving user share
    a paper, the protected-set query (`user_id <> ALL($1::int[])`) must feed the
    shared paper_id into `protected_paper_ids` so the survivor's vectors are kept.

    Drives `data_purge_task` with a mock pool whose two `conn.fetch` calls are
    keyed off the SQL: the expired-users SELECT returns uid 42 only, and the
    `user_library` protected query returns the paper (700) still held by survivor
    uid 7. `_purge_qdrant_for_user` is spied; we assert it was invoked for uid 42
    with 700 present in `protected_paper_ids`. This FAILS if the orchestration SQL
    were dropped (protected list would be empty).
    """
    expired_uid = 42
    shared_paper_id = 700

    async def fake_fetch(query, *args):
        if query == data_purge._SELECT_EXPIRED_USERS:
            return [{"id": expired_uid}]
        if "user_library" in query:
            # Survivor uid 7 still holds the shared paper; expired uid 42 excluded
            # by the `user_id <> ALL($1::int[])` predicate the job passes.
            assert args[0] == [expired_uid], "expired ids not passed to protected query"
            return [{"paper_id": shared_paper_id}]
        return []

    conn = AsyncMock()
    conn.fetch.side_effect = fake_fetch
    conn.execute.return_value = "DELETE 1"

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = AsyncMock()
    pool.acquire = acquire

    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool, qdrant_client=AsyncMock()))

    # Spy on the per-user purge so we observe the protected set the job built,
    # and stub the audit anonymize (its own transactional SQL is out of scope).
    purge_spy = AsyncMock(return_value=5)
    monkeypatch.setattr(data_purge, "_purge_qdrant_for_user", purge_spy)
    monkeypatch.setattr(data_purge, "_anonymize_audit_log_for_users", AsyncMock(return_value=0))
    monkeypatch.setattr(data_purge, "log_audit", AsyncMock())

    await data_purge.data_purge_task(app)

    purge_spy.assert_awaited_once()
    await_args = purge_spy.await_args
    assert await_args is not None
    called_uid = await_args.args[1]
    protected = await_args.args[2]
    assert called_uid == expired_uid, f"purge ran for wrong uid: {called_uid}"
    assert shared_paper_id in protected, (
        f"survivor's shared paper {shared_paper_id} not protected; got {protected}"
    )
