"""Admin-only reader for the ``audit_log`` table.

Single endpoint: ``GET /api/admin/audit-log`` — cursor-paginated by ``id``
DESC with an optional ``action`` prefix filter. Admin session required
(``require_admin``: API-key-only ops callers are NOT admins).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from jarvis_common.auth import require_admin, verify_api_key
from jarvis_common.db_helpers import escape_like

from platform_api.deps import get_db_pool, limiter

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-audit"],
    dependencies=[Depends(verify_api_key), Depends(require_admin)],
)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an asyncpg Record to a JSON-serialisable dict."""
    d = dict(row)
    created = d.get("created_at")
    if created is not None:
        d["created_at"] = created.isoformat() if isinstance(created, datetime) else str(created)
    return d


def _build_audit_query(
    before_id: int | None,
    action_prefix: str | None,
    limit: int,
) -> tuple[str, list[Any]]:
    """Return a parameterized ``(sql, params)`` pair for the audit_log cursor query.

    Placeholder numbers are always ``len(params)`` at insertion time — never
    derived from user input.  User values travel exclusively in ``params``.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if before_id is not None:
        params.append(before_id)
        conditions.append(f"id < ${len(params)}")

    if action_prefix:
        escaped = escape_like(action_prefix)
        params.append(escaped + "%")
        conditions.append(rf"action LIKE ${len(params)} ESCAPE '\'")

    params.append(limit + 1)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = (
        'SELECT id, user_id, action, resource, metadata, "timestamp" AS created_at '
        "FROM audit_log "
        + (where + " " if where else "")
        + "ORDER BY id DESC LIMIT $"
        + str(len(params))
    )
    return sql, params


@router.get("/audit-log")
@limiter.limit("60/minute")
async def list_audit_log(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, ge=1),
    action_prefix: str | None = Query(None, max_length=128),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, Any]:
    """Return up to *limit* audit_log rows ordered by id DESC.

    Cursor pagination: pass ``before_id=<last id>`` to fetch the next
    (older) page. Optional ``action_prefix`` filters on ``action LIKE
    prefix||'%'`` (prefix is escaped so ``%``/``_`` are treated literally).
    """
    sql, params = _build_audit_query(before_id, action_prefix, limit)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    entries = [_row_to_dict(r) for r in rows[:limit]]
    next_before_id: int | None = None
    if len(rows) > limit:
        next_before_id = entries[-1]["id"]

    return {"entries": entries, "next_before_id": next_before_id}


__all__ = ["router"]
