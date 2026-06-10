"""RAG (Retrieval-Augmented Generation) and summarization endpoints.

Includes single-paper ask, cross-paper ask, streaming SSE variants,
summarization, and weekly digest.
"""

import json
import logging
import os

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, JobCreateResponse, get_smart_model
from jarvis_common.auth import get_current_user_id
from jarvis_common.db_helpers import assert_paper_ownership
from jarvis_common.litellm_observer import observed_share
from jarvis_common.llm_client import (
    LLM_TIMEOUT_DEFAULT,
    ChatCompletionOptions,
    EmptyVisibleLLMContentError,
    call_llm_structured,
    get_litellm_config,
    observe,
    strip_think_blocks,
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
    BatchSummarizeResponse,
    CrossPaperAskRequest,
    WeeklyDigestResponse,
)
from paper_ingestion.rag.exceptions import NoRelevantChunksError, PaperNotFoundError
from paper_ingestion.rag.streaming import (
    CrossPaperRagNoResults,
    prepare_cross_paper_rag,
    prepare_single_paper_rag,
    sse_error_stream,
    stream_rag_events,
)
from paper_ingestion.rag.verification import verify_answer_summary

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

# Allows bench to exempt itself without application code risk; default preserves
# the production rate limit (10/minute per user/IP).
_ASK_RATE_LIMIT = os.getenv("ASK_RATE_LIMIT", "10/minute")


class _RagServiceNotReadyError(RuntimeError):
    """Raised when the OpenAI client is not yet initialised (startup misconfiguration)."""


