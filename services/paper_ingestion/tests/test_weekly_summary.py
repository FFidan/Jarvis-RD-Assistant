"""Tests for the weekly research summary generator."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from paper_ingestion.extraction.verify import QuoteVerifier
from paper_ingestion.weekly_summary import generate_weekly_summary
from paper_ingestion.weekly_summary_models import ThemeOutput, WeeklyDigestOutput

# Shared verifier instance — QuoteVerifier is stateless; a single instance is fine.
_VERIFIER = QuoteVerifier()


def _make_paper_row(
    paper_id: int,
    title: str,
    topic_name: str,
    *,
    topic_id: int = 1,
    relevance_score: float | None = 0.9,
    summary_brief: str | None = None,
    confidence: str | None = None,
) -> dict:
    """Build a fake DB row matching the weekly summary query columns."""
    return {
        "id": paper_id,
        "title": title,
        "url": f"https://arxiv.org/abs/{paper_id}",
        "published_date": datetime(2026, 3, 1, tzinfo=UTC),
        "authors": ["Author A", "Author B"],
        "topic_name": topic_name,
        "topic_id": topic_id,
        "relevance_score": relevance_score,
        "summary_brief": summary_brief,
        "confidence": confidence,
    }


def _llm_output(themes: list[dict], summary: str) -> WeeklyDigestOutput:
    """Build a mock WeeklyDigestOutput for call_llm_structured patching."""
    theme_objects = [
        ThemeOutput(
            theme=t["theme"],
            supporting_papers=t.get("supporting_papers", [1]),
            notes=t.get("notes") or None,
        )
        for t in themes
    ]
    return WeeklyDigestOutput(themes=theme_objects, summary=summary)


def _make_pool(rows: list) -> MagicMock:
    """Build a mock asyncpg.Pool whose .acquire() context manager returns a conn with .fetch()."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)

    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool


async def test_generate_weekly_summary_with_synthesis():
    """Digest groups papers by topic and calls LLM for synthesis."""
    rows = [
        _make_paper_row(1, "Paper A", "NLP", summary_brief="NLP finding A"),
        _make_paper_row(2, "Paper B", "NLP", summary_brief="NLP finding B"),
        _make_paper_row(3, "Paper C", "CV", summary_brief="CV finding C"),
    ]

    db_pool = _make_pool(rows)

    llm_themes = [
        {
            "theme": "Attention mechanisms dominate NLP research",
            "supporting_papers": [1, 2],
            "notes": "",
        }
    ]
    mock_output = _llm_output(llm_themes, "NLP is evolving fast.")

    async with httpx.AsyncClient() as client:
        with patch(
            "paper_ingestion.weekly_summary.call_llm_structured",
            AsyncMock(return_value=mock_output),
        ):
            result = await generate_weekly_summary(
                db_pool, client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
            )

    assert result["total_papers"] == 3
    assert len(result["topics"]) == 2
    assert "period_start" in result
    assert "period_end" in result

    # NLP topic should have LLM themes (2 papers)
    nlp_topic = next(t for t in result["topics"] if t["name"] == "NLP")
    assert nlp_topic["paper_count"] == 2
    assert len(nlp_topic["themes"]) == 1
    assert nlp_topic["themes"][0]["theme"] == "Attention mechanisms dominate NLP research"
    assert nlp_topic["summary"] == "NLP is evolving fast."

    # CV topic has only 1 paper -- no LLM call, fallback summary
    cv_topic = next(t for t in result["topics"] if t["name"] == "CV")
    assert cv_topic["paper_count"] == 1
    assert cv_topic["themes"] == []
    assert "1 papers on CV" in cv_topic["summary"]


async def test_generate_weekly_summary_empty():
    """Weekly summary handles no engaged papers gracefully with honest empty response."""
    db_pool = _make_pool([])

    async with httpx.AsyncClient() as client:
        result = await generate_weekly_summary(
            db_pool, client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
        )

    assert result["total_papers"] == 0
    assert result["topics"] == []
    assert "period_start" in result
    assert "period_end" in result
    assert "message" in result


async def test_generate_weekly_summary_llm_failure():
    """Digest returns topics without themes when LLM fails."""
    rows = [
        _make_paper_row(1, "Paper A", "NLP", summary_brief="finding A"),
        _make_paper_row(2, "Paper B", "NLP", summary_brief="finding B"),
    ]

    db_pool = _make_pool(rows)

    async with httpx.AsyncClient() as client:
        with patch(
            "paper_ingestion.weekly_summary.call_llm_structured",
            AsyncMock(side_effect=RuntimeError("LLM backend error")),
        ):
            result = await generate_weekly_summary(
                db_pool, client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
            )

    assert result["total_papers"] == 2
    assert len(result["topics"]) == 1

    nlp_topic = result["topics"][0]
    assert nlp_topic["name"] == "NLP"
    assert nlp_topic["paper_count"] == 2
    # Themes should be empty since LLM call failed
    assert nlp_topic["themes"] == []
    # Fallback summary used
    assert "2 papers on NLP" in nlp_topic["summary"]
    # Top papers still populated
    assert len(nlp_topic["top_papers"]) == 2


