"""Shared paper_user_state DB helpers for JARVIS microservices.

Consolidates all ON CONFLICT variants for the ``paper_user_state`` table so
that each call site only specifies *what* it wants, not *how* the SQL is
built.

Variants (``on_conflict`` parameter)
-------------------------------------
``update_dynamic``
    Build INSERT columns dynamically from non-None kwargs, then DO UPDATE SET
    only those same columns.  Used by the bulk-action and per-state endpoints
    that write exactly the fields they supply.

``update_starred_only``
    ``ON CONFLICT DO UPDATE SET starred = TRUE``.  Used by the star-paper
    endpoint which must always clobber starred to TRUE regardless of prior
    value.  Returns ``(is_new_row, prev_starred)`` via a RETURNING clause so
    the caller can detect a genuine off→on transition.

``update_partial``
    ``ON CONFLICT DO UPDATE SET rating=COALESCE($3,…), user_notes=COALESCE(…),
    flagged=COALESCE(…)``.  Partial update: NULL kwargs preserve the stored
    value on conflict.  Returns the full state row so the caller can respond
    with the current persisted values.

``do_nothing``
    ``ON CONFLICT DO NOTHING``.  First-sync-wins: initial INSERT for a paper
    never overwrites existing user state (used by Zotero sync).

``update_state_when_inbox_or_to_read``
    ``ON CONFLICT DO UPDATE SET state = <value> WHERE paper_user_state.state
    IN ('inbox', 'to_read')``.  Advances state only when the paper hasn't
    been progressed further (used by focus-session logging in learning_engine).
"""

from __future__ import annotations

from typing import Any, Literal

import asyncpg
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

OnConflictVariant = Literal[
    "update_dynamic",
    "update_starred_only",
    "update_partial",
    "do_nothing",
    "update_state_when_inbox_or_to_read",
]

# Type alias for asyncpg connection objects accepted by these helpers.
_Conn = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy  # type: ignore[type-arg]


# ---------------------------------------------------------------------------
# upsert_paper_user_state — multi-variant upsert
# ---------------------------------------------------------------------------


