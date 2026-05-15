"""Admin-only reader for the ``audit_log`` table (WS-ADMIN-AUDIT).

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

from paper_ingestion.deps import get_db_pool, limiter

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
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if before_id is not None:
        conditions.append(f"id < ${idx}")
        params.append(before_id)
        idx += 1

    if action_prefix:
        escaped = action_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(rf"action LIKE ${idx} ESCAPE '\'")
        params.append(escaped + "%")
        idx += 1

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit + 1)

    sql = f"""
        SELECT id, user_id, action, resource, metadata, created_at
        FROM audit_log
        {where_clause}
        ORDER BY id DESC
        LIMIT ${idx}
    """

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    entries = [_row_to_dict(r) for r in rows[:limit]]
    next_before_id: int | None = None
    if len(rows) > limit:
        next_before_id = entries[-1]["id"]

    return {"entries": entries, "next_before_id": next_before_id}


__all__ = ["router"]
