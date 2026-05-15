"""Canonical-corpus regressions for project-paper linking."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from learning_engine.routers import project_papers


def _request(user_id: int | None = 42):
    return SimpleNamespace(state=SimpleNamespace(user_id=user_id))


def _pool(conn):
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


@pytest.mark.asyncio
async def test_link_paper_uses_canonical_ownership_and_enqueues_for_caller(monkeypatch):
    """Linking must not query removed papers.user_id or enqueue ownerless Zotero pushes."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": 5},  # project exists and belongs to caller
            {"starred": True, "zotero_item_key": None},  # push intent
        ]
    )
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = _pool(conn)
    ownership = AsyncMock()
    add_to_library = AsyncMock()
    monkeypatch.setattr(project_papers, "assert_paper_ownership", ownership, raising=False)
    monkeypatch.setattr(project_papers, "add_to_library", add_to_library, raising=False)

    import jarvis_common.task_registry as task_registry

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()
    with patch.dict(task_registry.KIND_TO_TASK, {"zotero.push": mock_task}):
        result = await project_papers.link_paper.__wrapped__(
            _request(42),
            project_id=5,
            paper_id=10,
            db_pool=pool,
            user_id=42,
        )

    assert result == {"project_id": 5, "paper_id": 10}
    ownership.assert_awaited_once_with(conn, 10, 42)
    add_to_library.assert_awaited_once_with(
        conn,
        user_id=42,
        paper_id=10,
        added_via="manual_save",
    )
    executed_sql = "\n".join(str(call.args[0]) for call in conn.fetchrow.await_args_list)
    assert "papers.user_id" not in executed_sql
    assert "user_id IS NULL OR" not in executed_sql
    mock_task.defer_async.assert_awaited_once()
    assert mock_task.defer_async.await_args.kwargs["user_id"] == 42
