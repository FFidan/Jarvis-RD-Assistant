"""Research-owned Platform erasure command boundary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import assert_paper_ownership
from jarvis_common.library import add_to_library
from pydantic import BaseModel, Field

from paper_ingestion.deps import get_db_pool
from paper_ingestion.jobs.data_purge import _purge_qdrant_for_user
from paper_ingestion.services.paper_content_reclaim import erase_orphaned_user_papers

router = APIRouter(prefix="/internal/domains", tags=["internal", "domains"])
type DatabasePool = Annotated[asyncpg.Pool, Depends(get_db_pool)]


class ErasureRequest(BaseModel):
    """Platform request for Research-owned user cleanup and Qdrant proof."""

    user_id: int = Field(gt=0)


class LibraryCommand(BaseModel):
    """Learning request for one Research-owned library membership."""

    request_id: uuid.UUID
    user_id: int = Field(gt=0)
    paper_id: int = Field(gt=0)


def _require_erasure_subject(request: Request, user_id: int) -> None:
    """Require the exact Platform principal and signed account subject."""
    if (
        getattr(request.state, "identity_principal", None) != "platform"
        or getattr(request.state, "user_id", None) != user_id
    ):
        raise HTTPException(status_code=403, detail="Erasure command is forbidden")


def _require_library_subject(request: Request, body: LibraryCommand) -> None:
    """Bind a Learning library command to its signed subject and request ID."""
    if (
        getattr(request.state, "identity_principal", None) != "learning"
        or getattr(request.state, "user_id", None) != body.user_id
        or request.headers.get("X-Request-Id") != str(body.request_id)
    ):
        raise HTTPException(status_code=403, detail="Library command is forbidden")


@router.post("/library")
async def add_library_membership(
    body: LibraryCommand, request: Request, db_pool: DatabasePool
) -> dict[str, bool]:
    """Add one visible paper to the signed user's Research library."""
    _require_library_subject(request, body)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await assert_paper_ownership(conn, body.paper_id, body.user_id)
            await add_to_library(
                conn,
                user_id=body.user_id,
                paper_id=body.paper_id,
                added_via="manual_save",
            )
    return {"acknowledged": True}


@router.post("/erasure/{request_id}/qdrant")
async def erase_user_vectors(
    request_id: uuid.UUID, body: ErasureRequest, request: Request, db_pool: DatabasePool
) -> dict[str, object]:
    """Remove or redact user-owned vectors and return the zero-residual proof."""
    _require_erasure_subject(request, body.user_id)
    async with db_pool.acquire() as conn:
        protected_rows = await conn.fetch(
            """
            SELECT p.id AS paper_id FROM papers AS p
            WHERE p.visibility_scope = 'public' OR EXISTS (
                SELECT 1 FROM user_library AS library
                WHERE library.paper_id = p.id AND library.user_id <> $1
            )
            """,
            body.user_id,
        )
    qdrant = getattr(request.app.state, "qdrant_client", None)
    if qdrant is None:
        raise HTTPException(status_code=503, detail="Research erasure is temporarily unavailable")
    protected_paper_ids = [int(row["paper_id"]) for row in protected_rows]
    counts = await _purge_qdrant_for_user(qdrant, body.user_id, protected_paper_ids)
    if counts.residual_points:
        raise HTTPException(status_code=503, detail="Research erasure is temporarily unavailable")
    return {
        "request_id": str(request_id),
        "receipt": {
            "request_id": str(request_id),
            "user_id": body.user_id,
            "collection": "paper_chunks",
            "filter_fingerprint": sha256(
                f"user={body.user_id};protected={protected_paper_ids}".encode()
            ).hexdigest(),
            "deleted_points": counts.deleted,
            "redacted_points": counts.redacted,
            "residual_points": counts.residual_points,
            "scan_completed_at": datetime.now(UTC).isoformat(),
            "acknowledged_at": datetime.now(UTC).isoformat(),
        },
    }


@router.post("/erasure/{request_id}/research")
async def erase_user_research_data(
    request_id: uuid.UUID, body: ErasureRequest, request: Request, db_pool: DatabasePool
) -> dict[str, object]:
    """Run the fixed Research-owned relational erasure capability.

    Papers the account was the only holder of are removed first, together with
    their chunks and stored files. The order is load-bearing: the set is
    derived from library membership, which the capability below deletes.
    """
    _require_erasure_subject(request, body.user_id)
    async with db_pool.acquire() as conn:
        await erase_orphaned_user_papers(conn, body.user_id)
        await conn.execute("SELECT research.erase_user_data($1)", body.user_id)
    return {"request_id": str(request_id), "acknowledged": True}


__all__ = ["router"]
