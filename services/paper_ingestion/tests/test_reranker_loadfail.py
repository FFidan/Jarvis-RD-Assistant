"""Pure-unit tests for Reranker._load_model_if_needed fail-fast latch.

Covers: narrow inner except + _load_failed latch to prevent infinite
reload attempts when model loading fails.
"""

from unittest.mock import MagicMock, patch

import pytest

from paper_ingestion.ingestion.reranker import Reranker


def _make_reranker() -> Reranker:
    """Return a Reranker with a mocked _load_model_if_needed's CrossEncoder gate bypassed."""
    return Reranker(model_name="test-model")


class TestRerankerLoadFailPropagatesFirstCall:
    """OSError on factory raises on first _load_model_if_needed call."""

    def test_reranker_load_failure_propagates_first_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory = MagicMock(side_effect=OSError("model weights not found"))
        reranker = _make_reranker()

        with patch("paper_ingestion.ingestion.reranker.CrossEncoder", factory):
            with pytest.raises(OSError, match="model weights not found"):
                reranker._load_model_if_needed()


class TestRerankerLoadFailShortCircuitsSecondCall:
    """After a failed load, second call raises RuntimeError without re-invoking factory."""

    def test_reranker_load_failure_short_circuits_second_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory = MagicMock(side_effect=OSError("model weights not found"))
        reranker = _make_reranker()

        with patch("paper_ingestion.ingestion.reranker.CrossEncoder", factory):
            # First call: expect the original error (factory may be called for CUDA + CPU paths)
            with pytest.raises(OSError):
                reranker._load_model_if_needed()

            count_after_first = factory.call_count

            # Second call: latch is set; must raise RuntimeError without re-invoking factory
            with pytest.raises(RuntimeError, match="previously failed"):
                reranker._load_model_if_needed()

        # Factory call count must not increase on the second _load_model_if_needed call
        assert factory.call_count == count_after_first


class TestRerankerUnexpectedErrorPropagates:
    """ValueError (not in the narrowed except list) propagates unchanged from inner try block."""

    def test_reranker_unexpected_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = MagicMock(side_effect=ValueError("unexpected internal error"))
        reranker = _make_reranker()

        with patch("paper_ingestion.ingestion.reranker.CrossEncoder", factory):
            # ValueError is not in (OSError, ImportError, RuntimeError, FileNotFoundError)
            # so it must bubble up from the outer except Exception (CUDA fallback)
            with pytest.raises(ValueError, match="unexpected internal error"):
                reranker._load_model_if_needed()
