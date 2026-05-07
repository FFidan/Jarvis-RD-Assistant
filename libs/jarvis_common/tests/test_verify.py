"""Smoke tests for jarvis_common.verify consolidation (W3-DRY-4).

Verifies:
1. QuoteVerifier is importable from both the new canonical path and the
   legacy shim path, and they resolve to the exact same class object.
2. Basic functional behaviour is preserved: exact substring match verified,
   non-matching quote rejected.
3. DictChunk adapts plain dicts to the ChunkLike Protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime

from jarvis_common.verify import DictChunk, QuoteVerifier
from jarvis_common.verify import QuoteVerifier as QV_canonical


def test_shim_exports_same_class_as_canonical():
    """Legacy import path must resolve to the identical class object."""
    from paper_ingestion.extraction.verify import QuoteVerifier as QV_shim  # noqa: PLC0415

    assert QV_shim is QV_canonical, (
        "paper_ingestion.extraction.verify.QuoteVerifier must be the same object as "
        "jarvis_common.verify.QuoteVerifier (shim re-export broken)"
    )


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
