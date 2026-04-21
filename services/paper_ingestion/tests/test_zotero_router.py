"""Zotero router tests.

D3 (routers/zotero.py) has not been created yet.
This file provides a minimal smoke-test that the Zotero integration
modules can be imported successfully, so that the test suite stays green
while router work is deferred.

When D3 lands, replace this file with full endpoint tests using
httpx.AsyncClient(app=app, base_url="http://test").
"""

from __future__ import annotations


def test_zotero_imports():
    """Zotero client and service modules import without errors."""
    from paper_ingestion.integrations import zotero_client, zotero_service

    assert zotero_client is not None
    assert zotero_service is not None


def test_zotero_client_class_exists():
    """ZoteroClient class is importable and has expected public methods."""
    from paper_ingestion.integrations.zotero_client import ZoteroClient

    for method in (
        "create_item",
        "search_by_doi",
        "ensure_collection",
        "fetch_bbt_citation_key",
        "test_connection",
    ):
        assert hasattr(ZoteroClient, method), f"ZoteroClient missing method: {method}"


def test_zotero_service_functions_exist():
    """push_paper_to_zotero and resync_paper_to_zotero are importable callables."""
    import inspect

    from paper_ingestion.integrations.zotero_service import (
        push_paper_to_zotero,
        resync_paper_to_zotero,
    )

    assert inspect.iscoroutinefunction(push_paper_to_zotero)
    assert inspect.iscoroutinefunction(resync_paper_to_zotero)
