"""Tests for ZoteroClient.

Uses respx to mock the Zotero Web API and BBT local API.
All tests are async (asyncio_mode = auto in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
import respx
from jarvis_common.pinned_transport import PinnedAsyncTransport, pinned_async_client
from paper_ingestion.integrations import zotero_client
from paper_ingestion.integrations.zotero_client import (
    BBT_LOCAL_BASE,
    ZOTERO_API_BASE,
    ZoteroClient,
    validate_bbt_base_url,
)

USER_ID = "123456"
BASE = f"{ZOTERO_API_BASE}/users/{USER_ID}"


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


async def test_zotero_sink_refuses_quarantine_before_http(monkeypatch, tmp_path, http_client):
    from jarvis_common.maintenance import OutboundEgressBlockedError

    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.write_text("malformed")
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    client = ZoteroClient(api_key="restored-key", user_id=USER_ID, http_client=http_client)

    with pytest.raises(OutboundEgressBlockedError, match="credential review"):
        await client.create_item({"itemType": "journalArticle", "title": "Blocked"})


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
# ensure_collection — pagination
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
# fetch_items_since — pagination
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
# validate_bbt_base_url() — scheme + private-IP guard
# ---------------------------------------------------------------------------


def test_validate_bbt_base_url_rejects_file_scheme():
    """file:// scheme must be rejected by validate_bbt_base_url."""
    with pytest.raises(ValueError, match="unsupported scheme"):
        validate_bbt_base_url("file:///etc/passwd")


def test_validate_bbt_base_url_rejects_ftp_scheme():
    """ftp:// scheme must be rejected by validate_bbt_base_url."""
    with pytest.raises(ValueError, match="unsupported scheme"):
        validate_bbt_base_url("ftp://host.docker.internal:23119")


def test_validate_bbt_base_url_rejects_private_ip():
    """Private IP addresses not in the allow-list are rejected."""
    with pytest.raises(ValueError, match="non-public"):
        validate_bbt_base_url("http://192.168.1.1:23119")


def test_validate_bbt_base_url_rejects_loopback_ip():
    """Loopback IP 127.0.0.1 is rejected (not the docker alias)."""
    with pytest.raises(ValueError, match="non-public"):
        validate_bbt_base_url("http://127.0.0.1:23119")


def test_validate_bbt_base_url_accepts_host_docker_internal():
    """host.docker.internal is explicitly allowed (Docker-Desktop standard)."""
    # Must not raise — this is the default BBT_BASE_URL hostname.
    validate_bbt_base_url("http://host.docker.internal:23119")


def test_validate_bbt_base_url_accepts_https_public_host():
    """https:// with a public hostname is accepted."""
    validate_bbt_base_url("https://my-zotero-bbt.example.com:23119")


def test_validate_bbt_base_url_rejects_cgnat_ip():
    """CGNAT space (100.64.0.0/10) is non-public and must be refused (SSRF guard)."""
    with pytest.raises(ValueError, match="non-public"):
        validate_bbt_base_url("http://100.64.0.1:23119")


def test_validate_bbt_base_url_rejects_reserved_ip():
    """Reserved space (240.0.0.0/4) is non-public and must be refused (SSRF guard)."""
    with pytest.raises(ValueError, match="non-public"):
        validate_bbt_base_url("http://240.0.0.1:23119")


def test_validate_bbt_base_url_rejects_multicast_ip():
    """Multicast space (224.0.0.0/4) is non-public and must be refused (SSRF guard)."""
    with pytest.raises(ValueError, match="non-public"):
        validate_bbt_base_url("http://224.0.0.1:23119")


def test_validate_bbt_base_url_rejects_unspecified_ip():
    """0.0.0.0 (unspecified) is non-public and must be refused (SSRF guard)."""
    with pytest.raises(ValueError, match="non-public"):
        validate_bbt_base_url("http://0.0.0.0:23119")


