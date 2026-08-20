"""Unit contracts for Qdrant visibility payloads and checkpoint fencing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_vector_visibility_payload_is_complete_and_immutable() -> None:
    """Every persisted point receives source, scope, and deployment generation."""
    from paper_ingestion.ingestion.payload_schema import VectorVisibility

    visibility = VectorVisibility(
        source_type="arxiv",
        visibility_scope="public",
        visibility_generation="0123456789abcdef0123456789abcdef",
    )

    assert visibility.payload == {
        "source_type": "arxiv",
        "visibility_scope": "public",
        "visibility_generation": "0123456789abcdef0123456789abcdef",
    }
    with pytest.raises(AttributeError):
        visibility.visibility_scope = "private"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("scope", "generation"),
    [
        ("shared", "0123456789abcdef0123456789abcdef"),
        ("private", "not-a-generation"),
    ],
)
def test_vector_visibility_rejects_invalid_authorization_metadata(
    scope: str,
    generation: str,
) -> None:
    """Invalid scope or generation cannot be written to Qdrant."""
    from paper_ingestion.ingestion.payload_schema import VectorVisibility

    with pytest.raises(ValueError):
        VectorVisibility(
            source_type="arxiv",
            visibility_scope=scope,  # type: ignore[arg-type]
            visibility_generation=generation,
        )


def test_fail_closed_visibility_never_matches_a_runtime_generation() -> None:
    """Compatibility callers write complete metadata that stays unsearchable."""
    from paper_ingestion.ingestion.payload_schema import VectorVisibility

    visibility = VectorVisibility.fail_closed()

    assert visibility.visibility_scope == "private"
    assert visibility.source_type == "unknown"
    assert visibility.visibility_generation == "0" * 32


@pytest.mark.asyncio
async def test_checkpoint_rotation_replaces_generation_and_revokes_worker_lease() -> None:
    """Collection replacement publishes a fresh pending checkpoint atomically."""
    from paper_ingestion.ingestion.payload_schema import (
        CHECKPOINT_KEY,
        rotate_visibility_checkpoint,
    )

    value = {
        "version": 1,
        "visibility_generation": "f" * 32,
        "status": "pending",
        "last_chunk_id": 0,
        "qdrant_recovery": "collection_recreated",
        "rotated_at": "2026-07-22T00:00:00+00:00",
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"value": value})

    checkpoint = await rotate_visibility_checkpoint(
        conn,
        generation="f" * 32,
        qdrant_recovery="collection_recreated",
    )

    assert checkpoint.visibility_generation == "f" * 32
    assert checkpoint.status == "pending"
    assert checkpoint.last_chunk_id == 0
    assert "worker_lease_token" not in value
    assert conn.fetchrow.await_args.args[1] == CHECKPOINT_KEY


@pytest.mark.asyncio
async def test_stale_worker_cannot_advance_or_complete_a_checkpoint() -> None:
    """A failed compare-and-swap returns False and performs no fallback write."""
    from paper_ingestion.ingestion.payload_schema import (
        CHECKPOINT_KEY,
        advance_visibility_checkpoint,
        claim_visibility_lease,
        complete_visibility_checkpoint,
    )

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    claimed = await claim_visibility_lease(
        conn,
        generation="a" * 32,
        worker_token="worker-a",
    )
    advanced = await advance_visibility_checkpoint(
        conn,
        generation="a" * 32,
        worker_token="worker-a",
        last_chunk_id=42,
    )
    completed = await complete_visibility_checkpoint(
        conn,
        generation="a" * 32,
        worker_token="worker-a",
    )

    assert claimed is False
    assert advanced is False
    assert completed is False
    assert conn.fetchrow.await_count == 3
    assert all(call.args[1] == CHECKPOINT_KEY for call in conn.fetchrow.await_args_list)


@pytest.mark.asyncio
async def test_payload_indexes_cover_every_filter_field() -> None:
    """Collection setup creates one index for each authorization filter field."""
    from paper_ingestion.ingestion.payload_schema import ensure_visibility_payload_indexes

    qdrant = AsyncMock()
    await ensure_visibility_payload_indexes(qdrant)

    fields = {call.kwargs["field_name"] for call in qdrant.create_payload_index.await_args_list}
    assert fields == {
        "paper_id",
        "source_type",
        "visibility_generation",
        "visibility_scope",
    }
    assert all(call.kwargs["wait"] is True for call in qdrant.create_payload_index.await_args_list)


def test_checkpoint_parser_rejects_copied_or_malformed_completion() -> None:
    """Only the exact schema and generation can certify completion."""
    from paper_ingestion.ingestion.payload_schema import VisibilityCheckpoint

    with pytest.raises(ValueError):
        VisibilityCheckpoint.from_mapping(
            {
                "version": 1,
                "visibility_generation": "short",
                "status": "complete",
                "last_chunk_id": 2,
            }
        )

    checkpoint = VisibilityCheckpoint.from_mapping(
        {
            "version": 1,
            "visibility_generation": "b" * 32,
            "status": "complete",
            "last_chunk_id": 2,
            "worker_lease_token": "ignored-after-complete",
        }
    )
    assert checkpoint.status == "complete"
    assert checkpoint.worker_lease_token == "ignored-after-complete"


@pytest.mark.asyncio
async def test_collection_validation_rotates_empty_unrelated_qdrant_state() -> None:
    """A copied complete DB checkpoint cannot certify an empty collection."""
    from paper_ingestion.ingestion.payload_schema import (
        VisibilityCheckpoint,
        validate_checkpoint_collection_pair,
    )

    checkpoint = VisibilityCheckpoint.from_mapping(
        {
            "version": 1,
            "visibility_generation": "c" * 32,
            "status": "complete",
            "last_chunk_id": 9,
        }
    )
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=3)
    conn.fetchrow = AsyncMock(
        return_value={
            "value": {
                "version": 1,
                "visibility_generation": "d" * 32,
                "status": "pending",
                "last_chunk_id": 0,
                "qdrant_recovery": "collection_mismatch",
                "rotated_at": "2026-07-22T00:00:00+00:00",
            }
        }
    )
    qdrant = AsyncMock()
    qdrant.count = AsyncMock(return_value=SimpleNamespace(count=0))

    validated = await validate_checkpoint_collection_pair(conn, qdrant, checkpoint)

    assert validated.visibility_generation == "d" * 32
    assert validated.status == "pending"
    conn.fetchrow.assert_awaited_once()