def _is_timeout_failure(exc: BaseException) -> bool:
    """Return True when an exception chain came from an httpx timeout."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, httpx.TimeoutException):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _empty_visible_detail() -> dict[str, str]:
    return {
        "status": "degraded",
        "code": "llm_empty_visible_content",
        "message": "LLM response contained no visible answer.",
    }


def _strip_think_blocks(text: str) -> str:
    """Strip <think>…</think> blocks from final RAG answer text.

    Thin wrapper around jarvis_common.llm_client.strip_think_blocks so Pyright
    can resolve the symbol on import.
    """
    return strip_think_blocks(text)


@observe()
async def _call_rag_llm(
    messages: list[dict[str, str]],
    *,
    smart_model: str,
) -> "AskResponse":
    """Call the LLM for a RAG answer and return a validated AskResponse.

    Extracted from the ask_paper / ask_cross_paper handlers so that
    Langfuse can instrument the LLM span via ``@observe()``.
    Uses ``svc.openai_client`` set by the service lifespan.

    Parameters
    ----------
    messages:
        Fully-built message list (system + context + user).
    smart_model:
        Resolved model alias (e.g. ``"smart"`` or a concrete name).
    """
    from paper_ingestion._state import svc  # noqa: PLC0415

    _openai_client = svc.openai_client
    if _openai_client is None:
        raise _RagServiceNotReadyError(
            "openai_client not initialized — check _init_langfuse_hook ran during lifespan"
        )
    # messages is built by prepare_single_paper_rag / prepare_cross_paper_rag, both of which
    # emit [system, user] Shape-A pairs (PI-02/PI-03); the static checker cannot follow a variable.
    # llm-prompt-shape: SINGLE-USER
    return await call_llm_structured(
        _openai_client,
        response_model=AskResponse,
        messages=messages,
        options=ChatCompletionOptions(
            model=smart_model,
            max_tokens=700,
            temperature=0.1,
            timeout=LLM_TIMEOUT_DEFAULT,
        ),
        config=get_litellm_config(),
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
    user_id: int = Depends(get_current_user_id),
) -> JobCreateResponse:
    """Enqueue LLM summary generation with quote verification."""
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


@router.post("/papers/batch-summarize", response_model=BatchSummarizeResponse, status_code=202)
@limiter.limit("2/minute")
async def batch_summarize_papers(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
    user_id: int = Depends(get_current_user_id),
) -> dict[str, int | str | None]:
    """Enqueue a single batch-summarize job for processed papers without summaries.

    Returns immediately with a ``job_id`` that can be polled via
    ``GET /api/jobs/{job_id}``.
    """
    import uuid  # noqa: PLC0415

    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    # Select only papers in the caller's user_library. The resolver
    # hard-401s sessionless callers, so the previous unscoped corpus
    # fallback (which leaked every user's papers) is removed.
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.id FROM papers p
               JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $2
               WHERE EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)
                 AND NOT EXISTS (SELECT 1 FROM paper_summaries ps WHERE ps.paper_id = p.id)
               ORDER BY p.created_at DESC LIMIT $1""",
            limit,
            user_id,
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
    user_id: int = Depends(get_current_user_id),
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
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    try:
        messages, raw_sources = await prepare_single_paper_rag(
            embedder, db_pool, paper_id, body, http_client, user_id=user_id
        )
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Paper not found") from exc
    except NoRelevantChunksError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    smart_model = get_smart_model()

    try:
        ask_result = await _call_rag_llm(messages, smart_model=smart_model)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="LLM request timed out") from exc
    except EmptyVisibleLLMContentError as exc:
        logger.warning("RAG LLM returned no visible content for paper %d", paper_id, exc_info=True)
        raise HTTPException(status_code=502, detail=_empty_visible_detail()) from exc
    except _RagServiceNotReadyError as exc:
        raise HTTPException(status_code=503, detail="RAG service not initialized") from exc
    except RuntimeError as exc:
        if _is_timeout_failure(exc):
            raise HTTPException(status_code=504, detail="LLM request timed out") from exc
        logger.error("RAG LLM call failed for paper %d: %s", paper_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="LLM request failed") from exc
    except Exception as exc:
        logger.error("RAG LLM call failed for paper %d: %s", paper_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="LLM request failed") from exc

    answer = _strip_think_blocks(ask_result.answer)

    # Enrich sources with paper_id for verification
    sources = [{**s, "paper_id": paper_id} for s in raw_sources]

    verification: dict[str, object] = {
        "confidence": None,
        "verified_fraction": None,
        "per_sentence": [],
    }
    try:
        verification = await verify_answer_summary(answer, sources, verifier, db_pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG verification failed for paper %d: %s", paper_id, exc, exc_info=True)

    return {
        "answer": answer,
        "sources": sources,
        **verification,
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
    user_id: int = Depends(get_current_user_id),
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
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    try:
        messages, raw_sources = await prepare_single_paper_rag(
            embedder, db_pool, paper_id, body, http_client, user_id=user_id
        )
    except PaperNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Paper not found") from exc
    except NoRelevantChunksError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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

    async def _stream_with_backend_badge():
        served, _ = observed_share("smart")
        configured = os.getenv("JARVIS_LLM_BACKEND", "ollama")
        is_fallback = bool(served and configured == "vllm" and served.startswith("ollama/"))
        payload = json.dumps({"served_by": served, "fallback": is_fallback})
        yield f"event: backend\ndata: {payload}\n\n"
        async for event in stream_rag_events(
            http_client,
            messages,
            sources,
            model=smart_model,
            verifier=verifier,
            db_pool=db_pool,
        ):
            yield event

    return StreamingResponse(
        _stream_with_backend_badge(),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# POST /api/ask  (cross-paper RAG)
# ---------------------------------------------------------------------------


@router.post("/ask", response_model=AskResponse)
@limiter.limit(_ASK_RATE_LIMIT)
async def ask_cross_paper(
    request: Request,
    body: CrossPaperAskRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder=Depends(get_embedder),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
    user_id: int = Depends(get_current_user_id),
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
    result = await prepare_cross_paper_rag(embedder, db_pool, body, http_client, user_id=user_id)

    # Short-circuit when no chunks were found
    if isinstance(result, CrossPaperRagNoResults):
        return {"answer": result.answer, "sources": result.sources}

    messages, sources_list = result.messages, result.sources

    smart_model = get_smart_model()

    try:
        ask_result = await _call_rag_llm(messages, smart_model=smart_model)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="LLM request timed out") from exc
    except EmptyVisibleLLMContentError as exc:
        logger.warning("Cross-paper RAG LLM returned no visible content", exc_info=True)
        raise HTTPException(status_code=502, detail=_empty_visible_detail()) from exc
    except _RagServiceNotReadyError as exc:
        raise HTTPException(status_code=503, detail="RAG service not initialized") from exc
    except RuntimeError as exc:
        if _is_timeout_failure(exc):
            raise HTTPException(status_code=504, detail="LLM request timed out") from exc
        logger.error("Cross-paper RAG LLM call failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="LLM request failed") from exc
    except Exception as exc:
        logger.error("Cross-paper RAG LLM call failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="LLM request failed") from exc

    answer = _strip_think_blocks(ask_result.answer)

    verification: dict[str, object] = {
        "confidence": None,
        "verified_fraction": None,
        "per_sentence": [],
    }
    try:
        verification = await verify_answer_summary(answer, sources_list, verifier, db_pool)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cross-paper RAG verification failed: %s", exc, exc_info=True)

    return {
        "answer": answer,
        "sources": sources_list,
        **verification,
    }


# ---------------------------------------------------------------------------
# POST /api/ask/stream -- Streaming cross-paper RAG
# ---------------------------------------------------------------------------


@router.post("/ask/stream")
@limiter.limit(_ASK_RATE_LIMIT)
async def ask_cross_paper_stream(
    request: Request,
    body: CrossPaperAskRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder=Depends(get_embedder),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    verifier: QuoteVerifier = Depends(get_verifier),
    user_id: int = Depends(get_current_user_id),
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

    served, _ = observed_share("smart")
    configured = os.getenv("JARVIS_LLM_BACKEND", "ollama")
    is_fallback = bool(served and configured == "vllm" and served.startswith("ollama/"))
    _backend_event = (
        f"event: backend\ndata: {json.dumps({'served_by': served, 'fallback': is_fallback})}\n\n"
    )

    # Short-circuit when no chunks were found -- return canned answer as SSE
    if isinstance(result, CrossPaperRagNoResults):
        no_result = result

        async def _no_results_stream():
            yield _backend_event
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

    async def _cross_paper_stream_with_backend_badge():
        yield _backend_event
        async for event in stream_rag_events(
            http_client,
            messages,
            sources,
            model=smart_model,
            verifier=verifier,
            db_pool=db_pool,
        ):
            yield event

    return StreamingResponse(
        _cross_paper_stream_with_backend_badge(),
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
    user_id: int = Depends(get_current_user_id),
) -> dict[str, object]:
    """Generate a weekly research digest grouped by topic.

    Groups recent papers by topic and uses LLM cross-paper synthesis
    to identify themes across each topic cluster.  Each theme is verified
    against the topic corpus and returned in ``verified_themes`` /
    ``unverified_themes`` (ephemeral — not persisted to DB).

    ``user_id`` is resolved from the session via ``get_current_user_id``
    rather than accepted as a query parameter (which was an IDOR vector —
    any authenticated user could pass any user_id and read another user's
    digest).

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
    user_id: int = Depends(get_current_user_id),
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
    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["digest.weekly"].defer_async(
        job_id=jarvis_job_id, days=days, user_id=user_id
    )
    return JobCreateResponse(job_id=jarvis_job_id, status="queued")
