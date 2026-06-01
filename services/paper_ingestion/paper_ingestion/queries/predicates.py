"""State-based SQL predicate fragments for paper_user_state filtering.

All fragments assume the alias is `pus`. Fragments use COALESCE so that
papers without a paper_user_state row (LEFT JOIN NULL) are treated as
state='inbox' — the freshly-discovered default per spec §6.
"""

# Per-view predicates (spec §6). Used by routers/feed.py, list_papers,
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

# Recommender exclusion (spec §7.3.1): papers in trash or done are never
# recommended again. The 60-day negative-feedback exclusion lives in
# recommender.py to avoid coupling this constant to the
# recommendation_feedback table.
RECOMMENDER_EXCLUDE_SQL = "COALESCE(pus.state, 'inbox') IN ('trash','done')"

# Pulse candidate filter (spec §6 + §7.3.1): same as RECOMMENDER_EXCLUDE_SQL
# today, kept as a separate name in case Pulse and Recommender diverge.
PULSE_CANDIDATE_EXCLUDE_SQL = "COALESCE(pus.state, 'inbox') IN ('trash','done')"