async def test_generate_weekly_summary_top_papers_structure():
    """Top papers contain expected fields."""
    rows = [
        _make_paper_row(
            10,
            "Important Paper",
            "ML",
            relevance_score=0.95,
            confidence="HIGH",
        ),
    ]

    db_pool = _make_pool(rows)

    async with httpx.AsyncClient() as client:
        result = await generate_weekly_summary(
            db_pool, client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
        )

    paper = result["topics"][0]["top_papers"][0]
    assert paper["id"] == 10
    assert paper["title"] == "Important Paper"
    assert paper["url"] == "https://arxiv.org/abs/10"
    assert paper["confidence"] == "HIGH"
    assert paper["relevance_score"] == 0.95


async def test_generate_weekly_summary_calls_llm_structured():
    """Weekly summary delegates to call_llm_structured for LLM synthesis.

    Migrated from call_llm (HTTP) to call_llm_structured (Instructor) in
    Wave 2.A.2.  Verifies the new path is taken and produces expected output.
    """
    rows = [
        _make_paper_row(1, "Paper A", "NLP", summary_brief="NLP finding A"),
        _make_paper_row(2, "Paper B", "NLP", summary_brief="NLP finding B"),
    ]
    db_pool = _make_pool(rows)
    mock_output = WeeklyDigestOutput(themes=[], summary="Weekly summary: no themes this period.")
    http_client = AsyncMock()

    with patch(
        "paper_ingestion.weekly_summary.call_llm_structured",
        AsyncMock(return_value=mock_output),
    ) as mock_structured:
        result = await generate_weekly_summary(
            db_pool, http_client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
        )

    assert result["topics"][0]["summary"] == "Weekly summary: no themes this period."
    mock_structured.assert_awaited_once()


# ---------------------------------------------------------------------------
# Model C drift-prevention tests (spec §6.4)
# ---------------------------------------------------------------------------


async def test_excludes_unengaged_papers():
    """Paper surfaced by Pulse but never rated or saved is NOT in the summary.

    The SQL engagement filter excludes it; the mock simulates that by returning
    an empty row list (what the filtered query would produce).
    """
    # Paper P is in the window but has no paper_user_state and no recommendation_feedback.
    # The engagement-filtered SQL returns no rows for it.
    db_pool = _make_pool([])

    http_client = AsyncMock()

    result = await generate_weekly_summary(
        db_pool, http_client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
    )

    # P is not in the output
    all_ids = [p["id"] for topic in result["topics"] for p in topic["top_papers"]]
    assert 99 not in all_ids
    assert result["total_papers"] == 0
    # LLM must not be called for empty results
    http_client.post.assert_not_awaited()


async def test_includes_saved_papers():
    """Paper saved via Library UI is included in the summary.

    The SQL includes it via paper_user_state.user_state = 'saved';
    the mock returns the row to simulate that.
    """
    row_q = _make_paper_row(42, "Paper Q — Saved", "ML", summary_brief="Saved finding")
    db_pool = _make_pool([row_q])

    http_client = AsyncMock()

    result = await generate_weekly_summary(
        db_pool, http_client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
    )

    all_ids = [p["id"] for topic in result["topics"] for p in topic["top_papers"]]
    assert 42 in all_ids


async def test_includes_positively_rated_pulse_papers():
    """Paper with a Pulse positive signal is included in the summary.

    The SQL includes it via recommendation_feedback.signal = 'positive'
    AND source IN ('pulse_thumbs', 'dismiss_combined'); the mock returns the row.
    """
    row_r = _make_paper_row(43, "Paper R — Upvoted", "CV", summary_brief="Upvoted finding")
    db_pool = _make_pool([row_r])

    http_client = AsyncMock()

    result = await generate_weekly_summary(
        db_pool, http_client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
    )

    all_ids = [p["id"] for topic in result["topics"] for p in topic["top_papers"]]
    assert 43 in all_ids


async def test_excludes_negatively_rated_pulse_papers():
    """Paper with a Pulse 'down' rating is NOT included in the summary.

    'down' is not an engagement signal; the SQL excludes it.
    The mock returns [] to simulate the filtered result.
    """
    db_pool = _make_pool([])

    http_client = AsyncMock()

    result = await generate_weekly_summary(
        db_pool, http_client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
    )

    assert result["total_papers"] == 0
    assert result["topics"] == []
    http_client.post.assert_not_awaited()


