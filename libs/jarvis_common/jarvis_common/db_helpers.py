"""Shared database helper functions for dynamic UPDATE and DELETE operations."""

import json
import logging
import re
from typing import Any

import asyncpg
from fastapi import HTTPException

_logger = logging.getLogger(__name__)

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


def quote_ident(name: str) -> str:
    """Quote a PostgreSQL identifier per SQL standard (double-quote + escape embedded quotes)."""
    if "\x00" in name:
        raise ValueError("Identifier contains null byte")
    return '"' + name.replace('"', '""') + '"'


async def init_pg_connection(conn: asyncpg.Connection) -> None:
    """Register JSON/JSONB codec so asyncpg returns dicts, not strings."""
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def fmt_safe(s: str) -> str:
    """Escape curly braces in user content before passing to str.format()."""
    return str(s).replace("{", "{{").replace("}", "}}")


_ALIAS_MODELS = frozenset({"smart", "fast", "embed"})
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")


def validated_model(model: str) -> str:
    """Return *model* only if it is a LiteLLM alias, else ``'smart'``.

    LLM calls go through LiteLLM which only accepts its configured aliases.
    The user_config table stores raw Ollama model names for display in the
    Settings UI, but actual API calls must use the alias.
    """
    if model in _ALIAS_MODELS:
        return model
    return "smart"


async def get_smart_model(conn) -> str:
    """Return the LiteLLM alias for the smart model role.

    The user_config ``llm.smart_model`` value stores the Ollama model name
    for display purposes only.  Actual LLM calls always use the ``'smart'``
    alias which LiteLLM routes to the configured Ollama model.
    """
    return "smart"


async def get_fast_model(conn) -> str:
    """Return the LiteLLM alias for the fast model role."""
    return "fast"


async def get_embed_model(conn) -> str:
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
        Subset of *allowed_columns* whose values need ``::jsonb`` cast and
        ``json.dumps`` serialisation.
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
    HTTPException(400)
        If *updates* contains a key not in *allowed_columns*.
    """
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
            params.append(json.dumps(value))
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
