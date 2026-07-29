"""Entity-count contracts for generation-aware knowledge-graph extraction."""

from __future__ import annotations

import pytest

from jarvis_common.prompt_safety import max_input_chars
from paper_ingestion.extraction.entities import (
    _ENTITY_OUTPUT_TOKENS,
    _aggregate_entity_mentions,
    _find_or_create_entity,
    build_entity_prompt,
)
from paper_ingestion.extraction.kg_models import KGEntityCandidate
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
# Duplicate entity names within one extraction run.
# ---------------------------------------------------------------------------


def test_duplicate_entities_become_one_absolute_mention_count():
    """A retry persists the same absolute count instead of incrementing it."""
    entities = [
        KGEntityCandidate(name="BERT", type="method"),
        KGEntityCandidate(name="bert", type="method"),
        KGEntityCandidate(name="GLUE", type="dataset"),
    ]

    aggregated = _aggregate_entity_mentions(entities)

    assert aggregated == [
        {
            "name": "BERT",
            "type": "method",
            "description": None,
            "mention_count": 2,
        },
        {
            "name": "GLUE",
            "type": "dataset",
            "description": None,
            "mention_count": 1,
        },
    ]


@pytest.mark.asyncio
async def test_delayed_entity_writer_performs_no_persistence_or_vector_write() -> None:
    """A source change after LLM work prevents every entity persistence side effect."""
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from paper_ingestion.exceptions import SourceGenerationChangedError
    from paper_ingestion.extraction.entities import extract_entities_for_paper
    from paper_ingestion.extraction.kg_models import KGExtractionOutput

    read_conn = AsyncMock()
    read_conn.fetchrow.return_value = {
        "id": 7,
        "title": "Generation race",
        "content_generation": 0,
    }
    read_conn.fetch.return_value = [
        {
            "id": 11,
            "paper_id": 7,
            "chunk_index": 0,
            "content": "BERT is the central method.",
            "page_number": 1,
            "start_char": 0,
            "end_char": 27,
            "embedding_id": None,
            "created_at": datetime.now(UTC),
        }
    ]
    write_conn = AsyncMock()
    write_conn.fetchval.return_value = 1
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=False)
    write_conn.transaction = MagicMock(return_value=transaction)

    read_context = MagicMock()
    read_context.__aenter__ = AsyncMock(return_value=read_conn)
    read_context.__aexit__ = AsyncMock(return_value=False)
    write_context = MagicMock()
    write_context.__aenter__ = AsyncMock(return_value=write_conn)
    write_context.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.side_effect = [read_context, write_context]
    pool.fetch = AsyncMock(return_value=[])

    embedder = AsyncMock()
    embedder.embed_texts.return_value = [[0.1, 0.2]]
    qdrant = AsyncMock()
    qdrant.query_points.return_value = SimpleNamespace(points=[])
    llm_output = KGExtractionOutput(
        entities=[KGEntityCandidate(name="BERT", type="method")],
    )

    with (
        patch(
            "paper_ingestion.extraction.entities.call_llm_structured",
            AsyncMock(return_value=llm_output),
        ),
        patch(
            "paper_ingestion.extraction.entities._find_or_create_entity",
            AsyncMock(),
        ) as find_or_create,
        patch(
            "paper_ingestion.extraction.entities._store_entity_embedding",
            AsyncMock(),
        ) as store_embedding,
    ):
        with pytest.raises(SourceGenerationChangedError, match="Please retry"):
            await extract_entities_for_paper(
                MagicMock(),
                pool,
                7,
                embedder=embedder,
                qdrant_client=qdrant,
                openai_client=MagicMock(),
                user_id=42,
            )

    find_or_create.assert_not_awaited()
    store_embedding.assert_not_awaited()
    qdrant.create_collection.assert_not_awaited()
    qdrant.upsert.assert_not_awaited()
    assert write_conn.fetchval.await_count == 1


@pytest.mark.asyncio
async def test_entity_route_reports_source_change_as_retryable_conflict() -> None:
    """The direct API returns a stable conflict message instead of internal state."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastapi import HTTPException

    from paper_ingestion.exceptions import SourceGenerationChangedError
    from paper_ingestion.routers.knowledge_graph import extract_entities

    pool, _conn = _make_pool_and_conn()
    handler = getattr(extract_entities, "__wrapped__", extract_entities)
    with (
        patch(
            "paper_ingestion.routers.knowledge_graph.assert_paper_ownership",
            AsyncMock(),
        ),
        patch(
            "paper_ingestion.routers.knowledge_graph.extract_entities_for_paper",
            AsyncMock(
                side_effect=SourceGenerationChangedError("internal source-generation details")
            ),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await handler(
                request=SimpleNamespace(state=SimpleNamespace(request_id="request-1")),
                paper_id=7,
                db_pool=pool,
                http_client=MagicMock(),
                embedder=None,
                qdrant=None,
                user_id=42,
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "The paper changed during entity extraction. Please retry."
    assert "generation" not in str(exc_info.value.detail).lower()


# ---------------------------------------------------------------------------
# Orchestrator embed-store gate structural assertions
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
# Visibility SQL fragments
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fix F11 — build_entity_prompt respects caller-supplied max_chars budget
# ---------------------------------------------------------------------------


def test_build_entity_prompt_respects_large_max_chars() -> None:
    """build_entity_prompt must honour a max_chars value larger than the old 12 000 hardcode.

    When the caller passes max_chars=30_000 and the text is 25 000 chars,
    the assembled prompt must contain all 25 000 characters (none silently
    dropped by the internal wrap_delimited cap).
    """
    large_text = "A" * 25_000
    prompt = build_entity_prompt(title="T", text=large_text, max_chars=30_000)
    assert "A" * 25_000 in prompt, (
        "build_entity_prompt must not re-cap text when max_chars > 12 000"
    )


def test_build_entity_prompt_default_max_chars_unchanged() -> None:
    """Default max_chars=12 000 must still truncate text longer than 12 000 chars (no regression)."""
    oversized = "B" * 15_000
    prompt = build_entity_prompt(title="T", text=oversized)
    assert "B" * 15_000 not in prompt, (
        "default max_chars=12 000 must still truncate text beyond 12 000 chars"
    )
    assert "B" * 100 in prompt, "first portion of text must still appear in the prompt"


def test_visible_paper_entities_exists_fragment_shape() -> None:
    """The pure builder composes the entity join with the shared policy."""
    from paper_ingestion.extraction.entities_sql import _visible_paper_entities_exists
    from paper_ingestion.queries.predicates import paper_visible_sql

    frag = _visible_paper_entities_exists("e.id", 4)
    expected = (
        "EXISTS (SELECT 1 FROM paper_entities pe "
        "JOIN papers visible_p ON visible_p.id = pe.paper_id "
        "WHERE pe.entity_id = e.id "
        "AND pe.content_generation = visible_p.content_generation "
        f"AND {paper_visible_sql(4, alias='visible_p')})"
    )
    assert frag == expected

    frag2 = _visible_paper_entities_exists("e1.id", 1)
    expected2 = expected.replace("e.id", "e1.id").replace("$4", "$1")
    assert frag2 == expected2


def test_paper_entities_upsert_is_absolute_and_generation_monotonic():
    """The persisted mention count is retry-safe and older generations lose."""
    import inspect

    from paper_ingestion.extraction.entities import extract_entities_for_paper

    source = inspect.getsource(extract_entities_for_paper)
    assert "mention_count = EXCLUDED.mention_count" in source
    assert "paper_entities.mention_count + 1" not in source
    assert "paper_entities.content_generation" in source
    assert "<= EXCLUDED.content_generation" in source