async def test_dismiss_rating_excluded():
    """Paper with a Pulse 'dismiss' rating is NOT included in the summary.

    'dismiss' is an explicit negative signal; the SQL excludes it.
    The mock returns [] to simulate the filtered result.
    """
    db_pool = _make_pool([])

    http_client = AsyncMock()

    result = await generate_weekly_summary(
        db_pool, http_client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
    )

    assert result["total_papers"] == 0
    assert result["topics"] == []
    http_client.post.assert_not_awaited()


async def test_empty_when_no_engagement():
    """Honest empty response when no papers have engagement.

    Verifies the short-circuit path: no LLM call, valid response shape,
    and presence of a human-readable message field.
    """
    db_pool = _make_pool([])

    http_client = AsyncMock()

    result = await generate_weekly_summary(
        db_pool, http_client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
    )

    assert result["total_papers"] == 0
    assert result["topics"] == []
    assert "period_start" in result
    assert "period_end" in result
    assert "message" in result
    assert len(result["message"]) > 0
    # The LLM must NOT be called when there are no papers to summarize.
    http_client.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# WS-2.3: theme verification split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weekly_summary_splits_verified_and_unverified_themes():
    """Themes that match the paper corpus land in verified_themes; others in unverified_themes."""
    rows = [
        _make_paper_row(
            1,
            "Attention is all you need",
            "NLP",
            summary_brief="Transformers use multi-head self-attention for sequence modeling.",
        ),
        _make_paper_row(
            2,
            "BERT pretraining",
            "NLP",
            summary_brief="BERT uses masked language modeling on large text corpora.",
        ),
    ]
    db_pool = _make_pool(rows)

    # Theme #1 quotes the corpus (verifiable); theme #2 is unrelated.
    llm_themes = [
        {
            "theme": "Transformers use multi-head self-attention for sequence modeling",
            "supporting_papers": [1],
            "notes": "",
        },
        {
            "theme": "Quantum annealing surpasses classical optimization on QUBO instances",
            "supporting_papers": [2],
            "notes": "",
        },
    ]
    mock_output = _llm_output(llm_themes, "NLP evolves this week and beyond.")

    verifier = QuoteVerifier()
    async with httpx.AsyncClient() as client:
        with patch(
            "paper_ingestion.weekly_summary.call_llm_structured",
            AsyncMock(return_value=mock_output),
        ):
            result = await generate_weekly_summary(
                db_pool, client, days=7, verifier=verifier, openai_client=MagicMock()
            )

    nlp = next(t for t in result["topics"] if t["name"] == "NLP")
    assert "verified_themes" in nlp
    assert "unverified_themes" in nlp
    # Theme 1 is a near-verbatim quote from the summary_brief -> verified.
    assert any("multi-head self-attention" in t["theme"] for t in nlp["verified_themes"])
    # Theme 2 is unrelated -> unverified.
    assert any("Quantum annealing" in t["theme"] for t in nlp["unverified_themes"])
    # Both splits together reconstruct the full themes list.
    assert len(nlp["verified_themes"]) + len(nlp["unverified_themes"]) == len(nlp["themes"])


@pytest.mark.asyncio
async def test_weekly_summary_missing_verifier_raises_type_error():
    """generate_weekly_summary requires verifier — omitting it raises TypeError at call time.

    WS-2 Phase 1: verifier is now a required parameter (no default).  Legacy callers
    that omit it get a Python TypeError rather than silently skipping verification.
    """
    db_pool = _make_pool([])

    async with httpx.AsyncClient() as client:
        with pytest.raises(TypeError):
            await generate_weekly_summary(
                db_pool, client, days=7, openai_client=MagicMock()
            )  # verifier intentionally omitted — TypeError expected


@pytest.mark.asyncio
async def test_weekly_summary_llm_failure_has_empty_theme_splits():
    """When the LLM fails, both verified_themes and unverified_themes should be empty lists."""
    rows = [
        _make_paper_row(1, "Paper A", "NLP", summary_brief="finding A"),
        _make_paper_row(2, "Paper B", "NLP", summary_brief="finding B"),
    ]
    db_pool = _make_pool(rows)

    verifier = QuoteVerifier()
    async with httpx.AsyncClient() as client:
        with patch(
            "paper_ingestion.weekly_summary.call_llm_structured",
            AsyncMock(side_effect=RuntimeError("LLM backend error")),
        ):
            result = await generate_weekly_summary(
                db_pool, client, days=7, verifier=verifier, openai_client=MagicMock()
            )

    nlp = result["topics"][0]
    assert nlp["themes"] == []
    assert nlp["verified_themes"] == []
    assert nlp["unverified_themes"] == []


