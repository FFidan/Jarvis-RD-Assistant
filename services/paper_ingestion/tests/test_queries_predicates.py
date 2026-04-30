"""Smoke tests for state-based SQL predicate fragments (queries/predicates.py)."""

from __future__ import annotations

from paper_ingestion.queries.predicates import (
    PULSE_CANDIDATE_EXCLUDE_SQL,
    RECOMMENDER_EXCLUDE_SQL,
    VIEW_PREDICATES,
)


def test_view_predicates_has_ten_named_views() -> None:
    """The VIEW_PREDICATES dict must expose exactly the 10 spec §6 named views."""
    assert set(VIEW_PREDICATES) == {
        "inbox",
        "library",
        "reading_list",
        "reading",
        "done",
        "starred",
        "trash",
        "active",
        "kept",
        "all_non_trash",
    }


def test_view_predicates_use_state_or_starred_column() -> None:
    """Every VIEW_PREDICATES entry must reference pus.state or pus.starred."""
    for name, sql in VIEW_PREDICATES.items():
        assert ("pus.state" in sql) or ("pus.starred" in sql), (
            f"VIEW_PREDICATES[{name!r}] does not reference pus.state or pus.starred"
        )


def test_view_predicates_inbox_uses_coalesce() -> None:
    """Inbox view must default missing user_state rows to 'inbox'."""
    assert VIEW_PREDICATES["inbox"] == "COALESCE(pus.state, 'inbox') = 'inbox'"


def test_view_predicates_library_includes_three_states() -> None:
    """Library view spans to_read / reading / done per spec §5.4."""
    library_sql = VIEW_PREDICATES["library"]
    assert "to_read" in library_sql
    assert "reading" in library_sql
    assert "done" in library_sql


def test_view_predicates_starred_excludes_trash() -> None:
    """Starred view must exclude trashed papers per spec §2.4."""
    starred_sql = VIEW_PREDICATES["starred"]
    assert "pus.starred = TRUE" in starred_sql
    assert "trash" in starred_sql  # exclusion clause


def test_view_predicates_trash_does_not_use_coalesce() -> None:
    """Trash view targets state='trash' directly; COALESCE is unnecessary."""
    assert VIEW_PREDICATES["trash"] == "pus.state = 'trash'"


def test_recommender_exclude_sql_matches_spec() -> None:
    """Spec §7.3.1 — papers in trash or done are excluded from recommender output."""
    assert RECOMMENDER_EXCLUDE_SQL == "COALESCE(pus.state, 'inbox') IN ('trash','done')"


def test_pulse_candidate_exclude_sql_matches_spec() -> None:
    """Spec §6 + §7.3.1 — pulse candidate filter excludes trash and done."""
    assert PULSE_CANDIDATE_EXCLUDE_SQL == "COALESCE(pus.state, 'inbox') IN ('trash','done')"


def test_legacy_predicates_no_longer_importable() -> None:
    """Legacy IS_ARCHIVED_SQL et al. must be deleted (spec §11 atomic cutover)."""
    import paper_ingestion.queries.predicates as predicates_mod

    assert not hasattr(predicates_mod, "IS_ARCHIVED_SQL")
    assert not hasattr(predicates_mod, "IS_NOT_ARCHIVED_SQL")
    assert not hasattr(predicates_mod, "IS_DISMISSED_SQL")
    assert not hasattr(predicates_mod, "IS_SAVED_SQL")
