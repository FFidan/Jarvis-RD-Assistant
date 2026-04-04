"""Structured data extraction from papers using LLM.

Uses user-defined extraction templates to extract structured fields from
papers. Each field includes a supporting quote verified against source text.

Anti-hallucination strategy:
1. Select relevant chunks per field using semantic search
2. Focus LLM on relevant sections only
3. Require verbatim quotes for each value
4. Verify quotes using QuoteVerifier
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx

from jarvis_common import get_smart_model
from jarvis_common.llm_client import ChatCompletionOptions, call_llm
from app.models import (
    BatchExtractionResponse,
    ExtractedField,
    ExtractionResponse,
)

logger = logging.getLogger(__name__)


def build_extraction_prompt(
    fields: list[dict], title: str, text: str
) -> str:
    """Build the LLM prompt for field extraction."""
    field_specs = "\n".join(
        f'- "{f["name"]}" ({f.get("type", "text")}): {f.get("description", f["label"])}'
        for f in fields
    )

    return f"""You are a precise research paper data extractor. Extract structured data from the following paper.

PAPER TITLE: {title}

FIELDS TO EXTRACT:
{field_specs}

RULES:
1. For each field, provide:
   - "value": the extracted value (string for text, number for numeric, null if not found)
   - "quote": a VERBATIM quote from the paper that supports this value (copy-paste exact text)
2. If a field cannot be determined from the text, set value to null and quote to null
3. Do NOT invent or paraphrase quotes — they must be exact substrings of the source text
4. Be conservative: prefer null over uncertain values

PAPER TEXT:
{text[:15000]}

Respond with ONLY a JSON object where keys are field names and values are objects with "value" and "quote" keys.
Example format:
{{"methodology": {{"value": "randomized controlled trial", "quote": "We conducted a randomized controlled trial..."}}}}

