"""Provider-prefix helpers for the local Ollama transports.

LiteLLM routes ``ollama/<tag>`` to ``/api/generate`` and ``ollama_chat/<tag>``
to ``/api/chat``. Chat aliases use ``ollama_chat/`` so grammar-constrained
decoding fires; embedding aliases stay on ``ollama/``. Guards that branch on a
local-Ollama deployment or strip the prefix to recover the bare tag must accept
both spellings — hence this leaf helper (dependency-free so every caller can
import it without a cycle).
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
