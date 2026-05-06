"""Tests for the Qwen3Reranker adapter (qwen3_reranker.py).

All tests are mock-only — the model is never downloaded.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Sentinel to detect whether _instance attribute existed before tests ran.
_SENTINEL = object()


def _make_torch_stub(yes_logits: list[float], no_logits: list[float]):
    """Return a minimal torch-shaped stub used by the adapter.

    The adapter does::
        logits = output.scores[0][0]        # shape (vocab,)
        score  = logits[yes_id] - logits[no_id]

    We stub torch.tensor so indexing works without real PyTorch.
    """
    import importlib.util

    # If real torch is available, use real tensors for simplicity.
    if importlib.util.find_spec("torch") is not None:
        import torch  # noqa: F401 — only imported if available

        def _make_tensor(vals: list[float]):
            return torch.tensor(vals)

    else:
        # Minimal array-like that supports integer indexing.
        class _FakeTensor(list):
            pass

        def _make_tensor(vals: list[float]):
            return _FakeTensor(vals)

    # Each call to generate returns one output; scores is a 1-tuple of
    # tensors shaped [batch, vocab].  We expose scores[0][0] as the
    # per-token logit vector.
    class _Vocab:
        def __init__(self, logits):
            self._logits = _make_tensor(logits)

        def __getitem__(self, idx):
            return self._logits[idx]

    return _Vocab


def _build_generate_side_effect(scores_per_passage: list[tuple[float, float]]):
    """Return a callable that simulates model.generate output per passage.

    Each element of *scores_per_passage* is (yes_logit, no_logit).
    The adapter calls generate once per query-passage pair.
    """
    import importlib.util

    use_real_torch = importlib.util.find_spec("torch") is not None
    call_index = [0]  # mutable counter

    def _generate(*args, **kwargs):
        idx = call_index[0]
        call_index[0] += 1
        yes_l, no_l = scores_per_passage[idx]

        # Build a vocab vector large enough to hold yes_id=0, no_id=1.
        # We place yes_logit at index 0 and no_logit at index 1.
        if use_real_torch:
            import torch

            logit_row = torch.zeros(2)
            logit_row[0] = yes_l
            logit_row[1] = no_l
            # output.scores is a tuple; scores[0] has shape [batch, vocab]
            return SimpleNamespace(scores=(logit_row.unsqueeze(0),))
        else:
            row = [yes_l, no_l]

            class _BatchRow:
                def __getitem__(self, _):
                    return row

            return SimpleNamespace(scores=(_BatchRow(),))

    return _generate


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _build_tokenizer_mock():
    """Tokenizer mock that converts 'yes' → 0 and 'no' → 1."""
    tok = MagicMock()
    # encode("yes") → [0], encode("no") → [1]
    tok.encode.side_effect = lambda text, *a, **kw: [0] if text == "yes" else [1]
    # __call__ returns something with .input_ids and .to()
    input_ids_mock = MagicMock()
    input_ids_mock.input_ids = MagicMock()
    input_ids_mock.to.return_value = input_ids_mock
    tok.return_value = input_ids_mock
    return tok


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_rerank_orders_by_logit_diff():
    """Passages are returned sorted by (yes_logit - no_logit) descending.

    Three passages with logit differences [0.5, 2.0, -1.0] should come back
    as [(1, 2.0), (0, 0.5), (2, -1.0)].
    """
    # yes=0.5, no=0.0  → diff 0.5   (passage 0)
    # yes=2.0, no=0.0  → diff 2.0   (passage 1)
    # yes=0.0, no=1.0  → diff -1.0  (passage 2)
    scores = [(0.5, 0.0), (2.0, 0.0), (0.0, 1.0)]
    generate_fn = _build_generate_side_effect(scores)

    tok_mock = _build_tokenizer_mock()
    model_mock = MagicMock()
    model_mock.generate.side_effect = generate_fn
    model_mock.device = "cpu"
    # `.to(device)` and `.eval()` are chained inside __init__; ensure they
    # return the same mock so generate.side_effect lands on the right object.
    model_mock.to.return_value = model_mock
    model_mock.eval.return_value = model_mock

    with (
        patch(
            "transformers.AutoTokenizer.from_pretrained",
            return_value=tok_mock,
        ),
        patch(
            "transformers.AutoModelForCausalLM.from_pretrained",
            return_value=model_mock,
        ),
    ):
        from paper_ingestion.ingestion import qwen3_reranker as mod

        reranker = mod.Qwen3Reranker.__new__(mod.Qwen3Reranker)
        reranker.__init__()

    passages = ["passage-A", "passage-B", "passage-C"]
    # Reset generate side-effect for the rerank call
    model_mock.generate.side_effect = generate_fn

    result = reranker.rerank("my query", passages, top_k=3)

    assert len(result) == 3
    assert result[0][0] == 1, "passage-B (index 1) has highest diff (2.0)"
    assert result[1][0] == 0, "passage-A (index 0) is second (0.5)"
    assert result[2][0] == 2, "passage-C (index 2) is last (-1.0)"
    assert result[0][1] > result[1][1] > result[2][1], "scores descending"


def test_rerank_respects_top_k():
    """rerank() returns at most top_k items, highest-scoring first."""
    scores = [(3.0, 0.0), (1.0, 0.0), (2.0, 0.0), (0.5, 0.0), (4.0, 0.0)]
    generate_fn = _build_generate_side_effect(scores)

    tok_mock = _build_tokenizer_mock()
    model_mock = MagicMock()
    model_mock.generate.side_effect = generate_fn
    model_mock.device = "cpu"
    # `.to(device)` and `.eval()` are chained inside __init__; ensure they
    # return the same mock so generate.side_effect lands on the right object.
    model_mock.to.return_value = model_mock
    model_mock.eval.return_value = model_mock

    with (
        patch(
            "transformers.AutoTokenizer.from_pretrained",
            return_value=tok_mock,
        ),
        patch(
            "transformers.AutoModelForCausalLM.from_pretrained",
            return_value=model_mock,
        ),
    ):
        from paper_ingestion.ingestion import qwen3_reranker as mod2

        reranker = mod2.Qwen3Reranker.__new__(mod2.Qwen3Reranker)
        reranker.__init__()

    model_mock.generate.side_effect = generate_fn

    result = reranker.rerank("q", ["a", "b", "c", "d", "e"], top_k=2)

    assert len(result) == 2
    # Highest diff is passage 4 (score 4.0), then passage 0 (score 3.0)
    assert result[0][0] == 4
    assert result[1][0] == 0


def test_singleton_factory_caches_instance():
    """get_qwen3_reranker() returns the same object on repeated calls."""
    tok_mock = _build_tokenizer_mock()
    model_mock = MagicMock()
    model_mock.device = "cpu"

    with (
        patch(
            "transformers.AutoTokenizer.from_pretrained",
            return_value=tok_mock,
        ),
        patch(
            "transformers.AutoModelForCausalLM.from_pretrained",
            return_value=model_mock,
        ),
    ):
        import paper_ingestion.ingestion.qwen3_reranker as qmod

        # Reset module-level singleton so we can test from a clean state.
        original = qmod._instance if hasattr(qmod, "_instance") else _SENTINEL
        qmod._instance = None
        try:
            first = qmod.get_qwen3_reranker()
            second = qmod.get_qwen3_reranker()
        finally:
            if original is _SENTINEL:
                del qmod._instance
            else:
                qmod._instance = original

    assert first is second, "Factory must return the cached instance"


def test_init_caches_yes_no_token_ids():
    """Qwen3Reranker resolves yes/no token ids exactly once at __init__."""
    tok_mock = _build_tokenizer_mock()
    model_mock = MagicMock()
    model_mock.device = "cpu"

    with (
        patch(
            "transformers.AutoTokenizer.from_pretrained",
            return_value=tok_mock,
        ),
        patch(
            "transformers.AutoModelForCausalLM.from_pretrained",
            return_value=model_mock,
        ),
    ):
        import paper_ingestion.ingestion.qwen3_reranker as qmod

        reranker = qmod.Qwen3Reranker.__new__(qmod.Qwen3Reranker)
        reranker.__init__()

    # encode should have been called for "yes" and "no" exactly once each.
    encode_calls = tok_mock.encode.call_args_list
    called_texts = [c.args[0] for c in encode_calls]
    assert "yes" in called_texts, "yes token id must be resolved at init"
    assert "no" in called_texts, "no token id must be resolved at init"
    # No additional encode calls should happen for the token-id resolution.
    yes_count = called_texts.count("yes")
    no_count = called_texts.count("no")
    assert yes_count == 1, f"expected 1 encode('yes'), got {yes_count}"
    assert no_count == 1, f"expected 1 encode('no'), got {no_count}"
