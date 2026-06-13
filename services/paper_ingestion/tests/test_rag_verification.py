"""Unit tests for rag/verification.py — PI-10 sentence splitter coverage.

PI-10: _SENTENCE_RE must NOT split on lowercase-continuation (e.g. "one. and this").
       Also verifies digit and quote boundaries ARE split correctly.

Also covers RAG verification calibration (v0.7):
- the trailing "Citations:" block is stripped before sentence splitting,
- short non-claim segments (< 4 words) are excluded from the verification set,
- the grounded-support path accepts >= RAG_SUPPORT_FUZZY matches that the
  SHARED QuoteVerifier still rejects at its untouched verbatim bar (97).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from jarvis_common.verify import FUZZY_THRESHOLD, DictChunk, QuoteVerifier

from paper_ingestion.rag.verification import (
    _SENTENCE_RE,
    RAG_SUPPORT_FUZZY,
    RagConfidence,
    _build_confidence,
    _split_sentences,
    verify_answer_sentences,
)

if TYPE_CHECKING:
    import asyncpg


def _unused_pool() -> asyncpg.Pool:
    """Sentinel pool — the single-paper path (no paper_id) must never touch the DB."""

    class _ExplodingPool:
        def acquire(self):
            raise AssertionError("DB pool must not be used in the single-paper path")

    return cast("asyncpg.Pool", _ExplodingPool())


# ---------------------------------------------------------------------------
# PI-10: sentence regex behaviour
# ---------------------------------------------------------------------------


def test_sentence_re_does_not_split_lowercase_continuation():
    """PI-10: 'This is one. and this should be a continuation.' must yield 1 sentence, not 2."""
    text = "This is one. and this should be a continuation of one."
    parts = _SENTENCE_RE.split(text)
    assert len(parts) == 1, (
        f"Lowercase-continuation must NOT be counted as a new sentence; got {len(parts)} parts: {parts}"
    )


def test_sentence_re_splits_on_uppercase():
    """Normal sentence boundary with uppercase start must be split."""
    text = "First sentence. Second sentence."
    parts = _SENTENCE_RE.split(text)
    assert len(parts) == 2, f"Expected 2 parts; got {len(parts)}: {parts}"


def test_sentence_re_splits_on_digit():
    """Sentence boundary followed by a digit must be split (PI-10 extension)."""
    text = "There are two results. 3 of them are significant."
    parts = _SENTENCE_RE.split(text)
    assert len(parts) == 2, (
        f"Digit-start continuation should be a new sentence; got {len(parts)}: {parts}"
    )


def test_sentence_re_splits_on_question_and_exclamation():
    """? and ! boundaries with uppercase continuation must split."""
    text = "Is this correct? Yes, it is. Really!"
    parts = _SENTENCE_RE.split(text)
    assert len(parts) >= 2, f"Expected ≥2 parts; got {len(parts)}: {parts}"


def test_split_sentences_filters_empty_segments():
    """_split_sentences must drop empty / non-alphanumeric fragments."""
    text = "Only one sentence."
    sentences = _split_sentences(text)
    assert sentences == ["Only one sentence."]


def test_split_sentences_multiple():
    """_split_sentences returns one element per sentence boundary."""
    text = "Alpha sentence. Beta sentence. Gamma sentence."
    sentences = _split_sentences(text)
    assert len(sentences) == 3, f"Expected 3 sentences; got {sentences}"


def test_split_sentences_lowercase_continuation_is_one_sentence():
    """End-to-end: _split_sentences treats lowercase-continuation as single sentence."""
    text = "This is one. and this continues the thought."
    sentences = _split_sentences(text)
    assert len(sentences) == 1, (
        f"Lowercase continuation must not generate a second sentence; got: {sentences}"
    )


# ---------------------------------------------------------------------------
# RAG verification calibration (v0.7)
# ---------------------------------------------------------------------------

_CLAIM = "The model improves benchmark accuracy substantially across evaluated datasets."


async def test_citations_block_excluded():
    """The trailing Citations block (multi-line AND single-line tail) is stripped."""
    sources = [{"content": _CLAIM + " Additional supporting context follows here."}]
    verifier = QuoteVerifier()

    multi_line = _CLAIM + "\nCitations:\n[1] Vaswani et al., Attention Is All You Need."
    # Single-line tail with >= 4 words so it would FAIL verification if not stripped
    # (i.e. this form is not rescued by the short-segment filter alone).
    single_line = _CLAIM + "\nCitations: [Paper 1], [Paper 2], [Paper 3]"

    for answer in (multi_line, single_line):
        report = await verify_answer_sentences(answer, sources, verifier, _unused_pool())
        texts = [s.text for s in report.per_sentence]
        assert report.total == 1, f"citations block leaked into sentences: {texts}"
        assert all("citation" not in t.lower() for t in texts), texts
        assert all("[1]" not in t and "[Paper" not in t for t in texts), texts
        assert report.verified_count == 1
        assert report.pass_rate == 1.0


async def test_short_label_segments_skipped():
    """A <= 3-word segment (e.g. 'In summary:') is excluded from the total count."""
    sources = [{"content": _CLAIM}]
    verifier = QuoteVerifier()

    answer = _CLAIM + " In summary:"
    report = await verify_answer_sentences(answer, sources, verifier, _unused_pool())

    texts = [s.text for s in report.per_sentence]
    assert report.total == 1, f"short label must not count toward total; got: {texts}"
    assert "In summary:" not in texts
    assert report.verified_count == 1
    assert report.pass_rate == 1.0  # the label counted neither as pass nor fail
    assert report.confidence == RagConfidence.HIGH


# Fixture pair for the grounded-support band: the paraphrase swaps
# "mechanisms" -> "modules" and "representations" -> "encodings", which lands
# its rapidfuzz partial_ratio against the chunk at ~86.5 (in [80, 97)).
_SUPPORT_CHUNK = (
    "The transformer architecture relies entirely on self-attention mechanisms "
    "to compute representations of its input and output sequences, dispensing "
    "with recurrence and convolutions entirely."
)
_SUPPORT_PARAPHRASE = (
    "The transformer architecture relies entirely on self-attention modules "
    "to compute encodings of its input and output sequences."
)


async def test_support_bar_accepts_what_97_rejects():
    """A ~86 partial_ratio paraphrase passes the support path while the SHARED verifier
    still rejects it at the untouched 97 verbatim bar."""
    verifier = QuoteVerifier()

    # 1) Direct check against the shared verifier: NOT verified at 97 …
    chunks = [DictChunk({"id": 0, "content": _SUPPORT_CHUNK, "page_number": None})]
    result = verifier.verify_quote(_SUPPORT_PARAPHRASE, _SUPPORT_CHUNK, chunks)
    assert result.verified is False, "shared verifier must NOT have been loosened"
    assert result.match_score is not None
    score = result.match_score * 100
    assert RAG_SUPPORT_FUZZY <= score < FUZZY_THRESHOLD, (
        f"fixture drifted out of the support band [80, 97): partial_ratio={score:.2f}"
    )

    # 2) … but the RAG grounded-support path (>= 80) accepts it.
    sources = [{"content": _SUPPORT_CHUNK}]
    report = await verify_answer_sentences(_SUPPORT_PARAPHRASE, sources, verifier, _unused_pool())
    assert report.total == 1
    assert report.verified_count == 1, (
        f"support-band sentence must be accepted; per_sentence={report.per_sentence}"
    )
    assert report.pass_rate == 1.0
    assert report.confidence == RagConfidence.HIGH


# ---------------------------------------------------------------------------
# Short-answer sentinel: None confidence vs the checked-and-failed UNVERIFIED path
# ---------------------------------------------------------------------------


async def test_short_answer_yields_none_confidence():
    """A sub-4-word answer like 'MNIST and CIFAR-10.' has no verifiable sentences.

    total==0 path must return confidence=None, not UNVERIFIED.  The router
    already initialises verification with confidence=None as its fallback, so
    this is the consistent sentinel: no badge, no amber banner on the FE.
    """
    sources = [{"content": "We evaluated on MNIST and CIFAR-10 benchmarks."}]
    verifier = QuoteVerifier()
    report = await verify_answer_sentences("MNIST and CIFAR-10.", sources, verifier, _unused_pool())
    assert report.total == 0, (
        f"short answer must produce 0 verifiable sentences; got {report.total}"
    )
    assert report.confidence is None, (
        f"total==0 must yield confidence=None, not {report.confidence!r}"
    )


async def test_all_fail_yields_unverified():
    """When sentences exist but none verify, confidence==UNVERIFIED (not None).

    UNVERIFIED stays for real failures (pass_rate==0.0, total>0).
    """
    sources = [{"content": "Completely unrelated source text about something else entirely."}]
    verifier = QuoteVerifier()
    # Long enough to be a verifiable sentence (>= 4 words) and not in the source.
    answer = "The transformer architecture was introduced by Einstein in 1905."
    report = await verify_answer_sentences(answer, sources, verifier, _unused_pool())
    assert report.total >= 1
    assert report.verified_count == 0
    assert report.confidence == RagConfidence.UNVERIFIED


def test_build_confidence_medium_boundary_pin():
    """DELIBERATE: Ask path uses pass_rate >= 0.5 → MEDIUM (inclusive lower boundary).

    This diverges from the jarvis_common verify_findings path (pass_rate > 0.5).
    Both boundaries are intentional and independently pinned in their respective
    test files.  Do NOT unify them without updating both tests and both docstrings.
    """
    # Exactly 0.5 → MEDIUM (inclusive)
    assert _build_confidence(0.5, 2) == RagConfidence.MEDIUM
    # Just below 0.5 → LOW
    assert _build_confidence(0.49, 100) == RagConfidence.LOW
    # Strictly above 0.5 → MEDIUM
    assert _build_confidence(0.6, 5) == RagConfidence.MEDIUM


def test_build_confidence_none_when_total_zero():
    """total==0 → None (not UNVERIFIED); preserves the no-badge sentinel."""
    assert _build_confidence(0.0, 0) is None
