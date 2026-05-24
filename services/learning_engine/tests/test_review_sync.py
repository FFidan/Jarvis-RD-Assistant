"""Pure unit tests for ReviewSyncResponse schema — CFG-SYNCSTATS-1.

These tests assert only on the Pydantic model schema itself, with no
mock-units patching router internals. Shape: pure-unit (no DB, no HTTP).
"""

from __future__ import annotations

from learning_engine.models import ReviewSyncResponse


def test_sync_response_has_already_synced_field():
    """already_synced field must be present in ReviewSyncResponse schema."""
    assert "already_synced" in ReviewSyncResponse.model_fields


def test_already_synced_has_default_zero():
    """already_synced must default to 0 so existing callers need not pass it."""
    resp = ReviewSyncResponse(synced=3, skipped=1)
    assert resp.already_synced == 0


def test_already_synced_rejects_negative():
    """already_synced must reject negative values (ge=0 constraint)."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReviewSyncResponse(synced=0, skipped=0, already_synced=-1)


def test_synced_and_already_synced_are_independent():
    """synced counts new writes; already_synced counts replays — distinct fields."""
    resp = ReviewSyncResponse(synced=5, skipped=2, already_synced=3)
    assert resp.synced == 5
    assert resp.skipped == 2
    assert resp.already_synced == 3
