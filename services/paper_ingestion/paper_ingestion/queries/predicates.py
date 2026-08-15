"""State-based SQL predicate fragments for paper_user_state filtering.

All fragments assume the alias is `pus`. Fragments use COALESCE so that
papers without a paper_user_state row (LEFT JOIN NULL) are treated as
state='inbox' — the freshly-discovered default.
"""

from jarvis_common.paper_visibility import paper_visibility_sql

# Per-view predicates. Used by routers/feed.py, list_papers,
# get_feed_counts, and other surface-bound queries.
VIEW_PREDICATES: dict[str, str] = {
    "inbox": "COALESCE(pus.state, 'inbox') = 'inbox'",
    "library": "COALESCE(pus.state, 'inbox') IN ('to_read','reading','done')",
    "reading_list": "COALESCE(pus.state, 'inbox') = 'to_read'",
    "reading": "COALESCE(pus.state, 'inbox') = 'reading'",
    "done": "COALESCE(pus.state, 'inbox') = 'done'",
    "starred": "pus.starred = TRUE AND COALESCE(pus.state, 'inbox') != 'trash'",
    "trash": "pus.state = 'trash'",
    "active": "COALESCE(pus.state, 'inbox') IN ('inbox','to_read','reading')",
    "kept": "COALESCE(pus.state, 'inbox') IN ('to_read','reading','done')",
    "all_non_trash": "COALESCE(pus.state, 'inbox') != 'trash'",
}

# Recommender + Pulse exclusion: papers in trash or done
# are never recommended again. The 60-day negative-feedback exclusion lives
# in recommender.py to avoid coupling this constant to the
# recommendation_feedback table.
EXCLUDED_STATE_SQL = "COALESCE(pus.state, 'inbox') IN ('trash','done')"


def paper_visible_sql(param_index: int, alias: str = "p") -> str:
    """Return the centralized paper-visibility predicate for service SQL.

    Parameters
    ----------
    param_index : int
        One-based PostgreSQL placeholder index for the caller's user ID.
    alias : str
        Trusted alias for the `papers` relation.

    Returns
    -------
    str
        Persisted public scope or explicit caller-library membership.
    """
    return paper_visibility_sql(param_index, alias=alias)


def source_types_sql(first_param_index: int, count: int, alias: str = "p") -> str:
    """Return the source-type predicate used by feed queries.

    Parameters
    ----------
    first_param_index : int
        One-based index of the first source-type binding.
    count : int
        Number of consecutive source-type bindings.
    alias : str
        Trusted alias for the ``papers`` relation.

    Returns
    -------
    str
        Source-type membership predicate.

    Raises
    ------
    ValueError
        If ``count`` is less than one.
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    placeholders = ", ".join(f"${first_param_index + offset}" for offset in range(count))
    return f"{alias}.source_type IN ({placeholders})"


def paper_topic_id_sql(param_index: int, alias: str = "p") -> str:
    """Return the exact-topic predicate used by feed queries.

    Parameters
    ----------
    param_index : int
        One-based PostgreSQL placeholder index for the topic ID.
    alias : str
        Trusted alias for the ``papers`` relation.

    Returns
    -------
    str
        Predicate requiring a paper-to-topic link.
    """
    return (
        f"{alias}.id IN (SELECT pt.paper_id FROM paper_topics pt "
        f"WHERE pt.topic_id = ${param_index})"
    )


def paper_untagged_sql(alias: str = "p") -> str:
    """Return the no-topic predicate used by feed queries.

    Parameters
    ----------
    alias : str
        Trusted alias for the ``papers`` relation.

    Returns
    -------
    str
        Predicate requiring no paper-to-topic link.
    """
    return f"NOT EXISTS (SELECT 1 FROM paper_topics pt WHERE pt.paper_id = {alias}.id)"
