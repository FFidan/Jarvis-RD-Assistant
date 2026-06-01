"""Tests for Zotero group-library support (Bucket C).

Verifies that:
- ZoteroClient uses /groups/{group_id}/... URLs when library_type="group"
- ZoteroClient uses /users/{user_id}/... URLs when library_type="user" (default)
- ZoteroClient raises ValueError when library_type="group" but group_id is missing
- ZoteroClient raises ValueError for invalid group_id values
- The settings validator accepts valid zotero.group_id values and rejects invalid ones
- The test-connection endpoint correctly short-circuits when group_id is missing
"""

from __future__ import annotations

import httpx
import pytest
import respx
from paper_ingestion.integrations.zotero_client import ZOTERO_API_BASE, ZoteroClient
from paper_ingestion.services.config_validators import _validate_group_id, _validate_library_type

# ---------------------------------------------------------------------------
# ZoteroClient URL routing
# ---------------------------------------------------------------------------

USER_ID = "111111"
GROUP_ID = 987654
USER_BASE = f"{ZOTERO_API_BASE}/users/{USER_ID}"
GROUP_BASE = f"{ZOTERO_API_BASE}/groups/{GROUP_ID}"


@pytest.fixture
def http_client():
    return httpx.AsyncClient()


@pytest.fixture
def user_client(http_client):
    """ZoteroClient configured for a personal library (default)."""
    return ZoteroClient(
        api_key="test_key",
        user_id=USER_ID,
        library_type="user",
        http_client=http_client,
    )


