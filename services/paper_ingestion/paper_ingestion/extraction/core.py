"""Structured data extraction from papers using LLM.

Uses system-global extraction templates to extract structured fields from
papers.  Templates are instance-wide (no ``user_id`` column on
``extraction_templates``): all users share the same template catalogue.
Do NOT add per-user filtering to template lookups — that would require a
new schema column + multi-tenancy design review (see CFG-EXTPL-1 / db/init.sql).

Each field includes a supporting quote verified against source text.

Anti-hallucination strategy:
1. Select relevant chunks per field using semantic search
2. Focus LLM on relevant sections only
3. Require verbatim quotes for each value
4. Verify quotes using QuoteVerifier
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg
import httpx
from jarvis_common import get_smart_model
from jarvis_common.llm_client import ChatCompletionOptions, call_llm_structured, observe
from jarvis_common.prompt_safety import safe_for_prompt, wrap_delimited

from paper_ingestion.extraction.dynamic_models import (
    ExtractedFieldOutput,
    _build_extraction_response_model,
)
from paper_ingestion.models import (
    BatchExtractionResponse,
    ExtractedField,
    ExtractionResponse,
)

if TYPE_CHECKING:
    import openai
    from jarvis_common.jobs import ProgressContext
    from jarvis_common.verify import QuoteVerifier

    from paper_ingestion.ingestion.embedder import Embedder

logger = logging.getLogger(__name__)

_SYSTEM_EXTRACTION = """\
You are a precise research paper data extractor. Extract structured data from the following paper.

RULES:
1. For each field, provide:
   - "value": the extracted value (string for text, number for numeric, null if not found)
   - "quote": a VERBATIM quote from the paper that supports this value (copy-paste exact text)
2. If a field cannot be determined from the text, set value to null and quote to null
3. Do NOT invent or paraphrase quotes — they must be exact substrings of the source text
4. Be conservative: prefer null over uncertain values

Respond with ONLY a JSON object where keys are field names and values are objects
with "value" and "quote" keys.
Example format:
{"methodology": {"value": "RCT", "quote": "We conducted a randomized controlled trial..."}}

