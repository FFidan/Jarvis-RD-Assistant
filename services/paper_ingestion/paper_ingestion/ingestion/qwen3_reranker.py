"""Qwen3-Reranker-0.6B generative reranker adapter.

Qwen3-Reranker is a *generative* (causal-LM) reranker: it answers a yes/no
prompt and the relevance score is ``logit("yes") - logit("no")`` over the
next-token distribution.  This is fundamentally different from the
sentence-transformers CrossEncoder used by :class:`Reranker`, so it lives in a
separate module rather than a mode flag.

The public surface — ``__init__(model_name)`` + ``rerank(query, passages,
top_k) -> list[tuple[int, float]]`` — is duck-type-compatible with
:class:`~paper_ingestion.ingestion.reranker.Reranker` so the eval harness
(D6-A) can swap them transparently.

**GPU requirement:** This module requires a CUDA-capable GPU for practical
inference.  CPU inference is supported but very slow for large passage sets.
Inputs are capped at ``MAX_PASSAGES`` passages per call to bound latency.
"""

from __future__ import annotations

import logging
import os

try:
    import torch
    from transformers import (
        AutoModelForCausalLM,  # pyright: ignore[reportPrivateImportUsage]
        AutoTokenizer,  # pyright: ignore[reportPrivateImportUsage]
    )

    _HAS_QWEN3 = True
except ImportError:
    torch = None  # type: ignore[assignment]
    AutoModelForCausalLM = None  # type: ignore[assignment]
    AutoTokenizer = None  # type: ignore[assignment]
    _HAS_QWEN3 = False

logger = logging.getLogger(__name__)

# Maximum number of passages accepted per rerank() call.
# Bounds GPU memory usage and per-call latency on large result sets.
MAX_PASSAGES = 50

# ---------------------------------------------------------------------------
# Prompt template (per Qwen3-Reranker model card)
# ---------------------------------------------------------------------------
_PROMPT_TEMPLATE = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the Query. "
    'Answer "yes" or "no".\n'
    "<|im_end|>\n"
    "<|im_start|>user\n"
    "<Instruct>: Given a scientific question, retrieve passages that answer it.\n"
    "<Query>: {query}\n"
    "<Document>: {passage}\n"
    "<|im_end|>\n"
    "<|im_start|>assistant\n"
)


class Qwen3Reranker:
    """Generative reranker backed by Qwen/Qwen3-Reranker-0.6B.

    Scores each (query, passage) pair by running a single-token generation
    step and taking ``logit("yes") - logit("no")``.  The difference is
    order-preserving; no softmax is required.

    Parameters
    ----------
    model_name:
        HuggingFace model ID or local path.  Defaults to
        ``"Qwen/Qwen3-Reranker-0.6B"``.
    """

    def __init__(self, model_name: str = "Qwen/Qwen3-Reranker-0.6B") -> None:
        self._model_name = model_name
        self._device = "cuda" if torch.cuda.is_available() else "cpu"  # type: ignore[union-attr]
        self._dtype = torch.float16 if self._device == "cuda" else torch.float32  # type: ignore[union-attr]

        logger.info(
            "Loading Qwen3Reranker model %s on %s (%s)",
            model_name,
            self._device,
            self._dtype,
        )
        self._tokenizer = AutoTokenizer.from_pretrained(model_name)  # type: ignore[union-attr]
        self._model = AutoModelForCausalLM.from_pretrained(  # type: ignore[union-attr]
            model_name,
            torch_dtype=self._dtype,
        ).to(self._device)
        self._model.eval()

        # Cache token IDs for "yes" and "no" once at construction time.
        # If the tokenizer produces multiple sub-tokens, we take the first
        # (per task spec).
        self._yes_id: int = self._tokenizer.encode("yes", add_special_tokens=False)[0]
        self._no_id: int = self._tokenizer.encode("no", add_special_tokens=False)[0]

        logger.debug(
            "Qwen3Reranker token ids — yes: %d, no: %d",
            self._yes_id,
            self._no_id,
        )

    def rerank(
        self,
        query: str,
        passages: list[str],
        top_k: int = 5,
    ) -> list[tuple[int, float]]:
        """Re-score and rerank passages using the generative reranker.

        Parameters
        ----------
        query:
            The search query.
        passages:
            List of passage texts to rerank.  Silently truncated to
            ``MAX_PASSAGES`` entries to bound GPU memory and latency.
        top_k:
            Number of top results to return.

        Returns
        -------
        list[tuple[int, float]]
            List of (original_index, score) tuples sorted by score descending,
            limited to ``top_k``.  ``original_index`` is the index into the
            input ``passages`` list — NOT the rank position.
        """
        if not passages:
            return []

        if len(passages) > MAX_PASSAGES:
            logger.warning(
                "Qwen3Reranker.rerank: truncating %d passages to MAX_PASSAGES=%d",
                len(passages),
                MAX_PASSAGES,
            )
            passages = passages[:MAX_PASSAGES]

        indexed_scores: list[tuple[int, float]] = []

        # Determinism note: with do_sample=False and a fixed model checkpoint,
        # each forward pass over the same token sequence is fully deterministic
        # (no temperature sampling, no random dropout in eval() mode).  No
        # manual seed is required.
        with torch.inference_mode():  # type: ignore[union-attr]
            for idx, passage in enumerate(passages):
                prompt = _PROMPT_TEMPLATE.format(query=query, passage=passage)
                inputs = self._tokenizer(
                    prompt,
                    return_tensors="pt",
                    truncation=True,
                ).to(self._device)

                output = self._model.generate(
                    **inputs,
                    max_new_tokens=1,
                    return_dict_in_generate=True,
                    output_scores=True,
                    do_sample=False,
                )

                # output.scores is a tuple of length max_new_tokens; each
                # element has shape [batch_size, vocab_size].  We take the
                # first (and only) step, then squeeze the batch dimension.
                logits = output.scores[0][0]  # shape: [vocab_size]
                score = float(logits[self._yes_id] - logits[self._no_id])
                indexed_scores.append((idx, score))

        indexed_scores.sort(key=lambda x: x[1], reverse=True)
        return indexed_scores[:top_k]


# ---------------------------------------------------------------------------
# Singleton (lazy)
# ---------------------------------------------------------------------------

_instance: Qwen3Reranker | None = None


def get_qwen3_reranker() -> Qwen3Reranker | None:
    """Get or create the singleton Qwen3Reranker instance.

    Returns ``None`` when the model cannot be loaded (e.g. ``transformers``
    not installed, download failure).  Uses the ``QWEN3_RERANKER_MODEL`` env
    var to override the default ``"Qwen/Qwen3-Reranker-0.6B"``.

    Tests can reset by setting ``qwen3_reranker._instance = None``.
    """
    if not _HAS_QWEN3:
        return None
    global _instance
    if _instance is not None:
        return _instance
    try:
        model_name = os.environ.get("QWEN3_RERANKER_MODEL", "Qwen/Qwen3-Reranker-0.6B")
        _instance = Qwen3Reranker(model_name=model_name)
        return _instance
    except Exception:
        logger.warning(
            "Qwen3Reranker unavailable; using retrieval scores only",
            exc_info=True,
        )
        return None
