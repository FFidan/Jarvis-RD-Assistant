"""Per-user library helpers (Sprint B canonical-corpus refactor).

A user's "library" is the set of papers they have explicitly accepted into
their working set. Membership is recorded in the ``user_library`` table
introduced by migration 072. Engagement (reading-list state) implies
membership via the migration backfill, but ongoing writes go through the
helpers below so attribution (``added_via``) stays accurate.

Acceptable ``added_via`` values mirror the CHECK constraint on
``user_library.added_via`` (see ``db/migrations/072_canonical_corpus.sql``).
The validation is duplicated here as a defence-in-depth so misuse fails fast
instead of bouncing off a Postgres constraint with a less-helpful message.
"""

from __future__ import annotations

from typing import Final

import asyncpg

# Public type alias for accepted asyncpg connection objects (mirrors
# ``jarvis_common.paper_state``).
_Conn = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy  # type: ignore[type-arg]
DbLike = _Conn | asyncpg.Pool


# Mirrors the CHECK constraint on ``user_library.added_via``.
ALLOWED_ADDED_VIA: Final[frozenset[str]] = frozenset(
    {
        "manual_save",
        "batch_save",
        "zotero_pull",
        "pulse_acceptance",
        "auto_fetch_topic_match",
        "backfill_engagement",
        "backfill_legacy_user_id",
        "topic_discovery",
        "citation_graph",
    }
)


async def _execute(db: DbLike, sql: str, *args: object) -> str:
    """Run ``conn.execute`` against either a Pool or a Connection."""
    if isinstance(db, asyncpg.Pool):
        return await db.execute(sql, *args)
    return await db.execute(sql, *args)


async def _fetch(db: DbLike, sql: str, *args: object) -> list[asyncpg.Record]:
    """Run ``conn.fetch`` against either a Pool or a Connection."""
    if isinstance(db, asyncpg.Pool):
        async with db.acquire() as conn:
            return await conn.fetch(sql, *args)
    return await db.fetch(sql, *args)


async def add_to_library(
    db: DbLike,
    *,
    user_id: int,
    paper_id: int,
    added_via: str,
) -> None:
    """Idempotent insert into ``user_library``.

    Parameters
    ----------
    db:
        An asyncpg ``Pool`` or ``Connection``-like object. Passing the
        in-flight transaction's connection ensures the library write
        participates in the caller's transaction (recommended).
    user_id:
        The library owner.
    paper_id:
        The canonical paper to add.
    added_via:
        Provenance tag — must be one of ``ALLOWED_ADDED_VIA``. Mismatch
        raises ``ValueError`` rather than letting the DB constraint fire so
        the error pinpoints the call site.

    Notes
    -----
    Conflicts (same ``(user_id, paper_id)`` already present) are silently
    ignored — the original ``added_via`` and ``added_at`` are preserved.
    Re-classification is not supported here; if you need it, write SQL.
    """
    if added_via not in ALLOWED_ADDED_VIA:
        raise ValueError(
            f"add_to_library: invalid added_via={added_via!r}; allowed: {sorted(ALLOWED_ADDED_VIA)}"
        )
    await _execute(
        db,
        """INSERT INTO user_library (user_id, paper_id, added_via)
           VALUES ($1, $2, $3)
           ON CONFLICT (user_id, paper_id) DO NOTHING""",
        user_id,
        paper_id,
        added_via,
    )


async def list_users_with_topic(
    db: DbLike,
    *,
    topic_id: int,
) -> list[int]:
    """Return user_ids who should receive papers tagged with ``topic_id``.

    Today the ``topics`` table has no per-user column — topics are global,
    inherited from the single-tenant origin of the codebase. Until that's
    rebuilt as a per-user concept, this helper returns *all active users*
    so the auto-fetch fan-out fans the new paper into every user's
    library. When topics become per-user (a future migration), narrow this
    query to the join.

    The ``topic_id`` parameter is currently unused but kept in the signature
    so call sites don't need to change when the topic-user wiring lands.

    Returns ``[]`` if the ``users`` table doesn't exist (single-tenant
    pre-multi-user deployments) — caller should treat as "no fan-out".
    """
    _ = topic_id  # see docstring
    try:
        rows = await _fetch(
            db,
            "SELECT id FROM users WHERE deleted_at IS NULL ORDER BY id ASC",
        )
    except asyncpg.exceptions.UndefinedTableError:
        return []
    return [int(r["id"]) for r in rows]


async def fan_out_to_topic_users(
    db: DbLike,
    *,
    paper_id: int,
    topic_ids: list[int],
) -> int:
    """Add ``paper_id`` to every user subscribed to any of ``topic_ids``.

    Used by ``run_auto_pipeline`` after upserting a canonical paper that
    matched one or more topics. Returns the count of *distinct* users that
    received a library entry (idempotent: existing rows count as zero).

    Implementation detail: we collect the user-set across topics first to
    avoid issuing one INSERT per (topic, user) pair when many topics share
    subscribers.
    """
    if not topic_ids:
        return 0

    user_set: set[int] = set()
    for topic_id in topic_ids:
        user_set.update(await list_users_with_topic(db, topic_id=topic_id))

    if not user_set:
        return 0

    # Bulk INSERT via VALUES expansion — one round-trip rather than N.
    # We rely on ON CONFLICT DO NOTHING for idempotency.
    placeholders = ", ".join(
        f"(${i + 1}, ${len(user_set) + 1}, 'auto_fetch_topic_match')" for i in range(len(user_set))
    )
    sql = (
        "INSERT INTO user_library (user_id, paper_id, added_via) "
        f"VALUES {placeholders} "
        "ON CONFLICT (user_id, paper_id) DO NOTHING"
    )
    args: list[object] = list(user_set)
    args.append(paper_id)
    await _execute(db, sql, *args)
    return len(user_set)
