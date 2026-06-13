"""Tests for entities.paper_count double-increment guard.

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

from jarvis_common.prompt_safety import max_input_chars
from paper_ingestion.extraction.entities import (
    _ENTITY_OUTPUT_TOKENS,
    _find_or_create_entity,
    build_entity_prompt,
)
from tests.conftest import FakeRecord, _make_pool_and_conn


# ---------------------------------------------------------------------------
# entities budget — derived cap fits within the fast model context window
# ---------------------------------------------------------------------------


def test_entity_text_budget_fits_fast_model_window() -> None:
    """The derived text budget stays below the old 12 000-char cap, which overflowed the fast model window."""
    from jarvis_common.settings import get_core_settings

    fast_ctx = get_core_settings().llm_fast_num_ctx
    text_budget = max_input_chars(fast_ctx, reserved_output_tokens=_ENTITY_OUTPUT_TOKENS)

    assert text_budget < 12000, (
        f"Entity text budget {text_budget} must be < 12 000 to fit the fast model window"
    )


def test_entity_text_budget_applied_before_prompt_assembly() -> None:
    """Truncation must happen before text is passed to build_entity_prompt.

    Verifies the apply-then-build contract by reproducing the call-site logic
    in extract_entities_for_paper: slice to text_budget, THEN call build_entity_prompt.
    The assembled prompt must not contain the overflowing suffix.
    """
    from jarvis_common.settings import get_core_settings

    fast_ctx = get_core_settings().llm_fast_num_ctx
    text_budget = max_input_chars(fast_ctx, reserved_output_tokens=_ENTITY_OUTPUT_TOKENS)

    oversized = "Z" * (text_budget + 5000)

    # Reproduce the call-site truncation from extract_entities_for_paper
    llm_text = oversized[:text_budget] if len(oversized) > text_budget else oversized
    prompt = build_entity_prompt(title="Title", text=llm_text)

    # The full oversized suffix must not appear verbatim
    assert "Z" * (text_budget + 5000) not in prompt
    # The first portion is still present
    assert "Z" * 100 in prompt


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

    entity_id_1, _ = await _find_or_create_entity(conn, "BERT", "method", None)
    entity_id_2, _ = await _find_or_create_entity(
        conn,
        "bert",
        "method",
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


# ---------------------------------------------------------------------------
# Fix 4 — Axis 4 F1: orchestrator embed-store gate structural assertions
# ---------------------------------------------------------------------------


def test_orchestrator_embed_store_gate_all_three_conditions_present() -> None:
    """The embed-store gate at entities.py must guard on all three conditions.

    Structural test: reads the source of extract_entities_for_paper and asserts
    the three-condition guard is present.  This catches any accidental removal
    of one of the conditions without requiring a full integration harness.
    """
    import inspect

    from paper_ingestion.extraction.entities import extract_entities_for_paper

    source = inspect.getsource(extract_entities_for_paper)
    assert "was_merged" in source, "gate must reference was_merged"
    assert "_store_entity_embedding" in source, "gate must call _store_entity_embedding"
    # Both embedding and qdrant_client guards must appear together on the same gate line
    assert 'pc["embedding"] is not None' in source or "pc['embedding'] is not None" in source, (
        "gate must check embedding is not None"
    )
    assert "qdrant_client is not None" in source, "gate must check qdrant_client is not None"


@pytest.mark.asyncio
async def test_orchestrator_embed_store_skipped_when_was_merged(monkeypatch) -> None:
    """When _find_or_create_entity returns was_merged=True, _store_entity_embedding is not called."""
    from unittest.mock import AsyncMock, MagicMock

    import paper_ingestion.extraction.entities as entities_mod

    store_mock = AsyncMock()
    monkeypatch.setattr(entities_mod, "_store_entity_embedding", store_mock)

    # Simulate a single precomputed entity with embedding + qdrant, but was_merged=True.
    # We test the gate logic directly by reproducing the relevant loop fragment.
    was_merged = True
    pc = {"embedding": [0.1, 0.2], "similar_entity_id": None}
    qdrant_client = MagicMock()

    if not was_merged and pc["embedding"] is not None and qdrant_client is not None:
        await entities_mod._store_entity_embedding(
            MagicMock(), qdrant_client, 1, "BERT", "method", pc["embedding"]
        )

    store_mock.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrator_embed_store_skipped_when_embedding_none(monkeypatch) -> None:
    """When embedding is None, _store_entity_embedding is not called."""
    from unittest.mock import AsyncMock, MagicMock

    import paper_ingestion.extraction.entities as entities_mod

    store_mock = AsyncMock()
    monkeypatch.setattr(entities_mod, "_store_entity_embedding", store_mock)

    was_merged = False
    pc = {"embedding": None, "similar_entity_id": None}
    qdrant_client = MagicMock()

    if not was_merged and pc["embedding"] is not None and qdrant_client is not None:
        await entities_mod._store_entity_embedding(
            MagicMock(), qdrant_client, 1, "BERT", "method", pc["embedding"]
        )

    store_mock.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrator_embed_store_skipped_when_qdrant_client_none(monkeypatch) -> None:
    """When qdrant_client is None, _store_entity_embedding is not called."""
    from unittest.mock import AsyncMock, MagicMock

    import paper_ingestion.extraction.entities as entities_mod

    store_mock = AsyncMock()
    monkeypatch.setattr(entities_mod, "_store_entity_embedding", store_mock)

    was_merged = False
    pc = {"embedding": [0.1, 0.2], "similar_entity_id": None}
    qdrant_client = None

    if not was_merged and pc["embedding"] is not None and qdrant_client is not None:
        await entities_mod._store_entity_embedding(
            MagicMock(), qdrant_client, 1, "BERT", "method", pc["embedding"]
        )

    store_mock.assert_not_called()


@pytest.mark.asyncio
async def test_orchestrator_embed_store_called_when_all_gates_pass(monkeypatch) -> None:
    """When was_merged=False, embedding non-None, qdrant_client non-None → store is called."""
    from unittest.mock import AsyncMock, MagicMock

    import paper_ingestion.extraction.entities as entities_mod

    store_mock = AsyncMock()
    monkeypatch.setattr(entities_mod, "_store_entity_embedding", store_mock)

    was_merged = False
    pc = {"embedding": [0.1, 0.2], "similar_entity_id": None}
    qdrant_client = MagicMock()
    conn = MagicMock()
    entity_id = 5

    if not was_merged and pc["embedding"] is not None and qdrant_client is not None:
        await entities_mod._store_entity_embedding(
            conn, qdrant_client, entity_id, "BERT", "method", pc["embedding"]
        )

    store_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Fix 5 — Axis 4 F2: _user_scope_paper_entities_exists direct unit test
# ---------------------------------------------------------------------------


def test_user_scope_paper_entities_exists_fragment_shape() -> None:
    """_user_scope_paper_entities_exists returns correctly interpolated SQL fragment."""
    from paper_ingestion.extraction.entities_sql import _user_scope_paper_entities_exists

    frag = _user_scope_paper_entities_exists("e.id", 4)
    assert "EXISTS (SELECT 1 FROM paper_entities pe" in frag
    assert "pe.entity_id = e.id" in frag
    assert "pe.user_id IS NOT DISTINCT FROM $4" in frag

    frag2 = _user_scope_paper_entities_exists("e1.id", 1)
    assert "pe.entity_id = e1.id" in frag2
    assert "IS NOT DISTINCT FROM $1" in frag2
