"""Streaming RAG helpers — SSE event generator and RAG preparation.

This module is kept separate from ``main.py`` so that tests can import
these functions without triggering the ``fitz`` (PyMuPDF) import chain.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import asyncpg
import httpx
from fastapi import HTTPException
from jarvis_common import get_fast_model
from jarvis_common.llm_client import (
    LITELLM_FALLBACK_ENV_NAMES,
    build_litellm_headers,
    get_litellm_config,
)
from jarvis_common.prompt_safety import escape_llm_text, wrap_delimited

from paper_ingestion.decomposition import decompose_query
from paper_ingestion.models import AskRequest, CrossPaperAskRequest

if TYPE_CHECKING:
    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.verification import QuoteVerifier

logger = logging.getLogger(__name__)

__all__ = [
    "CrossPaperRagNoResults",
    "CrossPaperRagPrep",
    "prepare_single_paper_rag",
    "prepare_cross_paper_rag",
    "sse_error_stream",
    "stream_rag_events",
    "_SEARCH_SCORE_THRESHOLD",
]

_SEARCH_SCORE_THRESHOLD = 0.05


# ---------------------------------------------------------------------------
# Return-type dataclasses for _prepare_cross_paper_rag
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrossPaperRagPrep:
    """Successful preparation result: LLM messages + source metadata."""

    messages: list[dict[str, str]]
    sources: list[dict]


@dataclass(frozen=True, slots=True)
class CrossPaperRagNoResults:
    """Short-circuit result when no relevant chunks were found."""

    answer: str
    sources: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SSE error helper
# ---------------------------------------------------------------------------


async def sse_error_stream(message: str):
    """Yield a single SSE error event followed by [DONE] sentinel."""
    yield f"data: {json.dumps({'type': 'error', 'message': message})}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# RAG preparation helpers
# ---------------------------------------------------------------------------


async def prepare_single_paper_rag(
    embedder: "Embedder",
    db_pool: asyncpg.Pool,
    paper_id: int,
    body: AskRequest,
    http_client: httpx.AsyncClient,
) -> tuple[list[dict], list[dict]]:
    """Retrieve chunks for a single paper, rerank, and build LLM messages.

    Returns ``(messages, sources_list)``.
    Raises 404 if paper not found, 422 if no relevant chunks.
    """
    async with db_pool.acquire() as conn:
        paper = await conn.fetchrow("SELECT id, title FROM papers WHERE id = $1", paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    # Retrieve top-k relevant chunks from this paper, over-fetch for reranking
    chunks = await embedder.search_chunks_in_paper(
        query_text=body.question,
        paper_id=paper_id,
        limit=body.max_chunks * 4,
        score_threshold=_SEARCH_SCORE_THRESHOLD,
    )
    # Cross-encoder rerank for quality, then trim to requested max_chunks
    chunks = await embedder.rerank_chunks(body.question, chunks, top_k=body.max_chunks)
    if not chunks:
        raise HTTPException(
            status_code=422,
            detail=(
                "No relevant chunks found. Has this paper been processed? "
                "Run 'Process PDF' first to extract and embed the paper text."
            ),
        )

    # Build RAG prompt — full chunk text flows through to the prompt.
    # C-10: Wrap question and title in XML-style delimiters to prevent prompt injection.
    # Content between XML tags is DATA — never instructions.
    safe_question = escape_llm_text(body.question)
    context_blocks = "\n\n".join(
        f'<excerpt page="{c["page_number"] or "?"}">{escape_llm_text(c["content"])}</excerpt>'
        for c in chunks
    )
    prompt = (
        f"Paper: {wrap_delimited('title', paper['title'])}\n\n"
        "Answer using ONLY these excerpts. If not covered, say so.\n\n"
        f"EXCERPTS:\n{context_blocks}\n\n"
        f"<question>{safe_question}</question>\n\nANSWER:"
    )

    messages = [{"role": "user", "content": prompt}]
    sources_list = [
        {
            "content": c["content"],
            "page_number": c["page_number"],
            "score": round(c["score"], 3),
        }
        for c in chunks
    ]
    return messages, sources_list


async def prepare_cross_paper_rag(
    embedder: "Embedder",
    db_pool: asyncpg.Pool,
    body: CrossPaperAskRequest,
    http_client: httpx.AsyncClient,
) -> "CrossPaperRagPrep | CrossPaperRagNoResults":
    """Retrieve chunks across papers, rerank, and build LLM messages.

    Returns a :class:`CrossPaperRagPrep` on success, or a
    :class:`CrossPaperRagNoResults` short-circuit when no relevant chunks are
    found.
    """
    # 1. Search all chunks — optionally via query decomposition
    if body.decompose:
        fast_model = get_fast_model()
        sub_queries = await decompose_query(body.question, http_client, model=fast_model)
        per_query_limit = max(body.max_chunks * 2 // len(sub_queries), 3)

        results = await asyncio.gather(
            *(
                embedder.search_chunks_global(
                    query_text=sq,
                    limit=per_query_limit,
                    score_threshold=_SEARCH_SCORE_THRESHOLD,
                )
                for sq in sub_queries
            )
        )

        # Merge: flatten all chunks, dedup by (paper_id, chunk_index) keeping highest score
        seen: dict[tuple[int, int], dict] = {}
        for chunk_list in results:
            for chunk in chunk_list:
                key = (chunk["paper_id"], chunk["chunk_index"])
                if key not in seen or chunk["score"] > seen[key]["score"]:
                    seen[key] = chunk
        all_chunks = list(seen.values())
    else:
        all_chunks = await embedder.search_chunks_global(
            query_text=body.question,
            limit=body.max_chunks * 2,
            score_threshold=_SEARCH_SCORE_THRESHOLD,
        )

    if not all_chunks:
        logger.warning(
            "Cross-paper RAG found 0 chunks for query: %.100s (decompose=%s)",
            body.question,
            body.decompose,
        )
        return CrossPaperRagNoResults(
            answer="No relevant information found in the paper collection.",
            sources=[],
        )

    # 1b. Cross-encoder rerank merged results using original question
    all_chunks = await embedder.rerank_chunks(body.question, all_chunks, top_k=body.max_chunks * 2)

    # 2. Deduplicate: group by paper_id, keep top 2 chunks per paper
    chunks_by_paper: dict[int, list[dict]] = {}
    for chunk in all_chunks:
        pid = chunk["paper_id"]
        if pid not in chunks_by_paper:
            chunks_by_paper[pid] = []
        chunks_by_paper[pid].append(chunk)

    for pid in chunks_by_paper:
        chunks_by_paper[pid].sort(key=lambda c: c["score"], reverse=True)
        chunks_by_paper[pid] = chunks_by_paper[pid][:2]

    # 3. Trim to max_papers (pick papers with highest top-chunk score)
    paper_ids_sorted = sorted(
        chunks_by_paper.keys(),
        key=lambda pid: chunks_by_paper[pid][0]["score"],
        reverse=True,
    )
    paper_ids_sorted = paper_ids_sorted[: body.max_papers]

    # Flatten and trim to max_chunks total
    selected_chunks: list[dict] = []
    for pid in paper_ids_sorted:
        selected_chunks.extend(chunks_by_paper[pid])
    selected_chunks.sort(key=lambda c: c["score"], reverse=True)
    selected_chunks = selected_chunks[: body.max_chunks]

    # 4. Fetch paper metadata
    unique_paper_ids = list({c["paper_id"] for c in selected_chunks})
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, authors, url FROM papers WHERE id = ANY($1::int[])",
            unique_paper_ids,
        )
    paper_meta = {row["id"]: row for row in rows}

    # 5. Build prompt with per-paper sections
    safe_question = escape_llm_text(body.question)

    # Group selected chunks by paper for the prompt
    prompt_chunks_by_paper: dict[int, list[dict]] = {}
    for chunk in selected_chunks:
        pid = chunk["paper_id"]
        if pid not in prompt_chunks_by_paper:
            prompt_chunks_by_paper[pid] = []
        prompt_chunks_by_paper[pid].append(chunk)

    context_sections: list[str] = []
    paper_number_map: dict[int, int] = {}
    for i, pid in enumerate(prompt_chunks_by_paper.keys(), start=1):
        meta = paper_meta.get(pid)
        title = wrap_delimited("title", meta["title"]) if meta else f"Paper ID {pid}"
        paper_number_map[pid] = i

        excerpts = "\n".join(
            f'<excerpt page="{c["page_number"] or "?"}">{escape_llm_text(c["content"])}</excerpt>'
            for c in prompt_chunks_by_paper[pid]
        )
        context_sections.append(f"--- Paper {i}: {title} ---\n{excerpts}")

    context_block = "\n\n".join(context_sections)
    prompt = (
        "You are a research assistant answering questions using evidence "
        "from multiple papers.\n"
        "Answer ONLY based on the provided excerpts. Cite each claim with "
        "[Paper N] where N is the paper number.\n"
        "If the excerpts don't contain enough information, say so.\n\n"
        f"{context_block}\n\n"
        f"<question>{safe_question}</question>\n\nANSWER:"
    )

    messages = [{"role": "user", "content": prompt}]
    sources_list = [
        {
            "paper_id": c["paper_id"],
            "paper_title": (
                paper_meta[c["paper_id"]]["title"] if c["paper_id"] in paper_meta else "Unknown"
            ),
            "content": c["content"],
            "page_number": c["page_number"],
            "score": round(c["score"], 3),
        }
        for c in selected_chunks
    ]
    return CrossPaperRagPrep(messages=messages, sources=sources_list)


# ---------------------------------------------------------------------------
# SSE event generator for streaming RAG responses
# ---------------------------------------------------------------------------


async def stream_rag_events(
    http_client: httpx.AsyncClient,
    messages: list[dict],
    sources_list: list[dict],
    *,
    model: str = "smart",
    verifier: "QuoteVerifier | None" = None,
    db_pool: "asyncpg.Pool | None" = None,
):
    """Stream LLM response as SSE events (token → sources → done → confidence → [DONE])."""
    litellm_config = get_litellm_config(fallback_env_names=LITELLM_FALLBACK_ENV_NAMES)
    full_answer = ""
    try:
        async with http_client.stream(
            "POST",
            f"{litellm_config.base_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "stream": True,
                "temperature": 0.1,
                "max_tokens": 700,
            },
            headers=build_litellm_headers(litellm_config),
            timeout=300.0,  # RAG prompts with many chunks need longer prefill
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                chunk = json.loads(data_str)
                choices = chunk.get("choices")
                if not choices:
                    continue
                content = choices[0].get("delta", {}).get("content", "")
                if content:
                    full_answer += content
                    yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
    except Exception as e:
        _err_msgs = {
            httpx.TimeoutException: "LLM request timed out. Please try again.",
            httpx.ConnectError: "Cannot connect to LLM service. Check that services are running.",
        }
        msg = next(
            (m for t, m in _err_msgs.items() if isinstance(e, t)),
            "An error occurred while generating the response. Please try again later.",
        )
        logger.error("LLM streaming failed: %r", e, exc_info=True)
        async for event in sse_error_stream(msg):
            yield event
        return
    yield f"data: {json.dumps({'type': 'sources', 'sources': sources_list})}\n\n"
    yield f"data: {json.dumps({'type': 'done', 'full_answer': full_answer})}\n\n"
    # Sentence-level verification — runs after tokens have streamed (additive latency only)
    if verifier is not None and db_pool is not None:
        try:
            from paper_ingestion.rag.verification import verify_answer_sentences

            report = await verify_answer_sentences(full_answer, sources_list, verifier, db_pool)
            payload = {
                "type": "confidence",
                "confidence": report.confidence.value,
                "verified_fraction": report.pass_rate,
                "per_sentence": [
                    {"text": s.text, "verified": s.verified} for s in report.per_sentence
                ],
            }
            yield f"data: {json.dumps(payload)}\n\n"
        except Exception as exc:  # noqa: BLE001 — don't break the stream if verification errors
            logger.warning("RAG verification failed: %s", exc)
    yield "data: [DONE]\n\n"
