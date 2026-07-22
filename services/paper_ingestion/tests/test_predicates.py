"""Unit tests for queries.predicates SQL fragment builders.

These guard the hoisted, behavior-preserving fragments:
* ``EXCLUDED_STATE_SQL`` — collapsed recommender/pulse exclusion constant.
* ``paper_visible_sql`` — KG/citation visibility fragment.
"""

from __future__ import annotations

from paper_ingestion.queries.predicates import (
    EXCLUDED_STATE_SQL,
    VIEW_PREDICATES,
    paper_visible_sql,
)


def test_excluded_state_sql_value():
    """The collapsed exclude constant is the trash/done state filter."""
    assert EXCLUDED_STATE_SQL == "COALESCE(pus.state, 'inbox') IN ('trash','done')"


def test_paper_visible_sql_default_alias():
    """The service wrapper preserves the shared builder's default alias."""
    from jarvis_common.paper_visibility import paper_visibility_sql

    assert paper_visible_sql(2) == paper_visibility_sql(2)


def test_paper_visible_sql_custom_alias_and_index():
    """The service wrapper forwards a trusted alias and placeholder index."""
    from jarvis_common.paper_visibility import paper_visibility_sql

    assert paper_visible_sql(3, alias="papers") == paper_visibility_sql(3, alias="papers")


def test_paper_visible_sql_matches_kg_inline_fragment():
    """Every caller receives the same policy with only its placeholder changed."""
    assert paper_visible_sql(2).replace("$2", "$1") == paper_visible_sql(1)


def test_view_predicates_library_unchanged():
    """The library view predicate is the shape pulse/profile reuses by import."""
    assert (
        VIEW_PREDICATES["library"] == "COALESCE(pus.state, 'inbox') IN ('to_read','reading','done')"
    )
