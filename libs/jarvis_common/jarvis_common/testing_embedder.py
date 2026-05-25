"""Shared embedder test fakes for paper_ingestion tests.

``_FakeEncoding``, ``_make_embedder``, and ``_dict_to_record`` are the single
authoritative copies, promoted from services/paper_ingestion/tests/_embedder_fakes.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


class _FakeEncoding:
    """Character-level tiktoken stand-in (1 char == 1 token).

    Replaces the real tiktoken model in unit tests so tests run without
    a network download or large model binary.
    """

    def encode(self, text: str) -> list[str]:
        return list(text)

    def decode(self, tokens: list[str]) -> str:
        return "".join(tokens)


def _make_embedder():  # type: ignore[return]
    """Return an Embedder with mocked HTTP/Qdrant clients and _FakeEncoding."""
    from paper_ingestion.ingestion.embedder import Embedder

    e = Embedder(AsyncMock(), AsyncMock())
    e._encoding = _FakeEncoding()  # type: ignore[assignment]
    return e


def _dict_to_record(d: dict) -> MagicMock:
    """Simulate an asyncpg.Record with dict-style item access."""
    rec = MagicMock()
    rec.__getitem__ = lambda self, key: d[key]
    rec.keys = lambda: d.keys()
    return rec
