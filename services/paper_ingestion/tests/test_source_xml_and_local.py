"""Tests for XML safety helpers and the local-source registry stub."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

import pytest
from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.sources._xml_safe import safe_fromstring, safe_parse
from paper_ingestion.sources.local_source import LocalSource


def test_safe_fromstring_accepts_text_and_blocks_entity_resolution() -> None:
    """XML text input should parse without expanding external entities."""
    xml = """<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><root>&xxe;</root>"""

    root = safe_fromstring(xml)

    assert root.tag == "root"
    assert root.text in (None, "")


def test_safe_parse_uses_same_parser_contract_for_file_like_input() -> None:
    """File-like XML parsing should preserve normal element text."""
    tree = safe_parse(BytesIO(b"<root><child>ok</child></root>"))

    assert tree.getroot().findtext("child") == "ok"


@pytest.mark.asyncio
async def test_local_source_search_is_empty_noop() -> None:
    """Local PDFs are imported through upload/scan endpoints, not source search."""
    config = PaperSourceConfig(id=1, source_type=SourceType.LOCAL, enabled=True, config={})
    source = LocalSource(config=config, http_client=MagicMock())

    assert await source.search("anything", max_results=5, year_from=2020, author="Ada") == []


@pytest.mark.asyncio
async def test_local_source_fetch_by_id_is_empty_noop() -> None:
    """Local PDFs have no external fetch-by-id contract."""
    config = PaperSourceConfig(id=1, source_type=SourceType.LOCAL, enabled=True, config={})
    source = LocalSource(config=config, http_client=MagicMock())

    assert await source.fetch_by_id("local:1") is None
