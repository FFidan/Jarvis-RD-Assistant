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
    validate_bbt_base_url,
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
# ensure_collection — PI-012 pagination
# ---------------------------------------------------------------------------


def _make_page(start: int, count: int, prefix: str = "COL") -> list[dict]:
    """Build a page of `count` fake collection dicts starting at index `start`."""
    return [
        {"key": f"{prefix}{start + i:04d}", "data": {"name": f"Collection {start + i}"}}
        for i in range(count)
    ]


@respx.mock
async def test_ensure_collection_paginates_three_pages(client):
    """ensure_collection fetches all pages when >100 collections exist.

    Scenario: 250 total collections across 3 pages (100 + 100 + 50).
    The target collection is on the third page — single-shot GET would miss it.
    """
    page1 = _make_page(0, 100)
    page2 = _make_page(100, 100)
    # Third page contains our target
    page3 = _make_page(200, 49) + [{"key": "TARGET", "data": {"name": "My Target"}}]

    total = 250
    call_count = 0

    def side_effect(request):
        nonlocal call_count
        params = dict(request.url.params)
        start = int(params.get("start", 0))
        if start == 0:
            page = page1
        elif start == 100:
            page = page2
        else:
            page = page3
        call_count += 1
        return httpx.Response(
            200,
            json=page,
            headers={"Total-Results": str(total)},
        )

    respx.get(f"{BASE}/collections").mock(side_effect=side_effect)

    col_key = await client.ensure_collection("My Target")

    assert col_key == "TARGET"
    assert call_count == 3, f"Expected 3 GET requests, got {call_count}"


@respx.mock
async def test_ensure_collection_deduplicates_across_pages(client):
    """ensure_collection deduplicates collections that appear on multiple pages."""
    # Page 1 has 100 items; page 2 has 50 new + 10 duplicates (same key as page 1 start)
    page1 = _make_page(0, 100)
    # Duplicate the first 10 keys from page1 + 50 new ones
    duplicates = _make_page(0, 10)
    new_items = _make_page(100, 50)
    page2 = duplicates + new_items  # 60 items, < 100 → loop stops

    total = 150

    call_count = 0

    def side_effect(request):
        nonlocal call_count
        start = int(dict(request.url.params).get("start", 0))
        page = page1 if start == 0 else page2
        call_count += 1
        return httpx.Response(200, json=page, headers={"Total-Results": str(total)})

    respx.get(f"{BASE}/collections").mock(side_effect=side_effect)
    # The target is in the new items on page 2
    target_col = {"key": "UNIQUE150", "data": {"name": "Last Collection"}}
    # Inject it — rebuild page2 with target at end
    page2_with_target = duplicates + new_items[:-1] + [target_col]

    # Reset and remock with updated page2
    respx.get(f"{BASE}/collections").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json=page1 if int(dict(req.url.params).get("start", 0)) == 0 else page2_with_target,
            headers={"Total-Results": str(total)},
        )
    )

    col_key = await client.ensure_collection("Last Collection")
    assert col_key == "UNIQUE150"


@respx.mock
async def test_ensure_collection_single_page_no_extra_requests(client):
    """ensure_collection stops after one page when fewer than 100 items returned."""
    collections = _make_page(0, 50)
    get_route = respx.get(f"{BASE}/collections").mock(
        return_value=httpx.Response(
            200,
            json=collections,
            headers={"Total-Results": "50"},
        )
    )
    respx.post(f"{BASE}/collections").mock(
        return_value=httpx.Response(
            200,
            json={"successful": {"0": {"key": "NEWCOL"}}, "unchanged": {}, "failed": {}},
        )
    )

    # Request a collection that doesn't exist → should create it
    col_key = await client.ensure_collection("Brand New Collection")

    assert col_key == "NEWCOL"
    # Only one GET page fetched
    assert get_route.call_count == 1


