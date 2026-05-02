"""Tests for row_to_feed_paper converter — W1.7-A regression coverage.

Ensures that state and state_before_trash are forwarded from the SQL row
rather than silently falling back to Pydantic defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime

from paper_ingestion.converters import row_to_feed_paper

# ---------------------------------------------------------------------------
# Minimal row builder
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)

_BASE_ROW: dict = {
    "id": 1,
    "external_id": "ext-001",
    "source_type": "arxiv",
    "title": "Test Paper",
    "authors": ["Alice", "Bob"],
    "abstract": "An abstract.",
    "published_date": _NOW.date(),
    "url": "https://example.com/paper",
    "pdf_url": None,
    "pdf_local_path": None,
    "pdf_downloaded": False,
    "citation_count": 0,
    "metadata": {},
    "created_at": _NOW,
}


def _row(**overrides) -> dict:
    """Return a copy of _BASE_ROW with any overrides applied."""
    return {**_BASE_ROW, **overrides}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_row_to_feed_paper_emits_state_when_present():
    """row with state='reading' → FeedPaper.state == 'reading'."""
    row = _row(state="reading")
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.state == "reading"


def test_row_to_feed_paper_emits_state_before_trash():
    """row with state='trash', state_before_trash='reading' → fields preserved."""
    row = _row(state="trash", state_before_trash="reading")
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.state == "trash"
    assert result.state_before_trash == "reading"


def test_row_to_feed_paper_defaults_when_state_keys_missing():
    """Row missing both state keys (legacy papers-only fetch) → inbox defaults."""
    row = _row()  # no state / state_before_trash keys
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.state == "inbox"
    assert result.state_before_trash is None