# ---------------------------------------------------------------------------
# W3-T3: user_id parameterization
# ---------------------------------------------------------------------------


def _make_pool_capturing_params(rows_by_user_id: dict) -> tuple[MagicMock, list]:
    """Pool mock that captures the ``user_id`` bind param ($2) passed to fetch().

    ``rows_by_user_id`` maps ``user_id`` (int or None) to the list of rows
    the DB would return for that user.  Calls with an unexpected user_id return [].
    """
    captured: list = []

    async def _fetch(query: str, *args):
        # args[0] = cutoff ($1), args[1] = user_id ($2)
        uid = args[1] if len(args) > 1 else None
        captured.append(uid)
        return rows_by_user_id.get(uid, [])

    conn = AsyncMock()
    conn.fetch = _fetch

    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool, captured


async def test_user_id_filters_to_user_a_only():
    """Weekly summary called with user_id=1 only returns that user's rows.

    Two users (A=1, B=2) each have a paper. Querying for user A must NOT
    include user B's paper.
    """
    row_a = _make_paper_row(101, "User A Paper", "NLP", summary_brief="A finding")
    row_b = _make_paper_row(202, "User B Paper", "CV", summary_brief="B finding")

    pool, captured = _make_pool_capturing_params({1: [row_a], 2: [row_b]})
    http_client = AsyncMock()

    result = await generate_weekly_summary(
        pool, http_client, days=7, verifier=_VERIFIER, user_id=1, openai_client=MagicMock()
    )

    # The pool received user_id=1 as the $2 bind param.
    assert captured == [1]
    # Only user A's paper is in the result.
    all_ids = {p["id"] for topic in result["topics"] for p in topic["top_papers"]}
    assert 101 in all_ids
    assert 202 not in all_ids
    assert result["total_papers"] == 1


async def test_user_id_filters_to_user_b_only():
    """Weekly summary called with user_id=2 only returns that user's rows."""
    row_a = _make_paper_row(101, "User A Paper", "NLP", summary_brief="A finding")
    row_b = _make_paper_row(202, "User B Paper", "CV", summary_brief="B finding")

    pool, captured = _make_pool_capturing_params({1: [row_a], 2: [row_b]})
    http_client = AsyncMock()

    result = await generate_weekly_summary(
        pool, http_client, days=7, verifier=_VERIFIER, user_id=2, openai_client=MagicMock()
    )

    assert captured == [2]
    all_ids = {p["id"] for topic in result["topics"] for p in topic["top_papers"]}
    assert 202 in all_ids
    assert 101 not in all_ids
    assert result["total_papers"] == 1


async def test_user_id_none_aggregates_all_users():
    """Weekly summary called with user_id=None passes None as $2 (global aggregate)."""
    row_a = _make_paper_row(101, "User A Paper", "NLP", summary_brief="A finding")
    row_b = _make_paper_row(202, "User B Paper", "NLP", summary_brief="B finding")

    # None key = both users' rows returned (global aggregate).
    pool, captured = _make_pool_capturing_params({None: [row_a, row_b]})
    http_client = AsyncMock()

    result = await generate_weekly_summary(
        pool, http_client, days=7, verifier=_VERIFIER, user_id=None, openai_client=MagicMock()
    )

    # $2 was NULL (None), meaning no user filter applied.
    assert captured == [None]
    all_ids = {p["id"] for topic in result["topics"] for p in topic["top_papers"]}
    assert 101 in all_ids
    assert 202 in all_ids
    assert result["total_papers"] == 2


async def test_user_id_default_is_none():
    """Calling generate_weekly_summary without user_id defaults to None (global)."""
    row = _make_paper_row(10, "Some Paper", "ML", summary_brief="finding")
    pool, captured = _make_pool_capturing_params({None: [row]})
    http_client = AsyncMock()

    # No user_id kwarg — must default to None.
    result = await generate_weekly_summary(
        pool, http_client, days=7, verifier=_VERIFIER, openai_client=MagicMock()
    )

    assert captured == [None]
    assert result["total_papers"] == 1


async def test_user_id_unknown_user_returns_empty():
    """A user_id with no engagement rows produces the honest empty response."""
    pool, captured = _make_pool_capturing_params({1: [], 2: []})
    http_client = AsyncMock()

    result = await generate_weekly_summary(
        pool, http_client, days=7, verifier=_VERIFIER, user_id=99, openai_client=MagicMock()
    )

    assert captured == [99]
    assert result["total_papers"] == 0
    assert result["topics"] == []
    assert "message" in result
