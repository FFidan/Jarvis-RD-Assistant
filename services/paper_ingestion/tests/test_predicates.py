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
    """Default alias is ``p`` and the param index is interpolated verbatim."""
    assert paper_visible_sql(2) == "(p.discovered_by IS NULL OR p.discovered_by = $2)"


def test_paper_visible_sql_custom_alias_and_index():
    """A custom alias (e.g. the unaliased table name) and index are honoured."""
    assert (
        paper_visible_sql(3, alias="papers")
        == "(papers.discovered_by IS NULL OR papers.discovered_by = $3)"
    )


def test_paper_visible_sql_matches_kg_inline_fragment():
    """Emitted SQL is byte-identical to the fragment KG queries used inline."""
    assert paper_visible_sql(2) == "(p.discovered_by IS NULL OR p.discovered_by = $2)"
    assert paper_visible_sql(1) == "(p.discovered_by IS NULL OR p.discovered_by = $1)"


def test_view_predicates_library_unchanged():
    """The library view predicate is the shape pulse/profile reuses by import."""
    assert (
        VIEW_PREDICATES["library"] == "COALESCE(pus.state, 'inbox') IN ('to_read','reading','done')"
    )
