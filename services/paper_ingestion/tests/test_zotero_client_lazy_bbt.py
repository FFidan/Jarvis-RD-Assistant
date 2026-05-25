"""Tests for the lazy BBT_LOCAL_BASE module attribute in zotero_client.

Verifies that:
1. Accessing ``zotero_client.BBT_LOCAL_BASE`` reflects the CURRENT settings
   value each time (lazy resolution, not frozen at import time).
2. Accessing an unknown module attribute raises ``AttributeError`` with a
   message that names the missing attribute.
"""

from __future__ import annotations

import pytest

from paper_ingestion.integrations import zotero_client


class _FakeSettings:
    def __init__(self, bbt_base_url: str) -> None:
        self.bbt_base_url = bbt_base_url


def test_bbt_local_base_lazy_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """BBT_LOCAL_BASE must re-evaluate settings on every attribute access."""
    monkeypatch.setattr(
        "paper_ingestion.integrations.zotero_client.get_paper_ingestion_settings",
        lambda: _FakeSettings("http://test-bbt.example/"),
    )
    assert zotero_client.BBT_LOCAL_BASE == "http://test-bbt.example//better-bibtex"

    # Update the monkeypatch — the value must change on the next access.
    monkeypatch.setattr(
        "paper_ingestion.integrations.zotero_client.get_paper_ingestion_settings",
        lambda: _FakeSettings("http://other-bbt.example"),
    )
    assert zotero_client.BBT_LOCAL_BASE == "http://other-bbt.example/better-bibtex"


def test_unknown_module_attribute_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accessing a non-existent module attribute must raise AttributeError."""
    with pytest.raises(AttributeError, match="no_such_thing"):
        _ = zotero_client.no_such_thing  # type: ignore[attr-defined]
