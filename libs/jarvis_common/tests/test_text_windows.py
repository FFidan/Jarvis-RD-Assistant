"""Tests for chunk_windows — ordered chunk grouping into char-budget windows."""

from __future__ import annotations

import pytest
from jarvis_common.text_windows import chunk_windows


def test_every_chunk_lands_in_exactly_one_window_in_order():
    """Flattening the windows reproduces the input sequence exactly."""
    chunks = [f"chunk-{i:02d} " + "x" * 40 for i in range(10)]

    windows = chunk_windows(chunks, max_chars=120)

    assert [c for w in windows for c in w] == chunks
    assert all(w for w in windows)


def test_windows_respect_char_budget_including_joiners():
    """Joined window text (one joiner char between chunks) stays within budget."""
    chunks = ["a" * 50, "b" * 50, "c" * 50]

    windows = chunk_windows(chunks, max_chars=101)

    assert windows == [["a" * 50, "b" * 50], ["c" * 50]]
    assert all(len("\n".join(w)) <= 101 for w in windows)


def test_everything_fits_one_window():
    assert chunk_windows(["alpha", "beta"], max_chars=1000) == [["alpha", "beta"]]


def test_zero_heading_text_windows_by_budget_alone():
    chunks = ["p" * 30] * 6

    windows = chunk_windows(chunks, max_chars=61)

    assert [len(w) for w in windows] == [2, 2, 2]


def test_overflow_mid_section_moves_section_start_to_next_window():
    """The chunks of the most recent section travel with their continuation."""
    intro = "intro " + "x" * 50
    heading = "## Methods\n" + "y" * 30
    continuation = "z" * 40

    windows = chunk_windows([intro, heading, continuation], max_chars=110)

    assert windows == [[intro], [heading, continuation]]


def test_no_backtrack_when_overflowing_chunk_starts_a_section():
    """A heading chunk already breaks cleanly — the previous window is kept full."""
    body = "a" * 60
    first = "## One\n" + "b" * 30
    second = "## Two\n" + "c" * 30

    windows = chunk_windows([body, first, second], max_chars=100)

    assert windows == [[body, first], [second]]


def test_section_larger_than_budget_splits_by_budget():
    """A section that cannot fit one window splits instead of looping or dropping."""
    heading = "## Big\n" + "a" * 50
    cont_one = "b" * 50
    cont_two = "c" * 50

    windows = chunk_windows([heading, cont_one, cont_two], max_chars=110)

    assert windows == [[heading, cont_one], [cont_two]]


def test_backtracked_section_still_splits_when_larger_than_budget():
    intro = "a" * 40
    heading = "## S\n" + "b" * 50
    big_cont = "c" * 80

    windows = chunk_windows([intro, heading, big_cont], max_chars=100)

    assert windows == [[intro], [heading], [big_cont]]


def test_oversized_single_chunk_gets_its_own_window():
    chunks = ["small", "X" * 500, "tail"]

    windows = chunk_windows(chunks, max_chars=100)

    assert windows == [["small"], ["X" * 500], ["tail"]]


def test_empty_input_returns_no_windows():
    assert chunk_windows([], max_chars=10) == []


def test_rejects_nonpositive_budget():
    with pytest.raises(ValueError, match="max_chars"):
        chunk_windows(["x"], max_chars=0)