JSON:"""


async def extract_fields_for_paper(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    paper_id: int,
    template_id: int,
    embedder: Any | None = None,
    verifier: Any | None = None,
) -> ExtractionResponse:
    """Extract template fields for one paper and persist the extraction payload."""
    async with db_pool.acquire() as conn:
        try:
            template = await conn.fetchrow(
                "SELECT * FROM extraction_templates WHERE id = $1", template_id
            )
        except asyncpg.exceptions.UndefinedTableError:
            raise ValueError(
                "extraction_templates table not found (migration 011 not applied)"
            ) from None
        if not template:
            raise ValueError(f"Template {template_id} not found")

        fields = template["fields"]  # JSONB, already parsed

        paper = await conn.fetchrow(
            "SELECT id, title FROM papers WHERE id = $1", paper_id
        )
        if not paper:
            raise ValueError(f"Paper {paper_id} not found")

        chunks = await conn.fetch(
            """SELECT id, chunk_index, content, page_number
               FROM paper_chunks WHERE paper_id = $1
               ORDER BY chunk_index""",
            paper_id,
        )

        if not chunks:
            raise ValueError(f"No chunks found for paper {paper_id}")

        smart_model = get_smart_model()

    # If any per-field chunk search fails, keep full-paper context but move any
    # already-selected chunks to the front so truncation does not discard them.
    if embedder:
        selected_chunks = set()
        chunk_search_failed = False
        for field in fields:
            query = f"{field.get('label', '')} {field.get('description', '')}"
            try:
                field_chunks = await embedder.search_chunks_in_paper(
                    query, paper_id, limit=3, score_threshold=0.05
                )
                for fc in field_chunks:
                    selected_chunks.add(fc.get("chunk_index", 0))
            except Exception:
                chunk_search_failed = True
                logger.debug("Chunk search failed for field %s", field.get("name"))
                break

        if selected_chunks:
            if chunk_search_failed:
                prioritized_chunks = [
                    c for c in chunks if c["chunk_index"] in selected_chunks
                ]
                remaining_chunks = [
                    c for c in chunks if c["chunk_index"] not in selected_chunks
                ]
                chunks = prioritized_chunks + remaining_chunks
            else:
                chunks = [c for c in chunks if c["chunk_index"] in selected_chunks]

    full_text = "\n\n".join(c["content"] for c in chunks)
    if len(full_text) > 15000:
        full_text = full_text[:15000]

    prompt = build_extraction_prompt(fields, paper["title"], full_text)
    try:
        llm_result = await call_llm(
            http_client,
            prompt,
            options=ChatCompletionOptions(model=smart_model),
        )
    except Exception:
        logger.exception("LLM extraction failed for paper %d", paper_id)
        raise

    from app.models import ChunkResponse

    extractions: dict[str, ExtractedField] = {}
    chunk_responses = [
        ChunkResponse(
            id=c["id"],
            paper_id=paper_id,
            chunk_index=c["chunk_index"],
            content=c["content"],
            page_number=c["page_number"],
            created_at=datetime.now(UTC),
        )
        for c in chunks
    ] if verifier else []

    for field in fields:
        field_name = field["name"]
        field_data = llm_result.get(field_name, {})
        if not isinstance(field_data, dict):
            field_data = {"value": field_data, "quote": None}

        value = field_data.get("value")
        quote = field_data.get("quote")
        verified = False
        chunk_id = None
        page_number = None

        if verifier and quote and quote.strip():
            try:
                vr = verifier.verify_quote(quote, full_text, chunk_responses)
                verified = vr.verified
                chunk_id = vr.chunk_id
                page_number = vr.page_number
            except Exception:
                # Recoverable: verification is best-effort; a failure means the
                # field is stored with verified=False and confidence=0.5 instead
                # of being lost entirely.
                logger.debug("Quote verification failed for field %s", field_name)

        extractions[field_name] = ExtractedField(
            value=value,
            quote=quote,
            verified=verified,
            confidence=1.0 if verified else 0.5 if quote else 0.0,
            chunk_id=chunk_id,
            page_number=page_number,
        )

    extraction_json = {
        k: v.model_dump() for k, v in extractions.items()
    }

    async with db_pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO paper_extractions (paper_id, template_id, extractions,
                       extraction_model, extraction_raw)
                   VALUES ($1, $2, $3::jsonb, $4, $5)
                   ON CONFLICT (paper_id, template_id)
                   DO UPDATE SET extractions = EXCLUDED.extractions,
                                 extraction_model = EXCLUDED.extraction_model,
                                 extraction_raw = EXCLUDED.extraction_raw
                   RETURNING id, paper_id, template_id, extractions, extraction_model, created_at""",
                paper_id,
                template_id,
                json.dumps(extraction_json),
                smart_model,
                json.dumps(llm_result),
            )
        except asyncpg.exceptions.UndefinedTableError:
            raise ValueError(
                "paper_extractions table not found (migration 011 not applied)"
            ) from None

    stored = row["extractions"]
    parsed_extractions = {
        k: ExtractedField(**v) if isinstance(v, dict) else ExtractedField(value=v)
        for k, v in stored.items()
    }

    return ExtractionResponse(
        id=row["id"],
        paper_id=row["paper_id"],
        template_id=row["template_id"],
        extractions=parsed_extractions,
        extraction_model=row["extraction_model"],
        created_at=row["created_at"],
    )


async def batch_extract(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    paper_ids: list[int],
    template_id: int,
    embedder: Any | None = None,
    verifier: Any | None = None,
) -> BatchExtractionResponse:
    """Extract fields for multiple papers, skipping those already extracted."""
    extracted = 0
    failed = 0
    skipped = 0

    for paper_id in paper_ids:
        async with db_pool.acquire() as conn:
            try:
                existing = await conn.fetchval(
                    "SELECT id FROM paper_extractions WHERE paper_id = $1 AND template_id = $2",
                    paper_id, template_id,
                )
            except asyncpg.exceptions.UndefinedTableError:
                existing = None
        if existing:
            skipped += 1
            continue

        try:
            await extract_fields_for_paper(
                http_client, db_pool, paper_id, template_id, embedder, verifier
            )
            extracted += 1
        except Exception:
            logger.exception("Extraction failed for paper %d", paper_id)
            failed += 1

    return BatchExtractionResponse(extracted=extracted, failed=failed, skipped=skipped)
