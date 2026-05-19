"""Tests for the fuzzy-match early-break fix in QuoteVerifier.

Verifies that the fuzzy scan selects the **best** matching chunk rather
than stopping at the first chunk above FUZZY_THRESHOLD, while still
breaking early on a perfect (100) match for performance.
"""

from datetime import UTC, datetime
from unittest.mock import patch

from jarvis_common.verify import QuoteVerifier
from paper_ingestion.models import ChunkResponse

_NOW = datetime.now(tz=UTC)


def _make_chunk(
    chunk_id: int,
    content: str,
    *,
    page_number: int = 1,
    chunk_index: int | None = None,
) -> ChunkResponse:
    """Create a minimal ChunkResponse for testing."""
    return ChunkResponse(
        id=chunk_id,
        paper_id=1,
        chunk_index=chunk_index if chunk_index is not None else chunk_id,
        content=content,
        page_number=page_number,
        created_at=_NOW,
    )


class TestFuzzyBestMatch:
    """Verify that fuzzy matching finds the BEST chunk, not just the first above threshold."""

    def test_selects_best_match_not_first_above_threshold(self) -> None:
        """Chunk[0] scores ~97, chunk[1] scores 100 -- chunk[1] must be selected."""
        verifier = QuoteVerifier()

        quote = "the cat sat on the mat near the door"

        # chunk_0: near-match (slightly altered) -- will score high but < 100
        chunk_0 = _make_chunk(
            10,
            "the cat sat on a mat near the door",
            page_number=1,
            chunk_index=0,
        )
        # chunk_1: exact content -- will score 100
        chunk_1 = _make_chunk(
            20,
            "the cat sat on the mat near the door",
            page_number=5,
            chunk_index=1,
        )

        # full_text intentionally does NOT contain the quote so exact-match
        # strategy is skipped and fuzzy matching kicks in.
        full_text = "unrelated text that does not contain the quote"

        # Provide realistic scores: chunk_0 near-match (97), chunk_1 exact (100).
        # The conftest rapidfuzz stub returns a flat 80, so we override here.
        with patch(
            "jarvis_common.verify.fuzz.partial_ratio",
            side_effect=[97, 100],
        ):
            result = verifier.verify_quote(quote, full_text, [chunk_0, chunk_1])

        assert result.verified is True
        assert result.match_type == "fuzzy"
        assert result.chunk_id == 20, "Should select chunk_1 (best match), not chunk_0"
        assert result.page_number == 5
        assert result.match_score == 1.0  # 100 / 100

    def test_breaks_early_on_perfect_match(self) -> None:
        """Chunk[0] scores 100 -- loop should break immediately, chunk[1] never evaluated."""
        verifier = QuoteVerifier()

        quote = "gradient descent converges"

        chunk_0 = _make_chunk(
            10,
            "gradient descent converges",
            page_number=2,
            chunk_index=0,
        )
        chunk_1 = _make_chunk(
            20,
            "gradient descent converges slowly",
            page_number=7,
            chunk_index=1,
        )

        full_text = "unrelated text without the target quote"

        # Patch fuzz.partial_ratio to track how many times it's called.
        # Always return 100 (perfect match) — the conftest stub is flat-80, so
        # we substitute a real counting wrapper that returns 100 instead.
        call_count = 0

        def counting_partial_ratio(*args: object, **kwargs: object) -> float:
            nonlocal call_count
            call_count += 1
            return 100.0

        with patch(
            "jarvis_common.verify.fuzz.partial_ratio",
            side_effect=counting_partial_ratio,
        ):
            result = verifier.verify_quote(quote, full_text, [chunk_0, chunk_1])

        assert result.verified is True
        assert result.chunk_id == 10, "Should select chunk_0 (perfect match)"
        assert result.page_number == 2
        assert call_count == 1, "Loop should break after first chunk (perfect match)"
