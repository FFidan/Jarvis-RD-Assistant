"""Tests for small learning engine utility modules."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fsrs import Card, Rating
from learning_engine.anki_exporter import AnkiExporter
from learning_engine.fsrs_manager import FSRSManager


def test_create_new_card_returns_state_and_due_at() -> None:
    """New FSRS cards return serializable state plus the initial due timestamp."""
    manager = FSRSManager()

    state, due_at = manager.create_new_card()

    assert isinstance(state, dict)
    assert isinstance(due_at, datetime)
    assert state["state"] == Card().to_dict()["state"]


def test_schedule_review_updates_state_and_review_log() -> None:
    """Valid FSRS state produces a new card state, review log, and due date."""
    manager = FSRSManager()
    state, _ = manager.create_new_card()

    next_state, review_log, due_at = manager.schedule_review(state, rating=3)

    assert isinstance(next_state, dict)
    assert isinstance(review_log, dict)
    assert isinstance(due_at, datetime)
    assert next_state != state
    assert "rating" in review_log


def test_schedule_review_recovers_from_invalid_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid stored FSRS state falls back to a fresh Card instead of crashing."""
    manager = FSRSManager()
    fallback_card = Card()
    fake_log = SimpleNamespace(to_dict=lambda: {"rating": Rating.Good.value})
    captured: dict[str, Card] = {}

    def fake_review_card(card: Card, rating: Rating):
        captured["card"] = card
        captured["rating"] = rating
        return fallback_card, fake_log

    monkeypatch.setattr(
        "learning_engine.fsrs_manager.Card.from_dict", MagicMock(side_effect=KeyError("missing"))
    )
    monkeypatch.setattr(manager.scheduler, "review_card", fake_review_card)

    next_state, review_log, due_at = manager.schedule_review({"oops": "bad"}, rating=3)

    assert isinstance(captured["card"], Card)
    assert captured["rating"] is Rating.Good
    assert next_state == fallback_card.to_dict()
    assert review_log == {"rating": Rating.Good.value}
    assert due_at == fallback_card.due


def test_anki_exporter_returns_nonempty_apkg_bytes() -> None:
    """Deck export produces an Anki package payload."""
    exporter = AnkiExporter()

    payload = exporter.export_deck(
        "JARVIS Demo",
        [
            {
                "front": "What is retrieval-augmented generation?",
                "back": "A system that augments prompts with retrieved context.",
                "source": "paper-1",
                "evidence_text": "retrieval improves grounding",
            }
        ],
    )

    assert isinstance(payload, bytes)
    assert len(payload) > 0

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())

    assert "collection.anki2" in names


def test_anki_exporter_allows_missing_optional_fields() -> None:
    """Optional source/evidence fields default to empty strings during export."""
    exporter = AnkiExporter()

    payload = exporter.export_deck(
        "Minimal Deck",
        [
            {
                "front": "Q",
                "back": "A",
            }
        ],
    )

    assert isinstance(payload, bytes)
    assert len(payload) > 0
