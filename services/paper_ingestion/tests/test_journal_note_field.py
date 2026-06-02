"""Regression: the new optional ``note`` field on JournalPrompts (UI_v3 EOD).

Spec §3.10/§4.3 adds one optional free-note escape hatch to the EOD shutdown
ritual. It is an additive JSONB key — NO migration. These tests assert the new
field round-trips through the existing GET + POST-upsert journal route and that
omitting it is still valid (existing callers unaffected).
"""

from __future__ import annotations

from paper_ingestion.models.journal import JournalPrompts


def test_journal_prompts_note_optional_and_defaults_none():
    p = JournalPrompts()
    assert p.note is None
    # Existing callers that never set note are unaffected.
    assert JournalPrompts(first_move="x").note is None


def test_journal_prompts_note_excluded_when_none():
    """upsert uses model_dump(exclude_none=True); empty note must not be stored."""
    dumped = JournalPrompts(worked="shipped threads").model_dump(exclude_none=True)
    assert "note" not in dumped
    assert dumped == {"worked": "shipped threads"}


def test_journal_prompts_note_included_when_set():
    dumped = JournalPrompts(note="also fixed CI flake").model_dump(exclude_none=True)
    assert dumped == {"note": "also fixed CI flake"}
