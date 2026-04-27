"""Unit tests for paper_ingestion.rag.verification — verify_answer_sentences().

Coverage:
- Empty / non-alphanumeric input → UNVERIFIED with zero totals
- Happy path: all verified → HIGH confidence
- Partial verification → MEDIUM / LOW / UNVERIFIED confidence
- Multiple papers in sources
- 60-sentence answer (batch path: >50 sentences)
- Memoisation: DB fetched exactly once per paper_id per call
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest  # noqa: F401 — pytest is needed for pytest.mark.asyncio and pytest.approx
from paper_ingestion.rag.verification import (
    RagConfidence,
    RagVerificationReport,
    VerifiedSentence,
    _make_chunk_responses,
    verify_answer_sentences,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_verifier(verified_flags: list[bool]):
    """Return a QuoteVerifier stub whose verify_quote cycles through *verified_flags*.

    Each successive call returns a result where .verified matches the next flag.
    The stub also sets .match_type="exact" so the threshold branch is skipped.
    """
    call_idx = 0
    flags = verified_flags

    def _verify_quote(quote, full_text, chunks, _normalized_full=None):
        nonlocal call_idx
        flag = flags[call_idx % len(flags)]
        call_idx += 1
        result = MagicMock()
        result.verified = flag
        result.match_type = "exact" if flag else None
        result.match_score = 1.0 if flag else None
        return result

    verifier = MagicMock()
    verifier.verify_quote.side_effect = _verify_quote
    return verifier


def _always_verified_verifier():
    """Verifier that always returns verified=True, match_type='exact'."""
    result = MagicMock()
    result.verified = True
    result.match_type = "exact"
    result.match_score = 1.0
    verifier = MagicMock()
    verifier.verify_quote.return_value = result
    return verifier


def _never_verified_verifier():
    """Verifier that always returns verified=False."""
    result = MagicMock()
    result.verified = False
    result.match_type = None
    result.match_score = None
    verifier = MagicMock()
    verifier.verify_quote.return_value = result
    return verifier


def _make_pool_with_conn(conn: AsyncMock) -> MagicMock:
    """Wrap *conn* in a mock asyncpg pool whose .acquire() context manager yields it."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _fake_conn(rows_by_paper_id: dict[int, list[dict]]) -> AsyncMock:
    """Build a mock asyncpg connection whose .fetch() returns per-paper rows."""

    async def _fetch(sql, paper_id):  # noqa: ARG001
        return rows_by_paper_id.get(paper_id, [])

    conn = AsyncMock()
    conn.fetch.side_effect = _fetch
    return conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_answer_returns_unverified():
    """Empty string → total=0, confidence=UNVERIFIED, per_sentence=[]."""
    pool = MagicMock()
    verifier = _always_verified_verifier()
    report = await verify_answer_sentences("", [], verifier, pool)

    assert isinstance(report, RagVerificationReport)
    assert report.total == 0
    assert report.verified_count == 0
    assert report.pass_rate == 0.0
    assert report.confidence is RagConfidence.UNVERIFIED
    assert report.per_sentence == []
    # Pool should NOT have been touched
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_answer_with_no_alphanumeric_returns_unverified():
    """Punctuation-only input → total=0, UNVERIFIED (no sentences extracted)."""
    pool = MagicMock()
    verifier = _always_verified_verifier()
    for text in ("...", "!!!", "   ", "---"):
        report = await verify_answer_sentences(text, [], verifier, pool)
        assert report.total == 0
        assert report.confidence is RagConfidence.UNVERIFIED
        assert report.per_sentence == []


@pytest.mark.asyncio
async def test_happy_path_all_verified():
    """Three sentences, all verified → confidence=HIGH, pass_rate=1.0."""
    answer = (
        "Neural ODEs are continuous-depth models. "
        "They generalise ResNets to infinite depth. "
        "Training uses the adjoint method."
    )
    sources = [{"content": "chunk text about neural ODEs", "page_number": 1}]

    verifier = _always_verified_verifier()
    conn = AsyncMock()
    conn.fetch.return_value = []
    pool = _make_pool_with_conn(conn)

    # No paper_ids in sources → single-paper / synthetic path (no DB fetch)
    report = await verify_answer_sentences(answer, sources, verifier, pool)

    assert report.total == 3
    assert report.verified_count == 3
    assert report.pass_rate == 1.0
    assert report.confidence is RagConfidence.HIGH
    assert len(report.per_sentence) == 3
    assert all(s.verified for s in report.per_sentence)


@pytest.mark.asyncio
async def test_medium_pass_rate():
    """5 sentences, 3 verified → MEDIUM (pass_rate=0.6 >= 0.5)."""
    sentences = [f"Sentence number {i} is meaningful content." for i in range(5)]
    answer = " ".join(sentences)
    sources = [{"content": "some chunk", "page_number": 1}]

    # Flags cycle: True, True, True, False, False → 3 verified
    verifier = _stub_verifier([True, True, True, False, False])

    pool = MagicMock()
    pool.acquire = MagicMock()  # not called on the synthetic path

    report = await verify_answer_sentences(answer, sources, verifier, pool)

    assert report.total == 5
    assert report.verified_count == 3
    assert pytest.approx(report.pass_rate, abs=1e-6) == 0.6
    assert report.confidence is RagConfidence.MEDIUM


@pytest.mark.asyncio
async def test_low_pass_rate():
    """5 sentences, 1 verified → LOW (0 < pass_rate < 0.5)."""
    sentences = [f"Sentence number {i} is meaningful content." for i in range(5)]
    answer = " ".join(sentences)
    sources = [{"content": "some chunk", "page_number": 1}]

    verifier = _stub_verifier([True, False, False, False, False])

    pool = MagicMock()

    report = await verify_answer_sentences(answer, sources, verifier, pool)

    assert report.total == 5
    assert report.verified_count == 1
    assert report.confidence is RagConfidence.LOW


