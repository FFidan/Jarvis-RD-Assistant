"""Shared database helper functions for dynamic UPDATE and DELETE operations."""

import json
import logging
import re
import time
from typing import Any, Literal

import asyncpg
from fastapi import HTTPException

from jarvis_common.paper_visibility import paper_visibility_sql

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
        "thread",
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
    r"""Escape user-supplied LIKE/ILIKE pattern metacharacters.

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


_CLOUD_MODEL_PREFIXES = ("anthropic/", "openai/", "gemini/")

# Cloud providers accept huge contexts, but budgeting prompt input beyond this
# is cost-unbounded and unnecessary for per-paper prompts. This is a COST
# CEILING, not a model limit: a cloud model whose catalog context exceeds 32768
# is capped here so a single paper prompt cannot silently bill for a 200k-token
# input. Per-paper text rarely approaches even this; raising it trades money for
# marginal coverage on outlier papers.
_CLOUD_INPUT_TOKEN_CEILING = 32768

# Staleness window for the per-role effective-context cache. The budget callers
# query this once per LLM call, so without a cache a multi-stage summary would
# hit the DB on every window. 30s trades a brief staleness window after a
# num_ctx delivery (a in-flight generation may use the prior value for up to one
# TTL) against the per-call query cost; deliveries also call
# invalidate_effective_num_ctx_cache() to collapse that window to zero on the
# happy path, so the TTL only matters for cross-process / missed-invalidation
# cases.
_EFFECTIVE_NUM_CTX_TTL_SECONDS = 30.0

_catalog_context_tokens_by_id: dict[str, int] | None = None


def _catalog_context_tokens(model_id: str) -> int | None:
    global _catalog_context_tokens_by_id
    if _catalog_context_tokens_by_id is None:
        from jarvis_common.model_catalog import load_model_catalog  # noqa: PLC0415

        _catalog_context_tokens_by_id = {
            entry.id: entry.context_tokens for entry in load_model_catalog()
        }
    return _catalog_context_tokens_by_id.get(model_id)


class _EffectiveNumCtxCache:
    """Short-TTL per-role cache so budget callers don't query per LLM call."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, int]] = {}

    def get_cached(self, role: str, now: float) -> int | None:
        entry = self._entries.get(role)
        if entry is not None and now - entry[0] < _EFFECTIVE_NUM_CTX_TTL_SECONDS:
            return entry[1]
        return None

    def set(self, role: str, now: float, value: int) -> None:
        self._entries[role] = (now, value)

    def clear(self) -> None:
        self._entries.clear()


_effective_num_ctx_cache = _EffectiveNumCtxCache()


def invalidate_effective_num_ctx_cache() -> None:
    """Drop cached effective-context values (call after a num_ctx delivery)."""
    _effective_num_ctx_cache.clear()


def _fallback_num_ctx(role: str) -> int:
    from jarvis_common.settings import get_core_settings  # noqa: PLC0415

    settings = get_core_settings()
    return settings.llm_smart_num_ctx if role == "smart" else settings.llm_fast_num_ctx


async def effective_num_ctx(db: Any, role: Literal["smart", "fast"]) -> int:
    """Context window the prompt-input budget for *role* should be computed against.

    Resolution order (system scope — LiteLLM deployments are deployment-global,
    so the budget follows the last delivered value regardless of which machine
    wrote it):

    1. Cloud model assigned (``llm.{role}_model`` has a cloud prefix) — the
       catalog ``context_tokens`` capped at ``_CLOUD_INPUT_TOKEN_CEILING``.
    2. Delivered local context — the system ``llm.{role}_num_ctx`` row written
       on successful LiteLLM num_ctx delivery.
    3. ``CoreSettings.llm_{role}_num_ctx`` (env/boot default). This is also the
       vLLM path: ``--max-model-len`` lives only in compose env with no
       readable in-app surface, so vLLM deployments configure the matching
       ``LLM_{ROLE}_NUM_CTX`` env instead.

    Parameters
    ----------
    db:
        An ``asyncpg.Pool`` or ``asyncpg.Connection`` — anything exposing
        ``.fetch()``. Any read failure falls back to CoreSettings (uncached).
    role:
        ``"smart"`` or ``"fast"``.

    """
    now = time.monotonic()
    cached = _effective_num_ctx_cache.get_cached(role, now)
    if cached is not None:
        logger.debug("effective_num_ctx cache hit (role=%s, value=%d)", role, cached)
        return cached

    model_key = f"llm.{role}_model"
    num_ctx_key = f"llm.{role}_num_ctx"
    try:
        rows = await db.fetch(
            "SELECT key, value FROM user_config WHERE key = ANY($1::text[]) AND user_id IS NULL",
            [model_key, num_ctx_key],
        )
        values = {str(row["key"]): row["value"] for row in rows}
    except Exception:
        logger.warning("Could not read effective num_ctx for role %r", role, exc_info=True)
        return _fallback_num_ctx(role)

    model_id = values.get(model_key)
    if isinstance(model_id, str) and model_id.startswith(_CLOUD_MODEL_PREFIXES):
        tokens = _catalog_context_tokens(model_id) or _CLOUD_INPUT_TOKEN_CEILING
        result = min(tokens, _CLOUD_INPUT_TOKEN_CEILING)
        source = "cloud"
    else:
        delivered = values.get(num_ctx_key)
        if isinstance(delivered, int) and not isinstance(delivered, bool) and delivered > 0:
            result = delivered
            source = "delivered"
        else:
            fallback = _fallback_num_ctx(role)
            logger.debug(
                "effective_num_ctx resolved (role=%s, value=%d, source=fallback)", role, fallback
            )
            return fallback

    logger.debug("effective_num_ctx resolved (role=%s, value=%d, source=%s)", role, result, source)
    _effective_num_ctx_cache.set(role, now, result)
    return result


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


