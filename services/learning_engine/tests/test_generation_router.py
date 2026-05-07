"""Direct tests for the card generation router (jobs-backed implementation)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.db_helpers import get_smart_model  # noqa: E402
from jarvis_common.jobs import JobContext, JobError  # noqa: E402
from learning_engine import _state as le_state  # noqa: E402
from learning_engine.models import BatchGenerateRequest, GenerateCardsRequest  # noqa: E402
from learning_engine.routers import generation  # noqa: E402


class FakeRecord(dict):
    """Dict-like asyncpg.Record substitute."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


def _now():
    return datetime.now(UTC)


def _make_pool_and_conn():
    """Create a mock pool whose acquire() returns an async context manager."""
    conn = AsyncMock()

    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _make_card_row(id=1, deck_id=1, paper_id=1):
    """Return a fake row compatible with row_to_card_response."""
    return FakeRecord(
        id=id,
        deck_id=deck_id,
        paper_id=paper_id,
        card_type="concept",
        front="What changed?",
        back="The method improved retrieval.",
        evidence={"quote": "Improved retrieval", "page_number": 2},
        fsrs_state={},
        due_at=_now(),
        created_at=_now(),
        updated_at=_now(),
    )


def _make_ctx(job_id="test-job-001"):
    """Return a minimal JobContext stub."""
    ctx = MagicMock(spec=JobContext)
    ctx.job_id = job_id
    ctx.update_progress = AsyncMock()
    ctx.is_cancelled = AsyncMock(return_value=False)
    return ctx


def test_get_smart_model_returns_alias():
    """get_smart_model always returns the 'smart' LiteLLM alias."""
    assert get_smart_model() == "smart"


def test_get_smart_model_no_conn_param():
    """get_smart_model requires no arguments (conn was removed)."""
    assert get_smart_model() == "smart"


# ---------------------------------------------------------------------------
# generate_cards_core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_cards_core_success():
    """generate_cards_core fetches chunks, calls LLM, inserts cards, returns dict."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()

    fsrs_manager = MagicMock()
    fsrs_manager.create_new_card.return_value = ({"state": "new"}, _now())

    card_generator = AsyncMock()
    card_generator.generate_cards.return_value = {
        "cards": [
            {
                "card_type": "concept",
                "front": "What changed?",
                "back": "The method improved retrieval.",
                "evidence": {"quote": "Improved retrieval", "page_number": 2},
            }
        ],
        "confidence": "HIGH",
    }

    # fetchval (deck) → fetchrow (paper) → fetch (chunks) in the same acquire()
    conn.fetchval.return_value = 1  # deck exists
    conn.fetchrow.return_value = FakeRecord(
        id=101, title="Paper 101", authors=["Ada"], abstract="A paper"
    )
    conn.fetch.return_value = [FakeRecord(id=1, content="chunk", page_number=2)]

    with (
        patch.object(generation, "get_smart_model", MagicMock(return_value="smart")),
        patch.object(
            generation, "insert_card", AsyncMock(return_value=_make_card_row(id=501, paper_id=101))
        ),
        patch.object(le_state.svc, "openai_client", MagicMock()),
    ):
        result = await generation.generate_cards_core(
            pool=pool,
            http_client=http_client,
            paper_id=101,
            deck_id=1,
            max_cards=5,
            fsrs_manager=fsrs_manager,
            card_generator=card_generator,
        )

    assert result["cards_created"] == 1
    assert result["confidence"] == "HIGH"


@pytest.mark.asyncio
async def test_generate_cards_core_no_chunks_raises_job_error():
    """generate_cards_core raises JobError with action_link when paper has no chunks."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()

    conn.fetchval.return_value = 1  # deck exists
    conn.fetchrow.return_value = FakeRecord(
        id=42, title="Empty Paper", authors=["Bob"], abstract=None
    )
    conn.fetch.return_value = []  # no chunks

    card_generator = AsyncMock()
    fsrs_manager = MagicMock()

    with pytest.raises(JobError) as exc_info:
        await generation.generate_cards_core(
            pool=pool,
            http_client=http_client,
            paper_id=42,
            deck_id=1,
            max_cards=5,
            fsrs_manager=fsrs_manager,
            card_generator=card_generator,
        )

    exc = exc_info.value
    assert "no processed chunks" in str(exc).lower()
    assert exc.action_link is not None
    assert exc.action_link["href"] == "/paper/42?action=process"
    assert "Process PDF" in exc.action_link["label"]
    card_generator.generate_cards.assert_not_called()