# ---------------------------------------------------------------------------
# get_item_children
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_item_children_paginates_annotations(client):
    """get_item_children fetches all annotation children for a Zotero item."""
    item_key = "ITEM1234"
    page1 = [{"key": f"ANN{i:03d}", "data": {"itemType": "annotation"}} for i in range(100)]
    page2 = [{"key": "ANN100", "data": {"itemType": "annotation"}}]
    starts: list[int] = []

    def side_effect(request):
        start = int(dict(request.url.params).get("start", 0))
        starts.append(start)
        return httpx.Response(
            200,
            json=page1 if start == 0 else page2,
            headers={"Total-Results": "101"},
        )

    respx.get(f"{BASE}/items/{item_key}/children").mock(side_effect=side_effect)

    result = await client.get_item_children(item_key, item_type="annotation")

    assert len(result) == 101
    assert starts == [0, 100]


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


# ---------------------------------------------------------------------------
# fetch_items_since — PI-EDGE-001 pagination
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_items_since_paginates(client):
    """fetch_items_since accumulates all pages and returns version from last page.

    Scenario: 350 total items across 4 pages (100 + 100 + 100 + 50).
    Each page returns a distinct Zotero-Last-Modified-Version header;
    the returned version must be from the 4th (final) page.
    """
    page_versions = {0: "1001", 100: "1002", 200: "1003", 300: "1004"}
    starts: list[int] = []

    def side_effect(request):
        params = dict(request.url.params)
        start = int(params.get("start", 0))
        starts.append(start)
        count = 100 if start < 300 else 50
        items = [{"key": f"ITEM{start + i:04d}", "data": {}} for i in range(count)]
        return httpx.Response(
            200,
            json=items,
            headers={
                "Total-Results": "350",
                "Zotero-Last-Modified-Version": page_versions[start],
            },
        )

    respx.get(f"{BASE}/items").mock(side_effect=side_effect)

    items, new_version = await client.fetch_items_since(0)

    assert len(items) == 350, f"Expected 350 items, got {len(items)}"
    assert new_version == 1004, f"Expected version 1004 from last page, got {new_version}"
    assert starts == [0, 100, 200, 300], f"Unexpected page starts: {starts}"


@respx.mock
async def test_fetch_items_since_single_page_no_extra_requests(client):
    """fetch_items_since stops after one page when fewer than 100 items returned."""
    items = [{"key": f"ITEM{i:04d}", "data": {}} for i in range(25)]
    get_route = respx.get(f"{BASE}/items").mock(
        return_value=httpx.Response(
            200,
            json=items,
            headers={"Total-Results": "25", "Zotero-Last-Modified-Version": "99"},
        )
    )

    result_items, new_version = await client.fetch_items_since(0)

    assert len(result_items) == 25
    assert new_version == 99
    assert get_route.call_count == 1


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


# ---------------------------------------------------------------------------
# PI-EDGE-008: validate_bbt_base_url() — scheme + private-IP guard
# ---------------------------------------------------------------------------


def test_validate_bbt_base_url_rejects_file_scheme():
    """PI-EDGE-008: file:// scheme must be rejected by validate_bbt_base_url."""
    with pytest.raises(ValueError, match="unsupported scheme"):
        validate_bbt_base_url("file:///etc/passwd")


def test_validate_bbt_base_url_rejects_ftp_scheme():
    """PI-EDGE-008: ftp:// scheme must be rejected by validate_bbt_base_url."""
    with pytest.raises(ValueError, match="unsupported scheme"):
        validate_bbt_base_url("ftp://host.docker.internal:23119")


def test_validate_bbt_base_url_rejects_private_ip():
    """PI-EDGE-008: private IP addresses not in the allow-list are rejected."""
    with pytest.raises(ValueError, match="private/loopback"):
        validate_bbt_base_url("http://192.168.1.1:23119")


def test_validate_bbt_base_url_rejects_loopback_ip():
    """PI-EDGE-008: loopback IP 127.0.0.1 is rejected (not the docker alias)."""
    with pytest.raises(ValueError, match="private/loopback"):
        validate_bbt_base_url("http://127.0.0.1:23119")


def test_validate_bbt_base_url_accepts_host_docker_internal():
    """PI-EDGE-008: host.docker.internal is explicitly allowed (Docker-Desktop standard)."""
    # Must not raise — this is the default BBT_BASE_URL hostname.
    validate_bbt_base_url("http://host.docker.internal:23119")


def test_validate_bbt_base_url_accepts_https_public_host():
    """PI-EDGE-008: https:// with a public hostname is accepted."""
    validate_bbt_base_url("https://my-zotero-bbt.example.com:23119")
