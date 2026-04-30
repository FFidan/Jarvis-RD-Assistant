"""Reusable SQL predicate fragments for paper_user_state filtering.

These are SQL string constants intended for safe interpolation into
queries built around the `paper_user_state pus` alias. All fragments
assume the alias is `pus` and are NULL-safe via three-valued logic
— important for LEFT JOINs where ``pus.*`` is NULL when the user has
never interacted with the paper.
"""

# ``IS NOT DISTINCT FROM 'archived'`` evaluates FALSE (not NULL) when
# pus.status is NULL — without this, the legacy-status branch would
# poison the OR with a NULL and ``NOT IS_ARCHIVED_SQL`` would filter
# out every never-touched paper from ``unread_only`` feeds.
IS_ARCHIVED_SQL = "(COALESCE(pus.archived, FALSE) OR pus.status IS NOT DISTINCT FROM 'archived')"
IS_NOT_ARCHIVED_SQL = f"(NOT {IS_ARCHIVED_SQL})"
IS_DISMISSED_SQL = "COALESCE(pus.dismissed, FALSE)"
IS_SAVED_SQL = "COALESCE(pus.saved, FALSE)"
