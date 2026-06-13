"""Smoke tests for jarvis_common.verify.

Verifies:
1. QuoteVerifier is importable from the canonical path jarvis_common.verify.
2. Basic functional behaviour is preserved: exact substring match verified,
   non-matching quote rejected.
3. DictChunk adapts plain dicts to the ChunkLike Protocol.

Note: paper_ingestion.extraction.verify shim removed in an earlier audit pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jarvis_common.verify import DictChunk, QuoteVerifier
from jarvis_common.verify import QuoteVerifier as QV_canonical


def test_canonical_class_is_importable():
    """QuoteVerifier is importable from the canonical jarvis_common.verify path."""
    assert QV_canonical is QuoteVerifier


def _make_dict_chunk(id_: int, content: str, page_number: int | None = None) -> dict:
    return {"id": id_, "content": content, "page_number": page_number}


def test_verify_quote_exact_match():
    """Exact substring in full_text → verified=True, match_type='exact'."""
    verifier = QuoteVerifier()
    quote = "neural networks achieve state-of-the-art"
    full_text = "In this work, neural networks achieve state-of-the-art performance."
    chunks = [DictChunk(_make_dict_chunk(1, full_text, page_number=2))]

    result = verifier.verify_quote(quote, full_text, chunks)

    assert result.verified is True
    assert result.match_type == "exact"
    assert result.chunk_id == 1
    assert result.page_number == 2


def test_verify_quote_no_match():
    """Quote absent from source text → verified=False."""
    verifier = QuoteVerifier()
    quote = "completely unrelated content xyz"
    full_text = "Neural networks achieve state-of-the-art results."
    chunks = [DictChunk(_make_dict_chunk(1, full_text, page_number=1))]

    result = verifier.verify_quote(quote, full_text, chunks)

    assert result.verified is False


def test_dict_chunk_protocol():
    """DictChunk exposes .id, .content, .page_number as required by ChunkLike."""
    raw = {"id": 42, "content": "Some text here.", "page_number": 5}
    chunk = DictChunk(raw)

    assert chunk.id == 42
    assert chunk.content == "Some text here."
    assert chunk.page_number == 5


def test_dict_chunk_missing_page_number_defaults_to_none():
    """DictChunk.page_number is None when key absent from dict."""
    raw = {"id": 7, "content": "Text without page."}
    chunk = DictChunk(raw)

    assert chunk.page_number is None


def test_chunk_response_satisfies_chunk_like():
    """paper_ingestion.models.ChunkResponse satisfies ChunkLike at runtime."""
    from jarvis_common.verify import ChunkLike  # noqa: PLC0415
    from paper_ingestion.models import ChunkResponse  # noqa: PLC0415

    chunk = ChunkResponse(
        id=1,
        paper_id=10,
        chunk_index=0,
        content="content text",
        page_number=1,
        created_at=datetime.now(UTC),
    )

    assert isinstance(chunk, ChunkLike), (
        "ChunkResponse should satisfy ChunkLike runtime_checkable Protocol"
    )


def test_verify_findings_medium_boundary_pin():
    """DELIBERATE: Summary path uses pass_rate > 0.5 → MEDIUM (exclusive lower boundary).

    This diverges from rag/verification.py's Ask path (pass_rate >= 0.5).
    Both boundaries are intentional and independently pinned in their respective
    test files.  Do NOT unify them without updating both tests and both docstrings.
    """
    from jarvis_common.verify import Confidence  # noqa: PLC0415

    verifier = QuoteVerifier()
    source = "Neural networks achieve state-of-the-art results on many benchmarks."

    # Build two findings: one verified, one not — 50% pass rate
    class _Finding:
        def __init__(self, quote: str) -> None:
            self.quote = quote
            self.verified = False
            self.chunk_id = None
            self.page_number = None

    findings_50 = [
        _Finding("neural networks achieve state-of-the-art"),
        _Finding("completely fabricated claim xyz123"),
    ]
    chunks = [DictChunk({"id": 0, "content": source, "page_number": None})]
    report_50 = verifier.verify_findings(findings_50, source, chunks)
    assert report_50.pass_rate == 0.5
    # Exactly 0.5 → LOW (exclusive: >0.5 required for MEDIUM)
    assert report_50.confidence == Confidence.LOW, (
        f"pass_rate==0.5 must be LOW in the summary path; got {report_50.confidence!r}"
    )

    # 3 findings, 2 verified → pass_rate ≈ 0.667 → MEDIUM
    findings_67 = [
        _Finding("neural networks achieve state-of-the-art"),
        _Finding("neural networks achieve state-of-the-art results"),
        _Finding("completely fabricated claim xyz123"),
    ]
    report_67 = verifier.verify_findings(findings_67, source, chunks)
    assert report_67.pass_rate > 0.5
    assert report_67.confidence == Confidence.MEDIUM