@pytest.mark.asyncio
async def test_generate_cards_core_propagates_progress():
    """generate_cards_core calls ctx.update_progress at each stage."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()
    ctx = _make_ctx()

    fsrs_manager = MagicMock()
    fsrs_manager.create_new_card.return_value = ({"state": "new"}, _now())

    card_generator = AsyncMock()
    card_generator.generate_cards.return_value = {"cards": [], "confidence": "LOW"}

    conn.fetchval.return_value = 1
    conn.fetchrow.return_value = FakeRecord(id=7, title="T", authors=[], abstract=None)
    conn.fetch.return_value = [FakeRecord(id=1, content="chunk", page_number=1)]

    with (
        patch.object(generation, "get_smart_model", MagicMock(return_value="smart")),
        patch.object(generation, "insert_card", AsyncMock(return_value=_make_card_row())),
        patch.object(le_state.svc, "openai_client", MagicMock()),
    ):
        await generation.generate_cards_core(
            pool=pool,
            http_client=http_client,
            paper_id=7,
            deck_id=1,
            max_cards=5,
            fsrs_manager=fsrs_manager,
            card_generator=card_generator,
            ctx=ctx,
        )

    # Should have been called at least at 0.2, 0.3, 0.4, 0.85, 1.0
    assert ctx.update_progress.await_count >= 5
    progress_values = [call.args[0] for call in ctx.update_progress.await_args_list]
    assert 1.0 in progress_values


# ---------------------------------------------------------------------------
# generate_cards endpoint (now enqueues a job)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_cards_endpoint_returns_job_id():
    """POST /api/generate enqueues a procrastinate job and returns {job_id, status}."""
    pool, conn = _make_pool_and_conn()

    import jarvis_common.task_registry as task_registry

    mock_card_generate_task = MagicMock()
    mock_defer = AsyncMock()
    mock_card_generate_task.defer_async = mock_defer
    with patch.dict(task_registry.KIND_TO_TASK, {"card.generate": mock_card_generate_task}):
        response = await generation.generate_cards.__wrapped__(
            MagicMock(),
            body=GenerateCardsRequest(paper_id=101, deck_id=1),
            db_pool=pool,
        )

    assert response.status == "queued"
    assert response.job_id  # UUID generated inside the handler
    mock_defer.assert_awaited_once()
    call_kwargs = mock_defer.await_args
    assert call_kwargs is not None
    assert call_kwargs.kwargs["paper_id"] == 101
    assert call_kwargs.kwargs["deck_id"] == 1
    assert call_kwargs.kwargs["job_id"] == response.job_id
    assert call_kwargs.kwargs["user_id"] is None


# ---------------------------------------------------------------------------
# batch_generate_cards endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_generate_cards_returns_202_with_job_id():
    """POST /api/generate/batch enqueues a procrastinate job and returns 202 with job_id."""
    pool, conn = _make_pool_and_conn()
    conn.fetchval.return_value = 1  # deck exists

    import jarvis_common.task_registry as task_registry

    mock_card_generate_batch_task = MagicMock()
    mock_defer = AsyncMock()
    mock_card_generate_batch_task.defer_async = mock_defer
    with patch.dict(
        task_registry.KIND_TO_TASK, {"card.generate_batch": mock_card_generate_batch_task}
    ):
        response = await generation.batch_generate_cards.__wrapped__(
            MagicMock(),
            body=BatchGenerateRequest(deck_id=1),
            db_pool=pool,
        )

    assert response.status == "queued"
    assert response.job_id  # UUID generated inside the handler
    mock_defer.assert_awaited_once()
    call_kwargs = mock_defer.await_args
    assert call_kwargs is not None
    assert call_kwargs.kwargs["deck_id"] == 1
    assert call_kwargs.kwargs["job_id"] == response.job_id
    assert call_kwargs.kwargs["user_id"] is None


# ---------------------------------------------------------------------------
# _card_generate_batch_job — records errors per paper, continues loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_job_handler_records_missing_chunks_error():
    """_card_generate_batch_job records a JobError per paper and continues."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()
    ctx = _make_ctx()

    # Paper list query returns one paper
    conn.fetch.return_value = [FakeRecord(id=99)]

    # generate_cards_core will raise JobError (no chunks)
    with patch.object(
        generation,
        "generate_cards_core",
        AsyncMock(
            side_effect=JobError(
                "Paper has no processed chunks",
                action_link={"label": "Process PDF now", "href": "/paper/99?action=process"},
            )
        ),
    ):
        result = await generation._card_generate_batch_job(
            pool=pool,
            http_client=http_client,
            payload={"deck_id": 1, "max_per_paper": 5},
            ctx=ctx,
        )

    assert result["papers_processed"] == 0
    assert result["cards_created"] == 0
    assert len(result["errors"]) == 1
    assert "99" in result["errors"][0]


