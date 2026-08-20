"""Learning-owned commands consumed from signed Research and Platform requests."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from learning_engine.deps import get_db_pool
from learning_engine.repos.domain_commands import apply_command

router = APIRouter(prefix="/internal/domains", tags=["internal", "domains"])
type DatabasePool = Annotated[asyncpg.Pool, Depends(get_db_pool)]


class PaperCommand(BaseModel):
    """Reliable paper projection or cleanup command."""

    request_id: uuid.UUID
    user_id: int = Field(gt=0)
    paper_id: int = Field(gt=0)


class ProjectCollectionCommand(BaseModel):
    """Research request to persist one Learning-owned Zotero collection key."""

    request_id: uuid.UUID
    user_id: int = Field(gt=0)
    zotero_collection_key: str = Field(min_length=1, max_length=128)


class JournalCommand(BaseModel):
    """Research request to persist a Learning-owned journal entry."""

    request_id: uuid.UUID
    user_id: int = Field(gt=0)
    date: date
    prompts: dict[str, str | int | float | bool | None]


class ErasureCommand(BaseModel):
    """Platform command to clean Learning rows for one disabled account."""

    user_id: int = Field(gt=0)


def _require_principal(request: Request, expected: Literal["research", "platform"]) -> None:
    if getattr(request.state, "identity_principal", None) != expected:
        raise HTTPException(status_code=403, detail="Domain command is forbidden")


def _require_subject(request: Request, user_id: int) -> None:
    """Require the signed assertion to name exactly the command subject."""
    if getattr(request.state, "user_id", None) != user_id:
        raise HTTPException(status_code=403, detail="Domain command subject is forbidden")


def _require_request_id(request: Request, request_id: uuid.UUID) -> None:
    """Bind the command idempotency key to the verified assertion request."""
    if request.headers.get("X-Request-Id") != str(request_id):
        raise HTTPException(status_code=403, detail="Domain command request is forbidden")


@router.post("/paper-read")
async def project_paper_read(
    body: PaperCommand, request: Request, db_pool: DatabasePool
) -> dict[str, bool]:
    """Increment Learning activity once for a durable Research event."""
    _require_principal(request, "research")
    _require_subject(request, body.user_id)
    _require_request_id(request, body.request_id)
    applied = await apply_command(
        db_pool,
        command_type="paper.read",
        request_id=str(body.request_id),
        user_id=body.user_id,
        payload={"paper_id": body.paper_id},
    )
    return {"acknowledged": True, "applied": applied}


@router.post("/paper-deleted")
async def clean_deleted_paper(
    body: PaperCommand, request: Request, db_pool: DatabasePool
) -> dict[str, bool]:
    """Remove Learning-owned paper dependents once and acknowledge cleanup."""
    _require_principal(request, "research")
    _require_subject(request, body.user_id)
    _require_request_id(request, body.request_id)
    applied = await apply_command(
        db_pool,
        command_type="paper.deleted",
        request_id=str(body.request_id),
        user_id=body.user_id,
        payload={"paper_id": body.paper_id},
    )
    return {"acknowledged": True, "applied": applied}


@router.put("/projects/{project_id}/zotero-collection")
async def set_project_collection(
    project_id: int, body: ProjectCollectionCommand, request: Request, db_pool: DatabasePool
) -> dict[str, bool]:
    """Persist one exact Research-requested collection key in Learning."""
    _require_principal(request, "research")
    _require_subject(request, body.user_id)
    _require_request_id(request, body.request_id)
    applied = await apply_command(
        db_pool,
        command_type="project.zotero_collection",
        request_id=str(body.request_id),
        user_id=body.user_id,
        payload={"project_id": project_id, "zotero_collection_key": body.zotero_collection_key},
    )
    return {"acknowledged": True, "applied": applied}


@router.put("/journal")
async def upsert_journal(
    body: JournalCommand, request: Request, db_pool: DatabasePool
) -> dict[str, object]:
    """Persist a journal entry through its Learning owner boundary."""
    _require_principal(request, "research")
    _require_subject(request, body.user_id)
    _require_request_id(request, body.request_id)
    applied = await apply_command(
        db_pool,
        command_type="journal.upsert",
        request_id=str(body.request_id),
        user_id=body.user_id,
        payload={"date": body.date.isoformat(), "prompts": body.prompts},
    )
    async with db_pool.acquire() as conn:
        entry = await conn.fetchrow(
            """SELECT id, date, prompts, created_at, updated_at FROM journal_entries
               WHERE user_id = $1 AND date = $2""",
            body.user_id,
            body.date,
        )
    if entry is None:
        raise HTTPException(status_code=503, detail="Journal command is unavailable")
    return {"acknowledged": True, "applied": applied, "entry": dict(entry)}


@router.post("/erasure/{request_id}")
async def erase_user(
    request_id: uuid.UUID, body: ErasureCommand, request: Request, db_pool: DatabasePool
) -> dict[str, bool]:
    """Perform idempotent Learning cleanup for a Platform erasure request."""
    _require_principal(request, "platform")
    _require_subject(request, body.user_id)
    _require_request_id(request, request_id)
    applied = await apply_command(
        db_pool,
        command_type="user.erase",
        request_id=str(request_id),
        user_id=body.user_id,
        payload={},
    )
    return {"acknowledged": True, "applied": applied}


__all__ = ["router"]