# ---------------------------------------------------------------------------
# str BYTEA variant in _get_zotero_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_zotero_config_handles_str_encrypted_value(monkeypatch):
    """_get_zotero_config decrypts encrypted_value when Postgres returns it as str (not bytes).

    asyncpg may return BYTEA columns as str in certain session configurations.
    resolve_secret_row must handle the str branch without AttributeError.
    """
    from unittest.mock import AsyncMock, MagicMock

    from cryptography.fernet import Fernet
    from jarvis_common.crypto import encrypt_secret, refresh_fernet_cache
    from jarvis_common.testing_db import FakeRecord
    from paper_ingestion.integrations.zotero_service import _get_zotero_config

    test_key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", test_key)
    refresh_fernet_cache()

    plaintext = "str-variant-api-key-xyz"
    # Ciphertext as a plain str (simulate Postgres returning BYTEA as text).
    ciphertext_str = encrypt_secret(plaintext)

    rows = [
        FakeRecord(
            {
                "key": "zotero.api_key",
                "value": None,
                "encrypted_value": ciphertext_str,  # str, not bytes
                "user_id": None,
            }
        ),
    ]

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)

    config = await _get_zotero_config(pool)

    assert config.get("api_key") == plaintext, "str BYTEA encrypted_value must be decrypted"

    refresh_fernet_cache()


# ---------------------------------------------------------------------------
# MED-PI-EXT-03: httpx.AsyncClient constructed with explicit Timeout
# ---------------------------------------------------------------------------


def test_zotero_client_default_http_client_has_timeout():
    """ZoteroClient() without an http_client creates one with a Timeout object.

    Ensures no request can hang indefinitely when the caller does not provide
    a pre-configured httpx.AsyncClient.
    """
    zc = ZoteroClient(api_key="k", user_id="42")
    assert isinstance(zc._http.timeout, httpx.Timeout), (
        "Default httpx.AsyncClient must be constructed with an explicit Timeout"
    )
    # Default timeout should be 30 seconds on all axes.
    assert zc._http.timeout.read == 30.0


# ---------------------------------------------------------------------------
# MED-PI-EXT-03: Paginator cap at _MAX_ZOTERO_PAGES_ITEMS
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_items_since_raises_on_runaway_pagination(client):
    """fetch_items_since raises RuntimeError when total exceeds _MAX_ZOTERO_PAGES_ITEMS.

    Simulates a Zotero server that always says there are more items, causing
    the paginator to loop until the cap is hit.
    """
    from paper_ingestion.integrations.zotero_client import _MAX_ZOTERO_PAGES_ITEMS

    page_size = 100
    # Lie: claim there are always more items than fetched.
    fake_total = _MAX_ZOTERO_PAGES_ITEMS + 1

    def _side_effect(request):
        page = [{"key": f"ITEM{i}"} for i in range(page_size)]
        return httpx.Response(
            200,
            json=page,
            headers={
                "Total-Results": str(fake_total),
                "Zotero-Last-Modified-Version": "1",
            },
        )

    respx.get(f"{BASE}/items").mock(side_effect=_side_effect)

    with pytest.raises(RuntimeError, match="paginator exceeded"):
        await client.fetch_items_since(version=0)


@respx.mock
async def test_ensure_collection_raises_on_runaway_pagination(client):
    """ensure_collection raises RuntimeError when collections exceed _MAX_ZOTERO_PAGES_ITEMS."""
    from paper_ingestion.integrations.zotero_client import _MAX_ZOTERO_PAGES_ITEMS

    page_size = 100
    fake_total = _MAX_ZOTERO_PAGES_ITEMS + 1

    def _side_effect(request):
        page = [{"key": f"COL{i}", "data": {"name": f"Collection {i}"}} for i in range(page_size)]
        return httpx.Response(200, json=page, headers={"Total-Results": str(fake_total)})

    respx.get(f"{BASE}/collections").mock(side_effect=_side_effect)

    with pytest.raises(RuntimeError, match="paginator exceeded"):
        await client.ensure_collection("DoesNotMatter")