# ---------------------------------------------------------------------------
# generate_cards_core raises JobError (not HTTPException) for missing deck/paper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_cards_core_deck_not_found_raises_job_error():
    """generate_cards_core raises JobError (not HTTPException) when deck is missing."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()

    conn.fetchval.return_value = None  # deck does NOT exist

    card_generator = AsyncMock()
    fsrs_manager = MagicMock()

    with pytest.raises(JobError) as exc_info:
        await generation.generate_cards_core(
            pool=pool,
            http_client=http_client,
            paper_id=999999,
            deck_id=999999,
            max_cards=5,
            fsrs_manager=fsrs_manager,
            card_generator=card_generator,
        )

    exc = exc_info.value
    # str(JobError) returns the message passed to __init__
    assert str(exc) == "Deck not found"
    # Must NOT contain the "404:" prefix that HTTPException would produce
    assert "404" not in str(exc)


@pytest.mark.asyncio
async def test_generate_cards_core_deck_not_found_no_404_prefix():
    """str(JobError) is exactly 'Deck not found' — no '404:' prefix."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()
    conn.fetchval.return_value = None  # deck missing

    card_generator = AsyncMock()
    fsrs_manager = MagicMock()

    with pytest.raises(JobError) as exc_info:
        await generation.generate_cards_core(
            pool=pool,
            http_client=http_client,
            paper_id=1,
            deck_id=999999,
            max_cards=5,
            fsrs_manager=fsrs_manager,
            card_generator=card_generator,
        )

    assert str(exc_info.value) == "Deck not found"


@pytest.mark.asyncio
async def test_generate_cards_core_paper_not_found_raises_job_error():
    """generate_cards_core raises JobError when paper is missing."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()

    conn.fetchval.return_value = 1  # deck exists
    conn.fetchrow.return_value = None  # paper does NOT exist

    card_generator = AsyncMock()
    fsrs_manager = MagicMock()

    with pytest.raises(JobError) as exc_info:
        await generation.generate_cards_core(
            pool=pool,
            http_client=http_client,
            paper_id=999999,
            deck_id=1,
            max_cards=5,
            fsrs_manager=fsrs_manager,
            card_generator=card_generator,
        )

    exc = exc_info.value
    assert str(exc) == "Paper not found"
    assert "404" not in str(exc)
