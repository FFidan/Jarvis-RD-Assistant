"""Unit tests for shared-paper deletion behavior."""

from unittest.mock import AsyncMock

import pytest

from paper_ingestion import papers_service


@pytest.mark.asyncio
async def test_scoped_delete_preserves_the_canonical_row() -> None:
    conn = AsyncMock()

    deleted = await papers_service._hard_delete_scoped(conn, 7, 42)

    assert deleted is False
    statements = [" ".join(call.args[0].split()) for call in conn.execute.await_args_list]
    assert statements == [
        "DELETE FROM author_alert_log WHERE paper_id = $1 AND user_id = $2",
        "DELETE FROM cards WHERE paper_id = $1 AND user_id = $2",
        "DELETE FROM paper_contradictions WHERE (paper_a_id = $1 OR paper_b_id = $1) AND user_id = $2",
        "WITH deleted AS ( DELETE FROM paper_entities WHERE paper_id = $1 AND user_id = $2 RETURNING entity_id ) UPDATE entities AS entity SET paper_count = ( SELECT count(*) FROM paper_entities AS remaining WHERE remaining.entity_id = entity.id AND NOT (remaining.paper_id = $1 AND remaining.user_id = $2) ) WHERE entity.id IN (SELECT entity_id FROM deleted)",
        "DELETE FROM paper_extractions WHERE paper_id = $1 AND user_id = $2",
        "DELETE FROM paper_highlights WHERE paper_id = $1 AND user_id = $2",
        "DELETE FROM paper_notes WHERE paper_id = $1 AND user_id = $2",
        "DELETE FROM paper_recommendations WHERE paper_id = $1 AND user_id = $2",
        "DELETE FROM paper_summaries WHERE paper_id = $1 AND user_id = $2",
        "DELETE FROM paper_user_zotero_links WHERE paper_id = $1 AND user_id = $2",
        "WITH deleted AS ( DELETE FROM pulse_cards WHERE paper_id = $1 AND user_id = $2 RETURNING deck_id ) UPDATE pulse_decks AS deck SET card_count = ( SELECT count(*) FROM pulse_cards AS remaining WHERE remaining.deck_id = deck.id AND NOT (remaining.paper_id = $1 AND remaining.user_id = $2) ) WHERE deck.id IN (SELECT deck_id FROM deleted)",
        "DELETE FROM recommendation_feedback WHERE paper_id = $1 AND user_id = $2",
        "DELETE FROM task_paper_links AS link USING tasks AS owner WHERE link.task_id = owner.id AND link.paper_id = $1 AND owner.user_id = $2",
        "DELETE FROM project_papers AS link USING projects AS owner WHERE link.project_id = owner.id AND link.paper_id = $1 AND owner.user_id = $2",
        "DELETE FROM paper_user_state WHERE paper_id = $1 AND user_id = $2",
        "DELETE FROM user_library WHERE paper_id = $1 AND user_id = $2",
    ]
    assert all(call.args[1:] == (7, 42) for call in conn.execute.await_args_list)
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_unscoped_delete_keeps_legacy_physical_cleanup() -> None:
    conn = AsyncMock()

    deleted = await papers_service._hard_delete_scoped(conn, 7, None)

    assert deleted is True
    conn.execute.assert_awaited_once_with("DELETE FROM papers WHERE id = $1", 7)