@respx.mock
async def test_get_item_children_raises_on_runaway_pagination(client):
    """get_item_children raises RuntimeError when children exceed _MAX_ZOTERO_PAGES_ITEMS."""
    from paper_ingestion.integrations.zotero_client import _MAX_ZOTERO_PAGES_ITEMS

    page_size = 100
    fake_total = _MAX_ZOTERO_PAGES_ITEMS + 1
    item_key = "TESTKEY"

    def _side_effect(request):
        page = [{"key": f"CHILD{i}"} for i in range(page_size)]
        return httpx.Response(200, json=page, headers={"Total-Results": str(fake_total)})

    respx.get(f"{BASE}/items/{item_key}/children").mock(side_effect=_side_effect)

    with pytest.raises(RuntimeError, match="paginator exceeded"):
        await client.get_item_children(item_key)


# ---------------------------------------------------------------------------
# add_item_to_collections
# ---------------------------------------------------------------------------


@respx.mock
async def test_add_item_to_collections_merges_and_patches(client):
    """GETs the item for its version, unions in the new collection keys, and PATCHes
    only the grown set with an If-Unmodified-Since-Version precondition."""
    item_key = "ITEMABC"
    get_route = respx.get(f"{BASE}/items/{item_key}").mock(
        return_value=httpx.Response(
            200, json={"key": item_key, "version": 41, "data": {"collections": ["COLLA"]}}
        )
    )
    patch_route = respx.patch(f"{BASE}/items/{item_key}").mock(return_value=httpx.Response(204))

    await client.add_item_to_collections(item_key, ["COLLB", "COLLA"])

    assert get_route.called and patch_route.called
    sent = json.loads(patch_route.calls.last.request.content)
    assert sorted(sent["collections"]) == ["COLLA", "COLLB"]
    assert patch_route.calls.last.request.headers["If-Unmodified-Since-Version"] == "41"


@respx.mock
async def test_add_item_to_collections_noop_when_already_member(client):
    """No PATCH when the item is already in every requested collection."""
    item_key = "ITEMXYZ"
    respx.get(f"{BASE}/items/{item_key}").mock(
        return_value=httpx.Response(
            200, json={"key": item_key, "version": 7, "data": {"collections": ["C1", "C2"]}}
        )
    )
    patch_route = respx.patch(f"{BASE}/items/{item_key}").mock(return_value=httpx.Response(204))

    await client.add_item_to_collections(item_key, ["C1"])

    assert not patch_route.called


# ---------------------------------------------------------------------------
# Better BibTeX host policy — private hosts need an explicit allowlist entry
# ---------------------------------------------------------------------------

_LAN_BBT_BASE = "http://zotero.lan:23119"


@pytest.fixture
def _lan_bbt(monkeypatch):
    """Point the client at a LAN hostname and start from an empty allowlist."""
    monkeypatch.setattr(
        zotero_client,
        "get_paper_ingestion_settings",
        lambda: SimpleNamespace(bbt_base_url=_LAN_BBT_BASE),
    )
    zotero_client.set_configured_private_hosts(frozenset())
    yield
    zotero_client.set_configured_private_hosts(frozenset())


def _install_bbt_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    address: str | None = None,
    response: httpx.Response | None = None,
) -> list[object]:
    """Route the real BBT client through a deterministic pinned transport."""
    observed: list[object] = []

    def client_factory(policy, *, timeout):  # type: ignore[no-untyped-def]
        observed.append(policy)
        if response is not None:

            async def handler(request: httpx.Request) -> httpx.Response:
                observed.append(request)
                return response

            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler), timeout=timeout, trust_env=False
            )

        async def resolver(host: str, port: int) -> list[tuple[int, str]]:
            observed.append((host, port))
            if address is None:
                raise httpcore.ConnectError("Unable to resolve host")
            return [(2, address)]

        return pinned_async_client(
            policy,
            timeout=timeout,
            transport=PinnedAsyncTransport(policy, resolver=resolver),
        )

    monkeypatch.setattr(zotero_client, "pinned_async_client", client_factory)
    return observed


