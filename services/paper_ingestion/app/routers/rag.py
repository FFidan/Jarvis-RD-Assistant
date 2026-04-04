"""RAG (Retrieval-Augmented Generation) and summarization endpoints.

Includes single-paper ask, cross-paper ask, streaming SSE variants,
summarization, and weekly digest.
"""

import json
import logging

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import StreamingResponse

from app.deps import get_db_pool, get_http_client, get_verifier, limiter
from jarvis_common import get_smart_model
from jarvis_common.llm_client import (
    ChatCompletionOptions,
    LLM_TIMEOUT_DEFAULT,
    LITELLM_FALLBACK_ENV_NAMES,
    get_litellm_config,
    request_chat_completion_content,
)
from app.models import (
    AskRequest,
    AskResponse,
    CrossPaperAskRequest,
    SummaryResponse,
    WeeklyDigestResponse,
)
from app.services.summarization import generate_paper_summary
from app.streaming import (
    _prepare_cross_paper_rag,
    _prepare_single_paper_rag,
    _sse_error_stream,
    _stream_rag_events,
)
from app.verification import QuoteVerifier

logger = logging.getLogger(__name__)
router = APIRouter(tags=["rag"])


# ---------------------------------------------------------------------------
# POST /api/summarize/{paper_id}
# ---------------------------------------------------------------------------


@router.post("/api/summarize/{paper_id}", response_model=SummaryResponse)
@limiter.limit("5/minute")
async def summarize_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
) -> SummaryResponse:
    """Generate an LLM summary with quote verification. Rate-limited to 5/minute."""
    return await generate_paper_summary(paper_id, db_pool, http_client, verifier, request.app.state.embedder)


# ---------------------------------------------------------------------------
# POST /api/papers/batch-summarize
# ---------------------------------------------------------------------------


@router.post("/api/papers/batch-summarize")
@limiter.limit("2/minute")
async def batch_summarize_papers(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
):
    """Find processed papers without summaries and summarize them."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.id FROM papers p
               WHERE EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)
                 AND NOT EXISTS (SELECT 1 FROM paper_summaries ps WHERE ps.paper_id = p.id)
               ORDER BY p.created_at DESC LIMIT $1""",
            limit,
        )
    summarized, failed = 0, 0
    for row in rows:
        try:
            await generate_paper_summary(
                row["id"], db_pool, http_client, verifier, request.app.state.embedder,
            )
            summarized += 1
        except Exception:
            logger.exception("Batch summarize failed for paper %d", row["id"])
            failed += 1
    return {"summarized": summarized, "failed": failed, "total_unsummarized": len(rows)}


# ---------------------------------------------------------------------------
# POST /api/papers/{paper_id}/ask -- Conversational RAG
# ---------------------------------------------------------------------------


@router.post("/api/papers/{paper_id}/ask", response_model=AskResponse)
@limiter.limit("20/minute")
async def ask_paper(
    request: Request,
    paper_id: int,
    body: AskRequest,
):
    """Answer a question about a specific paper using RAG.

    Embeds the question, retrieves the top-k relevant chunks from this
    paper's Qdrant vectors, then prompts LiteLLM with the chunks as context.
    Returns the answer with source evidence (chunk content + page number).

    Parameters
    ----------
    paper_id : int
        Database ID of the paper.
    body : AskRequest
        Question and optional max_chunks parameter.

    Returns
    -------
    dict
        {answer: str, sources: [{content, page_number, score}]}
    """
    from app.embedder import Embedder

    embedder: Embedder = request.app.state.embedder
    http_client: httpx.AsyncClient = request.app.state.http_client

    messages, sources_list = await _prepare_single_paper_rag(
        embedder, request.app.state.db_pool, paper_id, body, http_client
    )

    smart_model = get_smart_model()

    litellm_config = get_litellm_config(
        fallback_env_names=LITELLM_FALLBACK_ENV_NAMES
    )

    try:
        answer = await request_chat_completion_content(
            http_client,
            messages=messages,
            options=ChatCompletionOptions(
                model=smart_model,
                max_tokens=512,
                temperature=0.1,
                timeout=LLM_TIMEOUT_DEFAULT,
            ),
            config=litellm_config,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="LLM request timed out")
    except Exception as exc:
        logger.error("RAG LLM call failed for paper %d: %s", paper_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="LLM request failed") from exc

    return {"answer": answer, "sources": sources_list}


# ---------------------------------------------------------------------------
# POST /api/papers/{paper_id}/ask/stream -- Streaming single-paper RAG
# ---------------------------------------------------------------------------


