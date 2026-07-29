"""URL-encode item_key in Zotero/BBT requests.

Verifies that special characters in item_key are percent-encoded in the URL,
preventing path injection (e.g. item_key containing '/' could traverse path segments).
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

USER_ID = "123456"
BASE = f"{ZOTERO_API_BASE}/users/{USER_ID}"

SPECIAL_ITEM_KEY = "ABCD/EFG?h=1"
ENCODED_ITEM_KEY = "ABCD%2FEFG%3Fh%3D1"


@pytest.fixture
def client(http_client):
    return ZoteroClient(
        api_key="test_key",
        user_id=USER_ID,
        library_type="user",
        http_client=http_client,
    )


@respx.mock
async def test_zotero_client_encodes_item_key_in_url(client):
    """M3: get_item_children must percent-encode item_key in the request path."""
    route = respx.get(
        f"{BASE}/items/{ENCODED_ITEM_KEY}/children",
    ).mock(
        return_value=httpx.Response(
            200,
            json=[],
            headers={"Total-Results": "0"},
        )
    )

    result = await client.get_item_children(SPECIAL_ITEM_KEY)

    assert route.called, (
        f"Expected request to encoded URL containing '{ENCODED_ITEM_KEY}', but it was not made. "
        f"Raw key '{SPECIAL_ITEM_KEY}' must be percent-encoded before being placed in the URL path."
    )
    assert result == []
    # Also verify the raw key is NOT in the URL (path injection guard)
    actual_url = str(route.calls[0].request.url)
    assert SPECIAL_ITEM_KEY not in actual_url, (
        f"Raw item_key '{SPECIAL_ITEM_KEY}' must NOT appear in URL — only encoded form allowed"
    )


@respx.mock
async def test_zotero_client_encodes_item_key_in_bbt_url(client):
    """M3: fetch_bbt_citation_key must percent-encode item_key in the BBT query string."""
    route = respx.get(
        f"{BBT_LOCAL_BASE}/export/item",
    ).mock(return_value=httpx.Response(200, json=[{"id": "MyKey2024"}]))

    result = await client.fetch_bbt_citation_key(SPECIAL_ITEM_KEY)

    assert result == "MyKey2024"
    assert route.called
    actual_url = str(route.calls[0].request.url)
    # Encoded form must be present; raw slash/question-mark must not be in the query value
    assert ENCODED_ITEM_KEY in actual_url, (
        f"Expected percent-encoded key '{ENCODED_ITEM_KEY}' in BBT URL, got: {actual_url}"
    )
