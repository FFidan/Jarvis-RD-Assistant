"""Streaming RAG helpers — SSE event generator and RAG preparation.

This module is kept separate from ``main.py`` so that tests can import
these functions without triggering the ``pypdfium2`` import chain.
"""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import asyncpg
import httpx
from jarvis_common import effective_num_ctx, get_fast_model
from jarvis_common.llm_client import (
    build_litellm_headers,
    could_be_visible_work_note_prefix,
    detect_visible_work_notes,
    get_litellm_config,
    observe,
    strip_think_streaming,
)
from jarvis_common.maintenance import ensure_outbound_egress_allowed
from jarvis_common.prompt_safety import max_input_chars, safe_for_prompt, wrap_delimited
from jarvis_common.settings import get_core_settings, get_reranker_settings
from jarvis_common.sse import SSE_DONE, sse_event

from paper_ingestion.ingestion.search_scope import SearchScope
from paper_ingestion.models import AskRequest, CrossPaperAskRequest
from paper_ingestion.perf_probe import probe_span
from paper_ingestion.queries.predicates import paper_visible_sql
from paper_ingestion.rag.decomposition import decompose_query
from paper_ingestion.rag.exceptions import NoRelevantChunksError, PaperNotFoundError

if TYPE_CHECKING:
    from jarvis_common.verify import QuoteVerifier

    from paper_ingestion.ingestion.embedder import Embedder
    from paper_ingestion.models.rag import HistoryTurn


logger = logging.getLogger(__name__)

__all__ = [
    "CrossPaperRagNoResults",
    "CrossPaperRagPrep",
    "prepare_single_paper_rag",
    "prepare_cross_paper_rag",
    "sse_error_stream",
    "stream_rag_events",
    "_SEARCH_SCORE_THRESHOLD",
    "PaperNotFoundError",
    "NoRelevantChunksError",
]

_SEARCH_SCORE_THRESHOLD = 0.05

_ANSWER_MAX_TOKENS = 700
_PARAGRAPH_BOUNDARY_RE = re.compile(r"\n\s*\n")

_SYSTEM_SINGLE_PAPER_RAG = "Answer using ONLY the excerpts provided. If not covered, say so."

_SYSTEM_CROSS_PAPER_RAG = (
    "You are a research assistant answering questions using evidence "
    "from multiple papers.\n"
    "Answer ONLY based on the provided excerpts. Cite each claim with "
    "[Paper N] where N is the paper number.\n"
    "If the excerpts don't contain enough information, say so."
)


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
# Relevance-gate + conversation-history helpers
# ---------------------------------------------------------------------------

_HISTORY_MAX_TURNS = 6
_HISTORY_MAX_CHARS = 6000


def _rerank_score_floor() -> float:
    """Backend-aware minimum rerank score (Layer 2 relevance gate)."""
    # Falsy check (not `is not None`): compose forwards the var with an
    # empty-string default, which means "use the backend default".
    _floor_env = os.getenv("RAG_MIN_RERANK_SCORE")
    if _floor_env:
        try:
            return float(_floor_env)
        except ValueError:
            logger.warning(
                "RAG_MIN_RERANK_SCORE=%r is not a valid float; using backend default",
                _floor_env,
            )
    # qwen3 reranker: logit(yes)-logit(no), 0.0 is the decision boundary.
    # Default cross-encoder (mxbai-rerank-v2 via LogitScore): unbounded
    # logits where irrelevant passages still score ~+0.4..+2.7 — 3.0
    # separates the observed relevant (~8+) from irrelevant bands.
    return 0.0 if get_reranker_settings().reranker_backend == "qwen3" else 3.0


def _apply_relative_cutoff(chunks: list[dict]) -> list[dict]:
    """Drop chunks scoring far below the best hit of the SAME query embedding.

    Cosine scores are only comparable within one query — across decomposed
    sub-queries the scales differ, so callers apply this per sub-query result
    list BEFORE merging (absolute floors don't transfer across embedding
    models either). The top-scoring chunk always survives.
    """
    cutoff_env = os.getenv("RAG_RELATIVE_SCORE_CUTOFF") or "0.85"
    try:
        rel_cutoff = float(cutoff_env)
    except ValueError:
        logger.warning(
            "RAG_RELATIVE_SCORE_CUTOFF=%r is not a valid float; using default 0.85",
            cutoff_env,
        )
        rel_cutoff = 0.85
    if not chunks or not 0.0 < rel_cutoff < 1.0:
        return chunks
    top = max(c["score"] for c in chunks)
    return [c for c in chunks if c["score"] >= top * rel_cutoff]


