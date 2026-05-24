"""Unit tests for build_feed_queries predicate-composition logic.

BUG-FEED-1: when both unread_only=True AND an explicit view= are passed,
only the view predicate must appear in the WHERE clause.  The active-state
guard must be silenced because the view already encodes its own state predicate.
"""

from __future__ import annotations

import re

import pytest

from paper_ingestion.queries.predicates import VIEW_PREDICATES
from paper_ingestion.services.feed_query import build_feed_queries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ACTIVE_PRED = VIEW_PREDICATES["active"]
_DONE_PRED = VIEW_PREDICATES["done"]

# Minimal keyword args common to all calls below.
_DEFAULTS: dict = dict(
    sort="date",
    limit=20,
    offset=0,
    q=None,
    statuses=None,
    source_types=None,
    topic_names=None,
    date_from=None,
    date_to=None,
    include_zotero_notes=False,
)


# ---------------------------------------------------------------------------
# BUG-FEED-1: unread_only + explicit view must not produce contradictory WHERE
# ---------------------------------------------------------------------------


def test_unread_only_with_view_done_excludes_active_predicate() -> None:
    """unread_only=True is ignored when view='done' — active pred must be absent."""
    parts = build_feed_queries(unread_only=True, view="done", **_DEFAULTS)
    sql = parts.count_query

    active_found = re.search(re.escape(_ACTIVE_PRED), sql)
    done_found = re.search(re.escape(_DONE_PRED), sql)

    # The done predicate must appear …
    assert done_found is not None, "view='done' predicate missing from query"
    # … and the active predicate must be absent to avoid contradictory rows = 0.
    assert active_found is None, "active predicate must not coexist with explicit view='done'"


def test_unread_only_with_view_done_no_contradiction() -> None:
    """Neither active+done together in the same WHERE — regression guard."""
    parts = build_feed_queries(unread_only=True, view="done", **_DEFAULTS)
    sql = parts.count_query

    both_present = re.search(re.escape(_ACTIVE_PRED), sql) and re.search(re.escape(_DONE_PRED), sql)
    assert not both_present, "contradictory view predicates must not coexist together"


@pytest.mark.parametrize(
    "view_name",
    ["inbox", "reading_list", "reading", "done", "starred", "trash", "active", "kept"],
)
def test_unread_only_with_any_explicit_view_suppresses_active_guard(view_name: str) -> None:
    """active-state guard is suppressed for every recognised explicit view."""
    parts = build_feed_queries(unread_only=True, view=view_name, **_DEFAULTS)
    sql = parts.count_query

    view_pred = VIEW_PREDICATES[view_name]
    # view predicate present
    assert re.search(re.escape(view_pred), sql), f"view='{view_name}' predicate missing from query"
    # active guard absent (unless the view itself *is* 'active')
    if view_name != "active":
        assert not re.search(re.escape(_ACTIVE_PRED), sql), (
            f"active predicate must not appear alongside view='{view_name}'"
        )


# ---------------------------------------------------------------------------
# Positive path: unread_only without view still applies the active guard
# ---------------------------------------------------------------------------


def test_unread_only_without_view_applies_active_predicate() -> None:
    """unread_only=True with no view must include the active-state guard."""
    parts = build_feed_queries(unread_only=True, view=None, **_DEFAULTS)
    sql = parts.count_query

    assert re.search(re.escape(_ACTIVE_PRED), sql), (
        "active predicate must appear when unread_only=True and no view is set"
    )


def test_neither_flag_produces_no_view_predicate() -> None:
    """With unread_only=False and view=None no state predicate is appended."""
    parts = build_feed_queries(unread_only=False, view=None, **_DEFAULTS)
    sql = parts.count_query

    assert not re.search(re.escape(_ACTIVE_PRED), sql), (
        "active predicate must be absent when unread_only=False and view=None"
    )