async def dynamic_update(
    db_pool_or_conn: Any,
    table: str,
    record_id: int,
    updates: dict[str, Any],
    allowed_columns: frozenset[str],
    jsonb_columns: frozenset[str] = frozenset(),
    extra_sets: list[str] | None = None,
    extra_where: tuple[str, Any] | None = None,
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
    extra_where:
        Optional ``(column, value)`` pair appended as ``AND col = $N`` to the
        WHERE clause. The column name is identifier-quoted; the value is bound as
        a positional parameter. Default ``None`` ⇒ SQL is identical to the
        pre-existing ``WHERE id = $1`` form (fully backward-compatible).

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

    where_sql = "WHERE id = $1"
    if extra_where is not None:
        col, val = extra_where
        params.append(val)
        where_sql += f" AND {quote_ident(col)} = ${len(params)}"
    row = await db_pool_or_conn.fetchrow(
        f"UPDATE {quote_ident(table)} SET {', '.join(sets)} {where_sql} RETURNING *",  # nosec B608 - identifiers quoted, values parameterized
        *params,
    )
    return row


async def assert_paper_ownership(
    conn: asyncpg.Connection,
    paper_id: int,
    user_id: int | None,
) -> None:
    """Require a paper to be public or present in the caller's library.

    Parameters
    ----------
    conn : asyncpg.Connection
        Open database connection owned by the caller.
    paper_id : int
        The paper primary key to check.
    user_id : int | None
        Authenticated caller ID. ``None`` is reserved for trusted internal or
        compatibility paths that intentionally bypass user authorization.

    Raises
    ------
    fastapi.HTTPException
        With status 404 when the paper is absent, or 403 when it is private and
        not explicitly present in the caller's ``user_library``.

    Notes
    -----
    ``source_type`` and ``discovered_by`` are descriptive/audit fields and never
    grant access.
    """
    if user_id is None:
        return

    visibility_sql = paper_visibility_sql(2, alias="p")
    row = await conn.fetchrow(
        f"SELECT p.id, {visibility_sql} AS is_visible FROM papers p WHERE p.id = $1",
        paper_id,
        user_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="paper not found")
    if not bool(row["is_visible"]):
        raise HTTPException(status_code=403, detail="paper not owned by current user")


async def assert_papers_ownership(
    conn: asyncpg.Connection,
    paper_ids: list[int],
    user_id: int | None,
) -> None:
    """Require every requested paper to satisfy the shared visibility policy.

    Parameters
    ----------
    conn : asyncpg.Connection
        Open database connection owned by the caller.
    paper_ids : list[int]
        Paper primary keys to validate as one batch.
    user_id : int | None
        Authenticated caller ID. ``None`` is reserved for trusted internal or
        compatibility paths that intentionally bypass user authorization.

    Raises
    ------
    fastapi.HTTPException
        With status 404 for the first missing paper, or 403 when any existing
        paper is private and absent from the caller's ``user_library``.
    """
    if user_id is None or not paper_ids:
        return

    unique_ids = sorted(set(paper_ids))
    visibility_sql = paper_visibility_sql(2, alias="p")
    rows = await conn.fetch(
        f"SELECT p.id, {visibility_sql} AS is_visible FROM papers p WHERE p.id = ANY($1::int[])",
        unique_ids,
        user_id,
    )
    by_id = {int(row["id"]): row for row in rows}
    missing = [paper_id for paper_id in unique_ids if paper_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"paper not found: {missing[0]}")

    for paper_id in unique_ids:
        if not bool(by_id[paper_id]["is_visible"]):
            raise HTTPException(status_code=403, detail="paper not owned by current user")


async def record_author_alert(
    conn: asyncpg.Connection,
    *,
    tracked_author_id: int,
    paper_id: int,
    user_id: int,
) -> bool:
    """Insert (tracked_author_id, paper_id, user_id) into author_alert_log.

    Returns True if the row was newly inserted, False if a conflict was skipped.
    """
    row = await conn.fetchrow(
        """INSERT INTO author_alert_log (tracked_author_id, paper_id, user_id)
           VALUES ($1, $2, $3)
           ON CONFLICT (tracked_author_id, paper_id, user_id) DO NOTHING
           RETURNING tracked_author_id""",
        tracked_author_id,
        paper_id,
        user_id,
    )
    return row is not None


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