@pytest.mark.asyncio
async def test_unverified_with_sentences():
    """3 sentences, 0 verified → UNVERIFIED with total=3 and pass_rate=0.0."""
    answer = (
        "First claim about something. "
        "Second claim about something else. "
        "Third claim entirely unsubstantiated."
    )
    sources = [{"content": "unrelated chunk", "page_number": 1}]

    verifier = _never_verified_verifier()
    pool = MagicMock()

    report = await verify_answer_sentences(answer, sources, verifier, pool)

    assert report.total == 3
    assert report.verified_count == 0
    assert report.pass_rate == 0.0
    assert report.confidence is RagConfidence.UNVERIFIED
    assert len(report.per_sentence) == 3
    assert all(not s.verified for s in report.per_sentence)
    assert all(isinstance(s, VerifiedSentence) for s in report.per_sentence)


@pytest.mark.asyncio
async def test_multiple_papers_in_sources():
    """Sources span 2 paper_ids; a sentence matches if ANY paper's chunks verify it."""
    # Two papers, sentence is verified against paper_id=2 only
    sources = [
        {"paper_id": 1, "chunk_index": 0, "content": "paper 1 content", "page_number": 1},
        {"paper_id": 2, "chunk_index": 0, "content": "paper 2 content", "page_number": 3},
    ]
    answer = (
        "Paper one discusses topic A. "
        "Paper two discusses topic B. "
        "Both papers agree on the conclusion."
    )

    # DB returns one chunk row per paper
    rows_by_pid = {
        1: [{"content": "paper 1 content"}],
        2: [{"content": "paper 2 content"}],
    }
    conn = _fake_conn(rows_by_pid)
    pool = _make_pool_with_conn(conn)

    # Always-verified: every sentence verified against first paper tried
    verifier = _always_verified_verifier()

    report = await verify_answer_sentences(answer, sources, verifier, pool)

    assert report.total == 3
    assert report.verified_count == 3
    assert report.confidence is RagConfidence.HIGH
    # DB should have been queried for both paper_ids
    assert conn.fetch.call_count == 2


@pytest.mark.asyncio
async def test_batching_over_50_sentences():
    """60-sentence answer exercises the asyncio.gather batching branch (>50 sentences)."""
    sentences = [f"This is sentence number {i} about machine learning." for i in range(60)]
    answer = " ".join(sentences)
    sources = [{"content": "machine learning content", "page_number": 1}]

    verifier = _always_verified_verifier()
    pool = MagicMock()

    report = await verify_answer_sentences(answer, sources, verifier, pool)

    # 60 sentences: all processed without error
    assert report.total == 60
    assert len(report.per_sentence) == 60
    # All verified with always-verified verifier
    assert report.verified_count == 60
    assert report.confidence is RagConfidence.HIGH


@pytest.mark.asyncio
async def test_memoization_single_paper_fulltext_fetch():
    """DB queried exactly once for a given paper_id, regardless of sentence count."""
    paper_id = 42
    sources = [
        {"paper_id": paper_id, "chunk_index": 0, "content": f"chunk {i}", "page_number": i}
        for i in range(5)
    ]
    # 5 sentences, all from the same paper_id=42
    answer = " ".join(f"Sentence {i} discusses content from chunk {i}." for i in range(5))

    chunk_rows = [{"content": f"chunk {i}"} for i in range(5)]

    async def _fetch(sql, pid):  # noqa: ARG001
        return chunk_rows

    conn = AsyncMock()
    conn.fetch.side_effect = _fetch
    pool = _make_pool_with_conn(conn)

    verifier = _always_verified_verifier()

    report = await verify_answer_sentences(answer, sources, verifier, pool)

    assert report.total == 5
    # DB must have been queried exactly once for paper_id=42
    assert conn.fetch.call_count == 1
    call_args = conn.fetch.call_args
    # Second positional arg is paper_id
    assert call_args.args[1] == paper_id


# ---------------------------------------------------------------------------
# M7: consolidated _make_chunk_responses
# ---------------------------------------------------------------------------


def test_make_chunk_responses_consolidated_optional_skip():
    """M7: consolidated _make_chunk_responses with and without skip_paper_id."""
    sources = [
        {"paper_id": 1, "chunk_index": 0, "content": "chunk for paper 1", "page_number": 1},
        {"paper_id": 2, "chunk_index": 0, "content": "chunk for paper 2", "page_number": 2},
        {"content": "chunk without paper_id", "page_number": 3},
    ]

    # --- No skip_paper_id → no-pid path: all sources with "content" included, paper_id=-1 ---
    no_skip = _make_chunk_responses(sources)
    assert len(no_skip) == 3
    assert all(c.paper_id == -1 for c in no_skip)
    contents_no_skip = [c.content for c in no_skip]
    assert "chunk for paper 1" in contents_no_skip
    assert "chunk for paper 2" in contents_no_skip
    assert "chunk without paper_id" in contents_no_skip

    # --- skip_paper_id=1 → keep only sources whose paper_id matches 1
    #     (or sources with no paper_id set); assigned paper_id == 1 ---
    with_pid = _make_chunk_responses(sources, skip_paper_id=1)
    # paper_id=2 source should be filtered out; paper_id=1 and no-pid source are kept
    contents_with_pid = [c.content for c in with_pid]
    assert "chunk for paper 1" in contents_with_pid
    assert "chunk for paper 2" not in contents_with_pid
    assert "chunk without paper_id" in contents_with_pid
    assert all(c.paper_id == 1 for c in with_pid)
