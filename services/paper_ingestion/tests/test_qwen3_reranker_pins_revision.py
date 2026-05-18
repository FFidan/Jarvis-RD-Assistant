"""A1 B615 — Qwen3Reranker must pass revision= to both from_pretrained calls.

Regression test: if revision= is missing from either call, the test fails,
surfacing the B615 supply-chain risk.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_model_mock() -> MagicMock:
    """Return a mock that satisfies .to().eval() chaining."""
    m = MagicMock()
    m.to.return_value = m
    m.eval.return_value = m
    return m


def _make_tokenizer_mock() -> MagicMock:
    tok = MagicMock()
    tok.encode.return_value = [1, 2]  # first element used for yes_id / no_id
    return tok


def test_from_pretrained_called_with_revision_main() -> None:
    """Both AutoTokenizer and AutoModelForCausalLM must receive revision='main'."""
    tokenizer_mock = _make_tokenizer_mock()
    model_mock = _make_model_mock()

    with (
        patch("paper_ingestion.ingestion.qwen3_reranker.AutoTokenizer") as mock_tok_cls,
        patch("paper_ingestion.ingestion.qwen3_reranker.AutoModelForCausalLM") as mock_model_cls,
        patch("paper_ingestion.ingestion.qwen3_reranker.torch") as mock_torch,
    ):
        mock_tok_cls.from_pretrained.return_value = tokenizer_mock
        mock_model_cls.from_pretrained.return_value = model_mock
        # Simulate CUDA unavailable so dtype/device are deterministic
        mock_torch.cuda.is_available.return_value = False
        mock_torch.float16 = "float16"
        mock_torch.float32 = "float32"

        # Import after patching so module-level _HAS_QWEN3 guard is bypassed
        from paper_ingestion.ingestion.qwen3_reranker import Qwen3Reranker

        Qwen3Reranker()

    # Both calls must include revision=
    _, tok_kwargs = mock_tok_cls.from_pretrained.call_args
    assert tok_kwargs.get("revision") == "main", (
        f"AutoTokenizer.from_pretrained missing revision='main'; kwargs={tok_kwargs}"
    )

    _, model_kwargs = mock_model_cls.from_pretrained.call_args
    assert model_kwargs.get("revision") == "main", (
        f"AutoModelForCausalLM.from_pretrained missing revision='main'; kwargs={model_kwargs}"
    )