def _build_history_messages(history: "list[HistoryTurn]") -> list[dict[str, str]]:
    """Convert prior chat turns into LLM chat messages.

    Keeps only the most recent ``_HISTORY_MAX_TURNS`` turns, escapes each
    content (history is DATA, never instructions), then drops oldest turns
    until the total stays within ``_HISTORY_MAX_CHARS``.  History must NOT
    influence retrieval — the question alone drives embedding/decomposition;
    these messages only sit between the system and final user message.
    """
    msgs = [
        {"role": turn.role, "content": safe_for_prompt(turn.content, mode="escape")}
        for turn in history[-_HISTORY_MAX_TURNS:]
    ]
    while msgs and sum(len(m["content"]) for m in msgs) > _HISTORY_MAX_CHARS:
        msgs.pop(0)
    return msgs


def _fit_chunks_to_budget(
    chunks: list[dict],
    build_user_content: Callable[[list[dict]], str],
    system_prompt: str,
    history_msgs: list[dict[str, str]],
    num_ctx: int | None = None,
) -> tuple[list[dict], str]:
    """Preconditions: ``chunks`` is in priority order with selection
    semantics already applied; at least one chunk is always kept; callers
    must build their sources list from the returned chunk list.
    """
    budget = max_input_chars(
        num_ctx if num_ctx is not None else get_core_settings().llm_smart_num_ctx,
        reserved_output_tokens=_ANSWER_MAX_TOKENS,
    )
    fixed = len(system_prompt) + sum(len(m["content"]) for m in history_msgs)
    user_content = build_user_content(chunks)
    kept = list(chunks)
    while len(kept) > 1 and fixed + len(user_content) > budget:
        kept.pop()
        user_content = build_user_content(kept)
    if len(kept) < len(chunks):
        logger.info(
            "RAG prompt over char budget %d: dropped %d of %d chunks",
            budget,
            len(chunks) - len(kept),
            len(chunks),
        )
    return kept, user_content


# ---------------------------------------------------------------------------
# SSE error helper
# ---------------------------------------------------------------------------


async def sse_error_stream(message: str):
    """Yield a single SSE error event followed by [DONE] sentinel."""
    yield sse_event({"type": "error", "message": message})
    yield SSE_DONE


# ---------------------------------------------------------------------------
# RAG preparation helpers
# ---------------------------------------------------------------------------


