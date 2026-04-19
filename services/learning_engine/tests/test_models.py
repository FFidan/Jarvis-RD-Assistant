"""Unit tests for learning_engine Pydantic models / validators."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "libs" / "jarvis_common"))

from app.models import (  # noqa: E402
    CardCreate,
    CardType,
    DeckCreate,
    Evidence,
    ProjectCreate,
    Rating,
    ReviewRequest,
)

# ---------------------------------------------------------------------------
# Evidence — _migrate_snapshot model_validator
# ---------------------------------------------------------------------------


def test_evidence_migrates_pdf_snapshot_path():
    """Old pdf_snapshot_path key is promoted to snapshot_path transparently."""
    ev = Evidence.model_validate({"quote": "foo", "pdf_snapshot_path": "/tmp/snap.png"})
    assert ev.snapshot_path == "/tmp/snap.png"
    assert ev.pdf_snapshot_path is None  # excluded / deprecated field is still None


def test_evidence_snapshot_path_not_overwritten_when_both_present():
    """snapshot_path wins if both keys are supplied."""
    ev = Evidence.model_validate(
        {"snapshot_path": "/new/path.png", "pdf_snapshot_path": "/old/path.png"}
    )
    assert ev.snapshot_path == "/new/path.png"


# ---------------------------------------------------------------------------
# ReviewRequest — Rating boundary
# ---------------------------------------------------------------------------


def test_review_request_accepts_boundary_ratings():
    """Rating 1 (AGAIN) and 4 (EASY) are both valid."""
    r1 = ReviewRequest(rating=Rating.AGAIN)
    r4 = ReviewRequest(rating=Rating.EASY)
    assert r1.rating == 1
    assert r4.rating == 4


def test_review_request_rejects_invalid_rating():
    """An integer outside 1-4 is rejected by the Rating enum."""
    with pytest.raises(ValidationError):
        ReviewRequest(**{"rating": 5})


# ---------------------------------------------------------------------------
# ProjectCreate — color hex pattern validator
# ---------------------------------------------------------------------------


def test_project_create_accepts_valid_hex_color():
    p = ProjectCreate(name="My Project", color="#1A2B3C")
    assert p.color == "#1A2B3C"


def test_project_create_rejects_invalid_color():
    with pytest.raises(ValidationError, match="color"):
        ProjectCreate(name="Bad Color", color="not-a-hex")


# ---------------------------------------------------------------------------
# CardCreate — front/back non-empty
# ---------------------------------------------------------------------------


def test_card_create_rejects_empty_front():
    with pytest.raises(ValidationError):
        CardCreate(deck_id=1, card_type=CardType.CONCEPT, front="", back="Answer")


# ---------------------------------------------------------------------------
# DeckCreate — name length constraint
# ---------------------------------------------------------------------------


def test_deck_create_rejects_empty_name():
    with pytest.raises(ValidationError):
        DeckCreate(name="")