@pytest.mark.usefixtures("_lan_bbt")
async def test_private_resolving_host_issues_no_request(client, monkeypatch, caplog):
    """A host resolving into private space is refused before anything goes out."""
    observed = _install_bbt_transport(monkeypatch, address="192.168.1.50")

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.integrations.zotero_client"):
        result = await client.fetch_bbt_citation_key("ABCD1234")

    assert result is None
    assert observed[1:] == [("zotero.lan", 23119)]
    assert any("zotero.allowed_private_hosts" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("_lan_bbt")
async def test_allowlisted_private_host_is_reached(client, monkeypatch):
    """The same host is reachable once the operator allowlists it."""
    requests: list[str] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readuntil(b"\r\n\r\n")
        requests.append(raw.decode("ascii").split("\r\n", 1)[0])
        body = b'[{"id":"Author2024xyz"}]'
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode(
                "ascii"
            )
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(
        zotero_client,
        "get_paper_ingestion_settings",
        lambda: SimpleNamespace(bbt_base_url=f"http://zotero.lan:{port}"),
    )
    zotero_client.set_configured_private_hosts(frozenset({"zotero.lan"}))

    async def resolver(host: str, resolved_port: int) -> list[tuple[int, str]]:
        assert (host, resolved_port) == ("zotero.lan", port)
        return [(2, "127.0.0.1")]

    observed: list[object] = []

    def client_factory(policy, *, timeout):  # type: ignore[no-untyped-def]
        observed.append(policy)
        return pinned_async_client(
            policy,
            timeout=timeout,
            transport=PinnedAsyncTransport(policy, resolver=resolver),
        )

    monkeypatch.setattr(zotero_client, "pinned_async_client", client_factory)
    try:
        assert await client.fetch_bbt_citation_key("ABCD1234") == "Author2024xyz"
    finally:
        server.close()
        await server.wait_closed()

    policy = observed[0]
    assert policy.allows("zotero.lan", ipaddress.ip_address("192.168.1.50"))
    assert requests == [
        "GET /better-bibtex/export/item?itemKey=ABCD1234&translator=csljson HTTP/1.1"
    ]


@pytest.mark.usefixtures("_lan_bbt")
async def test_host_resolving_to_a_scoped_address_is_refused(client, monkeypatch):
    """A scoped IPv6 answer cannot be classified, so it must not reach the request."""
    observed = _install_bbt_transport(monkeypatch, address="fe80::1%eth0")

    assert await client.fetch_bbt_citation_key("ABCD1234") is None
    assert observed[1:] == [("zotero.lan", 23119)]


@pytest.mark.usefixtures("_lan_bbt")
async def test_unresolvable_host_is_rechecked_and_still_refused(client, monkeypatch):
    """A boot-time DNS outage must not become a permanent verdict either way."""
    first = _install_bbt_transport(monkeypatch)

    assert await client.fetch_bbt_citation_key("ABCD1234") is None
    assert first[1:] == [("zotero.lan", 23119)]

    # DNS recovers, and now answers with a private address: still refused.
    second = _install_bbt_transport(monkeypatch, address="10.0.0.9")
    assert await client.fetch_bbt_citation_key("ABCD1234") is None
    assert second[1:] == [("zotero.lan", 23119)]


async def test_startup_hook_reports_a_private_host_without_aborting_boot(monkeypatch, caplog):
    """Boot must survive a private BBT host — Settings is where it gets allowlisted."""
    import paper_ingestion.main as main_module

    monkeypatch.setattr(
        zotero_client,
        "get_paper_ingestion_settings",
        lambda: SimpleNamespace(bbt_base_url="http://192.168.1.50:23119"),
    )
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.main"):
        await main_module._validate_bbt_url_hook(app)  # must not raise

    assert any("zotero.allowed_private_hosts" in r.getMessage() for r in caplog.records)


async def test_startup_hook_still_aborts_on_an_unsupported_scheme(monkeypatch):
    """A scheme typo is a configuration error, not a reachability policy."""
    import paper_ingestion.main as main_module

    monkeypatch.setattr(
        zotero_client,
        "get_paper_ingestion_settings",
        lambda: SimpleNamespace(bbt_base_url="file:///etc/passwd"),
    )
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with pytest.raises(ValueError, match="unsupported scheme"):
        await main_module._validate_bbt_url_hook(app)


def test_validate_bbt_base_url_honours_a_configured_allowlist():
    """A configured host clears the private-IP refusal for the startup check."""
    validate_bbt_base_url(
        "http://192.168.1.50:23119", allowed_private_hosts=frozenset({"192.168.1.50"})
    )