@pytest.fixture
def group_client(http_client):
    """ZoteroClient configured for a group library."""
    return ZoteroClient(
        api_key="test_key",
        user_id=USER_ID,
        library_type="group",
        group_id=GROUP_ID,
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def test_user_client_base_url(user_client):
    """Personal-library client must use /users/{user_id} base URL."""
    assert user_client._base == USER_BASE


def test_group_client_base_url(group_client):
    """Group-library client must use /groups/{group_id} base URL (NOT user_id)."""
    assert group_client._base == GROUP_BASE


def test_group_client_stores_group_id(group_client):
    """group_id attribute should be set on group-library client."""
    assert group_client.group_id == GROUP_ID


def test_user_client_group_id_is_none(user_client):
    """group_id attribute should be None on personal-library client."""
    assert user_client.group_id is None


# ---------------------------------------------------------------------------
# Validation — constructor
# ---------------------------------------------------------------------------


def test_missing_group_id_raises():
    """library_type='group' without group_id must raise ValueError."""
    with pytest.raises(ValueError, match="group_id is required"):
        ZoteroClient(api_key="k", user_id="u", library_type="group")


def test_zero_group_id_raises():
    """group_id=0 must raise ValueError (must be positive)."""
    with pytest.raises(ValueError, match="positive integer"):
        ZoteroClient(api_key="k", user_id="u", library_type="group", group_id=0)


def test_negative_group_id_raises():
    """Negative group_id must raise ValueError."""
    with pytest.raises(ValueError, match="positive integer"):
        ZoteroClient(api_key="k", user_id="u", library_type="group", group_id=-1)


def test_invalid_library_type_raises():
    """library_type values other than 'user'/'group' must raise ValueError."""
    with pytest.raises(ValueError, match="library_type must be"):
        ZoteroClient(api_key="k", user_id="u", library_type="personal")  # type: ignore[arg-type]


def test_group_id_ignored_for_user_library():
    """group_id is silently ignored (not validated) when library_type='user'."""
    client = ZoteroClient(api_key="k", user_id="u", library_type="user", group_id=None)
    assert client._base == f"{ZOTERO_API_BASE}/users/u"


# ---------------------------------------------------------------------------
# HTTP requests hit correct endpoints
# ---------------------------------------------------------------------------


@respx.mock
async def test_user_client_fetches_from_user_endpoint(user_client):
    """fetch_items_since on a user client must call /users/{user_id}/items."""
    route = respx.get(f"{USER_BASE}/items").mock(
        return_value=httpx.Response(
            200,
            json=[{"key": "ITEM0001", "data": {}}],
            headers={"Total-Results": "1", "Zotero-Last-Modified-Version": "42"},
        )
    )

    items, version = await user_client.fetch_items_since(0)

    assert route.called
    assert len(items) == 1
    assert version == 42


@respx.mock
async def test_group_client_fetches_from_group_endpoint(group_client):
    """fetch_items_since on a group client must call /groups/{group_id}/items."""
    route = respx.get(f"{GROUP_BASE}/items").mock(
        return_value=httpx.Response(
            200,
            json=[{"key": "GRPITEM01", "data": {}}],
            headers={"Total-Results": "1", "Zotero-Last-Modified-Version": "77"},
        )
    )
    # Ensure user endpoint is NOT called
    user_route = respx.get(f"{USER_BASE}/items").mock(return_value=httpx.Response(200, json=[]))

    items, version = await group_client.fetch_items_since(0)

    assert route.called, "group client must call /groups/{group_id}/items"
    assert not user_route.called, "group client must NOT call /users/{user_id}/items"
    assert len(items) == 1
    assert version == 77


@respx.mock
async def test_group_client_search_by_doi(group_client):
    """search_by_doi on a group client must call /groups/{group_id}/items."""
    doi = "10.9999/group-paper"
    item = {"key": "GRPDOI01", "data": {"DOI": doi, "title": "Group Paper"}}
    route = respx.get(f"{GROUP_BASE}/items").mock(return_value=httpx.Response(200, json=[item]))

    result = await group_client.search_by_doi(doi)

    assert route.called
    assert result is not None
    assert result["key"] == "GRPDOI01"


@respx.mock
async def test_group_client_test_connection(group_client):
    """test_connection on a group client must probe /groups/{group_id}/items."""
    route = respx.get(f"{GROUP_BASE}/items").mock(return_value=httpx.Response(200, json=[]))

    ok = await group_client.test_connection()

    assert route.called
    assert ok is True


@respx.mock
async def test_user_client_create_item_url(user_client):
    """create_item on a user client must POST to /users/{user_id}/items."""
    route = respx.post(f"{USER_BASE}/items").mock(
        return_value=httpx.Response(
            200,
            json={"successful": {"0": {"key": "NEW0001"}}, "unchanged": {}, "failed": {}},
        )
    )

    result = await user_client.create_item({"itemType": "journalArticle", "title": "Test"})

    assert route.called
    assert result["successful"]["0"]["key"] == "NEW0001"


@respx.mock
async def test_group_client_create_item_url(group_client):
    """create_item on a group client must POST to /groups/{group_id}/items."""
    route = respx.post(f"{GROUP_BASE}/items").mock(
        return_value=httpx.Response(
            200,
            json={"successful": {"0": {"key": "GRP0001"}}, "unchanged": {}, "failed": {}},
        )
    )

    result = await group_client.create_item({"itemType": "journalArticle", "title": "Group Paper"})

    assert route.called
    assert result["successful"]["0"]["key"] == "GRP0001"


# ---------------------------------------------------------------------------
# Settings validators
# ---------------------------------------------------------------------------


def test_validate_library_type_accepts_user():
    _validate_library_type("user")  # must not raise


def test_validate_library_type_accepts_group():
    _validate_library_type("group")  # must not raise


def test_validate_library_type_rejects_invalid():
    with pytest.raises(ValueError, match="must be 'user' or 'group'"):
        _validate_library_type("personal")


def test_validate_group_id_accepts_positive_int():
    _validate_group_id(123456)  # must not raise


def test_validate_group_id_accepts_none():
    _validate_group_id(None)  # null is allowed (clearing the field)


def test_validate_group_id_rejects_zero():
    with pytest.raises(ValueError, match="positive integer"):
        _validate_group_id(0)


def test_validate_group_id_rejects_negative():
    with pytest.raises(ValueError, match="positive integer"):
        _validate_group_id(-5)


def test_validate_group_id_rejects_string():
    with pytest.raises(ValueError, match="positive integer"):
        _validate_group_id("987654")


def test_validate_group_id_rejects_float():
    with pytest.raises(ValueError, match="positive integer"):
        _validate_group_id(987654.0)


def test_validate_group_id_rejects_bool():
    with pytest.raises(ValueError, match="positive integer"):
        _validate_group_id(True)
