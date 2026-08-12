"""Model-name normalization shared by every consumer of an Ollama tag.

LiteLLM routes ``ollama/<tag>`` to ``/api/generate`` and ``ollama_chat/<tag>``
to ``/api/chat``. Chat aliases use ``ollama_chat/`` so grammar-constrained
decoding fires; embedding aliases stay on ``ollama/``. Guards that branch on a
local-Ollama deployment or strip the prefix to recover the bare tag must accept
both spellings — hence these leaf helpers (dependency-free so every caller can
import them without a cycle).
"""

from __future__ import annotations

OLLAMA_PREFIXES: tuple[str, ...] = ("ollama/", "ollama_chat/")


def is_local_ollama(model: str) -> bool:
    """Return True when *model* carries a local-Ollama provider prefix."""
    return model.startswith(OLLAMA_PREFIXES)


def strip_ollama_prefix(model: str) -> str:
    """Remove a local-Ollama provider prefix, longest match first.

    ``ollama_chat/`` is checked before ``ollama/`` so the longer prefix wins;
    a model without an Ollama prefix is returned unchanged.
    """
    for prefix in sorted(OLLAMA_PREFIXES, key=len, reverse=True):
        if model.startswith(prefix):
            return model[len(prefix) :]
    return model


def strip_latest_tag(model: str) -> str:
    """Remove Ollama's implicit ``:latest`` tag, leaving everything else intact.

    ``qwen3:8b`` and ``qwen3:8b:latest`` name the same model and the implicit
    tag is never what gets stored, so both spellings have to compare equal.
    Only a trailing ``:latest`` counts — ``mistral-large-latest`` is a model
    name in its own right.
    """
    return model.removesuffix(":latest")