async def upsert_paper_user_state(
    conn: _Conn,
    paper_id: int,
    user_id: int | None,
    *,
    state: str | None = None,
    starred: bool | None = None,
    rating: int | None = None,
    user_notes: str | None = None,
    flagged: bool | None = None,
    on_conflict: OnConflictVariant = "update_dynamic",
) -> Any:
    """Upsert a ``paper_user_state`` row using the requested ON CONFLICT variant.

    Parameters
    ----------
    conn:
        An open asyncpg connection or pool-proxy object.
    paper_id:
        PK of the paper being updated.
    user_id:
        User owning the state row; ``None`` for single-tenant configurations.
    state:
        New state string (e.g. ``'reading'``, ``'done'``).  Meaning depends on
        the variant — see module docstring.
    starred:
        Boolean flag; meaning depends on variant.
    rating:
        Integer rating 1–5 (``update_partial`` variant only).
    user_notes:
        Free-text notes (``update_partial`` variant only).
    flagged:
        Boolean flag (``update_partial`` variant only).
    on_conflict:
        Which SQL variant to execute.  See :data:`OnConflictVariant`.

    Returns
    -------
    ``None`` for write-only variants (``update_dynamic``, ``do_nothing``,
    ``update_state_when_inbox_or_to_read``).

    For ``update_starred_only``: an asyncpg ``Record`` with keys
    ``is_new_row`` (bool) and ``prev_starred`` (bool).

    For ``update_partial``: an asyncpg ``Record`` with keys ``state``,
    ``state_before_trash``, ``starred``, ``rating``, ``user_notes``,
    ``flagged``, ``updated_at``.

    """
    if on_conflict == "update_dynamic":
        return await _upsert_dynamic(conn, paper_id, user_id, state=state, starred=starred)

    if on_conflict == "update_starred_only":
        return await _upsert_starred_only(conn, paper_id, user_id)

    if on_conflict == "update_partial":
        return await _upsert_partial(
            conn, paper_id, user_id, rating=rating, user_notes=user_notes, flagged=flagged
        )

    if on_conflict == "do_nothing":
        return await _upsert_do_nothing(conn, paper_id, user_id, state=state, starred=starred)

    if on_conflict == "update_state_when_inbox_or_to_read":
        if state is None:
            raise ValueError("state must be provided for update_state_when_inbox_or_to_read")
        return await _upsert_state_conditional(conn, paper_id, user_id, state=state)

    # Exhaustive check — should never reach here with a typed caller.
    raise ValueError(f"Unknown on_conflict variant: {on_conflict!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Private variant implementations
# ---------------------------------------------------------------------------


async def _upsert_dynamic(
    conn: _Conn,
    paper_id: int,
    user_id: int | None,
    *,
    state: str | None = None,
    starred: bool | None = None,
) -> None:
    """Build INSERT dynamically; DO UPDATE SET only supplied columns.

    This preserves the existing value for any column left as None.
    """
    if state is None and starred is None:
        return
    cols = ["paper_id", "user_id"]
    placeholders = ["$1", "$2"]
    values: list[object] = [paper_id, user_id]
    updates: list[str] = []
    if state is not None:
        cols.append("state")
        placeholders.append(f"${len(values) + 1}")
        values.append(state)
        updates.append(f"state = ${len(values)}")
    if starred is not None:
        cols.append("starred")
        placeholders.append(f"${len(values) + 1}")
        values.append(starred)
        updates.append(f"starred = ${len(values)}")
    sql = (
        f"INSERT INTO paper_user_state ({', '.join(cols)}) "  # noqa: S608
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (paper_id, user_id) DO UPDATE SET {', '.join(updates)}"
    )
    await conn.execute(sql, *values)


async def _upsert_starred_only(
    conn: _Conn,
    paper_id: int,
    user_id: int | None,
) -> Any:
    """INSERT with starred=TRUE; DO UPDATE SET starred = TRUE.

    Uses a CTE pre-snapshot to detect the off→on transition without a
    TOCTOU-prone pre-flight SELECT.

    Returns an asyncpg Record with ``is_new_row`` and ``prev_starred``.
    """
    return await conn.fetchrow(
        """
        WITH before AS (
            SELECT starred FROM paper_user_state
            WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2
        )
        INSERT INTO paper_user_state (paper_id, user_id, starred)
        VALUES ($1, $2, TRUE)
        ON CONFLICT (paper_id, user_id) DO UPDATE
            SET starred = TRUE
        RETURNING
            (xmax = 0) AS is_new_row,
            (SELECT COALESCE(starred, FALSE) FROM before) AS prev_starred
        """,
        paper_id,
        user_id,
    )


async def _upsert_partial(
    conn: _Conn,
    paper_id: int,
    user_id: int | None,
    *,
    rating: int | None,
    user_notes: str | None,
    flagged: bool | None,
) -> Any:
    """COALESCE-based partial upsert for rating, user_notes, flagged.

    NULL kwargs preserve the stored value on conflict.  Returns the full
    persisted row (state, state_before_trash, starred, rating, user_notes,
    flagged, updated_at).
    """
    return await conn.fetchrow(
        """INSERT INTO paper_user_state
               (paper_id, user_id, rating, user_notes, flagged)
           VALUES ($1, $2, $3, $4, COALESCE($5, FALSE))
           ON CONFLICT (paper_id, user_id) DO UPDATE SET
               rating     = COALESCE($3, paper_user_state.rating),
               user_notes = COALESCE($4, paper_user_state.user_notes),
               flagged    = COALESCE($5, paper_user_state.flagged)
           RETURNING
               COALESCE(state, 'inbox') AS state,
               state_before_trash,
               COALESCE(starred, FALSE) AS starred,
               rating, user_notes,
               COALESCE(flagged, FALSE) AS flagged,
               updated_at""",
        paper_id,
        user_id,
        rating,
        user_notes,
        flagged,
    )


async def _upsert_do_nothing(
    conn: _Conn,
    paper_id: int,
    user_id: int | None,
    *,
    state: str | None = None,
    starred: bool | None = None,
) -> None:
    """First-sync-wins: INSERT row; silently skip on conflict.

    Existing user state is never overwritten — the user may have already
    trashed or otherwise progressed the paper.
    """
    state_val = state if state is not None else "inbox"
    starred_val = starred if starred is not None else False
    await conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, starred)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (paper_id, user_id) DO NOTHING""",
        paper_id,
        user_id,
        state_val,
        starred_val,
    )


async def _upsert_state_conditional(
    conn: _Conn,
    paper_id: int,
    user_id: int | None,
    *,
    state: str,
) -> None:
    """INSERT state; DO UPDATE SET state = <value> only when still 'inbox' or 'to_read'.

    Advances the paper's state when it hasn't been progressed further.
    If the paper is already in 'reading', 'done', 'trash' etc. the UPDATE
    is skipped (WHERE clause excludes those rows).
    """
    await conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state)
           VALUES ($1, $2, $3)
           ON CONFLICT (paper_id, user_id) DO UPDATE
              SET state = $3
            WHERE paper_user_state.state IN ('inbox', 'to_read')""",
        paper_id,
        user_id,
        state,
    )


