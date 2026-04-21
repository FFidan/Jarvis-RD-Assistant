"""Structured extraction endpoints.

Template CRUD, per-paper extraction, batch extraction, and cross-paper
comparison table.
"""

import csv
import io
import json
import logging
from datetime import UTC, datetime

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse
from jarvis_common import jobs as jobs_lib
from starlette.responses import StreamingResponse

from paper_ingestion.deps import (
    get_db_pool,
    get_http_client,
    get_optional_embedder,
    get_optional_verifier,
    limiter,
)
from paper_ingestion.extraction import extract_fields_for_paper
from paper_ingestion.models import (
    BatchExtractionRequest,
    ExtractedField,
    ExtractionField,
    ExtractionRequest,
    ExtractionResponse,
    ExtractionTableRow,
    ExtractionTemplateCreate,
    ExtractionTemplateResponse,
    ExtractionTemplateUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["extractions"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)


# ---------------------------------------------------------------------------
# Template CRUD
# ---------------------------------------------------------------------------


@router.get("/extraction-templates", response_model=list[ExtractionTemplateResponse])
@limiter.limit("60/minute")
async def list_templates(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[ExtractionTemplateResponse]:
    """List all extraction templates."""
    async with db_pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT * FROM extraction_templates ORDER BY is_default DESC, name"
            )
        except asyncpg.exceptions.UndefinedTableError:
            raise HTTPException(
                503, "extraction_templates table not found (migration 011 not applied)"
            )
    return [
        ExtractionTemplateResponse(
            id=r["id"],
            name=r["name"],
            description=r["description"],
            fields=r["fields"],
            is_default=r["is_default"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@router.post("/extraction-templates", response_model=ExtractionTemplateResponse, status_code=201)
@limiter.limit("30/minute")
async def create_template(
    request: Request,
    body: ExtractionTemplateCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> ExtractionTemplateResponse:
    """Create a new extraction template."""
    fields_json = [f.model_dump() for f in body.fields]
    async with db_pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO extraction_templates (name, description, fields, is_default)
                   VALUES ($1, $2, $3::jsonb, $4) RETURNING *""",
                body.name,
                body.description,
                fields_json,
                body.is_default,
            )
        except asyncpg.exceptions.UndefinedTableError:
            raise HTTPException(
                503, "extraction_templates table not found (migration 011 not applied)"
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, f"Template '{body.name}' already exists")
    return ExtractionTemplateResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        fields=row["fields"],
        is_default=row["is_default"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.put("/extraction-templates/{template_id}", response_model=ExtractionTemplateResponse)
@limiter.limit("30/minute")
async def update_template(
    request: Request,
    template_id: int,
    body: ExtractionTemplateUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> ExtractionTemplateResponse:
    """Update an extraction template."""
    async with db_pool.acquire() as conn:
        try:
            existing = await conn.fetchrow(
                "SELECT * FROM extraction_templates WHERE id = $1", template_id
            )
        except asyncpg.exceptions.UndefinedTableError:
            raise HTTPException(
                503, "extraction_templates table not found (migration 011 not applied)"
            )
        if not existing:
            raise HTTPException(404, f"Template {template_id} not found")

        updates = body.model_dump(exclude_unset=True)
        if not updates:
            return ExtractionTemplateResponse(
                id=existing["id"],
                name=existing["name"],
                description=existing["description"],
                fields=existing["fields"],
                is_default=existing["is_default"],
                created_at=existing["created_at"],
                updated_at=existing["updated_at"],
            )

        # Build dynamic update
        set_clauses = ["updated_at = $1"]
        params: list = [datetime.now(UTC)]
        idx = 2

        if "name" in updates:
            set_clauses.append(f"name = ${idx}")
            params.append(updates["name"])
            idx += 1
        if "description" in updates:
            set_clauses.append(f"description = ${idx}")
            params.append(updates["description"])
            idx += 1
        if "fields" in updates and updates["fields"] is not None:
            set_clauses.append(f"fields = ${idx}::jsonb")
            params.append([f.model_dump() for f in body.fields or []])
            idx += 1
        if "is_default" in updates:
            set_clauses.append(f"is_default = ${idx}")
            params.append(updates["is_default"])
            idx += 1

        params.append(template_id)
        query = f"UPDATE extraction_templates SET {', '.join(set_clauses)} WHERE id = ${idx} RETURNING *"  # nosec B608 - updated columns are fixed in code and values stay parameterized
        row = await conn.fetchrow(query, *params)

    return ExtractionTemplateResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        fields=row["fields"],
        is_default=row["is_default"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.delete("/extraction-templates/{template_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_template(
    request: Request,
    template_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Delete an extraction template (cascades to extractions)."""
    async with db_pool.acquire() as conn:
        try:
            result = await conn.execute(
                "DELETE FROM extraction_templates WHERE id = $1", template_id
            )
        except asyncpg.exceptions.UndefinedTableError:
            raise HTTPException(
                503, "extraction_templates table not found (migration 011 not applied)"
            )
        if result == "DELETE 0":
            raise HTTPException(404, f"Template {template_id} not found")


# ---------------------------------------------------------------------------
# Extraction endpoints
# ---------------------------------------------------------------------------


@router.post("/papers/{paper_id}/extract", response_model=ExtractionResponse)
@limiter.limit("5/minute")
async def extract_paper(
    request: Request,
    paper_id: int,
    body: ExtractionRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    embedder=Depends(get_optional_embedder),
    verifier=Depends(get_optional_verifier),
) -> ExtractionResponse:
    """Extract structured fields from a single paper."""
    try:
        return await extract_fields_for_paper(
            http_client,
            db_pool,
            paper_id,
            body.template_id,
            embedder=embedder,
            verifier=verifier,
        )
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/papers/{paper_id}/extractions", response_model=list[ExtractionResponse])
@limiter.limit("60/minute")
async def get_paper_extractions(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[ExtractionResponse]:
    """Get all extractions for a paper."""
    async with db_pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """SELECT id, paper_id, template_id, extractions, extraction_model, created_at
                   FROM paper_extractions WHERE paper_id = $1
                   ORDER BY created_at DESC""",
                paper_id,
            )
        except asyncpg.exceptions.UndefinedTableError:
            raise HTTPException(
                503, "extraction_templates table not found (migration 011 not applied)"
            )
    result = []
    for r in rows:
        exts = r["extractions"] or {}
        parsed = {
            k: ExtractedField(**v) if isinstance(v, dict) else ExtractedField(value=v)
            for k, v in exts.items()
        }
        result.append(
            ExtractionResponse(
                id=r["id"],
                paper_id=r["paper_id"],
                template_id=r["template_id"],
                extractions=parsed,
                extraction_model=r["extraction_model"],
                created_at=r["created_at"],
            )
        )
    return result


@router.post("/extractions/batch")
@limiter.limit("2/minute")
async def batch_extract_papers(
    request: Request,
    body: BatchExtractionRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, object]:
    """Enqueue a background job to batch-extract fields for multiple papers."""
    job_id = await jobs_lib.enqueue(
        db_pool,
        "extraction.batch",
        payload={
            "paper_ids": body.paper_ids,
            "template_id": body.template_id,
        },
    )
    return {"job_id": job_id, "total": len(body.paper_ids)}


@router.get("/extractions/table", response_model=None)
@limiter.limit("60/minute")
async def get_extraction_table(
    request: Request,
    template_id: int,
    paper_ids: str | None = None,
    format: str = Query(default="json", pattern="^(json|csv)$"),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Get cross-paper extraction comparison table.

    Parameters
    ----------
    template_id : int
        Template to filter by.
    paper_ids : str, optional
        Comma-separated paper IDs to include. If None, includes all.
    format : str, optional
        Response format: ``json`` (default) or ``csv``.
    """
    async with db_pool.acquire() as conn:
        # Fetch template fields (needed for CSV column headers)
        try:
            template_row = await conn.fetchrow(
                "SELECT fields FROM extraction_templates WHERE id = $1",
                template_id,
            )
        except asyncpg.exceptions.UndefinedTableError:
            raise HTTPException(
                503, "extraction_templates table not found (migration 011 not applied)"
            )

        template_fields: list[ExtractionField] = []
        if template_row and template_row["fields"]:
            raw_fields = template_row["fields"]
            if isinstance(raw_fields, str):
                raw_fields = json.loads(raw_fields)
            template_fields = [ExtractionField(**f) for f in raw_fields]

        if paper_ids:
            try:
                ids = [int(x.strip()) for x in paper_ids.split(",") if x.strip()]
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="paper_ids must be comma-separated integers",
                )
            try:
                rows = await conn.fetch(
                    """SELECT pe.paper_id, p.title AS paper_title, pe.extractions
                       FROM paper_extractions pe
                       JOIN papers p ON p.id = pe.paper_id
                       WHERE pe.template_id = $1 AND pe.paper_id = ANY($2)
                       ORDER BY p.title""",
                    template_id,
                    ids,
                )
            except asyncpg.exceptions.UndefinedTableError:
                raise HTTPException(
                    503, "extraction_templates table not found (migration 011 not applied)"
                )
        else:
            try:
                rows = await conn.fetch(
                    """SELECT pe.paper_id, p.title AS paper_title, pe.extractions
                       FROM paper_extractions pe
                       JOIN papers p ON p.id = pe.paper_id
                       WHERE pe.template_id = $1
                       ORDER BY p.title""",
                    template_id,
                )
            except asyncpg.exceptions.UndefinedTableError:
                raise HTTPException(
                    503, "extraction_templates table not found (migration 011 not applied)"
                )

    result: list[ExtractionTableRow] = []
    for r in rows:
        exts = r["extractions"] or {}
        parsed = {
            k: ExtractedField(**v) if isinstance(v, dict) else ExtractedField(value=v)
            for k, v in exts.items()
        }
        result.append(
            ExtractionTableRow(
                paper_id=r["paper_id"],
                paper_title=r["paper_title"],
                extractions=parsed,
            )
        )

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Paper"] + [f.label for f in template_fields])
        for row in result:
            writer.writerow(
                [row.paper_title]
                + [
                    str(row.extractions.get(f.name, ExtractedField()).value or "")
                    for f in template_fields
                ]
            )
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="extractions.csv"'},
        )

    return result
