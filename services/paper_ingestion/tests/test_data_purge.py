"""Unit tests for the daily expired-user hard-purge job (Qdrant scoping)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from paper_ingestion.jobs import data_purge


@pytest.mark.asyncio
async def test_purge_qdrant_excludes_papers_still_held_by_survivors():
    """Protected points keep vectors but lose the erased user's audit ID."""
    qdrant = AsyncMock()
    qdrant.count.side_effect = [
        type("C", (), {"count": 5})(),
        type("C", (), {"count": 2})(),
    ]

    counts = await data_purge._purge_qdrant_for_user(
        qdrant,
        uid=42,
        protected_paper_ids=[101, 202],
    )

    delete_kwargs = qdrant.delete.await_args.kwargs
    flt = delete_kwargs["points_selector"]
    assert any(c.match.value == 42 for c in flt.must), "user_id match dropped"
    excluded = {pid for c in (flt.must_not or []) for pid in c.match.any}
    assert excluded == {101, 202}, f"protected paper_ids not excluded: {excluded}"

    redact_kwargs = qdrant.set_payload.await_args.kwargs
    assert redact_kwargs["payload"] == {"user_id": None}
    redact_filter = redact_kwargs["points"]
    assert any(c.match.value == 42 for c in redact_filter.must)
    protected = {pid for c in redact_filter.must if hasattr(c.match, "any") for pid in c.match.any}
    assert protected == {101, 202}
    writes = [entry[0] for entry in qdrant.mock_calls if entry[0] in {"set_payload", "delete"}]
    expected_writes = ["set_payload", "delete"]
    assert writes == expected_writes
    assert counts == data_purge.QdrantPurgeCounts(deleted=5, redacted=2)


@pytest.mark.asyncio
async def test_purge_qdrant_no_protected_filters_only_by_user():
    """Without protected papers, every matching point is deleted."""
    qdrant = AsyncMock()
    qdrant.count.return_value = type("C", (), {"count": 3})()

    counts = await data_purge._purge_qdrant_for_user(qdrant, uid=9, protected_paper_ids=[])

    flt = qdrant.delete.await_args.kwargs["points_selector"]
    assert any(c.match.value == 9 for c in flt.must)
    assert not flt.must_not, "must_not must be empty when nothing is protected"
    qdrant.set_payload.assert_not_awaited()
    assert counts == data_purge.QdrantPurgeCounts(deleted=3, redacted=0)
