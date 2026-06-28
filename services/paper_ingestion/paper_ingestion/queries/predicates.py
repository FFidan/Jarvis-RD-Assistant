"""State-based SQL predicate fragments for paper_user_state filtering.

All fragments assume the alias is `pus`. Fragments use COALESCE so that
papers without a paper_user_state row (LEFT JOIN NULL) are treated as
state='inbox' — the freshly-discovered default.
"""

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
    """SQL fragment: a paper row is visible to the bound user_id.

    Emits ``(<alias>.discovered_by IS NULL OR <alias>.discovered_by = $N)`` —
    i.e. unattributed stubs plus papers the caller discovered. The user_library
    membership branch is composed at each call site (it varies by surrounding
    query shape).

    Both ``param_index`` (a literal int) and ``alias`` (a SQL identifier) are
    caller-controlled, never user input — f-string interpolation is safe.
    """
    return f"({alias}.discovered_by IS NULL OR {alias}.discovered_by = ${param_index})"
