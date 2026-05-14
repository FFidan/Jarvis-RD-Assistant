"""RAG (Retrieval-Augmented Generation) and summarization endpoints.

Includes single-paper ask, cross-paper ask, streaming SSE variants,
summarization, and weekly digest.
"""

import logging

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, JobCreateResponse, get_smart_model
from jarvis_common.auth import current_user_id_or_none
from jarvis_common.db_helpers import assert_paper_ownership
from jarvis_common.llm_client import (
    LLM_TIMEOUT_DEFAULT,
    ChatCompletionOptions,
    get_litellm_config,
    request_chat_completion_content,
)
from jarvis_common.sse import SSE_DONE, sse_event
from jarvis_common.verify import QuoteVerifier
from starlette.responses import StreamingResponse

from paper_ingestion.deps import (
    get_db_pool,
    get_embedder,
    get_http_client,
    get_verifier,
    limiter,
)
from paper_ingestion.models import (
    AskRequest,
    AskResponse,
    CrossPaperAskRequest,
    WeeklyDigestResponse,
)
from paper_ingestion.rag.streaming import (
    CrossPaperRagNoResults,
    prepare_cross_paper_rag,
    prepare_single_paper_rag,
    sse_error_stream,
    stream_rag_events,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api",
    tags=["rag"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)


# ---------------------------------------------------------------------------
# POST /api/summarize/{paper_id}
# ---------------------------------------------------------------------------


@router.post("/summarize/{paper_id}", response_model=JobCreateResponse, status_code=202)
@limiter.limit("5/minute")
async def summarize_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JobCreateResponse:
    """Enqueue LLM summary generation with quote verification."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    import uuid  # noqa: PLC0415

    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["paper.summarize"].defer_async(
        job_id=jarvis_job_id, user_id=user_id, paper_id=paper_id
    )
    return JobCreateResponse(job_id=jarvis_job_id, status="queued")


# ---------------------------------------------------------------------------
# POST /api/papers/batch-summarize
# ---------------------------------------------------------------------------


@router.post("/papers/batch-summarize")
@limiter.limit("2/minute")
async def batch_summarize_papers(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
) -> dict[str, int | str | None]:
    """Enqueue a single batch-summarize job for processed papers without summaries.

    Returns immediately with a ``job_id`` that can be polled via
    ``GET /api/jobs/{job_id}``.
    """
    import uuid  # noqa: PLC0415

    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    # Sprint B: select only papers in the caller's user_library; in
    # single-user mode (user_id=None) fall back to the canonical corpus.
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        if user_id is not None:
            rows = await conn.fetch(
                """SELECT p.id FROM papers p
                   JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $2
                   WHERE EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)
                     AND NOT EXISTS (SELECT 1 FROM paper_summaries ps WHERE ps.paper_id = p.id)
                   ORDER BY p.created_at DESC LIMIT $1""",
                limit,
                user_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT p.id FROM papers p
                   WHERE EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)
                     AND NOT EXISTS (SELECT 1 FROM paper_summaries ps WHERE ps.paper_id = p.id)
                   ORDER BY p.created_at DESC LIMIT $1""",
                limit,
            )
    paper_ids = [row["id"] for row in rows]
    job_id: str | None = None
    if paper_ids:
        jarvis_job_id = str(uuid.uuid4())
        await KIND_TO_TASK["papers.batch_summarize"].defer_async(
            job_id=jarvis_job_id,
            user_id=user_id,
            paper_ids=paper_ids,
        )
        job_id = jarvis_job_id
    return {"total_unsummarized": len(rows), "job_id": job_id}


# ---------------------------------------------------------------------------
# POST /api/papers/{paper_id}/ask -- Conversational RAG
# ---------------------------------------------------------------------------


