"""Unit tests for the daily expired-user hard-purge job (Qdrant scoping)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from qdrant_client import models as qdrant_models

from paper_ingestion.jobs import data_purge


def _count(value: int) -> object:
    """Return a Qdrant count response carrying *value*."""
    return type("C", (), {"count": value})()


@pytest.mark.asyncio
async def test_purge_qdrant_excludes_papers_still_held_by_survivors():
    """Protected points keep vectors but lose the erased user's audit ID."""
    qdrant = AsyncMock()
    qdrant.count.side_effect = [_count(5), _count(2), _count(0)]

    counts = await data_purge._purge_qdrant_for_user(
        qdrant,
        uid=42,
        protected_paper_ids=[101, 202],
    )

    delete_kwargs = qdrant.delete.await_args.kwargs
    flt = delete_kwargs["points_selector"]
    assert flt.must, "a selector without conditions matches the whole collection"
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
    assert counts == data_purge.QdrantPurgeCounts(deleted=5, redacted=2, residual_points=0)


@pytest.mark.asyncio
async def test_purge_qdrant_no_protected_filters_only_by_user():
    """Without protected papers, every matching point is deleted."""
    qdrant = AsyncMock()
    qdrant.count.side_effect = [_count(3), _count(0)]

    counts = await data_purge._purge_qdrant_for_user(qdrant, uid=9, protected_paper_ids=[])

    flt = qdrant.delete.await_args.kwargs["points_selector"]
    assert flt.must, "a selector without conditions matches the whole collection"
    assert any(c.match.value == 9 for c in flt.must)
    assert not flt.must_not, "must_not must be empty when nothing is protected"
    qdrant.set_payload.assert_not_awaited()
    assert counts == data_purge.QdrantPurgeCounts(deleted=3, redacted=0, residual_points=0)


@pytest.mark.asyncio
async def test_purge_qdrant_refuses_a_selector_that_matches_every_point(
    monkeypatch: pytest.MonkeyPatch,
):
    """A condition-less selector would erase the shared collection, so refuse it."""

    class _ConditionlessFilter(qdrant_models.Filter):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr(qdrant_models, "Filter", _ConditionlessFilter)
    qdrant = AsyncMock()
    qdrant.count.return_value = _count(4)

    with pytest.raises(ValueError, match="match-all"):
        await data_purge._purge_qdrant_for_user(qdrant, uid=7, protected_paper_ids=[])

    qdrant.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_qdrant_counts_and_warns_about_points_left_behind(
    caplog: pytest.LogCaptureFixture,
):
    """Points still carrying the purged id after both writes are the stores disagreeing."""
    qdrant = AsyncMock()
    qdrant.count.side_effect = [_count(5), _count(2), _count(3)]

    with caplog.at_level(logging.WARNING, logger=data_purge.logger.name):
        counts = await data_purge._purge_qdrant_for_user(qdrant, uid=42, protected_paper_ids=[101])

    assert counts.residual_points == 3
    assert "still carry user 42" in caplog.text


@pytest.mark.asyncio
async def test_purge_qdrant_is_idempotent_once_the_user_is_gone():
    """A repeat run finds nothing to redact and leaves no residue."""
    qdrant = AsyncMock()
    qdrant.count.side_effect = [_count(0), _count(0), _count(0)]

    counts = await data_purge._purge_qdrant_for_user(qdrant, uid=42, protected_paper_ids=[101])

    assert counts == data_purge.QdrantPurgeCounts(deleted=0, redacted=0, residual_points=0)
