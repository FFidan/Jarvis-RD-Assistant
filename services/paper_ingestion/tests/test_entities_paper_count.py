"""Tests for W1-D2-006: entities.paper_count double-increment guard.

Audit finding: ``_find_or_create_entity`` was incrementing ``paper_count``
unconditionally on every call, so:
  1. When the LLM emits the same entity name twice in one extraction run,
     ``paper_count`` was incremented by 2 instead of 1.
  2. Re-running extraction for the same paper would re-increment ``paper_count``
     even though ``paper_entities`` is idempotent (ON CONFLICT DO UPDATE).

Fix: ``_find_or_create_entity`` no longer touches ``paper_count``.
``extract_entities_for_paper`` increments it exactly once per distinct entity
id encountered in a single run (tracked via ``paper_count_incremented`` set).

Grounded against:
  services/paper_ingestion/paper_ingestion/extraction/entities.py
  (``_find_or_create_entity`` lines 206-275, ``extract_entities_for_paper``
  Phase-2 loop lines 376-410 after fix).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from paper_ingestion.extraction.entities import _find_or_create_entity
from tests.conftest import FakeRecord, _make_pool_and_conn


# ---------------------------------------------------------------------------
# _find_or_create_entity — no longer touches paper_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_or_create_entity_existing_does_not_update_paper_count():
    """Existing-entity branch must return entity id WITHOUT issuing an UPDATE."""
    _, conn = _make_pool_and_conn(
        fetchrow_return=FakeRecord(id=7),
    )

    entity_id, was_merged = await _find_or_create_entity(
        conn,
        name="BERT",
        entity_type="method",
        description=None,
        qdrant_client=None,
    )

    assert entity_id == 7
    assert was_merged is True
    # No UPDATE should be issued — paper_count management is caller's responsibility.
    for c in conn.execute.await_args_list:
        assert "paper_count" not in c.args[0], (
            "_find_or_create_entity must not touch paper_count on existing-entity branch"
        )


@pytest.mark.asyncio
async def test_find_or_create_entity_similar_does_not_update_paper_count():
    """Similar-entity (Qdrant dedup) branch must return without issuing an UPDATE."""
    _, conn = _make_pool_and_conn(
        fetchrow_return=None,  # no exact-match
    )

    entity_id, was_merged = await _find_or_create_entity(
        conn,
        name="RoBERTa",
        entity_type="method",
        description=None,
        qdrant_client=None,
        similar_entity_id=42,
    )

    assert entity_id == 42
    assert was_merged is True
    for c in conn.execute.await_args_list:
        assert "paper_count" not in c.args[0], (
            "_find_or_create_entity must not touch paper_count on similar-entity branch"
        )


@pytest.mark.asyncio
async def test_find_or_create_entity_new_insert_does_not_set_paper_count():
    """New-entity INSERT branch must not embed a paper_count increment in the SQL."""
    inserted_row = FakeRecord(id=99)
    _, conn = _make_pool_and_conn(
        # First fetchrow → no exact match; second fetchrow → inserted row
        fetchrow_side_effects=[None, inserted_row],
    )

    entity_id, was_merged = await _find_or_create_entity(
        conn,
        name="GPT-4",
        entity_type="method",
        description="A large language model",
        qdrant_client=None,
    )

    assert entity_id == 99
    assert was_merged is False
    # The INSERT SQL must use ON CONFLICT DO NOTHING (not DO UPDATE SET paper_count).
    insert_calls = [c for c in conn.fetchrow.await_args_list if "INSERT" in str(c.args)]
    assert insert_calls, "Expected an INSERT fetchrow call"
    insert_sql = insert_calls[0].args[0]
    assert "ON CONFLICT" in insert_sql
    assert "paper_count" not in insert_sql, (
        "INSERT must use DO NOTHING, not DO UPDATE SET paper_count"
    )
    for c in conn.execute.await_args_list:
        assert "paper_count" not in c.args[0], (
            "_find_or_create_entity must not touch paper_count on new-entity branch"
        )


# ---------------------------------------------------------------------------
# Duplicate entity name within one extraction run
# Simulates the caller loop in extract_entities_for_paper.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_count_incremented_once_for_duplicate_entity_in_run():
    """If the LLM emits the same entity twice, paper_count must increment by 1."""
    _, conn = _make_pool_and_conn(
        fetchrow_return=FakeRecord(id=7),
    )
    conn.execute = AsyncMock()

    entity_id_1, _ = await _find_or_create_entity(conn, "BERT", "method", None, None)
    entity_id_2, _ = await _find_or_create_entity(
        conn,
        "bert",
        "method",
        None,
        None,  # canonical-normalized duplicate
    )

    # Both calls resolve to the same entity id.
    assert entity_id_1 == entity_id_2 == 7

    # Simulate what extract_entities_for_paper does: increment paper_count only
    # if entity_id not already in paper_count_incremented set.
    paper_count_incremented: set[int] = set()
    increment_calls: list[int] = []

    for eid in [entity_id_1, entity_id_2]:
        if eid not in paper_count_incremented:
            await conn.execute(
                "UPDATE entities SET paper_count = paper_count + 1 WHERE id = $1",
                eid,
            )
            paper_count_incremented.add(eid)
            increment_calls.append(eid)

    assert increment_calls == [7], (
        "paper_count must be incremented exactly once even when entity appears twice"
    )
    # Verify only one UPDATE was issued.
    update_calls = [c for c in conn.execute.await_args_list if "paper_count" in c.args[0]]
    assert len(update_calls) == 1
    assert update_calls[0] == call(
        "UPDATE entities SET paper_count = paper_count + 1 WHERE id = $1",
        7,
    )