@router.post("/api/papers/{paper_id}/ask/stream")
@limiter.limit("20/minute")
async def ask_paper_stream(
    request: Request,
    paper_id: int,
    body: AskRequest,
):
    """Stream RAG response for a single paper via SSE.

    Uses the same retrieval pipeline as ``/api/papers/{paper_id}/ask`` but
    streams the LLM answer token-by-token as Server-Sent Events.

    Parameters
    ----------
    paper_id : int
        Database ID of the paper.
    body : AskRequest
        Question and optional max_chunks parameter.
    """
    from app.embedder import Embedder

    embedder: Embedder = request.app.state.embedder
    http_client: httpx.AsyncClient = request.app.state.http_client

    try:
        messages, sources = await _prepare_single_paper_rag(
            embedder, request.app.state.db_pool, paper_id, body, http_client
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Streaming RAG preparation failed for paper %d: %r", paper_id, exc, exc_info=True
        )
        return StreamingResponse(
            _sse_error_stream("An error occurred while preparing the response. Please try again."),
            media_type="text/event-stream",
        )

    smart_model = get_smart_model()

    return StreamingResponse(
        _stream_rag_events(http_client, messages, sources, model=smart_model),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# POST /api/ask  (cross-paper RAG)
# ---------------------------------------------------------------------------


@router.post("/api/ask", response_model=AskResponse)
@limiter.limit("10/minute")
async def ask_cross_paper(
    request: Request,
    body: CrossPaperAskRequest,
):
    """Ask a question across ALL embedded papers.

    Retrieves relevant chunks from multiple papers, builds a multi-paper
    context, and generates an answer with per-paper attribution.

    When ``decompose=True`` (default), the question is first broken into
    2-4 sub-queries via LLM.  Each sub-query is searched concurrently,
    results are merged (union + dedup by highest score), and the merged
    set is reranked using the original question.

    Parameters
    ----------
    body : CrossPaperAskRequest
        Question, max_chunks, max_papers, and decompose parameters.

    Returns
    -------
    dict
        {answer: str, sources: [{paper_id, paper_title, content, page_number, score}]}
    """
    from app.embedder import Embedder

    embedder: Embedder = request.app.state.embedder
    http_client: httpx.AsyncClient = request.app.state.http_client

    result = await _prepare_cross_paper_rag(
        embedder, request.app.state.db_pool, body, http_client
    )

    # Short-circuit when no chunks were found (helper returns a dict)
    if isinstance(result, dict):
        return result

    messages, sources_list = result

    smart_model = get_smart_model()

    litellm_config = get_litellm_config(
        fallback_env_names=LITELLM_FALLBACK_ENV_NAMES
    )

    try:
        answer = await request_chat_completion_content(
            http_client,
            messages=messages,
            options=ChatCompletionOptions(
                model=smart_model,
                max_tokens=700,
                temperature=0.1,
                timeout=LLM_TIMEOUT_DEFAULT,
            ),
            config=litellm_config,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="LLM request timed out")
    except Exception as exc:
        logger.error("Cross-paper RAG LLM call failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="LLM request failed") from exc

    return {"answer": answer, "sources": sources_list}


# ---------------------------------------------------------------------------
# POST /api/ask/stream -- Streaming cross-paper RAG
# ---------------------------------------------------------------------------


@router.post("/api/ask/stream")
@limiter.limit("10/minute")
async def ask_cross_paper_stream(
    request: Request,
    body: CrossPaperAskRequest,
):
    """Stream cross-paper RAG response via SSE.

    Uses the same retrieval pipeline as ``/api/ask`` but streams the LLM
    answer token-by-token as Server-Sent Events.

    Parameters
    ----------
    body : CrossPaperAskRequest
        Question, max_chunks, max_papers, and decompose parameters.
    """
    from app.embedder import Embedder

    embedder: Embedder = request.app.state.embedder
    http_client: httpx.AsyncClient = request.app.state.http_client

    try:
        result = await _prepare_cross_paper_rag(
            embedder, request.app.state.db_pool, body, http_client
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Streaming cross-paper RAG preparation failed: %r", exc, exc_info=True)
        return StreamingResponse(
            _sse_error_stream("An error occurred while preparing the response. Please try again."),
            media_type="text/event-stream",
        )

    # Short-circuit when no chunks were found -- return canned answer as SSE
    if isinstance(result, dict):

        async def _no_results_stream():
            yield f'data: {json.dumps({"type": "token", "content": result["answer"]})}\n\n'
            yield f'data: {json.dumps({"type": "sources", "sources": result["sources"]})}\n\n'
            yield f'data: {json.dumps({"type": "done", "full_answer": result["answer"]})}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _no_results_stream(),
            media_type="text/event-stream",
        )

    messages, sources = result

    smart_model = get_smart_model()

    return StreamingResponse(
        _stream_rag_events(http_client, messages, sources, model=smart_model),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# GET /api/digest/weekly
# ---------------------------------------------------------------------------


@router.get("/api/digest/weekly", response_model=WeeklyDigestResponse)
@limiter.limit("5/minute")
async def get_weekly_digest(
    request: Request,
    days: int = Query(default=7, ge=1, le=30),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
):
    """Generate a weekly research digest grouped by topic.

    Groups recent papers by topic and uses LLM cross-paper synthesis
    to identify themes across each topic cluster.

    Parameters
    ----------
    days : int
        Number of days to look back (1-30, default 7).

    Returns
    -------
    dict
        {topics, total_papers, period_start, period_end}
    """
    from app.digest import generate_weekly_digest

    return await generate_weekly_digest(db_pool, http_client, days=days)
