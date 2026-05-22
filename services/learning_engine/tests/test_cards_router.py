"""Pure-unit Pydantic tests for the LE CardCreate model.

Behavioral router-level coverage (create_card evidence persistence, missing-deck
404, update_card empty-body short-circuit, FK violation 404, paper-ownership)
is in services/learning_engine/tests/contract/test_le_contract.py — the
handler-bypass mock-unit equivalents that previously lived here were retired in
the Cluster 12 contract pass on 2026-05-22 with survivor citations:

  test_create_card_success_uses_evidence_payload   → LE-C-01 (test_create_card_persists_evidence_payload)
  test_update_card_returns_existing_row_when_body_is_empty → LE-C-03 (test_update_card_empty_body_returns_existing_row)
  test_update_card_uses_dynamic_update             → LE-C-03 (behavioral)
  test_create_card_raises_404_on_fk_violation_deck → LE-C-02 (test_create_card_missing_deck_returns_404)
  test_create_card_skips_ownership_check_when_no_paper → LE-C-01 (no-paper happy path)
  test_update_card_raises_404_when_missing         → test_update_card_user_b_gets_404 (test_le_contract.py:161)
  test_delete_card_raises_404_when_row_missing     → test_delete_card_user_b_gets_404 (test_le_contract.py:204)
  test_create_card_asserts_paper_ownership         → test_create_card_non_owner_deck_gets_404 (test_le_contract.py:475)

These remaining tests cover the Pydantic field-cap validation surface of
CardCreate — pure-unit shape per docs/contracts/07-testing.md §1.1; no I/O.
"""

from __future__ import annotations

import pydantic
import pytest
from learning_engine.models import CardCreate, CardType


def test_card_create_front_over_cap_is_rejected():
    """CardCreate.front must reject input exceeding max_length=500 (→ 422-style ValidationError)."""
    with pytest.raises(pydantic.ValidationError):
        CardCreate(
            deck_id=1,
            card_type=CardType.CONCEPT,
            front="x" * 501,
            back="valid back",
        )


def test_card_create_back_over_cap_is_rejected():
    """CardCreate.back must reject input exceeding max_length=2000 (→ 422-style ValidationError)."""
    with pytest.raises(pydantic.ValidationError):
        CardCreate(
            deck_id=1,
            card_type=CardType.CONCEPT,
            front="valid front",
            back="x" * 2001,
        )
