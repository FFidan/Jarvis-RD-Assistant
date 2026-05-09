"""Tests for shared text normalization and author matching helpers."""

from __future__ import annotations

from jarvis_common.text_utils import author_matches, normalize_author_name


def test_normalize_author_name_lowercases_removes_dots_and_collapses_whitespace() -> None:
    """Author normalization should make common citation spellings comparable."""
    assert normalize_author_name("  J.   R.  Smith  ") == "j r smith"


def test_author_matches_exact_normalized_name() -> None:
    """Exact normalized names should match despite case, dots, and spacing."""
    assert author_matches("Ada Lovelace", "  ADA   Lovelace ")
    assert author_matches("J. R. Smith", "j r smith")


def test_author_matches_last_name_and_first_initial() -> None:
    """Initial-plus-last-name references should match tracked full names."""
    assert author_matches("Geoffrey Hinton", "G Hinton")
    assert author_matches("G Hinton", "Geoffrey Hinton")


def test_author_matches_rejects_different_initial_or_last_name() -> None:
    """Different initials or surnames must not match a tracked author."""
    assert not author_matches("Geoffrey Hinton", "Y Hinton")
    assert not author_matches("Geoffrey Hinton", "G Bengio")
