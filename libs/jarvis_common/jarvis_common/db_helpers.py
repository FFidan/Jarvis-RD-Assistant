"""Shared database helper functions for dynamic UPDATE and DELETE operations."""

import json
import logging
import re
from typing import Any

import asyncpg
from fastapi import HTTPException

logger = logging.getLogger(__name__)

_ALLOWED_TABLES = frozenset(
    {
        "projects",
        "tasks",
        "milestones",
        "decks",
        "cards",
        "paper_sources",
        "scheduled_nudges",
        "user_config",
        "topics",
        "paper_notes",
        "tracked_authors",
    }
)

# Whitelist for extra_sets fragments: only col = NOW(), col = NULL, or col = $N
# are accepted. Anything else (subqueries, arbitrary expressions, string literals)
# is rejected to prevent SQL-injection-adjacent misuse.
_EXTRA_SET_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*\s*=\s*(NOW\(\)|NULL|\$\d+)$")


def quote_ident(name: str) -> str:
    """Quote a PostgreSQL identifier per SQL standard (double-quote + escape embedded quotes)."""
    if "\x00" in name:
        raise ValueError("Identifier contains null byte")
    return '"' + name.replace('"', '""') + '"'


def escape_like(q: str) -> str:
    """Escape user-supplied LIKE/ILIKE pattern metacharacters.

    Escapes ``\\``, ``%``, and ``_`` so that the string is treated as a
    literal value rather than a wildcard pattern.  Use together with the
    ``ESCAPE '\\'`` clause in SQL:

    .. code-block:: sql

        WHERE col ILIKE '%' || $1 || '%' ESCAPE '\\'

    Parameters
    ----------
    q:
        Raw user-supplied search term.

    Returns
    -------
    str
        The input with LIKE metacharacters escaped.
    """
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def init_pg_connection(conn: asyncpg.Connection) -> None:
    """Register JSON/JSONB codec so asyncpg returns dicts, not strings."""
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def fmt_safe(s: str) -> str:
    """Escape curly braces in user content before passing to str.format()."""
    return str(s).replace("{", "{{").replace("}", "}}")


_ALIAS_MODELS = frozenset({"smart", "fast", "embed"})


def validated_model(model: str) -> str:
    """Return *model* only if it is a LiteLLM alias, else ``'smart'``.

    LLM calls go through LiteLLM which only accepts its configured aliases.
    The user_config table stores raw Ollama model names for display in the
    Settings UI, but actual API calls must use the alias.

    See Also
    --------
    validated_model_with_reason : Returns ``(alias, fallback_reason)`` so callers
        can surface the original model name (e.g. via ``X-LLM-Fallback`` header).
    """
    alias, _ = validated_model_with_reason(model)
    return alias


def validated_model_with_reason(model: str) -> tuple[str, str | None]:
    """Return ``(alias, fallback_reason)`` where *fallback_reason* is None on success.

    Callers that need to surface the original model name — e.g. to set an
    ``X-LLM-Fallback: <original>`` response header — should use this function
    instead of :func:`validated_model`.

    Parameters
    ----------
    model:
        The model identifier from user config or request payload.

    Returns
    -------
    tuple[str, str | None]
        ``(resolved_alias, fallback_reason)`` — *fallback_reason* is ``None``
        when *model* was already a valid alias, or a human-readable message
        when a fallback was applied.
    """
    if model in _ALIAS_MODELS:
        return model, None
    reason = f"model {model!r} is not a valid LiteLLM alias; fell back to 'smart'"
    logger.warning("Ignoring invalid model %r; falling back to 'smart'", model)
    return "smart", reason


def get_smart_model() -> str:
    """Return the LiteLLM alias for the smart model role.

    The user_config ``llm.smart_model`` value stores the Ollama model name
    for display purposes only.  Actual LLM calls always use the ``'smart'``
    alias which LiteLLM routes to the configured Ollama model.
    """
    return "smart"


def get_fast_model() -> str:
    """Return the LiteLLM alias for the fast model role."""
    return "fast"


def get_embed_model() -> str:
    """Return the LiteLLM alias for the embedding model role."""
    return "embed"


