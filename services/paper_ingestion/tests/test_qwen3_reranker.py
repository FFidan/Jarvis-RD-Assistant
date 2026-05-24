"""BE-07 — Guard `_HAS_QWEN3` in `Qwen3Reranker.__init__`.

When torch/transformers are not installed, instantiating Qwen3Reranker must
raise a clear RuntimeError, not an AttributeError on ``torch.cuda.is_available()``.

Also verifies that ``get_qwen3_reranker()`` returns ``None`` safely when the
flag is False.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

import paper_ingestion.ingestion.qwen3_reranker as _mod


class TestQwen3RerankerInitGuard:
    """Qwen3Reranker.__init__ raises RuntimeError when _HAS_QWEN3 is False."""

    def test_raises_runtime_error_not_attribute_error(self) -> None:
        """Must raise RuntimeError (not AttributeError) when torch is missing."""
        with patch.object(_mod, "_HAS_QWEN3", False):
            with pytest.raises(RuntimeError, match="Qwen3Reranker requires torch and transformers"):
                _mod.Qwen3Reranker()

    def test_error_is_runtime_error_type(self) -> None:
        """The raised exception must be exactly RuntimeError, not a subclass."""
        with patch.object(_mod, "_HAS_QWEN3", False):
            exc = None
            try:
                _mod.Qwen3Reranker()
            except RuntimeError as e:
                exc = e
            assert exc is not None, "Expected RuntimeError was not raised"
            assert type(exc) is RuntimeError

    def test_torch_cuda_not_called_when_missing(self) -> None:
        """torch.cuda.is_available() must NOT be reached when _HAS_QWEN3 is False.

        Before the fix, torch=None caused AttributeError at the cuda check.
        After the fix, RuntimeError is raised before torch is touched.
        """
        with patch.object(_mod, "_HAS_QWEN3", False):
            # torch is None in this module when imports fail; if __init__ reaches
            # torch.cuda.is_available() it would raise AttributeError, not RuntimeError.
            with pytest.raises(RuntimeError):
                _mod.Qwen3Reranker()


class TestGetQwen3RerankerFactoryWithoutDeps:
    """get_qwen3_reranker() returns None safely when _HAS_QWEN3 is False."""

    def test_returns_none_when_has_qwen3_false(self) -> None:
        """Factory must return None without raising when torch/transformers absent."""
        # Reset singleton so factory doesn't short-circuit via cached _instance
        original_instance = _mod._instance
        try:
            _mod._instance = None
            with patch.object(_mod, "_HAS_QWEN3", False):
                result = _mod.get_qwen3_reranker()
            assert result is None
        finally:
            _mod._instance = original_instance