@router.post("/papers/{paper_id}/ask", response_model=AskResponse)
@limiter.limit("20/minute")
async def ask_paper(
    request: Request,
    paper_id: int,
    body: AskRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder=Depends(get_embedder),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
) -> dict[str, object]:
    """Answer a question about a specific paper using RAG.

    Embeds the question, retrieves the top-k relevant chunks from this
    paper's Qdrant vectors, then prompts LiteLLM with the chunks as context.
    Returns the answer with source evidence (chunk content + page number) and
    a sentence-level confidence score from the anti-hallucination verifier.

    Parameters
    ----------
    paper_id : int
        Database ID of the paper.
    body : AskRequest
        Question and optional max_chunks parameter.

    Returns
    -------
    dict
        {answer: str, sources: [...], confidence: str, verified_fraction: float}
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    messages, raw_sources = await prepare_single_paper_rag(
        embedder, db_pool, paper_id, body, http_client
    )

    smart_model = get_smart_model()

    litellm_config = get_litellm_config()

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

    # Enrich sources with paper_id for verification
    sources = [{**s, "paper_id": paper_id} for s in raw_sources]

    confidence: str | None = None
    verified_fraction: float | None = None
    per_sentence: list[dict[str, object]] = []
    try:
        from paper_ingestion.rag.verification import verify_answer_sentences

        report = await verify_answer_sentences(answer, sources, verifier, db_pool)
        confidence = report.confidence.value
        verified_fraction = report.pass_rate
        per_sentence = [{"text": s.text, "verified": s.verified} for s in report.per_sentence]
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG verification failed for paper %d: %s", paper_id, exc, exc_info=True)

    return {
        "answer": answer,
        "sources": sources,
        "confidence": confidence,
        "verified_fraction": verified_fraction,
        "per_sentence": per_sentence,
    }


# ---------------------------------------------------------------------------
# POST /api/papers/{paper_id}/ask/stream -- Streaming single-paper RAG
# ---------------------------------------------------------------------------


@router.post("/papers/{paper_id}/ask/stream")
@limiter.limit("20/minute")
async def ask_paper_stream(
    request: Request,
    paper_id: int,
    body: AskRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder=Depends(get_embedder),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
) -> StreamingResponse:
    """Stream RAG response for a single paper via SSE.

    Uses the same retrieval pipeline as ``/api/papers/{paper_id}/ask`` but
    streams the LLM answer token-by-token as Server-Sent Events.  After the
    answer is fully streamed, emits a ``confidence`` event with sentence-level
    verification results.

    Parameters
    ----------
    paper_id : int
        Database ID of the paper.
    body : AskRequest
        Question and optional max_chunks parameter.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    try:
        messages, raw_sources = await prepare_single_paper_rag(
            embedder, db_pool, paper_id, body, http_client
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Streaming RAG preparation failed for paper %d: %r", paper_id, exc, exc_info=True
        )
        return StreamingResponse(
            sse_error_stream("An error occurred while preparing the response. Please try again."),
            media_type="text/event-stream",
        )

    # Enrich sources with paper_id so verification can fetch full text per paper
    sources = [{**s, "paper_id": paper_id} for s in raw_sources]

    smart_model = get_smart_model()

    return StreamingResponse(
        stream_rag_events(
            http_client,
            messages,
            sources,
            model=smart_model,
            verifier=verifier,
            db_pool=db_pool,
        ),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# POST /api/ask  (cross-paper RAG)
# ---------------------------------------------------------------------------


@router.post("/ask", response_model=AskResponse)
@limiter.limit("10/minute")
async def ask_cross_paper(
    request: Request,
    body: CrossPaperAskRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder=Depends(get_embedder),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
) -> dict[str, object]:
    """Ask a question across ALL embedded papers.

    Retrieves relevant chunks from multiple papers, builds a multi-paper
    context, and generates an answer with per-paper attribution and a
    sentence-level confidence score from the anti-hallucination verifier.

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
        {answer: str, sources: [...], confidence: str, verified_fraction: float}
    """
    user_id = await current_user_id_or_none(request)
    result = await prepare_cross_paper_rag(embedder, db_pool, body, http_client, user_id=user_id)

    # Short-circuit when no chunks were found
    if isinstance(result, CrossPaperRagNoResults):
        return {"answer": result.answer, "sources": result.sources}

    messages, sources_list = result.messages, result.sources

    smart_model = get_smart_model()

    litellm_config = get_litellm_config()

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

    confidence: str | None = None
    verified_fraction: float | None = None
    per_sentence: list[dict[str, object]] = []
    try:
        from paper_ingestion.rag.verification import verify_answer_sentences

        report = await verify_answer_sentences(answer, sources_list, verifier, db_pool)
        confidence = report.confidence.value
        verified_fraction = report.pass_rate
        per_sentence = [{"text": s.text, "verified": s.verified} for s in report.per_sentence]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cross-paper RAG verification failed: %s", exc, exc_info=True)

    return {
        "answer": answer,
        "sources": sources_list,
        "confidence": confidence,
        "verified_fraction": verified_fraction,
        "per_sentence": per_sentence,
    }


# ---------------------------------------------------------------------------
# POST /api/ask/stream -- Streaming cross-paper RAG
# ---------------------------------------------------------------------------


@router.post("/ask/stream")
@limiter.limit("10/minute")
async def ask_cross_paper_stream(
    request: Request,
    body: CrossPaperAskRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder=Depends(get_embedder),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
) -> StreamingResponse:
    """Stream cross-paper RAG response via SSE.

    Uses the same retrieval pipeline as ``/api/ask`` but streams the LLM
    answer token-by-token as Server-Sent Events.  After the answer is fully
    streamed, emits a ``confidence`` event with sentence-level verification
    results.

    Parameters
    ----------
    body : CrossPaperAskRequest
        Question, max_chunks, max_papers, and decompose parameters.
    """
    user_id = await current_user_id_or_none(request)
    try:
        result = await prepare_cross_paper_rag(
            embedder, db_pool, body, http_client, user_id=user_id
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Streaming cross-paper RAG preparation failed: %r", exc, exc_info=True)
        return StreamingResponse(
            sse_error_stream("An error occurred while preparing the response. Please try again."),
            media_type="text/event-stream",
        )

    # Short-circuit when no chunks were found -- return canned answer as SSE
    if isinstance(result, CrossPaperRagNoResults):
        no_result = result

        async def _no_results_stream():
            yield sse_event({"type": "token", "content": no_result.answer})
            yield sse_event({"type": "sources", "sources": no_result.sources})
            yield sse_event({"type": "done", "full_answer": no_result.answer})
            yield SSE_DONE

        return StreamingResponse(
            _no_results_stream(),
            media_type="text/event-stream",
        )

    messages, sources = result.messages, result.sources

    smart_model = get_smart_model()

    return StreamingResponse(
        stream_rag_events(
            http_client,
            messages,
            sources,
            model=smart_model,
            verifier=verifier,
            db_pool=db_pool,
        ),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# GET /api/digest/weekly
# ---------------------------------------------------------------------------


@router.get("/digest/weekly", response_model=WeeklyDigestResponse)
@limiter.limit("5/minute")
async def get_weekly_digest(
    request: Request,
    days: int = Query(default=7, ge=1, le=30),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
) -> dict[str, object]:
    """Generate a weekly research digest grouped by topic.

    Groups recent papers by topic and uses LLM cross-paper synthesis
    to identify themes across each topic cluster.  Each theme is verified
    against the topic corpus and returned in ``verified_themes`` /
    ``unverified_themes`` (ephemeral — not persisted to DB).

    Phase 2 WS-2D: ``user_id`` is resolved from the session via
    ``current_user_id_or_none`` rather than accepted as a query parameter
    (which was an IDOR vector pre-WS-2A — any authenticated user could pass
    any user_id and read another user's digest).

    Parameters
    ----------
    days : int
        Number of days to look back (1-30, default 7).

    Returns
    -------
    dict
        {topics, total_papers, period_start, period_end}
    """
    from paper_ingestion.weekly_summary import generate_weekly_summary

    _ = http_client  # weekly_summary uses openai_client directly; dep kept for backwards-compat.
    user_id = await current_user_id_or_none(request)
    return await generate_weekly_summary(
        db_pool,
        days=days,
        verifier=verifier,
        user_id=user_id,
        openai_client=getattr(request.app.state, "openai_client", None),
    )


@router.post("/digest/weekly", response_model=JobCreateResponse, status_code=202)
@limiter.limit("3/hour")
async def enqueue_weekly_digest(
    request: Request,
    days: int = Query(default=7, ge=1, le=30),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JobCreateResponse:
    """Enqueue weekly digest regeneration while keeping GET synchronous.

    B.4 Step 3 canary: defers via procrastinate (``digest.weekly`` task) rather
    than the legacy ``jobs`` table. The JARVIS UUID we return is the same one
    embedded in the procrastinate row's ``args->>'job_id'`` so the SSE bridge
    can correlate.
    """
    import uuid  # noqa: PLC0415

    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    _ = db_pool  # retained for future use; procrastinate uses its own connector
    user_id = await current_user_id_or_none(request)
    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["digest.weekly"].defer_async(
        job_id=jarvis_job_id, days=days, user_id=user_id
    )
    return JobCreateResponse(job_id=jarvis_job_id, status="queued")