@observe()
async def prepare_single_paper_rag(
    embedder: "Embedder",
    db_pool: asyncpg.Pool,
    paper_id: int,
    body: AskRequest,
    http_client: httpx.AsyncClient,
    *,
    user_id: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Retrieve chunks for a single paper, rerank, and build LLM messages.

    Parameters
    ----------
    embedder : Embedder
        Vector search and reranking service.
    db_pool : asyncpg.Pool
        Database pool used for paper metadata and library scope.
    paper_id : int
        Canonical paper identifier.
    body : AskRequest
        Validated question, history, and retrieval limits.
    http_client : httpx.AsyncClient
        Shared service HTTP client.
    user_id : int or None
        Caller identity used to constrain vector search to visible papers.

    Returns
    -------
    tuple[list[dict], list[dict]]
        Prompt messages and the source excerpts returned to the caller.

    Raises
    ------
    PaperNotFoundError
        If ``paper_id`` does not identify a visible paper.
    NoRelevantChunksError
        If retrieval and reranking produce no usable excerpts.
    """
    library_paper_ids: list[int] | None = None
    async with db_pool.acquire() as conn:
        if user_id is None:
            paper = await conn.fetchrow("SELECT id, title FROM papers WHERE id = $1", paper_id)
        else:
            paper = await conn.fetchrow(
                f"SELECT p.id, p.title FROM papers p WHERE p.id = $1 AND {paper_visible_sql(2)}",
                paper_id,
                user_id,
            )
        # Fetch exact caller membership for private-vector filtering. Persisted
        # public vectors remain retrievable regardless of who embedded them.
        if paper and user_id is not None:
            lib_rows = await conn.fetch(
                "SELECT paper_id FROM user_library WHERE user_id = $1",
                user_id,
            )
            library_paper_ids = [row["paper_id"] for row in lib_rows]
    if not paper:
        raise PaperNotFoundError("Paper not found")

    # Retrieve top-k relevant chunks from this paper, over-fetch for reranking
    chunks = await embedder.search_chunks_in_paper(
        query_text=body.question,
        paper_id=paper_id,
        limit=body.max_chunks * 4,
        score_threshold=_SEARCH_SCORE_THRESHOLD,
        user_id=user_id,
        library_paper_ids=library_paper_ids,
    )
    # Cross-encoder rerank for quality, then trim to requested max_chunks
    chunks = await embedder.rerank_chunks(body.question, chunks, top_k=body.max_chunks)
    if not chunks:
        raise NoRelevantChunksError(
            "No relevant passages found. "
            "Analyze this paper first to extract and embed the paper text."
        )

    # Drop chunks below the backend-aware reranker floor when scores exist.
    # Chunks without a rerank score remain eligible.
    # No relative cosine cutoff here: a single paper's chunks are topically
    # homogeneous, so a relative cutoff would discard valid context.
    _min_rerank = _rerank_score_floor()
    chunks = [
        c for c in chunks if c.get("rerank_score") is None or c["rerank_score"] >= _min_rerank
    ]
    if not chunks:
        raise NoRelevantChunksError("No relevant passages found for this question in the paper.")

    # Build RAG prompt — full chunk text flows through to the prompt.
    # Wrap question and title in XML-style delimiters to prevent prompt injection.
    # Content between XML tags is treated as data, never as instructions.
    safe_question = safe_for_prompt(body.question, mode="escape")
    safe_title, _ = wrap_delimited("title", paper["title"])

    def build_user_content(kept: list[dict]) -> str:
        context_blocks = "\n\n".join(
            f'<excerpt page="{c["page_number"] or "?"}">'
            f"{safe_for_prompt(c['content'], mode='escape')}</excerpt>"
            for c in kept
        )
        return (
            f"Paper: {safe_title}\n\n"
            f"EXCERPTS:\n{context_blocks}\n\n"
            f"<question>{safe_question}</question>\n\nANSWER:"
        )

    history_msgs = _build_history_messages(body.history)
    chunks, user_content = _fit_chunks_to_budget(
        chunks,
        build_user_content,
        _SYSTEM_SINGLE_PAPER_RAG,
        history_msgs,
        num_ctx=await effective_num_ctx(db_pool, "smart"),
    )

    messages = [
        {"role": "system", "content": _SYSTEM_SINGLE_PAPER_RAG},
        *history_msgs,
        {"role": "user", "content": user_content},
    ]
    sources_list = [
        {
            "content": c["content"],
            "page_number": c["page_number"],
            "score": round(c["score"], 3),
        }
        for c in chunks
    ]
    return messages, sources_list


async def _search_and_merge_chunks(
    embedder: "Embedder",
    body: CrossPaperAskRequest,
    user_id: int | None,
    library_paper_ids: list[int] | None,
    allowed_paper_ids: list[int] | None,
) -> list[dict]:
    """Search all chunks — optionally via query decomposition — and merge."""
    scope = _cross_paper_search_scope(user_id, library_paper_ids, allowed_paper_ids)
    if body.decompose:
        fast_model = get_fast_model()
        sub_queries = await decompose_query(body.question, model=fast_model)
        per_query_limit = max(body.max_chunks * 2 // len(sub_queries), 3)

        results = await asyncio.gather(
            *(
                embedder.search_chunks_global(
                    query_text=sq,
                    limit=per_query_limit,
                    score_threshold=_SEARCH_SCORE_THRESHOLD,
                    scope=scope,
                )
                for sq in sub_queries
            )
        )

        # Merge: flatten all chunks, dedup by (paper_id, chunk_index) keeping
        # highest score. The relevance cutoff runs per sub-query list — each
        # list's scores are cosine vs its OWN sub-query embedding.
        seen: dict[tuple[int, int], dict] = {}
        for chunk_list in results:
            for chunk in _apply_relative_cutoff(chunk_list):
                key = (chunk["paper_id"], chunk["chunk_index"])
                if key not in seen or chunk["score"] > seen[key]["score"]:
                    seen[key] = chunk
        return list(seen.values())
    return _apply_relative_cutoff(
        await embedder.search_chunks_global(
            query_text=body.question,
            limit=body.max_chunks * 2,
            score_threshold=_SEARCH_SCORE_THRESHOLD,
            scope=scope,
        )
    )


def _cross_paper_search_scope(
    user_id: int | None,
    library_paper_ids: list[int] | None,
    requested_paper_ids: list[int] | None,
) -> SearchScope:
    """Build one validated scope for a cross-paper retrieval request."""
    if requested_paper_ids:
        return SearchScope.explicit_papers(
            user_id,
            requested_paper_ids,
            library_paper_ids or [],
        )
    if user_id is None:
        return SearchScope.internal()
    return SearchScope.caller_corpus(user_id, library_paper_ids or [])


def _requested_cross_paper_ids(requested_paper_ids: list[int] | None) -> list[int] | None:
    """Return unique request restrictions without treating them as grants."""
    if not requested_paper_ids:
        return None
    return list(dict.fromkeys(int(paper_id) for paper_id in requested_paper_ids))


def _select_chunks_by_paper(all_chunks: list[dict], max_papers: int, max_chunks: int) -> list[dict]:
    """Group by paper (top-2 each), trim to max_papers, flatten to max_chunks."""
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
    paper_ids_sorted = paper_ids_sorted[:max_papers]

    # Flatten and trim to max_chunks total
    selected_chunks: list[dict] = []
    for pid in paper_ids_sorted:
        selected_chunks.extend(chunks_by_paper[pid])
    selected_chunks.sort(key=lambda c: c["score"], reverse=True)
    return selected_chunks[:max_chunks]


@observe()
async def prepare_cross_paper_rag(
    embedder: "Embedder",
    db_pool: asyncpg.Pool,
    body: CrossPaperAskRequest,
    http_client: httpx.AsyncClient,
    *,
    user_id: int | None = None,
) -> "CrossPaperRagPrep | CrossPaperRagNoResults":
    """Retrieve chunks across papers, rerank, and build LLM messages.

    Parameters
    ----------
    embedder : Embedder
        Vector search and reranking service.
    db_pool : asyncpg.Pool
        Database pool used for library scope, paper metadata, and model context.
    body : CrossPaperAskRequest
        Validated question, optional paper scope, history, and retrieval limits.
    http_client : httpx.AsyncClient
        Shared service HTTP client.
    user_id : int or None
        Caller identity used to enforce current-generation persisted-public or
        explicit caller-library visibility. ``None`` is trusted internal use.

    Returns
    -------
    CrossPaperRagPrep or CrossPaperRagNoResults
        Prepared messages and sources, or an explicit no-results response.
    """
    with probe_span("prepare_cross_paper_rag", decompose=body.decompose):
        # Load exact private-paper memberships once. Persisted public scope is
        # carried by vector metadata and rechecked in PostgreSQL below.
        library_paper_ids: list[int] | None = None
        if user_id is not None:
            async with db_pool.acquire() as conn:
                lib_rows = await conn.fetch(
                    "SELECT paper_id FROM user_library WHERE user_id = $1",
                    user_id,
                )
            library_paper_ids = [row["paper_id"] for row in lib_rows]

        allowed_paper_ids = _requested_cross_paper_ids(body.paper_ids)

        # Search all chunks, optionally via query decomposition.
        all_chunks = await _search_and_merge_chunks(
            embedder, body, user_id, library_paper_ids, allowed_paper_ids
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

        # Rerank merged results against the original question.
        all_chunks = await embedder.rerank_chunks(
            body.question, all_chunks, top_k=body.max_chunks * 2
        )

        # Apply the backend-aware reranker floor when scores are available.
        _min_rerank = _rerank_score_floor()
        floored = [
            c
            for c in all_chunks
            if c.get("rerank_score") is None or c["rerank_score"] >= _min_rerank
        ]
        if floored:
            all_chunks = floored
        else:
            return CrossPaperRagNoResults(
                answer="No relevant information found in the paper collection.",
                sources=[],
            )

        # Deduplicate and trim the result set to the requested limits.
        selected_chunks = _select_chunks_by_paper(all_chunks, body.max_papers, body.max_chunks)

        # Recheck database visibility so a mistagged vector payload cannot expose
        # another user's paper metadata. Public scope and explicit caller-library
        # membership are the only authenticated access branches.
        unique_paper_ids = list({c["paper_id"] for c in selected_chunks})
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT p.id, p.title, p.authors, p.url FROM papers p"
                " WHERE p.id = ANY($1::int[])"
                " AND ("
                "   $2::int IS NULL"
                f"   OR {paper_visible_sql(2)}"
                " )"
                " AND ($3::int[] IS NULL OR p.id = ANY($3::int[]))",
                unique_paper_ids,
                user_id,
                allowed_paper_ids,
            )
        paper_meta = {row["id"]: row for row in rows}

        # Defense-in-depth: drop chunks the DB visibility check denied so they
        # never reach the prompt or the sources list.
        selected_chunks = [c for c in selected_chunks if c["paper_id"] in paper_meta]
        if not selected_chunks:
            return CrossPaperRagNoResults(
                answer="No relevant information found in the paper collection.",
                sources=[],
            )

        # Build the prompt with one section per paper.
        safe_question = safe_for_prompt(body.question, mode="escape")

        def build_user_content(kept: list[dict]) -> str:
            chunks_for_prompt: dict[int, list[dict]] = {}
            for c in kept:
                chunks_for_prompt.setdefault(c["paper_id"], []).append(c)

            context_sections: list[str] = []
            for i, pid in enumerate(chunks_for_prompt, start=1):
                meta = paper_meta.get(pid)
                title = wrap_delimited("title", meta["title"])[0] if meta else f"Paper ID {pid}"
                excerpts = "\n".join(
                    f'<excerpt page="{c["page_number"] or "?"}">'
                    f"{safe_for_prompt(c['content'], mode='escape')}</excerpt>"
                    for c in chunks_for_prompt[pid]
                )
                context_sections.append(f"--- Paper {i}: {title} ---\n{excerpts}")

            context_block = "\n\n".join(context_sections)
            return f"{context_block}\n\n<question>{safe_question}</question>\n\nANSWER:"

        history_msgs = _build_history_messages(body.history)
        selected_chunks, user_content = _fit_chunks_to_budget(
            selected_chunks,
            build_user_content,
            _SYSTEM_CROSS_PAPER_RAG,
            history_msgs,
            num_ctx=await effective_num_ctx(db_pool, "smart"),
        )

        messages = [
            {"role": "system", "content": _SYSTEM_CROSS_PAPER_RAG},
            *history_msgs,
            {"role": "user", "content": user_content},
        ]
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


def _extract_delta(line: str) -> tuple[str, str | None, bool]:
    """Parse one SSE line → (delta content, model, done). Empty content ⇒ skip."""
    if not line.startswith("data: "):
        return "", None, False
    data_str = line[6:]
    if data_str.strip() == "[DONE]":
        return "", None, True
    chunk = json.loads(data_str)
    model = chunk.get("model") or None
    choices = chunk.get("choices")
    if not choices:
        return "", model, False
    return choices[0].get("delta", {}).get("content", ""), model, False


def _stream_error_message(exc: Exception) -> str:
    """Map a streaming exception to a user-facing error message."""
    _err_msgs = {
        httpx.TimeoutException: "LLM request timed out. Please try again.",
        httpx.ConnectError: "Cannot connect to LLM service. Check that services are running.",
    }
    return next(
        (m for t, m in _err_msgs.items() if isinstance(exc, t)),
        "An error occurred while generating the response. Please try again later.",
    )


async def _confidence_events(
    full_answer: str,
    sources_list: list[dict],
    verifier: "QuoteVerifier | None",
    db_pool: "asyncpg.Pool | None",
):
    """Yield a trailing confidence SSE event after answer verification (or none)."""
    if verifier is None:
        logger.warning(
            "stream_rag_events called with verifier=None; confidence event will be omitted"
        )
    if verifier is not None and db_pool is not None:
        try:
            from paper_ingestion.rag.verification import verify_answer_summary

            summary = await verify_answer_summary(full_answer, sources_list, verifier, db_pool)
            # Nothing checkable → no confidence event at all; the FE renders no badge.
            if summary.get("confidence") is not None:
                yield sse_event({"type": "confidence", **summary})
        except Exception as exc:  # noqa: BLE001 — don't break the stream if verification errors
            logger.warning("RAG verification failed: %s", exc, exc_info=True)


class VisibleAnswerHygieneError(RuntimeError):
    """Raised when a visible streamed answer is empty or unsafe to show."""


def _visible_answer_error_message(full_answer: str) -> str | None:
    """Return a retryable user message when the visible answer is not safe to show."""
    work_notes = detect_visible_work_notes(full_answer)
    if work_notes.has_work_notes:
        logger.warning(
            "RAG answer failed visible-answer hygiene marker=%s",
            work_notes.marker,
        )
    if not full_answer.strip() or work_notes.has_work_notes:
        return "The model did not return a usable final answer. Please try again."
    return None


def _split_visible_guard_segment(pending: str) -> tuple[str, str]:
    """Split flushable paragraph text from the segment still needing prefix checks."""
    matches = list(_PARAGRAPH_BOUNDARY_RE.finditer(pending))
    if not matches:
        return "", pending
    boundary_end = matches[-1].end()
    return pending[:boundary_end], pending[boundary_end:]


def _release_safe_visible_prefix(pending: str, *, final: bool) -> tuple[str, str]:
    """Return text safe to emit now and text that still needs prefix quarantine."""
    flushable, guard_segment = _split_visible_guard_segment(pending)
    if guard_segment.strip():
        visible_answer_error = _visible_answer_error_message(guard_segment)
        if visible_answer_error is not None:
            raise VisibleAnswerHygieneError(visible_answer_error)
    if final or not guard_segment or not could_be_visible_work_note_prefix(guard_segment):
        return pending, ""
    if flushable.strip():
        return flushable, guard_segment
    return "", pending


def _visible_delta_from_line(
    line: str,
    *,
    model_used: str | None,
    in_think: bool,
    think_carry: str,
) -> tuple[str, str | None, bool, bool, str]:
    """Parse one LiteLLM SSE line into visible text and updated stream state."""
    content, chunk_model, done = _extract_delta(line)
    if done:
        return "", model_used, True, in_think, think_carry
    if model_used is None:
        model_used = chunk_model
    if not content:
        return "", model_used, False, in_think, think_carry
    visible, in_think, think_carry = strip_think_streaming(content, in_think, think_carry)
    return visible, model_used, False, in_think, think_carry


async def _stream_validated_visible_answer_parts(
    http_client: httpx.AsyncClient,
    messages: list[dict],
    *,
    model: str,
    answer_parts: list[str],
) -> AsyncIterator[tuple[str, str | None]]:
    """Yield visible deltas once their current paragraph prefix is safe to show.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared client used for the streaming LiteLLM request.
    messages : list[dict]
        Prompt messages sent to the configured model.
    model : str
        LiteLLM model alias.
    answer_parts : list[str]
        Mutable accumulator for all visible answer content.

    Yields
    ------
    tuple[str, str or None]
        A safe visible fragment and the served model identifier, when known.

    Raises
    ------
    OutboundEgressBlockedError
        If restored credentials await review before the stream is opened.
    VisibleAnswerHygieneError
        If a pending visible prefix contains prohibited work-note text, or the
        completed visible answer is empty or unsafe to show.
    """
    litellm_config = get_litellm_config()
    pending = ""
    model_used: str | None = None
    in_think = False
    think_carry = ""
    ensure_outbound_egress_allowed("streaming LLM completion")
    async with http_client.stream(
        "POST",
        f"{litellm_config.base_url}/v1/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": 0.1,
            "max_tokens": _ANSWER_MAX_TOKENS,
        },
        headers=build_litellm_headers(litellm_config),
        timeout=300.0,  # RAG prompts with many chunks need longer prefill
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            visible, model_used, done, in_think, think_carry = _visible_delta_from_line(
                line,
                model_used=model_used,
                in_think=in_think,
                think_carry=think_carry,
            )
            if done:
                break
            if not visible:
                continue
            answer_parts.append(visible)
            pending += visible
            safe_part, pending = _release_safe_visible_prefix(pending, final=False)
            if safe_part:
                yield safe_part, model_used
    if not in_think and think_carry:
        answer_parts.append(think_carry)
        pending += think_carry
    visible_answer_error = _visible_answer_error_message("".join(answer_parts))
    if visible_answer_error is not None:
        raise VisibleAnswerHygieneError(visible_answer_error)
    if pending:
        safe_part, pending = _release_safe_visible_prefix(pending, final=True)
        if safe_part:
            yield safe_part, model_used


@observe(as_type="generation")
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
    answer_parts: list[str] = []
    model_used: str | None = None
    try:
        async for visible, chunk_model in _stream_validated_visible_answer_parts(
            http_client,
            messages,
            model=model,
            answer_parts=answer_parts,
        ):
            if model_used is None:
                model_used = chunk_model
            yield sse_event({"type": "token", "content": visible})
    except VisibleAnswerHygieneError as e:
        async for event in sse_error_stream(str(e)):
            yield event
        return
    except Exception as e:
        msg = _stream_error_message(e)
        logger.error("LLM streaming failed: %r", e, exc_info=True)
        async for event in sse_error_stream(msg):
            yield event
        return

    full_answer = "".join(answer_parts)
    yield sse_event({"type": "sources", "sources": sources_list})
    yield sse_event({"type": "done", "full_answer": full_answer, "model_used": model_used})
    # Sentence-level verification runs only against the validated answer the user saw.
    async for event in _confidence_events(full_answer, sources_list, verifier, db_pool):
        yield event
    yield SSE_DONE
