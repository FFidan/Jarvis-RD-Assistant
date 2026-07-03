"""Zotero Web API client — stateless async wrapper."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import urllib.parse
from typing import Literal
from urllib.parse import urlparse

import httpx
from jarvis_common.net import parse_retry_after

from paper_ingestion.config import get_paper_ingestion_settings

ZOTERO_API_BASE = "https://api.zotero.org"

logger = logging.getLogger(__name__)


def __getattr__(name: str) -> str:
    """Lazy module attributes — resolved on first access, not at import time."""
    if name == "BBT_LOCAL_BASE":
        return f"{get_paper_ingestion_settings().bbt_base_url}/better-bibtex"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Hostnames that are intentionally private/docker-internal and explicitly allowed.
_BBT_ALLOWED_PRIVATE_HOSTS: frozenset[str] = frozenset({"host.docker.internal"})

# Maximum wait for a Retry-After header before giving up. Zotero rarely returns
# values above 60s; capping protects against a malicious or buggy upstream
# returning "Retry-After: 86400" which would block the worker for a day.
_MAX_RETRY_AFTER_SECONDS = 60.0

# Defensive cap on the number of items fetched by any paginator.  A Zotero
# library with more than 10 000 items in a single list is almost certainly a
# Zotero bug or a runaway loop rather than legitimate data.
_MAX_ZOTERO_PAGES_ITEMS = 10_000


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header value (delta-seconds OR HTTP-date).

    Delegates to :func:`jarvis_common.net.parse_retry_after`, capping at the
    Zotero-specific ``_MAX_RETRY_AFTER_SECONDS`` (60 s) and rejecting negative
    delta values (``negative_as_none=True``).  Returns the delay in seconds, or
    ``None`` when the header is absent / unparseable / negative.
    """
    parsed = parse_retry_after(
        value,
        max_seconds=int(_MAX_RETRY_AFTER_SECONDS),
        negative_as_none=True,
    )
    return None if parsed is None else float(parsed)


async def _zotero_request_with_retry(
    method: str,
    http: httpx.AsyncClient,
    url: str,
    **kwargs,
) -> httpx.Response:
    """Issue a Zotero request, honouring 429 Retry-After once.

    On HTTP 429 the server's ``Retry-After`` header (delta-seconds or
    HTTP-date) is parsed via :func:`_parse_retry_after` and the request is
    retried exactly once. The single retry mirrors the conservative
    source-plugin retry posture and bounds tail latency under sustained
    throttling.

    The caller still owns ``raise_for_status()`` — this helper only retries;
    it does not promote 429 into an exception.
    """
    resp = await http.request(method, url, **kwargs)
    if resp.status_code != 429:
        return resp
    delay = _parse_retry_after(resp.headers.get("Retry-After"))
    if delay is None:
        # No usable Retry-After — return the 429 unchanged so the caller's
        # raise_for_status() surfaces the rate limit. We don't blind-retry
        # because Zotero's rate-limit window is typically minutes-long.
        return resp
    logger.info(
        "Zotero 429 on %s — sleeping %.1fs before retry",
        method,
        delay,
    )
    await asyncio.sleep(delay)
    return await http.request(method, url, **kwargs)


