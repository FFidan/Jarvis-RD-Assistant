"""Tests for ZoteroClient.

Uses respx to mock the Zotero Web API and BBT local API.
All tests are async (asyncio_mode = auto in pyproject.toml).
"""

from __future__ import annotations

import httpx
import pytest
import respx
from paper_ingestion.integrations.zotero_client import (
    BBT_LOCAL_BASE,
    ZOTERO_API_BASE,
    ZoteroClient,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

USER_ID = "123456"
BASE = f"{ZOTERO_API_BASE}/users/{USER_ID}"


@pytest.fixture
def http_client():
    return httpx.AsyncClient()


@pytest.fixture
def client(http_client):
    return ZoteroClient(
        api_key="test_key",
        user_id=USER_ID,
        library_type="user",
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# create_item
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_item_success(client):
    """create_item POSTs to /items and returns the full API response dict."""
    route = respx.post(f"{BASE}/items").mock(
        return_value=httpx.Response(
            200,
            json={
                "successful": {"0": {"key": "ABCD1234", "data": {"title": "Test Paper"}}},
                "unchanged": {},
                "failed": {},
            },
        )
    )

    result = await client.create_item({"itemType": "journalArticle", "title": "Test Paper"})

    assert route.called
    assert result["successful"]["0"]["key"] == "ABCD1234"


# ---------------------------------------------------------------------------
# search_by_doi
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_by_doi_found(client):
    """search_by_doi returns first matching item when API returns results."""
    doi = "10.1234/test"
    item = {"key": "XYZT5678", "data": {"DOI": doi, "title": "Found Paper"}}
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json=[item]))

    result = await client.search_by_doi(doi)

    assert result is not None
    assert result["key"] == "XYZT5678"
    assert result["data"]["DOI"] == doi


@respx.mock
async def test_search_by_doi_not_found(client):
    """search_by_doi returns None when the API returns an empty list."""
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json=[]))

    result = await client.search_by_doi("10.9999/missing")

    assert result is None


# ---------------------------------------------------------------------------
# ensure_collection
# ---------------------------------------------------------------------------


@respx.mock
async def test_ensure_collection_creates_new(client):
    """ensure_collection creates a new collection when none with that name exist."""
    respx.get(f"{BASE}/collections").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/collections").mock(
        return_value=httpx.Response(
            200,
            json={"successful": {"0": {"key": "COLL1234"}}, "unchanged": {}, "failed": {}},
        )
    )

    col_key = await client.ensure_collection("My Project")

    assert col_key == "COLL1234"


@respx.mock
async def test_ensure_collection_reuses_existing(client):
    """ensure_collection returns existing key without POSTing when name matches."""
    existing = [{"key": "EXISTING", "data": {"name": "My Project"}}]
    get_route = respx.get(f"{BASE}/collections").mock(
        return_value=httpx.Response(200, json=existing)
    )
    post_route = respx.post(f"{BASE}/collections").mock(return_value=httpx.Response(200, json={}))

    col_key = await client.ensure_collection("My Project")

    assert col_key == "EXISTING"
    assert not post_route.called
    assert get_route.call_count == 1


# ---------------------------------------------------------------------------
# fetch_bbt_citation_key
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_bbt_citation_key_success(client):
    """fetch_bbt_citation_key returns the citation key from a 200 BBT response."""
    item_key = "ABCD1234"
    respx.get(f"{BBT_LOCAL_BASE}/export/item").mock(
        return_value=httpx.Response(200, json=[{"id": "Author2024xyz", "type": "article"}])
    )

    result = await client.fetch_bbt_citation_key(item_key)

    assert result == "Author2024xyz"


@respx.mock
async def test_fetch_bbt_citation_key_connection_error(client):
    """fetch_bbt_citation_key returns None silently when BBT is not running."""
    respx.get(f"{BBT_LOCAL_BASE}/export/item").mock(side_effect=httpx.ConnectError("refused"))

    result = await client.fetch_bbt_citation_key("ABCD1234")

    assert result is None


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


@respx.mock
async def test_test_connection_success(client):
    """test_connection returns True when the API responds with 200."""
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(200, json=[]))

    result = await client.test_connection()

    assert result is True


@respx.mock
async def test_test_connection_failure(client):
    """test_connection returns False when the API responds with 403."""
    respx.get(f"{BASE}/items").mock(return_value=httpx.Response(403, json={"error": "Forbidden"}))

    result = await client.test_connection()

    assert result is False
