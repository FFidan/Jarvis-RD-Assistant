"""Shared embedder test fakes — DRY consolidation (D3-05/09).

``_FakeEncoding``, ``_make_embedder``, and ``_dict_to_record`` were
duplicated across 3-5 files.  They are extracted here as the single
authoritative copy.  Import them as:

    from tests._embedder_fakes import _FakeEncoding, _dict_to_record, _make_embedder
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from paper_ingestion.embedder import Embedder


class _FakeEncoding:
    """Character-level tiktoken stand-in (1 char == 1 token).

    Replaces the real tiktoken model in unit tests so tests run without
    a network download or large model binary.
    """

    def encode(self, text: str) -> list[str]:
        return list(text)

    def decode(self, tokens: list[str]) -> str:
        return "".join(tokens)


def _make_embedder() -> Embedder:
    """Return an Embedder with mocked HTTP/Qdrant clients and _FakeEncoding."""
    e = Embedder(AsyncMock(), AsyncMock())
    e._encoding = _FakeEncoding()  # type: ignore[assignment]
    return e


def _dict_to_record(d: dict) -> MagicMock:
    """Simulate an asyncpg.Record with dict-style item access."""
    rec = MagicMock()
    rec.__getitem__ = lambda self, key: d[key]
    rec.keys = lambda: d.keys()
    return rec