async def dynamic_update(
    db_pool_or_conn: Any,
    table: str,
    record_id: int,
    updates: dict[str, Any],
    allowed_columns: frozenset[str],
    jsonb_columns: frozenset[str] = frozenset(),
    extra_sets: list[str] | None = None,
) -> Any:
    """Build and execute a dynamic UPDATE query.

    Parameters
    ----------
    db_pool_or_conn:
        An ``asyncpg.Pool`` or ``asyncpg.Connection`` — both expose ``.fetchrow()``.
    table:
        Target table name (must be a safe identifier — not user-supplied).
    record_id:
        Primary key value (``WHERE id = $1``).
    updates:
        Column→value mapping. Callers should pre-filter with
        ``model_dump(exclude_unset=True, include=ALLOWED)``, but this function
        also validates against *allowed_columns* as a safety net.
    allowed_columns:
        Whitelist of column names that may appear in *updates*.
    jsonb_columns:
        Subset of *allowed_columns* whose values need ``::jsonb`` cast.
        asyncpg's global JSONB codec handles serialisation automatically —
        do NOT call ``json.dumps`` here; that would double-encode.
    extra_sets:
        Literal SQL fragments appended to the SET clause, e.g.
        ``["updated_at = NOW()", "completed_at = NULL"]``.
        The caller is responsible for including ``updated_at = NOW()`` when the
        table has that column — this function does **not** add it automatically.
        Must be literal SQL fragments from trusted code only — never user-supplied.

    Returns
    -------
    asyncpg.Record | None
        The updated row (``RETURNING *``), or ``None`` if the id was not found
        (should not happen if the caller already checked existence).

    Raises
    ------
    ValueError
        If *updates* contains the ``"id"`` key (primary key must not be mutated).
    HTTPException(400)
        If *updates* contains a key not in *allowed_columns*.
    """
    if "id" in updates:
        raise ValueError(
            "'id' column cannot be updated via dynamic_update — use a dedicated SQL statement"
        )

    if extra_sets is not None:
        if not all(isinstance(s, str) for s in extra_sets):
            bad_types = [type(s).__name__ for s in extra_sets]
            raise TypeError(f"dynamic_update extra_sets must all be str, got {bad_types}")
        bad_frags = [s for s in extra_sets if not _EXTRA_SET_RE.match(s)]
        if bad_frags:
            raise ValueError(
                f"dynamic_update extra_sets must match {_EXTRA_SET_RE.pattern!r}; "
                f"got disallowed fragments: {bad_frags}"
            )

    if not updates and not extra_sets:
        raise ValueError("No updates to apply")

    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Table {table!r} not in allowed list")

    # Safety-net validation
    bad = updates.keys() - allowed_columns
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid field(s): {', '.join(sorted(bad))}",
        )

    sets: list[str] = []
    params: list[Any] = [record_id]  # $1 is always the record id
    idx = 2  # next positional parameter

    for col, value in updates.items():
        quoted_col = quote_ident(col)
        if col in jsonb_columns:
            sets.append(f"{quoted_col} = ${idx}::jsonb")
            params.append(value)  # asyncpg JSONB codec handles serialisation
        else:
            sets.append(f"{quoted_col} = ${idx}")
            params.append(value)
        idx += 1

    if extra_sets:
        sets.extend(extra_sets)

    row = await db_pool_or_conn.fetchrow(
        f"UPDATE {quote_ident(table)} SET {', '.join(sets)} WHERE id = $1 RETURNING *",  # nosec B608 - table/columns are identifier-quoted and values remain parameterized
        *params,
    )
    return row


