"""Regression tests for LocalSource registry-factory construction (HIGH-PI-11).

LocalSource.__init__ was missing the db_pool 3rd positional that PaperSource
declares, causing TypeError when the registry factory passed db_pool positionally.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.sources.local_source import LocalSource
from paper_ingestion.sources.registry import get_source_class


def _config() -> PaperSourceConfig:
    return PaperSourceConfig(id=1, source_type=SourceType.LOCAL, enabled=True, config={})


def test_local_source_registry_factory_accepts_db_pool_positional() -> None:
    """Constructing LocalSource with all 3 positional args raises no TypeError.

    Simulates the registry-factory path that passes (config, http_client, db_pool)
    positionally.
    """
    mock_pool = MagicMock()

    source = LocalSource(_config(), MagicMock(), mock_pool)

    assert isinstance(source, LocalSource)
    assert source.db_pool is mock_pool


def test_local_source_registry_lookup_returns_class() -> None:
    """get_source_class('local') resolves to LocalSource after module import."""
    assert get_source_class("local") is LocalSource