def validate_bbt_base_url(url: str | None = None) -> None:
    """Validate BBT_BASE_URL at startup to block unsafe schemes or private IPs.

    Raises ``ValueError`` if the URL has an unsupported scheme (not http/https)
    or if the hostname resolves to a private/loopback IP address and is not in
    the explicit allow-list (``_BBT_ALLOWED_PRIVATE_HOSTS``).

    ``host.docker.internal`` is intentionally allowed because it is the
    standard Docker-Desktop hostname for reaching the host machine from inside
    a container — it is not a general SSRF vector in a controlled Docker env.

    Parameters
    ----------
    url:
        The BBT base URL to validate. Defaults to ``bbt_base_url`` from
        settings (resolved lazily at call time, not at import time).

    Raises
    ------
    ValueError
        If the URL scheme is unsupported or the host is a private IP not in
        the explicit allowlist.
    """
    if url is None:
        url = get_paper_ingestion_settings().bbt_base_url
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"BBT_BASE_URL has unsupported scheme {scheme!r}; expected 'http' or 'https'. "
            f"Got: {url!r}"
        )

    hostname = parsed.hostname or ""

    # Explicitly allow-listed docker hostnames are safe to skip IP checks.
    if hostname in _BBT_ALLOWED_PRIVATE_HOSTS:
        return

    # Block private / loopback IP addresses (SSRF guard).
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            raise ValueError(
                f"BBT_BASE_URL hostname {hostname!r} resolves to a private/loopback address "
                f"which is not explicitly allowed. Add it to _BBT_ALLOWED_PRIVATE_HOSTS if "
                f"it is intentional. Got: {url!r}"
            )
    except ValueError as exc:
        # ip_address() raises ValueError for non-IP hostnames (e.g. "example.com") —
        # those are fine; re-raise only if we set the message ourselves (SSRF block).
        if "BBT_BASE_URL" in str(exc):
            raise


def _read_pdf_for_upload(pdf_path: str) -> tuple[bytes, str, int, int]:
    """Read PDF bytes + compute ``(bytes, md5_hex, filesize, mtime_ms)``.

    Runs off the event loop (see :meth:`ZoteroClient.upload_attachment`) because
    reading + MD5-hashing a multi-MB PDF is blocking. ``mtime`` is returned in
    **milliseconds**, the unit the Zotero upload-authorize request requires.
    The MD5 is a Zotero content fingerprint, not a security primitive.
    """
    st = os.stat(pdf_path)
    with open(pdf_path, "rb") as fh:
        file_bytes = fh.read()
    md5_hex = hashlib.md5(file_bytes, usedforsecurity=False).hexdigest()
    return file_bytes, md5_hex, st.st_size, int(st.st_mtime * 1000)