async def assert_paper_ownership(
    conn: asyncpg.Connection,
    paper_id: int,
    user_id: int | None,
    *,
    multitenant_enabled: bool = False,
) -> None:
    """Raise HTTPException if the caller does not own the paper.

    Sprint B canonical-corpus semantics
    ------------------------------------
    Papers are global (canonical corpus). Ownership = library membership.
    Rules:

    * Single-user mode (``user_id=None``): all papers are accessible — no check.
    * Multi-user mode (``user_id`` is set):
      - Paper not found → 404.
      - Paper present AND row exists in ``user_library`` for the caller → allowed.
      - Paper present BUT NOT in caller's ``user_library`` → 403.

    Parameters
    ----------
    conn:
        An open ``asyncpg.Connection`` (not a pool — caller must acquire).
    paper_id:
        The paper primary key to check.
    user_id:
        The caller's user ID from ``current_user_id_or_none()``.
        ``None`` means single-user mode; all access is allowed.
    multitenant_enabled:
        When ``True`` (multi-tenant deployment), papers with
        ``discovered_by IS NULL`` are *not* auto-granted — the caller must
        have an explicit ``user_library`` membership.  Defaults to ``False``
        to preserve single-tenant / canonical-corpus semantics.
    """
    if user_id is None:
        # Single-user mode: skip ownership check entirely.
        return

    row = await conn.fetchrow(
        "SELECT discovered_by FROM papers WHERE id = $1",
        paper_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="paper not found")

    # Sprint B: papers are canonical. Discovered-by-system (NULL) papers
    # remain freely accessible in single-tenant mode (canonical-corpus
    # semantics).  In multi-tenant mode, NULL discovered_by is NOT a free
    # pass — the caller still needs library membership.
    # Defensive: tolerate fixtures that still expose the legacy ``user_id``
    # key while production rows ship with ``discovered_by``.
    discovered_by: int | None
    try:
        discovered_by = row["discovered_by"]
    except (KeyError, IndexError):
        try:
            discovered_by = row["user_id"]
        except (KeyError, IndexError):
            discovered_by = None

    # Fast-grant: same owner, OR NULL discovered_by in single-tenant mode.
    if str(discovered_by) == str(user_id) or (discovered_by is None and not multitenant_enabled):
        return

    in_library = await conn.fetchval(
        "SELECT 1 FROM user_library WHERE paper_id = $1 AND user_id = $2",
        paper_id,
        user_id,
    )
    if in_library is None:
        raise HTTPException(status_code=403, detail="paper not owned by current user")


async def assert_papers_ownership(
    conn: asyncpg.Connection,
    paper_ids: list[int],
    user_id: int | None,
    *,
    multitenant_enabled: bool = False,
) -> None:
    """Raise HTTPException if the caller lacks access to any paper in *paper_ids*.

    Single-user/API-key-only mode keeps the legacy permissive behavior. In
    browser-authenticated mode this checks the whole batch at once so public
    batch job routes cannot enqueue work for another user's library entries.

    Parameters
    ----------
    multitenant_enabled:
        When ``True``, papers with ``discovered_by IS NULL`` are not
        auto-granted and require an explicit ``user_library`` entry.
    """
    if user_id is None or not paper_ids:
        return

    unique_ids = sorted(set(paper_ids))
    rows = await conn.fetch(
        "SELECT id, discovered_by FROM papers WHERE id = ANY($1::int[])",
        unique_ids,
    )
    by_id = {int(row["id"]): row for row in rows}
    missing = [paper_id for paper_id in unique_ids if paper_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"paper not found: {missing[0]}")

    candidate_ids: list[int] = []
    for paper_id in unique_ids:
        discovered_by = by_id[paper_id]["discovered_by"]
        if str(discovered_by) == str(user_id) or (
            discovered_by is None and not multitenant_enabled
        ):
            continue
        candidate_ids.append(paper_id)

    if not candidate_ids:
        return

    owned_rows = await conn.fetch(
        "SELECT paper_id FROM user_library WHERE user_id = $1 AND paper_id = ANY($2::int[])",
        user_id,
        candidate_ids,
    )
    owned = {int(row["paper_id"]) for row in owned_rows}
    for paper_id in candidate_ids:
        if paper_id not in owned:
            raise HTTPException(status_code=403, detail="paper not owned by current user")


async def delete_or_404(
    db_pool_or_conn: Any,
    sql: str,
    *params: Any,
    detail: str = "Not found",
) -> None:
    """Execute a DELETE statement and raise 404 if no rows were affected.

    Parameters
    ----------
    db_pool_or_conn:
        An ``asyncpg.Pool`` or ``asyncpg.Connection``.
    sql:
        The DELETE SQL statement (e.g. ``DELETE FROM foo WHERE id = $1``).
    *params:
        Positional bind parameters for the query.
    detail:
        Error message for the 404 response.
    """
    result = await db_pool_or_conn.execute(sql, *params)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=detail)