JSON:\
"""


def _escape_field_attr(s: object) -> str:
    """HTML-encode angle-brackets in user-controlled field attributes.

    Prevents prompt injection via crafted ``name``, ``type``, or ``description``
    values that embed fake XML closing tags (e.g. ``</paper_text>IGNORE ABOVE``),
    which could otherwise break out of the field-specs section in the LLM prompt.

    Uses the same escaping strategy as :func:`jarvis_common.prompt_safety.safe_for_prompt`
    (``mode='escape'``): ``<`` → ``&lt;``, ``>`` → ``&gt;``.
    """
    if not isinstance(s, str):
        return str(s) if s is not None else ""
    return safe_for_prompt(s, mode="escape")


def build_extraction_prompt(fields: list[dict], title: str, text: str) -> str:
    """Build the LLM user-role prompt for field extraction.

    The instruction head lives in ``_SYSTEM_EXTRACTION`` (system role).
    This function returns only the data payload wrapped via ``wrap_delimited``
    so untrusted paper text cannot escape into the instruction layer.
    ``wrap_delimited`` applies ``max_chars=15000`` truncation; do NOT pre-truncate
    ``text`` before calling this function (PI-09).
    """
    field_specs = "\n".join(
        f'- "{_escape_field_attr(f["name"])}" '
        f"({_escape_field_attr(f.get('type', 'text'))}): "
        f"{_escape_field_attr(f.get('description', f['label']))}"
        for f in fields
    )

    safe_title, _ = wrap_delimited("title", title, max_chars=500)
    safe_body, _ = wrap_delimited("paper_text", text, max_chars=15000)

    return (
        f"PAPER TITLE:\n{safe_title}\n\n"
        f"FIELDS TO EXTRACT:\n{field_specs}\n\n"
        f"PAPER TEXT:\n{safe_body}\n"
    )


@observe()
async def extract_fields_for_paper(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    paper_id: int,
    template_id: int,
    embedder: Embedder | None = None,
    verifier: QuoteVerifier | None = None,
    openai_client: openai.AsyncOpenAI | None = None,
) -> ExtractionResponse:
    """Extract template fields for one paper and persist the extraction payload.

    ``extraction_templates`` is **system-global** — the table has no ``user_id``
    column and the ``name`` constraint is instance-wide UNIQUE.  The query below
    (``SELECT * FROM extraction_templates WHERE id = $1``) is therefore correct
    as-is and must NOT gain a user predicate.  Templates are visible to all
    authenticated users; creation/update/deletion is admin-only (CFG-EXTPL-1).
    """
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

        paper = await conn.fetchrow("SELECT id, title FROM papers WHERE id = $1", paper_id)
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
                logger.debug("Chunk search failed for field %s", field.get("name"), exc_info=True)
                break

        if selected_chunks:
            if chunk_search_failed:
                prioritized_chunks = [c for c in chunks if c["chunk_index"] in selected_chunks]
                remaining_chunks = [c for c in chunks if c["chunk_index"] not in selected_chunks]
                chunks = prioritized_chunks + remaining_chunks
            else:
                chunks = [c for c in chunks if c["chunk_index"] in selected_chunks]

    full_text = "\n\n".join(c["content"] for c in chunks)

    prompt = build_extraction_prompt(fields, paper["title"], full_text)
    from paper_ingestion._state import svc  # noqa: PLC0415

    _openai_client = openai_client if openai_client is not None else svc.openai_client
    if _openai_client is None:
        raise RuntimeError(
            "openai_client not initialized — check _init_langfuse_hook ran during lifespan"
        )
    field_names = tuple(f["name"] for f in fields)
    response_model = _build_extraction_response_model(field_names)
    try:
        llm_result = await call_llm_structured(
            _openai_client,
            response_model=response_model,
            prompt=prompt,
            options=ChatCompletionOptions(model=smart_model, system=_SYSTEM_EXTRACTION),
        )
    except Exception:
        logger.exception("LLM extraction failed for paper %d", paper_id)
        raise

    from paper_ingestion.models import ChunkResponse

    extractions: dict[str, ExtractedField] = {}
    chunk_responses = (
        [
            ChunkResponse(
                id=c["id"],
                paper_id=paper_id,
                chunk_index=c["chunk_index"],
                content=c["content"],
                page_number=c["page_number"],
                created_at=datetime.now(UTC),
            )
            for c in chunks
        ]
        if verifier
        else []
    )

    for field in fields:
        field_name = field["name"]
        _raw: ExtractedFieldOutput | None = getattr(llm_result, field_name, None)
        field_output: ExtractedFieldOutput = _raw if _raw is not None else ExtractedFieldOutput()

        value = field_output.value
        quote = field_output.quote
        verified = False
        chunk_id = None
        page_number = None

        if verifier and quote and quote.strip():
            try:
                vr = verifier.verify_quote(quote, full_text, chunk_responses)
                verified = vr.verified
                chunk_id = vr.chunk_id
                page_number = vr.page_number
                if not vr.verified:
                    # Mirror entity_extractor policy: unverified extractions are
                    # dropped rather than persisted with uncertain values.
                    logger.debug(
                        "Quote verification failed for field %s — discarding value",
                        field_name,
                        exc_info=True,
                    )
                    value = None
            except Exception:
                # Verifier raised unexpectedly: treat as unverified and discard
                # value+quote so the anti-hallucination rule is never bypassed.
                logger.debug("Quote verifier raised for field %s — discarding value", field_name)
                value = None
                quote = None

        # confidence is binary — 1.0 when the quote was
        # verified end-to-end, 0.0 otherwise. There is no "partially trusted"
        # middle ground: an unverified quote (no verifier configured, verifier
        # crashed, verification failed, or the quote was whitespace-only and
        # the verifier was skipped) must carry confidence 0.0 so downstream
        # consumers using `confidence ≥ 0.5` cannot silently accept
        # hallucinated content. Previously, a whitespace-only quote with a
        # configured verifier produced confidence=0.5 — that branch is gone.
        if value is None:
            confidence = 0.0
        elif verified:
            confidence = 1.0
        else:
            confidence = 0.0
        extractions[field_name] = ExtractedField(
            value=value,
            quote=quote,
            verified=verified,
            confidence=confidence,
            chunk_id=chunk_id,
            page_number=page_number,
        )

    extraction_json = {k: v.model_dump() for k, v in extractions.items()}

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
                   RETURNING id, paper_id, template_id, extractions,
                       extraction_model, created_at""",
                paper_id,
                template_id,
                extraction_json,
                smart_model,
                llm_result.model_dump_json(),
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


@observe()
async def batch_extract(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    paper_ids: list[int],
    template_id: int,
    embedder: Embedder | None = None,
    verifier: QuoteVerifier | None = None,
    ctx: ProgressContext | None = None,
) -> BatchExtractionResponse:
    """Extract fields for multiple papers, skipping those already extracted.

    When ``ctx`` is provided (a JobContext-like object with ``update_progress``
    and ``is_cancelled`` coroutines), progress is reported between papers and
    cancellation is honored.
    """
    extracted = 0
    failed = 0
    skipped = 0
    total = len(paper_ids)

    if ctx is not None:
        await ctx.update_progress(0.0, f"Starting: {total} papers")

    for i, paper_id in enumerate(paper_ids):
        if ctx is not None and await ctx.is_cancelled():
            break
        async with db_pool.acquire() as conn:
            try:
                existing = await conn.fetchval(
                    "SELECT id FROM paper_extractions WHERE paper_id = $1 AND template_id = $2",
                    paper_id,
                    template_id,
                )
            except asyncpg.exceptions.UndefinedTableError:
                existing = None
        if existing:
            skipped += 1
        else:
            try:
                await extract_fields_for_paper(
                    http_client, db_pool, paper_id, template_id, embedder, verifier
                )
                extracted += 1
            except Exception:
                logger.exception("Extraction failed for paper %d", paper_id)
                failed += 1

        if ctx is not None:
            progress = (i + 1) / max(total, 1)
            await ctx.update_progress(progress, f"Processed {i + 1}/{total} papers")

    if ctx is not None:
        await ctx.update_progress(
            1.0, f"Done: {extracted} extracted, {skipped} skipped, {failed} failed"
        )

    return BatchExtractionResponse(extracted=extracted, failed=failed, skipped=skipped)
