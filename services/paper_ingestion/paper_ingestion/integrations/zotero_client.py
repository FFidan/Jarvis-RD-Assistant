"""Zotero Web API client — stateless async wrapper."""

from __future__ import annotations

import logging
import os

import httpx

ZOTERO_API_BASE = "https://api.zotero.org"
BBT_BASE_URL = os.getenv("BBT_BASE_URL", "http://host.docker.internal:23119")
BBT_LOCAL_BASE = f"{BBT_BASE_URL}/better-bibtex"

logger = logging.getLogger(__name__)


class ZoteroClient:
    def __init__(
        self,
        api_key: str,
        user_id: str,
        library_type: str = "user",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.user_id = user_id
        self.library_type = library_type  # "user" or "group"
        self._http = http_client or httpx.AsyncClient()
        self._base = f"{ZOTERO_API_BASE}/{library_type}s/{user_id}"

    def _headers(self) -> dict[str, str]:
        return {"Zotero-API-Key": self.api_key, "Zotero-API-Version": "3"}

    async def create_item(self, item_data: dict) -> dict:
        """POST /items — create a new Zotero item. Returns the created item."""
        resp = await self._http.post(
            f"{self._base}/items",
            json=[item_data],
            headers=self._headers(),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def upload_attachment(self, item_key: str, pdf_path: str) -> None:
        """Attach a PDF file to a Zotero item (best-effort, does not raise on failure).

        Zotero attachment upload is complex (3-stage); simplified version
        just logs intent — full impl can use linkMode "imported_file".
        """
        logger.info(
            "Zotero attachment upload not yet implemented for item %s path %s",
            item_key,
            pdf_path,
        )

    async def search_by_doi(self, doi: str) -> dict | None:
        """GET /items?q=doi — find existing item by DOI. Returns first match or None."""
        resp = await self._http.get(
            f"{self._base}/items",
            params={"q": doi, "qmode": "everything"},
            headers=self._headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        items = resp.json()
        return items[0] if items else None

    async def ensure_collection(self, name: str, parent_key: str | None = None) -> str:
        """Find or create a collection by name. Returns collection key.

        Paginates through all collections (100 per page) so users with more than
        100 collections are handled correctly.
        """
        all_collections: list[dict] = []
        start = 0
        while True:
            resp = await self._http.get(
                f"{self._base}/collections",
                params={"start": start, "limit": 100},
                headers=self._headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            items = resp.json()
            all_collections.extend(items)
            total = int(resp.headers.get("Total-Results", "0"))
            if len(items) < 100 or len(all_collections) >= total:
                break
            start += 100

        # Deduplicate by key (last write wins for duplicates, which shouldn't occur)
        seen: dict[str, dict] = {}
        for col in all_collections:
            seen[col["key"]] = col
        all_collections = list(seen.values())

        for col in all_collections:
            if col["data"]["name"] == name:
                return col["key"]

        # Create new collection
        payload = [{"name": name, "parentCollection": parent_key or False}]
        resp = await self._http.post(
            f"{self._base}/collections",
            json=payload,
            headers=self._headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()["successful"]["0"]["key"]

    async def fetch_bbt_citation_key(self, item_key: str) -> str | None:
        """Try to get Better BibTeX citation key from local BBT plugin.

        Returns None if unavailable (BBT not installed or not running).
        """
        try:
            resp = await self._http.get(
                f"{BBT_LOCAL_BASE}/export/item?itemKey={item_key}&translator=csljson",
                timeout=3.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data[0].get("id") if data else None
        except Exception:  # noqa: BLE001
            pass
        return None

    async def fetch_items_since(self, version: int) -> tuple[list[dict], int]:
        """GET /items?since={version} — fetch items modified after the given library version.

        Returns a tuple of (items, new_library_version). The new version is read from
        the ``Zotero-Last-Modified-Version`` response header.
        """
        resp = await self._http.get(
            f"{self._base}/items",
            params={"since": version, "format": "json", "limit": 100},
            headers=self._headers(),
            timeout=30.0,
        )
        resp.raise_for_status()
        new_version = int(resp.headers.get("Zotero-Last-Modified-Version", version))
        return resp.json(), new_version

    async def get_item_children(
        self,
        item_key: str,
        *,
        item_type: str = "annotation",
    ) -> list[dict]:
        """Fetch child items for a Zotero item, paginating through all results.

        Parameters
        ----------
        item_key:
            Zotero item key whose children should be fetched.
        item_type:
            Zotero child ``itemType`` filter. Defaults to ``annotation`` for
            imported PDF highlights/comments.
        """
        all_items: list[dict] = []
        start = 0
        while True:
            resp = await self._http.get(
                f"{self._base}/items/{item_key}/children",
                params={
                    "itemType": item_type,
                    "format": "json",
                    "start": start,
                    "limit": 100,
                },
                headers=self._headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            items = resp.json()
            all_items.extend(items)
            total = int(resp.headers.get("Total-Results", "0"))
            if len(items) < 100 or len(all_items) >= total:
                break
            start += 100
        return all_items

    async def test_connection(self) -> bool:
        """Verify API key and user ID are valid. Returns True on success."""
        try:
            resp = await self._http.get(
                f"{self._base}/items?limit=1",
                headers=self._headers(),
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False
