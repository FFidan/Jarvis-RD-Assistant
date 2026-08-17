"""Unit tests for shared-paper deletion behavior."""

import uuid

from unittest.mock import AsyncMock

import pytest

from paper_ingestion import papers_service


@pytest.mark.asyncio
async def test_scoped_delete_preserves_the_canonical_row(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = AsyncMock()
    event_id = uuid.uuid4()
    event = AsyncMock(return_value=event_id)
    monkeypatch.setattr(papers_service, "record_event", event)

    deleted = await papers_service._hard_delete_scoped(conn, 7, 42)

    assert deleted is False
    statements = [" ".join(call.args[0].split()) for call in conn.execute.await_args_list]
    assert statements == [
        "DELETE FROM user_library WHERE paper_id = $1 AND user_id = $2",
        "INSERT INTO pending_paper_deletions (event_id, user_id, paper_id) VALUES ($1, $2, $3) ON CONFLICT (event_id) DO NOTHING",
    ]
    assert conn.execute.await_args_list[0].args[1:] == (7, 42)
    assert conn.execute.await_args_list[1].args[1:] == (event_id, 42, 7)
    conn.fetchval.assert_not_awaited()
    event.assert_awaited_once_with(
        conn,
        event_type="paper.deleted",
        user_id=42,
        paper_id=7,
    )


@pytest.mark.asyncio
async def test_unscoped_delete_keeps_legacy_physical_cleanup() -> None:
    conn = AsyncMock()

    deleted = await papers_service._hard_delete_scoped(conn, 7, None)

    assert deleted is True
    conn.execute.assert_awaited_once_with("DELETE FROM papers WHERE id = $1", 7)