# ---------------------------------------------------------------------------
# trash_paper — atomic move to trash
# ---------------------------------------------------------------------------


async def trash_paper(
    conn: _Conn,
    paper_id: int,
    user_id: int | None,
) -> None:
    """Atomic move to Trash: ``state_before_trash := state; state := 'trash'``.

    For a paper without a ``paper_user_state`` row, the INSERT branch
    initialises ``state_before_trash`` to ``'inbox'`` (the implicit default
    per the application state machine). For an existing row, the UPDATE preserves the prior
    state into ``state_before_trash`` so the restore endpoint can return
    the paper to where it came from.

    **Idempotent on re-trash**: when the row is already in ``'trash'``, the
    CASE expression keeps the existing ``state_before_trash`` value unchanged,
    avoiding a CHECK-constraint violation (``state_before_trash`` cannot be
    ``'trash'`` per the schema).
    """
    await conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, state_before_trash)
           VALUES ($1, $2, 'trash', 'inbox')
           ON CONFLICT (paper_id, user_id) DO UPDATE
             SET state_before_trash = CASE
                     WHEN paper_user_state.state = 'trash' THEN paper_user_state.state_before_trash
                     ELSE paper_user_state.state
                 END,
                 state = 'trash'""",
        paper_id,
        user_id,
    )


# ---------------------------------------------------------------------------
# restore_paper — restore from trash
# ---------------------------------------------------------------------------


async def restore_paper(
    conn: Any,
    paper_id: int,
    user_id: int | None,
) -> None:
    """Restore a paper from Trash: ``state := COALESCE(state_before_trash, 'inbox')``.

    Also clears ``state_before_trash`` so the field only carries meaning while
    the paper is in trash.

    Raises
    ------
    HTTPException(404)
        If no row was updated — paper not found or not in trash for this caller.

    """
    status = await conn.execute(
        """UPDATE paper_user_state
              SET state = COALESCE(state_before_trash, 'inbox'),
                  state_before_trash = NULL
            WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2""",
        paper_id,
        user_id,
    )
    # asyncpg returns e.g. "UPDATE 1" — extract the row count.
    updated = int(status.split()[-1]) if status else 0
    if updated == 0:
        raise HTTPException(status_code=404, detail="Paper not found or not in trash")


# ---------------------------------------------------------------------------
# assert_paper_in_states — precondition guard
# ---------------------------------------------------------------------------


async def assert_paper_in_states(
    conn: _Conn,
    paper_id: int,
    user_id: int | None,
    *,
    allowed: tuple[str, ...],
) -> None:
    """Raise HTTP 409 if the paper's current state is not in ``allowed``.

    Treats missing rows as ``'inbox'`` (the implicit default).
    """
    current = (
        await conn.fetchval(
            """SELECT COALESCE(state, 'inbox') FROM paper_user_state
           WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2""",
            paper_id,
            user_id,
        )
        or "inbox"
    )
    if current not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Paper must be in one of {sorted(allowed)}; currently '{current}'",
        )