class ZoteroClient:
    def __init__(
        self,
        api_key: str,
        user_id: str,
        library_type: Literal["user", "group"] = "user",
        group_id: int | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a Zotero API client.

        Parameters
        ----------
        api_key:
            Zotero Web API key with read/write library access.
        user_id:
            Numeric Zotero user ID (from zotero.org/settings/keys).
        library_type:
            ``"user"`` for a personal library (default) or ``"group"`` for a
            group library.  When ``"group"``, ``group_id`` must be provided.
        group_id:
            Numeric Zotero group ID.  Required when ``library_type="group"``;
            must be a positive integer.  Ignored when ``library_type="user"``.
        http_client:
            Optional shared ``httpx.AsyncClient`` to reuse across calls.
        """
        if library_type not in ("user", "group"):
            raise ValueError(f"library_type must be 'user' or 'group', got {library_type!r}")
        if library_type == "group":
            if group_id is None:
                raise ValueError("group_id is required when library_type='group'")
            if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id <= 0:
                raise ValueError(f"group_id must be a positive integer, got {group_id!r}")

        self.api_key = api_key
        self.user_id = user_id
        self.library_type: Literal["user", "group"] = library_type
        self.group_id = group_id
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        # URL structure:
        #   personal library → https://api.zotero.org/users/{user_id}/...
        #   group library    → https://api.zotero.org/groups/{group_id}/...
        if library_type == "group":
            self._base = f"{ZOTERO_API_BASE}/groups/{group_id}"
        else:
            self._base = f"{ZOTERO_API_BASE}/users/{user_id}"

    def _headers(self) -> dict[str, str]:
        return {"Zotero-API-Key": self.api_key, "Zotero-API-Version": "3"}

    async def create_item(self, item_data: dict) -> dict:
        """POST /items — create a new Zotero item. Returns the created item."""
        resp = await _zotero_request_with_retry(
            "POST",
            self._http,
            f"{self._base}/items",
            json=[item_data],
            headers=self._headers(),
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def add_item_to_collections(self, item_key: str, collection_keys: list[str]) -> None:
        """Add an existing item to *collection_keys* (idempotent set-union merge).

        GETs the item for its current version + collections, unions in the new
        keys, and PATCHes only when the set grows. The If-Unmodified-Since-Version
        precondition makes the write a compare-and-swap (412 on a concurrent edit).
        """
        if not collection_keys:
            return
        encoded = urllib.parse.quote(item_key, safe="")
        resp = await _zotero_request_with_retry(
            "GET",
            self._http,
            f"{self._base}/items/{encoded}",
            headers=self._headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        item = resp.json() or {}
        version = int(item.get("version", 0))
        existing = list((item.get("data") or {}).get("collections") or [])
        merged = sorted(set(existing) | set(collection_keys))
        if merged == sorted(existing):
            return
        patch = await _zotero_request_with_retry(
            "PATCH",
            self._http,
            f"{self._base}/items/{encoded}",
            json={"collections": merged},
            headers={**self._headers(), "If-Unmodified-Since-Version": str(version)},
            timeout=30.0,
        )
        patch.raise_for_status()

    async def upload_attachment(self, item_key: str, pdf_path: str) -> None:
        """Upload a PDF file to a Zotero attachment item via the 3-stage protocol.

        ``item_key`` must already exist as an ``imported_file`` attachment item.

        1. **Authorize** — ``POST {base}/items/{KEY}/file`` (form-urlencoded,
           ``If-None-Match: *`` for a new file) carrying ``md5``/``filename``/
           ``filesize``/``mtime`` (milliseconds)/``params=1``. The 200 response is
           either ``{"exists": 1}`` (identical file already stored → done) or the
           upload parameters ``{url, contentType, prefix, suffix, uploadKey}``.
        2. **Upload bytes** — ``POST`` the concatenation ``prefix + bytes + suffix``
           to the Stage-1 ``url`` (S3, not api.zotero.org). This request carries the
           Stage-1 ``contentType`` and **no Zotero auth/version headers**, and
           bypasses the Zotero-host 429 retry wrapper (which is host-specific).
        3. **Register** — ``POST {base}/items/{KEY}/file`` with ``upload=<uploadKey>``
           and the same precondition; success is ``204 No Content``.

        Returns ``None`` on success (including the ``{"exists": 1}`` short-circuit).
        Raises ``httpx.HTTPStatusError`` on any Zotero-host failure (412/413/428/429);
        the caller maps these to a status (413 == storage quota exceeded).
        """
        file_bytes, md5_hex, filesize, mtime_ms = await asyncio.to_thread(
            _read_pdf_for_upload, pdf_path
        )
        filename = os.path.basename(pdf_path)
        file_url = f"{self._base}/items/{urllib.parse.quote(item_key, safe='')}/file"

        # Stage 1 — authorize. If-None-Match: * declares a brand-new file.
        authorize = await _zotero_request_with_retry(
            "POST",
            self._http,
            file_url,
            data={
                "md5": md5_hex,
                "filename": filename,
                "filesize": filesize,
                "mtime": mtime_ms,
                "params": 1,
            },
            headers={**self._headers(), "If-None-Match": "*"},
            timeout=30.0,
        )
        authorize.raise_for_status()
        auth = authorize.json()
        if auth.get("exists"):
            # Identical bytes already on the server — nothing to upload or register.
            return

        # Stage 2 — upload bytes to S3. No Zotero headers; plain client POST so the
        # Zotero-host 429 retry wrapper is bypassed. 2xx (201) == success.
        upload = await self._http.post(
            auth["url"],
            content=auth["prefix"].encode("utf-8") + file_bytes + auth["suffix"].encode("utf-8"),
            headers={"Content-Type": auth["contentType"]},
            timeout=300.0,
        )
        upload.raise_for_status()

        # Stage 3 — register the completed upload. 204 == success.
        register = await _zotero_request_with_retry(
            "POST",
            self._http,
            file_url,
            data={"upload": auth["uploadKey"]},
            headers={**self._headers(), "If-None-Match": "*"},
            timeout=30.0,
        )
        register.raise_for_status()

    async def search_by_doi(self, doi: str) -> dict | None:
        """GET /items?q=doi — find existing item by DOI. Returns first match or None."""
        resp = await _zotero_request_with_retry(
            "GET",
            self._http,
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
            resp = await _zotero_request_with_retry(
                "GET",
                self._http,
                f"{self._base}/collections",
                params={"start": start, "limit": 100},
                headers=self._headers(),
                timeout=15.0,
            )
            resp.raise_for_status()
            items = resp.json()
            all_collections.extend(items)
            if len(all_collections) > _MAX_ZOTERO_PAGES_ITEMS:
                raise RuntimeError(
                    f"Zotero collections paginator exceeded {_MAX_ZOTERO_PAGES_ITEMS} items; "
                    "this indicates a Zotero bug or a runaway loop."
                )
            total = int(resp.headers.get("Total-Results", "0"))
            if len(items) < 100 or len(all_collections) >= total:
                break
            start += 100

        # Deduplicate by key. Defensive ``.get()`` chain — Zotero responses
        # have, in rare cases, been observed to drop the ``key`` field on
        # malformed entries. Skip such rows rather than KeyError.
        seen: dict[str, dict] = {}
        for col in all_collections:
            key = col.get("key")
            if key is None:
                continue
            seen[key] = col
        all_collections = list(seen.values())

        for col in all_collections:
            data = col.get("data") or {}
            if data.get("name") == name:
                key = col.get("key")
                if key is not None:
                    return key

        # Create new collection
        payload = [{"name": name, "parentCollection": parent_key or False}]
        resp = await _zotero_request_with_retry(
            "POST",
            self._http,
            f"{self._base}/collections",
            json=payload,
            headers=self._headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json() or {}
        successful = body.get("successful") or {}
        # Zotero returns successful entries keyed by stringified index ("0", "1", ...).
        first_entry = successful.get("0") or {}
        new_key = first_entry.get("key")
        if not new_key:
            raise RuntimeError(
                f"Zotero create-collection response missing successful.0.key: {body!r}"
            )
        return new_key

    async def fetch_bbt_citation_key(self, item_key: str) -> str | None:
        """Try to get Better BibTeX citation key from local BBT plugin.

        Returns None if unavailable (BBT not installed or not running).
        """
        try:
            encoded_key = urllib.parse.quote(item_key, safe="")
            bbt_base = get_paper_ingestion_settings().bbt_base_url
            resp = await self._http.get(
                f"{bbt_base}/better-bibtex/export/item?itemKey={encoded_key}&translator=csljson",
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

        Paginates through all pages (100 per page) so large libraries are
        handled correctly.

        Returns a tuple of (items, new_library_version). The new version is
        read from the ``Zotero-Last-Modified-Version`` header of the LAST
        successful page (Zotero guarantees this header is stable for the full
        result set of a versioned request).
        """
        all_items: list[dict] = []
        new_version: int = version
        start = 0
        while True:
            resp = await _zotero_request_with_retry(
                "GET",
                self._http,
                f"{self._base}/items",
                params={"since": version, "format": "json", "limit": 100, "start": start},
                headers=self._headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            page = resp.json()
            new_version = int(resp.headers.get("Zotero-Last-Modified-Version", new_version))
            all_items.extend(page)
            if len(all_items) > _MAX_ZOTERO_PAGES_ITEMS:
                raise RuntimeError(
                    f"Zotero items paginator exceeded {_MAX_ZOTERO_PAGES_ITEMS} items; "
                    "this indicates a Zotero bug or a runaway loop."
                )
            total = int(resp.headers.get("Total-Results", "0"))
            if len(page) < 100 or len(all_items) >= total:
                break
            start += 100
        return all_items, new_version

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
            resp = await _zotero_request_with_retry(
                "GET",
                self._http,
                f"{self._base}/items/{urllib.parse.quote(item_key, safe='')}/children",
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
            if len(all_items) > _MAX_ZOTERO_PAGES_ITEMS:
                raise RuntimeError(
                    f"Zotero children paginator exceeded {_MAX_ZOTERO_PAGES_ITEMS} items; "
                    "this indicates a Zotero bug or a runaway loop."
                )
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
