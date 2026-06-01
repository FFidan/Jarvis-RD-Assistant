"""Char-tests for the PI-local embedder test fakes (tests/_embedder_fakes.py)."""

from __future__ import annotations

import pytest

from paper_ingestion.ingestion.embedder import Embedder
from tests._embedder_fakes import _FakeEncoding, _make_embedder


@pytest.mark.asyncio
async def test_make_embedder_returns_embed_capable_mock() -> None:
    embedder = _make_embedder()
    # _make_embedder returns a real Embedder instance with mocked HTTP/Qdrant clients
    assert isinstance(embedder, Embedder)
    assert isinstance(embedder._encoding, _FakeEncoding)
