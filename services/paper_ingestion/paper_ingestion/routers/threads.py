"""`thread` entity CRUD + auto-seed producers (UI_v3 My-Day § Open threads).

A ``thread`` is a user's resumable mid-flight line of work (spec
internal design spec (archived), §4.1).
Open-question 2 (RESOLVED 2026-05-15) requires both a manual create path AND
two auto-seed producers:

* ``POST /api/my-day/threads/seed/pomodoro`` — an interrupted Pomodoro session
  becomes a resumable thread.
* ``POST /api/my-day/threads/seed/eod`` — the EOD "make this a thread" action
  (spec §3.10) turns a blocker into a resumable Open Thread.

Every endpoint is strictly user-scoped via ``current_user_id_strict`` — no
cross-user read or write is possible (consistent with the RBAC model).

Note: ``from __future__ import annotations`` is intentionally absent — see
``routers/my_day.py`` for the verified PydanticUserError trace. Body
annotations on Pydantic models must remain concrete.
"""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common.auth import current_user_id_strict

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models.thread import (
    ThreadCreate,
    ThreadResponse,
    ThreadSeedResponse,
    ThreadUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/my-day", tags=["my-day"])

_ROW_COLS = "id, title, anchor, progress, last_at, status, created_at"

# Explicit allowlist of columns that PATCH /threads/{id} may write.
# Mirrors the _NOTE_ALLOWED_COLUMNS pattern in routers/notes.py — only
# code-defined names reach the UPDATE statement; user-supplied keys that are
# not in this set are silently dropped by model_dump(include=…).
_THREAD_ALLOWED_COLUMNS: set[str] = {"title", "anchor", "progress", "status"}


def _to_response(row: asyncpg.Record) -> ThreadResponse:
    return ThreadResponse(
        id=row["id"],
        title=row["title"],
        anchor=row["anchor"],
        progress=row["progress"],
        last_at=row["last_at"],
        status=row["status"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# GET /api/my-day/threads — list (feeds § Open threads + hero "thread" mode)
# ---------------------------------------------------------------------------


@router.get("/threads", response_model=list[ThreadResponse])
@limiter.limit("60/minute")
async def list_threads(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[ThreadResponse]:
    """List the caller's open threads, most-recently-touched first.

    Only ``status = 'open'`` rows are returned (§ Open threads is hidden when no
    threads; done/archived threads are excluded from the My-Day surface). The
    smart-hero "thread" mode consumes ``[0]`` (most recently touched).
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_ROW_COLS} FROM thread "  # noqa: S608 (static column list)
            "WHERE user_id = $1 AND status = 'open' "
            "ORDER BY last_at DESC",
            user_id,
        )
    return [_to_response(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/my-day/threads/{thread_id}
# ---------------------------------------------------------------------------


@router.get("/threads/{thread_id}", response_model=ThreadResponse)
@limiter.limit("60/minute")
async def get_thread(
    request: Request,
    thread_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> ThreadResponse:
    """Fetch a single thread the caller owns (404 otherwise — no leak)."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_ROW_COLS} FROM thread "  # noqa: S608
            "WHERE id = $1 AND user_id = $2",
            thread_id,
            user_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return _to_response(row)


# ---------------------------------------------------------------------------
# POST /api/my-day/threads — manual create path
# ---------------------------------------------------------------------------


@router.post("/threads", response_model=ThreadResponse, status_code=201)
@limiter.limit("30/minute")
async def create_thread(
    request: Request,
    body: ThreadCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> ThreadResponse:
    """Create a user-owned thread (the manual create path of §4.1)."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO thread (user_id, title, anchor, progress) "
            "VALUES ($1, $2, $3, $4) "
            f"RETURNING {_ROW_COLS}",  # noqa: S608
            user_id,
            body.title,
            body.anchor,
            body.progress,
        )
    return _to_response(row)


# ---------------------------------------------------------------------------
# PATCH /api/my-day/threads/{thread_id} — update-progress / status / fields
# ---------------------------------------------------------------------------


@router.patch("/threads/{thread_id}", response_model=ThreadResponse)
@limiter.limit("60/minute")
async def update_thread(
    request: Request,
    thread_id: int,
    body: ThreadUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> ThreadResponse:
    """Partial update. Any write also bumps ``last_at`` (it is activity).

    Used by the prototype's update-progress affordance and "mark done"
    (status='done'). User-scoped: a non-owned id is a 404, never an update.
    """
    fields = body.model_dump(exclude_unset=True, include=_THREAD_ALLOWED_COLUMNS)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses: list[str] = []
    params: list[object] = []
    for idx, (col, val) in enumerate(fields.items(), start=1):
        set_clauses.append(f'"{col}" = ${idx}')
        params.append(val)
    set_clauses.append("last_at = NOW()")
    params.append(thread_id)
    params.append(user_id)
    id_pos = len(params) - 1
    uid_pos = len(params)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE thread SET {', '.join(set_clauses)} "  # noqa: S608 (columns are code-defined via _THREAD_ALLOWED_COLUMNS)
            f"WHERE id = ${id_pos} AND user_id = ${uid_pos} "
            f"RETURNING {_ROW_COLS}",
            *params,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return _to_response(row)


# ---------------------------------------------------------------------------
# POST /api/my-day/threads/{thread_id}/resume — touch + return
# ---------------------------------------------------------------------------


@router.post("/threads/{thread_id}/resume", response_model=ThreadResponse)
@limiter.limit("60/minute")
async def resume_thread(
    request: Request,
    thread_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> ThreadResponse:
    """The prototype's ``resume →`` action: bump ``last_at`` and return.

    Resuming a thread re-floats it to the top of § Open threads / the hero.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE thread SET last_at = NOW() "
            "WHERE id = $1 AND user_id = $2 AND status = 'open' "
            f"RETURNING {_ROW_COLS}",  # noqa: S608
            thread_id,
            user_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return _to_response(row)


# ---------------------------------------------------------------------------
# Auto-seed producer 1 — interrupted Pomodoro session → thread
# ---------------------------------------------------------------------------


@router.post("/threads/seed/pomodoro", response_model=ThreadSeedResponse, status_code=201)
@limiter.limit("30/minute")
async def seed_thread_from_pomodoro(
    request: Request,
    body: ThreadCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> ThreadSeedResponse:
    """Auto-seed a thread from an interrupted Pomodoro session (§4.1, OQ-2).

    De-duplicated: if an open thread with the same title already exists for the
    caller it is touched (``last_at`` bumped, ``progress`` advanced if higher)
    rather than duplicated — an interrupted-then-resumed-then-interrupted loop
    must not spawn N threads.
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                f"SELECT {_ROW_COLS} FROM thread "  # noqa: S608
                "WHERE user_id = $1 AND status = 'open' AND title = $2 "
                "ORDER BY last_at DESC LIMIT 1 FOR UPDATE",
                user_id,
                body.title,
            )
            if existing is not None:
                row = await conn.fetchrow(
                    "UPDATE thread SET last_at = NOW(), "
                    "progress = GREATEST(progress, $2) "
                    f"WHERE id = $1 RETURNING {_ROW_COLS}",  # noqa: S608
                    existing["id"],
                    body.progress,
                )
                return ThreadSeedResponse(thread=_to_response(row), created=False)
            row = await conn.fetchrow(
                "INSERT INTO thread (user_id, title, anchor, progress) "
                "VALUES ($1, $2, $3, $4) "
                f"RETURNING {_ROW_COLS}",  # noqa: S608
                user_id,
                body.title,
                body.anchor,
                body.progress,
            )
    return ThreadSeedResponse(thread=_to_response(row), created=True)


# ---------------------------------------------------------------------------
# Auto-seed producer 2 — EOD "make this a thread" → thread
# ---------------------------------------------------------------------------


@router.post("/threads/seed/eod", response_model=ThreadSeedResponse, status_code=201)
@limiter.limit("30/minute")
async def seed_thread_from_eod(
    request: Request,
    body: ThreadCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> ThreadSeedResponse:
    """Auto-seed from the EOD "make this a thread" action (spec §3.10 / §4.3).

    A blocker line from the shutdown ritual becomes a resumable Open Thread
    instead of a dead journal line. De-duplicated on title the same way as the
    Pomodoro producer (the same blocker carried across two EODs must not
    duplicate).
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                f"SELECT {_ROW_COLS} FROM thread "  # noqa: S608
                "WHERE user_id = $1 AND status = 'open' AND title = $2 "
                "ORDER BY last_at DESC LIMIT 1 FOR UPDATE",
                user_id,
                body.title,
            )
            if existing is not None:
                row = await conn.fetchrow(
                    f"UPDATE thread SET last_at = NOW() WHERE id = $1 RETURNING {_ROW_COLS}",  # noqa: S608
                    existing["id"],
                )
                return ThreadSeedResponse(thread=_to_response(row), created=False)
            row = await conn.fetchrow(
                "INSERT INTO thread (user_id, title, anchor, progress) "
                "VALUES ($1, $2, $3, $4) "
                f"RETURNING {_ROW_COLS}",  # noqa: S608
                user_id,
                body.title,
                body.anchor,
                body.progress,
            )
    return ThreadSeedResponse(thread=_to_response(row), created=True)
