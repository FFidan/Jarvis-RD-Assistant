"""Tests for zotero_service: push_paper_to_zotero and resync_paper_to_zotero.

Uses AsyncMock / MagicMock for db_pool and httpx.AsyncClient.
ZoteroClient methods are patched at the class level.
"""

from __future__ import annotations

import hashlib
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs

import asyncpg
import httpx
import respx
from paper_ingestion.integrations.zotero_service import (
    poll_zotero_library,
    push_highlight_to_zotero,
    push_highlights_for_paper,
    push_paper_to_zotero,
    resync_paper_to_zotero,
    sync_annotations_for_paper,
)

from tests.conftest import FakeRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Keep local: multi-acquire side_effect semantics (successive acquire() yields different conns) not covered by canonical make_pool_and_conn.
def _make_pool(*conn_returns):
    """Return a mock pool whose successive acquire().__aenter__ calls return conn_returns."""
    pool = MagicMock()

    def _acquire_cm(return_value):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=return_value)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    # Each call to pool.acquire() returns a fresh context manager.
    pool.acquire = MagicMock(side_effect=[_acquire_cm(rv) for rv in conn_returns])
    return pool


def _make_conn(**kwargs):
    """Build a mock asyncpg connection with configurable fetchrow/fetch/execute."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=kwargs.get("fetchrow"))
    conn.fetch = AsyncMock(return_value=kwargs.get("fetch", []))
    conn.execute = AsyncMock(return_value=None)
    # Support `async with conn.transaction():` — transaction() must return a sync
    # callable that itself returns an async context manager.
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=None)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    return conn


def _zotero_enabled_config_rows():
    """Simulate user_config rows for a usable Zotero config."""
    return [
        FakeRecord({"key": "zotero.api_key", "value": "test_api_key", "encrypted_value": None}),
        FakeRecord({"key": "zotero.user_id", "value": "123456", "encrypted_value": None}),
        FakeRecord({"key": "zotero.library_type", "value": "user", "encrypted_value": None}),
    ]


def _zotero_enabled_with_annotations_rows():
    rows = _zotero_enabled_config_rows()
    rows.append(
        FakeRecord(
            {"key": "zotero.sync_annotations_enabled", "value": True, "encrypted_value": None}
        )
    )
    return rows


def _zotero_disabled_config_rows():
    return []


def _paper_row(
    *,
    paper_id: int = 1,
    title: str = "Test Paper",
    doi: str | None = "10.1234/test",
    project_ids: list[int] | None = None,
    zotero_item_key: str | None = None,
):
    """Build a fake paper row as push_paper_to_zotero expects."""
    return FakeRecord(
        {
            "id": paper_id,
            "title": title,
            "authors": ["Alice Johnson", "Bob Smith"],
            "doi": doi,
            "url": "https://example.com/paper",
            "abstract": "An abstract.",
            "pdf_local_path": None,
            "zotero_item_key": zotero_item_key,
            "project_ids": project_ids,
        }
    )


def _project_row(*, project_id: int = 10, name: str = "AI Research", col_key: str | None = None):
    return FakeRecord(
        {
            "id": project_id,
            "name": name,
            "zotero_collection_key": col_key,
        }
    )


def _cm(conn):
    """Wrap a connection in an async context manager mock."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Test: not configured
# ---------------------------------------------------------------------------


async def test_push_paper_not_configured():
    """push_paper_to_zotero returns early when Zotero credentials are absent."""
    # _get_zotero_config now uses acquire() — config conn is first acquire().
    config_conn = _make_conn(fetch=_zotero_disabled_config_rows())
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_cm(config_conn))

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)
        # ZoteroClient should never be instantiated
        mock_client.assert_not_called()


async def test_push_paper_reads_zotero_config_for_owner_user():
    """Zotero workers must use the queued user's personal credentials."""
    pool = MagicMock()
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch(
        "paper_ingestion.integrations._zotero_push._get_zotero_config",
        AsyncMock(return_value={"enabled": False}),
    ) as get_config:
        await push_paper_to_zotero(
            paper_id=1,
            db_pool=pool,
            http_client=http,
            owner_user_id=42,
        )

    get_config.assert_awaited_once_with(pool, user_id=42)


async def test_push_paper_filters_project_collections_by_owner_user():
    """A shared canonical paper should only push the caller's project collections."""
    paper = _paper_row(project_ids=[10])
    owner_project = _project_row(project_id=10, name="Owner Project", col_key=None)
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())

    push_conn = AsyncMock()
    push_conn.fetchrow = AsyncMock(side_effect=[paper, owner_project])
    push_conn.fetch = AsyncMock(return_value=[])
    push_conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(push_conn)])
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.search_by_doi = AsyncMock(return_value=None)
        mock_zotero.ensure_collection = AsyncMock(return_value="OWNER")
        mock_zotero.create_item = AsyncMock(
            return_value={"successful": {"0": {"key": "ITEM42"}}, "unchanged": {}, "failed": {}}
        )
        mock_zotero.fetch_bbt_citation_key = AsyncMock(return_value=None)

        await push_paper_to_zotero(
            paper_id=1,
            db_pool=pool,
            http_client=http,
            owner_user_id=42,
        )

    paper_sql = push_conn.fetchrow.await_args_list[0].args[0]
    project_sql = push_conn.fetchrow.await_args_list[1].args[0]
    assert "projects owner_project" in paper_sql
    assert "owner_project.user_id IS NOT DISTINCT FROM $2" in paper_sql
    # Zotero linkage is now read per-user from paper_user_zotero_links ($3 = owner).
    assert "paper_user_zotero_links l" in paper_sql
    assert push_conn.fetchrow.await_args_list[0].args[1:] == (1, 42, 42)
    assert "user_id IS NOT DISTINCT FROM $2" in project_sql
    assert push_conn.fetchrow.await_args_list[1].args[1:] == (10, 42)
    mock_zotero.ensure_collection.assert_awaited_once_with("Owner Project")


async def test_poll_zotero_library_reads_config_for_polling_user():
    """Manual and scheduled polls must read the polling user's Zotero config."""
    pool = MagicMock()
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch(
        "paper_ingestion.integrations._zotero_poll._get_zotero_config",
        AsyncMock(return_value={"enabled": False}),
    ) as get_config:
        result = await poll_zotero_library(pool, http, polling_user_id=42)

    assert result["status"] == "disabled"
    get_config.assert_awaited_once_with(pool, user_id=42)


# ---------------------------------------------------------------------------
# Test: no project links
# ---------------------------------------------------------------------------


async def test_push_paper_no_project_links():
    """push_paper_to_zotero returns early when paper has no project links."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    paper = _paper_row(project_ids=None)  # NULL → empty list in service
    paper_conn = _make_conn(fetchrow=paper)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(paper_conn)])

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http, owner_user_id=7)
        mock_client.return_value.create_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: happy path (new item created)
# ---------------------------------------------------------------------------


async def test_push_paper_happy_path():
    """push_paper_to_zotero creates item, stores key, and attempts BBT key fetch.

    Push acquires a single connection for all sub-queries.
    Connection sequence:
      1. acquire() for _get_zotero_config fetch (config_conn)
      2. acquire() for entire push body (push_conn — handles paper fetch, topics,
         project fetch, project key update, paper key update, BBT key update)
    """
    paper = _paper_row(project_ids=[10])
    project = _project_row(col_key=None)  # no pre-existing collection key
    topic_rows: list = []

    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())

    # Single connection used for all push sub-queries. owner_user_id is None
    # (single-user mode), so the push resolves None -> the sole active user via
    # _resolve_zotero_user_id's conn.fetch (first .fetch), then fetches topics.
    push_conn = AsyncMock()
    push_conn.fetchrow = AsyncMock(side_effect=[paper, project])
    push_conn.fetch = AsyncMock(side_effect=[[FakeRecord({"id": 7})], topic_rows])
    push_conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(push_conn)])

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.search_by_doi = AsyncMock(return_value=None)
        mock_zotero.ensure_collection = AsyncMock(return_value="COLL1234")
        mock_zotero.create_item = AsyncMock(
            return_value={
                "successful": {"0": {"key": "ABCD1234"}},
                "unchanged": {},
                "failed": {},
            }
        )
        mock_zotero.fetch_bbt_citation_key = AsyncMock(return_value="Johnson2024test")

        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)

        mock_zotero.create_item.assert_called_once()
        mock_zotero.ensure_collection.assert_called_once_with("AI Research")
        sql = push_conn.fetchrow.await_args_list[0].args[0]
        assert "project_papers" in sql
        assert "paper_projects" not in sql

        # The item key is persisted into the per-user link table, keyed by the
        # resolved sole user (id=7) — never the global papers.zotero_* columns.
        link_upserts = [
            c for c in push_conn.execute.call_args_list if "paper_user_zotero_links" in str(c)
        ]
        assert link_upserts, "item key must upsert into paper_user_zotero_links"
        assert any("ABCD1234" in str(c) and 7 in c.args for c in link_upserts)


# ---------------------------------------------------------------------------
# Test: DOI deduplication — reuse existing Zotero item
# ---------------------------------------------------------------------------


async def test_push_paper_doi_dedupe():
    """push_paper_to_zotero reuses existing Zotero item found by DOI search.

    Push acquires a single connection for all sub-queries.
    Connection sequence:
      1. acquire() for _get_zotero_config fetch (config_conn)
      2. acquire() for entire push body (push_conn — handles paper fetch + key persist)
    """
    paper = _paper_row(project_ids=[10], doi="10.1234/test")

    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    push_conn = AsyncMock()
    push_conn.fetchrow = AsyncMock(return_value=paper)
    push_conn.fetch = AsyncMock(return_value=[])
    push_conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(push_conn)])

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.search_by_doi = AsyncMock(
            return_value={"key": "EXISTING_KEY", "data": {"DOI": "10.1234/test"}}
        )
        mock_zotero.create_item = AsyncMock()
        mock_zotero.fetch_bbt_citation_key = AsyncMock(return_value=None)

        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http, owner_user_id=7)

        # create_item must NOT be called when DOI match is found
        mock_zotero.create_item.assert_not_called()
        # The existing key should be persisted
        assert any("EXISTING_KEY" in str(c) for c in push_conn.execute.call_args_list)


async def test_push_paper_duplicate_link_does_not_abort_job():
    """A same-DOI sibling paper resolving to an already-linked Zotero item must NOT
    raise out of the push (job-safe).

    papers are deduped by external_id, not DOI, so two rows can share a DOI. When
    the DOI-dedup branch resolves a Zotero item the user already linked to the
    sibling row, the per-user item-key persist violates the partial unique index
    uq_pu_zotero_item(user_id, zotero_item_key) — which ON CONFLICT (paper_id,
    user_id) does NOT arbitrate. The push must swallow that as "already linked"
    rather than abort the unwrapped _zotero_push_job / _zotero_resync_job.

    Regression: removing the try/except asyncpg.UniqueViolationError around the
    item-key persist makes the violation propagate and this test fails.
    """
    paper = _paper_row(project_ids=[10], doi="10.1234/dup")

    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())

    def _raise_on_item_link(sql, *args):
        # Only the item-key link persist can hit uq_pu_zotero_item; emulate the
        # secondary-index violation that ON CONFLICT (paper_id, user_id) can't catch.
        if "INSERT INTO paper_user_zotero_links" in sql and "zotero_item_key" in sql:
            raise asyncpg.UniqueViolationError("uq_pu_zotero_item")
        return None

    push_conn = AsyncMock()
    push_conn.fetchrow = AsyncMock(return_value=paper)
    push_conn.fetch = AsyncMock(return_value=[])
    push_conn.execute = AsyncMock(side_effect=_raise_on_item_link)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(push_conn)])

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.search_by_doi = AsyncMock(
            return_value={"key": "DUPITEM", "data": {"DOI": "10.1234/dup"}}
        )
        mock_zotero.create_item = AsyncMock()
        mock_zotero.fetch_bbt_citation_key = AsyncMock(return_value=None)

        # Must NOT raise — the job runner survives the duplicate-link violation.
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http, owner_user_id=7)

        mock_zotero.create_item.assert_not_called()
        # The item-key persist was attempted (and swallowed, not propagated).
        assert any(
            "INSERT INTO paper_user_zotero_links" in str(c) and "zotero_item_key" in str(c)
            for c in push_conn.execute.call_args_list
        )


# ---------------------------------------------------------------------------
# Test: BBT fallback — push still succeeds when BBT returns None
# ---------------------------------------------------------------------------


async def test_push_paper_bbt_fallback():
    """push_paper_to_zotero succeeds even when BBT returns None (non-fatal).

    Push acquires a single connection for all sub-queries.
    Connection sequence:
      1. acquire() for _get_zotero_config fetch (config_conn)
      2. acquire() for entire push body (push_conn)
    """
    paper = _paper_row(project_ids=[10])
    project = _project_row(col_key="PRECOLL")
    topic_rows: list = []

    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    push_conn = AsyncMock()
    push_conn.fetchrow = AsyncMock(side_effect=[paper, project])
    push_conn.fetch = AsyncMock(return_value=topic_rows)
    push_conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(push_conn)])

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.search_by_doi = AsyncMock(return_value=None)
        mock_zotero.create_item = AsyncMock(
            return_value={
                "successful": {"0": {"key": "ABCD1234"}},
                "unchanged": {},
                "failed": {},
            }
        )
        mock_zotero.fetch_bbt_citation_key = AsyncMock(return_value=None)

        # Should not raise
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http, owner_user_id=7)

        mock_zotero.create_item.assert_called_once()
        # zotero_citation_key update should NOT be called when bbt_key is None
        # (service only calls it when bbt_key is truthy)
        # push_conn.execute should have been called for zotero_item_key update only
        assert push_conn.execute.called


# ---------------------------------------------------------------------------
# Test: resync delegates to push (clear happens inside the locked push body)
# ---------------------------------------------------------------------------


async def test_resync_delegates_force_repush():
    """resync delegates to push with force=True; the NULL-clear now happens inside
    the locked push body, not on a separate connection."""
    pool = MagicMock()
    http = AsyncMock(spec=httpx.AsyncClient)
    with patch(
        "paper_ingestion.integrations._zotero_push.push_paper_to_zotero",
        new=AsyncMock(),
    ) as mock_push:
        await resync_paper_to_zotero(paper_id=42, db_pool=pool, http_client=http, owner_user_id=5)
    mock_push.assert_awaited_once_with(42, pool, http, owner_user_id=5, force=True)


async def test_push_force_clears_key_under_advisory_lock():
    """force=True nulls the owner's link item_key (bypassing the already-pushed
    early-return) and runs the push body under a session advisory lock."""
    paper = _paper_row(project_ids=[10], zotero_item_key=None)
    project = _project_row(col_key="PRECOLL")
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    push_conn = AsyncMock()
    push_conn.fetchrow = AsyncMock(side_effect=[paper, project])
    push_conn.fetch = AsyncMock(
        return_value=[]
    )  # explicit owner 7 -> no resolve fetch; topics empty
    push_conn.execute = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(push_conn)])
    http = AsyncMock(spec=httpx.AsyncClient)
    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mz = mock_client.return_value
        mz.search_by_doi = AsyncMock(return_value=None)
        mz.create_item = AsyncMock(
            return_value={"successful": {"0": {"key": "NEWK"}}, "unchanged": {}, "failed": {}}
        )
        mz.fetch_bbt_citation_key = AsyncMock(return_value=None)
        await push_paper_to_zotero(
            paper_id=42, db_pool=pool, http_client=http, owner_user_id=7, force=True
        )
    executed = [str(c) for c in push_conn.execute.call_args_list]
    assert any(
        "paper_user_zotero_links" in s and "zotero_item_key = NULL" in s for s in executed
    ), executed
    assert any("pg_advisory_lock" in s for s in executed), executed
    assert any("pg_advisory_unlock" in s for s in executed), executed
    # single push connection preserved (config + push only).
    assert pool.acquire.call_count == 2


# ---------------------------------------------------------------------------
# Helpers for poll tests
# ---------------------------------------------------------------------------


def _zotero_poll_enabled_config_rows():
    """user_config rows for an enabled Zotero config with polling on."""
    return [
        FakeRecord({"key": "zotero.api_key", "value": "test_api_key", "encrypted_value": None}),
        FakeRecord({"key": "zotero.user_id", "value": "123456", "encrypted_value": None}),
        FakeRecord({"key": "zotero.library_type", "value": "user", "encrypted_value": None}),
        FakeRecord({"key": "zotero.poll_enabled", "value": True, "encrypted_value": None}),
        FakeRecord({"key": "zotero.last_library_version", "value": 0, "encrypted_value": None}),
    ]


def _zotero_item(
    *,
    key: str = "ITEM0001",
    title: str = "A New Paper",
    doi: str = "",
    extra: str = "",
    abstract: str = "Some abstract.",
    url: str = "https://example.com/paper",
    creators: list[dict] | None = None,
) -> dict:
    """Build a minimal Zotero item dict."""
    return {
        "key": key,
        "data": {
            "key": key,
            "itemType": "journalArticle",
            "title": title,
            "DOI": doi,
            "extra": extra,
            "abstractNote": abstract,
            "url": url,
            "creators": creators or [{"firstName": "Alice", "lastName": "Smith"}],
        },
    }


def _make_poll_pool(*conn_returns, config_rows=None):
    """Pool mock where config is fetched via acquire() and subsequent acquire() uses conn_returns.

    _get_zotero_config now uses acquire() — config conn is prepended automatically.
    """
    config_conn = _make_conn(fetch=config_rows or _zotero_poll_enabled_config_rows())

    def _cm_rv(rv):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=rv)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    pool = MagicMock()
    pool.acquire = MagicMock(
        side_effect=[_cm_rv(config_conn)] + [_cm_rv(rv) for rv in conn_returns]
    )
    return pool


# ---------------------------------------------------------------------------
# E4 Poll tests
# ---------------------------------------------------------------------------


async def test_poll_library_disabled():
    """Returns disabled status when Zotero credentials are absent."""
    config_conn = _make_conn(fetch=_zotero_disabled_config_rows())
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_cm(config_conn))
    http = AsyncMock(spec=httpx.AsyncClient)

    result = await poll_zotero_library(db_pool=pool, http_client=http)

    assert result["status"] == "disabled"


async def test_poll_library_poll_disabled():
    """Returns poll_disabled status when zotero.poll_enabled is false."""
    config_rows = [
        FakeRecord({"key": "zotero.api_key", "value": "key", "encrypted_value": None}),
        FakeRecord({"key": "zotero.user_id", "value": "123", "encrypted_value": None}),
        FakeRecord({"key": "zotero.poll_enabled", "value": False, "encrypted_value": None}),
    ]
    config_conn = _make_conn(fetch=config_rows)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_cm(config_conn))
    http = AsyncMock(spec=httpx.AsyncClient)

    result = await poll_zotero_library(db_pool=pool, http_client=http)

    assert result["status"] == "poll_disabled"


async def test_poll_library_skips_jarvis_origin():
    """Items with jarvis_paper_id= in Extra are skipped (not enqueued)."""
    jarvis_item = _zotero_item(key="JARVIS01", extra="jarvis_paper_id=42")
    # version conn: persist new version (version 5 != 0 → execute)
    version_conn = _make_conn()
    pool = _make_poll_pool(version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([jarvis_item], 5))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_defer = AsyncMock()
        mock_analyze_task.defer_async = mock_analyze_defer
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    # JARVIS-originated item must not be enqueued
    mock_analyze_defer.assert_not_awaited()
    assert result["status"] == "ok"
    assert result["new_items"] == 0
    assert result["enqueued"] == 0


async def test_poll_library_enqueues_new_items():
    """New items without jarvis origin are upserted and enqueued as paper.analyze jobs.

    poll now calls upsert_paper → enqueues paper.analyze with paper_id.
    """
    new_item = _zotero_item(key="NEWITEM1", title="New Paper", doi="")
    # No DOI → no DOI-lookup conn needed.
    # upsert conn: fetchrow returns the upserted paper row; execute for zotero_item_key update.
    upsert_conn = _make_conn(fetchrow=FakeRecord({"id": 99, "is_insert": True}))
    # version conn: persist new version (10 != 0)
    version_conn = _make_conn()
    pool = _make_poll_pool(upsert_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([new_item], 10))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_defer = AsyncMock()
        mock_analyze_task.defer_async = mock_analyze_defer
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    mock_analyze_defer.assert_awaited_once()
    assert mock_analyze_defer.await_args is not None
    call_kwargs = mock_analyze_defer.await_args.kwargs
    # Job deferred via paper_analyze task with paper_id kwarg
    assert call_kwargs["paper_id"] == 99
    assert call_kwargs["user_id"] is None
    assert "job_id" in call_kwargs
    assert result["enqueued"] == 1
    assert result["new_items"] == 1


async def test_poll_repoll_existing_paper_does_not_enqueue():
    """Re-polling an already-imported item (is_insert=False) must not enqueue or count.

    upsert_paper returns is_insert=False for a row that already existed.
    The enqueue and counter must be skipped so cap slots are not wasted on re-polls.

    Regression: without the ``if is_new_paper`` guard the analyze job would be
    deferred and enqueued==1, causing this test to fail.
    """
    existing_item = _zotero_item(key="EXIST001", title="Existing Paper", doi="")
    upsert_conn = _make_conn(fetchrow=FakeRecord({"id": 42, "is_insert": False}))
    version_conn = _make_conn()
    pool = _make_poll_pool(upsert_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([existing_item], 10))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_defer = AsyncMock()
        mock_analyze_task.defer_async = mock_analyze_defer
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    mock_analyze_defer.assert_not_awaited()
    assert result["enqueued"] == 0


async def test_poll_library_updates_version():
    """zotero.last_library_version updated in user_config after poll (user-scoped row).

    See test_poll_library_updates_version_for_null_user for the NULL-user path.
    """
    item = _zotero_item(key="VER0001", doi="")
    # upsert conn for the item
    upsert_conn = _make_conn(fetchrow=FakeRecord({"id": 55, "is_insert": True}))
    # version conn: persists new version
    version_conn = _make_conn()
    pool = _make_poll_pool(upsert_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        # Return a newer version (42) than the current (0)
        mock_client.fetch_items_since = AsyncMock(return_value=([item], 42))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_task.defer_async = AsyncMock()
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            # Pass a real user_id so the version upsert runs (polling_user_id guard).
            result = await poll_zotero_library(db_pool=pool, http_client=http, polling_user_id=7)

    # The version-persist connection should have had execute called
    version_conn.execute.assert_called_once()
    sql, version_arg = version_conn.execute.call_args[0][:2]
    assert "zotero.last_library_version" in sql
    assert "42" in str(version_arg)
    assert result["version_to"] == 42


async def test_poll_library_updates_version_for_null_user():
    """The cursor is persisted to the NULL-user config row when polling_user_id is None.

    Regression: the old code SKIPPED the upsert for polling_user_id=None, so the
    cursor stayed at 0 and the whole library was re-polled from scratch forever.
    The user_config unique index is NULLS NOT DISTINCT, so the NULL-user row
    upserts cleanly. The persisted user_id arg must be None.
    """
    item = _zotero_item(key="VERNULL1", doi="")
    upsert_conn = _make_conn(fetchrow=FakeRecord({"id": 56, "is_insert": True}))
    version_conn = _make_conn()
    pool = _make_poll_pool(upsert_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([item], 42))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_task.defer_async = AsyncMock()
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            result = await poll_zotero_library(db_pool=pool, http_client=http, polling_user_id=None)

    version_conn.execute.assert_called_once()
    version_arg, user_arg = version_conn.execute.call_args[0][1:3]
    assert version_arg == 42
    assert user_arg is None, f"NULL-user cursor must persist with user_id=None; got {user_arg!r}"
    assert result["version_to"] == 42


# ---------------------------------------------------------------------------
# Annotation sync
# ---------------------------------------------------------------------------


async def test_sync_annotations_for_paper_imports_zotero_highlights_idempotently():
    """Zotero annotation children are upserted into paper_notes by annotation key.

    Annotations are attributed to the *syncing* user (owner_user_id), not to
    paper["discovered_by"], and upserted on the 3-col index
    (paper_id, user_id, zotero_annotation_key).
    """
    config_conn = _make_conn(fetch=_zotero_enabled_with_annotations_rows())
    paper_conn = _make_conn(fetchrow=FakeRecord({"id": 7, "zotero_item_key": "ITEM1234"}))
    persist_conn = _make_conn()
    pool = _make_pool(config_conn, paper_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)
    annotations = [
        {
            "key": "ANN1",
            "data": {
                "annotationText": "Important highlighted claim",
                "annotationComment": "Worth citing",
                "annotationPageLabel": "5",
            },
        },
        {
            "key": "ANN2",
            "data": {
                "annotationText": "",
                "annotationComment": "Standalone comment",
                "annotationPageLabel": "appendix",
            },
        },
    ]

    syncing_user_id = 42  # explicit syncing user — must appear in $7 bind

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.get_item_children = AsyncMock(return_value=annotations)

        result = await sync_annotations_for_paper(
            paper_id=7,
            db_pool=pool,
            http_client=http,
            owner_user_id=syncing_user_id,
        )

    assert result == {"paper_id": 7, "imported": 2, "status": "ok"}
    assert persist_conn.execute.await_count == 2
    # Per-user attribution + ON CONFLICT shape are proven behaviorally by the live-PG
    # contract test (test_zotero_contract); here we assert the call args, not SQL text (TS-02).
    assert persist_conn.execute.await_args_list[0].args[1:6] == (
        7,
        "zotero",
        "ANN1",
        "Worth citing",
        "Important highlighted claim",
    )
    assert persist_conn.execute.await_args_list[1].args[5] is None
    # $7 (user_id) must be the syncing user, not the paper discoverer.
    assert persist_conn.execute.await_args_list[0].args[7] == syncing_user_id


async def test_sync_annotations_binds_resolved_owner_for_none_user():
    """With owner_user_id=None (single-user), the paper_notes INSERT binds the
    RESOLVED sole-user id ($7), not raw None — matching the link row the JOIN used."""
    config_conn = _make_conn(fetch=_zotero_enabled_with_annotations_rows())
    # paper_conn handles _resolve_zotero_user_id (fetch -> sole user 8) + paper fetchrow.
    paper_conn = _make_conn(
        fetchrow=FakeRecord({"id": 7, "zotero_item_key": "ITEM1"}),
        fetch=[FakeRecord({"id": 8})],
    )
    persist_conn = _make_conn()
    pool = _make_pool(config_conn, paper_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)
    annotations = [
        {
            "key": "ANN1",
            "data": {"annotationText": "x", "annotationComment": "c", "annotationPageLabel": "1"},
        }
    ]
    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_cls:
        mock_cls.return_value.get_item_children = AsyncMock(return_value=annotations)
        result = await sync_annotations_for_paper(
            paper_id=7, db_pool=pool, http_client=http, owner_user_id=None
        )
    assert result["status"] == "ok"
    assert persist_conn.execute.await_args_list[0].args[7] == 8


async def test_poll_doi_link_dispatches_annotations_with_resolved_owner():
    """The DOI-link branch dispatches zotero.sync_annotations with the RESOLVED owner,
    not raw polling_user_id (None in single-user mode)."""
    # doi_conn: resolve sole user (fetch -> id 8), then DOI fetchrow with NO item key.
    doi_conn = _make_conn(
        fetchrow=FakeRecord({"id": 90, "zotero_item_key": None, "discovered_by": 3}),
        fetch=[FakeRecord({"id": 8})],
    )
    link_conn = _make_conn()  # the link-insert acquire
    version_conn = _make_conn()
    pool = _make_poll_pool(doi_conn, link_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)
    doi_item = _zotero_item(key="DOIRES1", doi="10.5555/res")
    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client_cls.return_value.fetch_items_since = AsyncMock(return_value=([doi_item], 1))

        import jarvis_common.task_registry as task_registry

        mock_ann_task = MagicMock()
        mock_ann_defer = AsyncMock()
        mock_ann_task.defer_async = mock_ann_defer
        with patch.dict(task_registry._TASK_MAP, {"zotero.sync_annotations": mock_ann_task}):
            await poll_zotero_library(db_pool=pool, http_client=http, polling_user_id=None)

    mock_ann_defer.assert_awaited_once()
    assert mock_ann_defer.await_args.kwargs["user_id"] == 8
    assert mock_ann_defer.await_args.kwargs["paper_id"] == 90


# ---------------------------------------------------------------------------
# _get_zotero_config — decrypt roundtrip tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-sync enqueue cap
# ---------------------------------------------------------------------------


async def test_poll_zotero_library_caps_enqueue_at_max_per_sync():
    """poll_zotero_library enqueues at most MAX_ENQUEUE_PER_SYNC items per cycle.

    When the cap is hit the library-version cursor must NOT advance, so the
    next sync resumes from the same starting point and processes the next batch.
    """
    from paper_ingestion.integrations.zotero_service import MAX_ENQUEUE_PER_SYNC

    # 50 new items, none with DOI (each triggers one upsert acquire()).
    items = [_zotero_item(key=f"BULK{i:04d}", doi="") for i in range(50)]

    # Build enough upsert conns for the cap + some headroom (should only use 20).
    upsert_conns = [
        _make_conn(fetchrow=FakeRecord({"id": 1000 + i, "is_insert": True})) for i in range(25)
    ]

    pool = _make_poll_pool(*upsert_conns)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        # Library version advances to 999 on the Zotero side.
        mock_client.fetch_items_since = AsyncMock(return_value=(items, 999))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_defer = AsyncMock()
        mock_analyze_task.defer_async = mock_analyze_defer
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    # Exactly MAX_ENQUEUE_PER_SYNC jobs must have been enqueued.
    assert mock_analyze_defer.await_count == MAX_ENQUEUE_PER_SYNC, (
        f"Expected {MAX_ENQUEUE_PER_SYNC} enqueued jobs, got {mock_analyze_defer.await_count}"
    )
    assert result["enqueued"] == MAX_ENQUEUE_PER_SYNC

    # Version cursor must NOT have advanced — upsert_conns used for version persist.
    # When capped, new_version is reset to last_version (0), so the persist branch
    # is skipped: none of the upsert_conns should have had execute called with
    # 'zotero.last_library_version'.
    all_execute_calls = [call for conn in upsert_conns for call in conn.execute.call_args_list]
    version_persist_calls = [
        c for c in all_execute_calls if "zotero.last_library_version" in str(c)
    ]
    assert not version_persist_calls, (
        "Version cursor must not advance when enqueue cap is hit; "
        f"found version-persist calls: {version_persist_calls}"
    )

    # The result version_to must match the original last_version (0), not 999.
    assert result["version_to"] == 0, (
        f"version_to should remain at 0 when capped, got {result['version_to']}"
    )


async def test_poll_multi_sync_advances_cursor_without_reenqueue(monkeypatch):
    """25 items, MAX_ENQUEUE_PER_SYNC=20. Poll once → enqueue the first 20 and pin
    the cursor (capped). Poll again on the same (re-fetched) batch → the first 20
    are now is_insert=False and MUST NOT be re-enqueued; the poll MUST advance to
    items 21-25 and move the cursor forward. Guards against gating the enqueue on
    an analysis-completion marker (e.g. pdf_downloaded), which never flips for
    pdf-url-less Zotero papers and would re-enqueue every imported item forever."""
    from paper_ingestion.integrations import _zotero_poll

    items = [_zotero_item(key=f"ITEM{i:02d}", title=f"P{i}", doi="") for i in range(1, 26)]

    seen: dict[str, int] = {}
    counter = {"next": 1}

    async def _stateful_upsert(conn, paper_create, *, discovered_by=None):
        ext = paper_create.external_id
        if ext in seen:
            return FakeRecord({"id": seen[ext], "is_insert": False})
        pid = counter["next"]
        counter["next"] += 1
        seen[ext] = pid
        return FakeRecord({"id": pid, "is_insert": True})

    monkeypatch.setattr(_zotero_poll, "upsert_paper", _stateful_upsert)
    monkeypatch.setattr(_zotero_poll, "add_to_library", AsyncMock())
    monkeypatch.setattr(_zotero_poll, "_upsert_paper_user_state", AsyncMock())
    monkeypatch.setattr(_zotero_poll, "_resolve_zotero_user_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        _zotero_poll,
        "_migrate_unambiguous_legacy_identity",
        AsyncMock(),
    )

    # One uniform conn for every acquire: .fetch → config rows (for _get_zotero_config),
    # .execute → no-op (version persist). upsert/resolve/library/state are monkeypatched,
    # so the conn is a passthrough and a single reused object suffices for both polls.
    conn = _make_conn(fetch=_zotero_poll_enabled_config_rows())
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda *a, **k: _cm(conn))
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        # Cursor pinned on poll 1 (capped) → poll 2 re-fetches the identical batch.
        mock_client_cls.return_value.fetch_items_since = AsyncMock(return_value=(items, 99))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_defer = AsyncMock()
        mock_analyze_task.defer_async = mock_analyze_defer
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            first = await poll_zotero_library(db_pool=pool, http_client=http)
            first_ids = [c.kwargs["paper_id"] for c in mock_analyze_defer.await_args_list]

            mock_analyze_defer.reset_mock()
            second = await poll_zotero_library(db_pool=pool, http_client=http)
            second_ids = [c.kwargs["paper_id"] for c in mock_analyze_defer.await_args_list]

    # Poll 1: first 20 enqueued; cursor pinned (capped → version_to == version_from).
    assert first["enqueued"] == 20
    assert first_ids == list(range(1, 21))
    assert first["version_to"] == first["version_from"]
    # Poll 2: only the NEW tail (21-25) enqueued; none of 1-20 re-enqueued; cursor advances.
    assert second["enqueued"] == 5
    assert second_ids == list(range(21, 26))
    assert not (set(second_ids) & set(range(1, 21)))
    assert second["version_to"] == 99 and second["version_to"] > second["version_from"]


# ---------------------------------------------------------------------------
# ING-4: DOI-matched papers must be added to the polling user's library
# ---------------------------------------------------------------------------


async def test_doi_match_adds_paper_to_polling_users_library(monkeypatch):
    """A Zotero item whose DOI matches an existing corpus paper is added to the
    polling user's library and seeded to_read (not just item-key linked)."""
    from paper_ingestion.integrations import _zotero_poll

    add_library_calls: list[tuple] = []
    upsert_state_calls: list[tuple] = []

    async def _spy_add_to_library(conn, *, user_id, paper_id, added_via):
        add_library_calls.append((user_id, paper_id, added_via))

    async def _spy_upsert_state(conn, paper_id, user_id, *, state, starred, on_conflict):
        upsert_state_calls.append((paper_id, user_id, state, on_conflict))

    monkeypatch.setattr(_zotero_poll, "add_to_library", _spy_add_to_library)
    monkeypatch.setattr(_zotero_poll, "_upsert_paper_user_state", _spy_upsert_state)

    # DOI lookup conn: fetchrow returns a row with existing zotero_item_key (skips key-update branch).
    doi_conn = _make_conn(
        fetchrow=FakeRecord({"id": 77, "zotero_item_key": "EXISTKEY", "discovered_by": 5})
    )
    # Connection for add_to_library + _upsert_paper_user_state (monkeypatched — conn not used).
    library_conn = _make_conn()
    # Version-persist conn (new_version=1 != last_version=0 → execute runs).
    version_conn = _make_conn()

    pool = _make_poll_pool(doi_conn, library_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    doi_item = _zotero_item(key="DOIITEM1", doi="10.9999/zzz")

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([doi_item], 1))

        result = await poll_zotero_library(db_pool=pool, http_client=http, polling_user_id=42)

    assert result["status"] == "ok", result
    assert result["linked"] == 1
    assert add_library_calls == [(42, 77, "zotero_pull")], (
        f"add_to_library not called correctly: {add_library_calls}"
    )
    assert upsert_state_calls == [(77, 42, "to_read", "do_nothing")], (
        f"_upsert_paper_user_state not called correctly: {upsert_state_calls}"
    )


async def test_doi_match_with_malformed_url_links_without_validation(monkeypatch):
    """A DOI-linking item with a malformed url must link cleanly.

    The poll resolves the DOI link before projecting the item into a PaperCreate
    model, so a Zotero item carrying an unparseable url (or an over-long title)
    that simply matches a paper already in the library links without ever hitting
    url validation. Building PaperCreate up-front would raise here and abort the
    whole poll, pinning the cursor and wedging every subsequent sync.
    """
    from paper_ingestion.integrations import _zotero_poll

    async def _spy_add_to_library(conn, *, user_id, paper_id, added_via):
        pass

    async def _spy_upsert_state(conn, paper_id, user_id, *, state, starred, on_conflict):
        pass

    monkeypatch.setattr(_zotero_poll, "add_to_library", _spy_add_to_library)
    monkeypatch.setattr(_zotero_poll, "_upsert_paper_user_state", _spy_upsert_state)

    doi_conn = _make_conn(
        fetchrow=FakeRecord({"id": 91, "zotero_item_key": "EXISTING", "discovered_by": 5})
    )
    library_conn = _make_conn()
    version_conn = _make_conn()
    pool = _make_poll_pool(doi_conn, library_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    # url has no scheme → PaperCreate.validate_url would raise if constructed.
    bad_item = _zotero_item(key="BADURL01", doi="10.1234/match", url="www.no-scheme.example")

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([bad_item], 1))

        result = await poll_zotero_library(db_pool=pool, http_client=http, polling_user_id=42)

    assert result["status"] == "ok", result
    assert result["linked"] == 1, result


async def test_non_doi_malformed_item_does_not_stall_sync(monkeypatch):
    """A non-DOI item whose url fails PaperCreate validation must not escape the poll loop.

    Without a guard, _parse_zotero_item raises ValidationError which exits
    poll_zotero_library before the failed_keys / cursor-pin logic, permanently
    wedging every subsequent sync. With the guard, the bad item is skipped
    (cursor stays at last_version so the batch retries), while a valid item in
    the same batch is still passed to _ingest_new_item.
    """
    from paper_ingestion.integrations import _zotero_poll

    enqueued_keys: list[str] = []

    async def _spy_ingest(db_pool, paper_create, item_key, polling_user_id, namespace):
        enqueued_keys.append(item_key)
        return True

    monkeypatch.setattr(_zotero_poll, "_ingest_new_item", _spy_ingest)

    # Config conn only — _persist_poll_cursor is not called when cursor is pinned.
    pool = _make_poll_pool()
    http = AsyncMock(spec=httpx.AsyncClient)

    bad_item = _zotero_item(key="BADURL99", doi="", url="ftp://not-http")
    good_item = _zotero_item(key="GOODITEM", doi="", url="https://valid.example.com/paper")

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client_cls.return_value.fetch_items_since = AsyncMock(
            return_value=([bad_item, good_item], 10)
        )

        result = await poll_zotero_library(db_pool=pool, http_client=http, polling_user_id=42)

    assert result["status"] == "ok", result
    # Cursor must be pinned because the bad item landed in failed_keys.
    assert result["version_from"] == result["version_to"], (
        f"Cursor must not advance when a parse failure occurs: {result}"
    )
    # The valid item must still be enqueued despite the bad item.
    assert "GOODITEM" in enqueued_keys, f"Valid item was not enqueued: {enqueued_keys}"
    assert "BADURL99" not in enqueued_keys, f"Bad item must not reach ingest: {enqueued_keys}"


async def test_doi_match_no_polling_user_skips_library_link(monkeypatch):
    """When polling_user_id is None the DOI-match branch must not call add_to_library."""
    from paper_ingestion.integrations import _zotero_poll

    add_library_calls: list = []

    async def _spy_add_to_library(conn, *, user_id, paper_id, added_via):
        add_library_calls.append((user_id, paper_id))

    monkeypatch.setattr(_zotero_poll, "add_to_library", _spy_add_to_library)

    doi_conn = _make_conn(
        fetchrow=FakeRecord({"id": 55, "zotero_item_key": "KEY2", "discovered_by": 3})
    )
    pool = _make_poll_pool(doi_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    doi_item = _zotero_item(key="DOIITEM2", doi="10.8888/qqq")

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([doi_item], 0))

        result = await poll_zotero_library(db_pool=pool, http_client=http, polling_user_id=None)

    assert result["status"] == "ok", result
    assert add_library_calls == [], (
        f"add_to_library must not run when polling_user_id=None: {add_library_calls}"
    )


async def test_poll_new_paper_none_user_skips_state_seed(monkeypatch):
    """The new-paper upsert branch must not seed paper_user_state when polling_user_id
    is None — a NULL user_id state row is an orphan (mirrors the DOI branch guard)."""
    from paper_ingestion.integrations import _zotero_poll

    state_calls: list = []

    async def _spy_state(conn, paper_id, user_id, *, state, starred, on_conflict):
        state_calls.append((paper_id, user_id, on_conflict))

    monkeypatch.setattr(_zotero_poll, "_upsert_paper_user_state", _spy_state)

    item = _zotero_item(key="NOUSER1", doi="")
    upsert_conn = _make_conn(fetchrow=FakeRecord({"id": 12, "is_insert": True}))
    version_conn = _make_conn()
    pool = _make_poll_pool(upsert_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client_cls.return_value.fetch_items_since = AsyncMock(return_value=([item], 5))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_task.defer_async = AsyncMock()
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            await poll_zotero_library(db_pool=pool, http_client=http, polling_user_id=None)

    assert state_calls == [], f"state seed must be skipped for None user, got {state_calls}"


async def test_get_zotero_config_encrypted_api_key(monkeypatch):
    """_get_zotero_config decrypts encrypted_value when present."""
    from cryptography.fernet import Fernet
    from jarvis_common.crypto import encrypt_secret, refresh_fernet_cache
    from paper_ingestion.integrations.zotero_service import _get_zotero_config

    # Set up a temporary Fernet key for this test and bust the lru_cache.
    test_key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", test_key)
    refresh_fernet_cache()

    plaintext_key = "secret-zotero-key-abc123"
    ciphertext = encrypt_secret(plaintext_key)

    rows = [
        FakeRecord(
            {
                "key": "zotero.api_key",
                "value": None,
                "encrypted_value": ciphertext.encode("ascii"),
            }
        ),
        FakeRecord(
            {
                "key": "zotero.user_id",
                "value": "99999",
                "encrypted_value": None,
            }
        ),
        FakeRecord(
            {
                "key": "zotero.library_type",
                "value": "user",
                "encrypted_value": None,
            }
        ),
    ]

    conn = _make_conn(fetch=rows)
    pool = _make_pool(conn)

    config = await _get_zotero_config(pool)

    assert config["api_key"] == plaintext_key, "encrypted api_key must be decrypted"
    assert config["user_id"] == "99999", "plaintext fallback must be returned as-is"
    assert config["library_type"] == "user", "plaintext fallback must be returned as-is"

    # Restore cache so other tests are not affected.
    refresh_fernet_cache()


async def test_get_zotero_config_legacy_plaintext_fallback():
    """_get_zotero_config returns plaintext value when encrypted_value is NULL (legacy rows)."""
    from paper_ingestion.integrations.zotero_service import _get_zotero_config

    rows = [
        FakeRecord(
            {
                "key": "zotero.api_key",
                "value": "legacy-plaintext-key",
                "encrypted_value": None,
            }
        ),
        FakeRecord(
            {
                "key": "zotero.user_id",
                "value": "123456",
                "encrypted_value": None,
            }
        ),
        FakeRecord(
            {
                "key": "zotero.library_type",
                "value": "user",
                "encrypted_value": None,
            }
        ),
    ]

    conn = _make_conn(fetch=rows)
    pool = _make_pool(conn)

    config = await _get_zotero_config(pool)

    assert config["api_key"] == "legacy-plaintext-key"
    assert config["user_id"] == "123456"
    assert config["library_type"] == "user"


async def test_get_zotero_config_handles_memoryview_encrypted_value(monkeypatch):
    """asyncpg may return BYTEA columns as memoryview; _get_zotero_config must not AttributeError."""
    from cryptography.fernet import Fernet
    from jarvis_common.crypto import encrypt_secret, refresh_fernet_cache
    from paper_ingestion.integrations.zotero_service import _get_zotero_config

    test_key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", test_key)
    refresh_fernet_cache()

    ciphertext_bytes = encrypt_secret("my_api_key").encode("ascii")

    rows = [
        FakeRecord(
            {
                "key": "zotero.api_key",
                "value": None,
                "encrypted_value": memoryview(ciphertext_bytes),
            }
        ),
    ]

    conn = _make_conn(fetch=rows)
    pool = _make_pool(conn)

    config = await _get_zotero_config(pool)

    assert config.get("api_key") == "my_api_key"

    refresh_fernet_cache()


# ---------------------------------------------------------------------------
# sync_annotations_for_paper — transaction rollback on mid-loop failure
# ---------------------------------------------------------------------------


async def test_sync_annotations_rolls_back_on_mid_loop_failure():
    """If conn.execute raises mid-loop, the whole transaction rolls back.

    We simulate 5 annotations where the 3rd upsert raises RuntimeError. The
    transaction context manager should propagate the exception, rolling back all
    previous upserts. We verify:
      - The exception propagates out of sync_annotations_for_paper.
      - The transaction was entered (conn.transaction called once).
      - conn.execute was awaited exactly 3 times (annotations 1 & 2 succeed; 3rd raises).
    """
    import pytest

    config_conn = _make_conn(fetch=_zotero_enabled_with_annotations_rows())
    paper_conn = _make_conn(fetchrow=FakeRecord({"id": 5, "zotero_item_key": "ITEM9999"}))

    # persist_conn: execute succeeds twice then raises on the 3rd call.
    persist_conn = _make_conn()
    execute_side_effects = [None, None, RuntimeError("DB error on 3rd upsert"), None, None]
    persist_conn.execute = AsyncMock(side_effect=execute_side_effects)

    pool = _make_pool(config_conn, paper_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    annotations = [
        {
            "key": f"ANN{i}",
            "data": {
                "annotationText": f"Highlight {i}",
                "annotationComment": "",
                "annotationPageLabel": str(i),
            },
        }
        for i in range(1, 6)
    ]

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client_cls.return_value.get_item_children = AsyncMock(return_value=annotations)

        with pytest.raises(RuntimeError, match="DB error on 3rd upsert"):
            await sync_annotations_for_paper(paper_id=5, db_pool=pool, http_client=http)

    # Transaction was entered exactly once.
    persist_conn.transaction.assert_called_once()
    # execute was awaited 3 times: annotations 1 & 2 succeed, 3rd raises.
    assert persist_conn.execute.await_count == 3


# ---------------------------------------------------------------------------
# _get_zotero_config — decrypt failure returns {} and logs warning
# ---------------------------------------------------------------------------


async def test_get_zotero_config_raises_on_decrypt_failure(caplog):
    """If decrypt_secret raises, _get_zotero_config raises ZoteroConfigDecryptError and warns."""
    import logging

    import pytest

    from paper_ingestion.integrations.zotero_service import (
        ZoteroConfigDecryptError,
        _get_zotero_config,
    )

    # One encrypted row — decrypt will fail.
    rows = [
        FakeRecord(
            {
                "key": "zotero.api_key",
                "value": None,
                "encrypted_value": b"bad-ciphertext",
            }
        )
    ]

    conn = _make_conn(fetch=rows)
    pool = _make_pool(conn)

    # decrypt_secret is imported inside _get_zotero_config from jarvis_common.crypto.
    with patch("jarvis_common.crypto.decrypt_secret", side_effect=ValueError("bad token")):
        with caplog.at_level(logging.WARNING, logger="paper_ingestion.integrations.zotero_service"):
            with pytest.raises(ZoteroConfigDecryptError):
                await _get_zotero_config(pool)

    assert any("decrypt failed" in record.message for record in caplog.records), (
        f"Expected warning about decrypt failure; got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# H10: poll loop cursor protection on item failure
# ---------------------------------------------------------------------------


async def test_poll_does_not_advance_cursor_when_items_fail():
    """H10: if any item raises during upsert/enqueue, cursor stays at last_version.

    One item triggers an exception in the upsert path.  The library version
    returned by fetch_items_since (999) must NOT be persisted — version_to
    in the result should equal the original last_version (0).
    """
    bad_item = _zotero_item(key="BADITEM1", doi="")

    # Simulate a DB error on acquire for the upsert path.
    bad_cm = MagicMock()
    bad_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("DB exploded"))
    bad_cm.__aexit__ = AsyncMock(return_value=False)

    pool = _make_poll_pool(bad_cm)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([bad_item], 999))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_defer = AsyncMock()
        mock_analyze_task.defer_async = mock_analyze_defer
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    # Cursor must NOT have advanced — version_to should remain at last_version (0).
    assert result["version_to"] == 0, (
        f"Expected version_to=0 (cursor pinned), got {result['version_to']}"
    )
    assert result["status"] == "ok"
    # No jobs should have been enqueued for the failed item.
    mock_analyze_defer.assert_not_awaited()


# ---------------------------------------------------------------------------
# H12: _get_zotero_config decrypt warning must not log exc string
# ---------------------------------------------------------------------------


async def test_get_zotero_config_does_not_log_exc_string(caplog):
    """H12: decrypt warning uses %r short_key form, not exc — no ciphertext leakage.

    The original exc object (which may contain ciphertext fragments) must NOT
    appear in any warning log record.  The short_key repr must appear.
    """
    import logging

    import pytest

    from paper_ingestion.integrations.zotero_service import (
        ZoteroConfigDecryptError,
        _get_zotero_config,
    )

    rows = [
        FakeRecord(
            {
                "key": "zotero.api_key",
                "value": None,
                "encrypted_value": b"bad-ciphertext",
            }
        )
    ]

    conn = _make_conn(fetch=rows)
    pool = _make_pool(conn)

    exc_message = "token has incorrect padding or is corrupted — secret_fragment_xyz"

    with patch("jarvis_common.crypto.decrypt_secret", side_effect=ValueError(exc_message)):
        with caplog.at_level(logging.WARNING, logger="paper_ingestion.integrations.zotero_service"):
            with pytest.raises(ZoteroConfigDecryptError):
                await _get_zotero_config(pool)

    # The raw exc message string must NOT appear in any log record.
    for record in caplog.records:
        assert exc_message not in record.message, f"exc string leaked into log: {record.message!r}"

    # The short_key repr ('api_key') MUST appear in a warning record.
    assert any("api_key" in record.message for record in caplog.records), (
        f"Expected short_key in log warning; got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# push_paper_to_zotero — single connection acquisition
# ---------------------------------------------------------------------------


async def test_push_paper_to_zotero_acquires_single_connection():
    """push_paper_to_zotero acquires exactly one DB connection for the push body.

    The config connection (_get_zotero_config) is a separate acquire that is
    always present. The push body must use exactly one additional acquire.
    Total expected: 2 acquires (1 config + 1 push).
    """
    paper = _paper_row(project_ids=[10])
    project = _project_row(col_key="PRECOLL")

    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    push_conn = AsyncMock()
    push_conn.fetchrow = AsyncMock(side_effect=[paper, project])
    push_conn.fetch = AsyncMock(return_value=[])
    push_conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(push_conn)])

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.search_by_doi = AsyncMock(return_value=None)
        mock_zotero.create_item = AsyncMock(
            return_value={"successful": {"0": {"key": "SINGLECONN"}}, "unchanged": {}, "failed": {}}
        )
        mock_zotero.fetch_bbt_citation_key = AsyncMock(return_value=None)

        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http, owner_user_id=7)

    # Exactly 2 acquire() calls: config + push body.
    assert pool.acquire.call_count == 2, (
        f"Expected 2 pool.acquire() calls (config + push), got {pool.acquire.call_count}"
    )


async def test_push_already_pushed_syncs_new_project_collection():
    """An already-pushed paper newly linked to a project files the existing Zotero
    item into that project's collection instead of returning a no-op."""
    paper = _paper_row(project_ids=[10], zotero_item_key="EXISTINGKEY")
    project = _project_row(col_key="COLLNEW")
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    push_conn = AsyncMock()
    push_conn.fetchrow = AsyncMock(side_effect=[paper, project])
    push_conn.fetch = AsyncMock(return_value=[])  # explicit owner 7 -> no resolve fetch
    push_conn.execute = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(push_conn)])
    http = AsyncMock(spec=httpx.AsyncClient)
    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mz = mock_client.return_value
        mz.create_item = AsyncMock()
        mz.add_item_to_collections = AsyncMock()
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http, owner_user_id=7)
    mz.create_item.assert_not_called()
    mz.add_item_to_collections.assert_awaited_once_with("EXISTINGKEY", ["COLLNEW"])


# ---------------------------------------------------------------------------
# BE-11: SourceType.ZOTERO enum value + Zotero poll uses source_type="zotero"
# ---------------------------------------------------------------------------


def test_source_type_zotero_enum_value():
    """BE-11: SourceType.ZOTERO must exist and equal the string 'zotero'."""
    from paper_ingestion.models.papers import SourceType

    assert SourceType.ZOTERO == "zotero", (
        f"Expected SourceType.ZOTERO == 'zotero', got {SourceType.ZOTERO!r}"
    )
    # Must be a member of the enum (not just a loose string match).
    assert SourceType("zotero") is SourceType.ZOTERO


async def test_poll_zotero_library_sets_source_type_zotero():
    """BE-11: Zotero-imported papers must carry source_type='zotero', not 'local'.

    poll_zotero_library calls upsert_paper with a PaperCreate whose source_type
    must be SourceType.ZOTERO after the BE-11 fix.
    """
    from paper_ingestion.models.papers import SourceType

    new_item = _zotero_item(key="ZTITEM1", title="Zotero Paper", doi="")
    upsert_conn = _make_conn(fetchrow=FakeRecord({"id": 77}))
    version_conn = _make_conn()
    pool = _make_poll_pool(upsert_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    captured: list = []

    async def _fake_upsert(conn, paper_create, *, discovered_by=None):
        captured.append(paper_create)
        return {"id": 77, "is_insert": True}

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([new_item], 5))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_task.defer_async = AsyncMock()
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            with patch(
                "paper_ingestion.integrations._zotero_poll.upsert_paper",
                side_effect=_fake_upsert,
            ):
                result = await poll_zotero_library(db_pool=pool, http_client=http)

    assert result["status"] == "ok"
    assert len(captured) == 1, f"Expected exactly one upsert_paper call, got {len(captured)}"
    paper_create = captured[0]
    assert paper_create.source_type == SourceType.ZOTERO, (
        f"Expected source_type=SourceType.ZOTERO ('zotero'), got {paper_create.source_type!r}"
    )


# ---------------------------------------------------------------------------
# ZoteroConfigDecryptError — caller-site handling
# ---------------------------------------------------------------------------


async def test_push_paper_to_zotero_handles_decrypt_error_silently(caplog):
    """push_paper_to_zotero returns early and warns when config decryption fails."""
    import logging

    from paper_ingestion.integrations.zotero_service import ZoteroConfigDecryptError

    pool = MagicMock()
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch(
        "paper_ingestion.integrations._zotero_push._get_zotero_config",
        AsyncMock(side_effect=ZoteroConfigDecryptError("api_key")),
    ):
        with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
            with caplog.at_level(
                logging.WARNING, logger="paper_ingestion.integrations.zotero_service"
            ):
                result = await push_paper_to_zotero(paper_id=5, db_pool=pool, http_client=http)

    assert result is None
    assert any("decryption failed" in r.message for r in caplog.records)
    mock_client.assert_not_called()


async def test_sync_annotations_for_paper_handles_decrypt_error(caplog):
    """sync_annotations_for_paper returns config_decrypt_failed when config decryption fails."""
    import logging

    from paper_ingestion.integrations.zotero_service import ZoteroConfigDecryptError

    pool = MagicMock()
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch(
        "paper_ingestion.integrations._zotero_highlights._get_zotero_config",
        AsyncMock(side_effect=ZoteroConfigDecryptError("api_key")),
    ):
        with caplog.at_level(logging.WARNING, logger="paper_ingestion.integrations.zotero_service"):
            result = await sync_annotations_for_paper(paper_id=7, db_pool=pool, http_client=http)

    assert result == {"paper_id": 7, "imported": 0, "status": "config_decrypt_failed"}
    assert any("decryption failed" in r.message for r in caplog.records)


async def test_poll_zotero_library_handles_decrypt_error(caplog):
    """poll_zotero_library returns config_decrypt_failed when config decryption fails."""
    import logging

    from paper_ingestion.integrations.zotero_service import ZoteroConfigDecryptError

    pool = MagicMock()
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch(
        "paper_ingestion.integrations._zotero_poll._get_zotero_config",
        AsyncMock(side_effect=ZoteroConfigDecryptError("api_key")),
    ):
        with caplog.at_level(logging.WARNING, logger="paper_ingestion.integrations.zotero_service"):
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    assert result == {"status": "config_decrypt_failed"}
    assert any("decryption failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# non-critical decrypt failure → partial config returned, no raise
# critical decrypt failure (api_key) → ZoteroConfigDecryptError raised
# ---------------------------------------------------------------------------


async def test_get_zotero_config_non_critical_decrypt_failure_does_not_raise(caplog):
    """Decrypt failure on a non-critical key (last_library_version) must not raise.

    The function should log a warning, skip the failed key, and return the
    remaining config (api_key and user_id from plaintext rows).
    """
    import logging

    from paper_ingestion.integrations.zotero_service import _get_zotero_config

    rows = [
        FakeRecord(
            {
                "key": "zotero.api_key",
                "value": "plaintext-api-key",
                "encrypted_value": None,
            }
        ),
        FakeRecord(
            {
                "key": "zotero.user_id",
                "value": "55555",
                "encrypted_value": None,
            }
        ),
        # Non-critical key with a corrupted ciphertext.
        FakeRecord(
            {
                "key": "zotero.last_library_version",
                "value": None,
                "encrypted_value": b"bad-ciphertext-for-non-critical",
            }
        ),
    ]

    conn = _make_conn(fetch=rows)
    pool = _make_pool(conn)

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.integrations.zotero_service"):
        # Must NOT raise — non-critical key failure is tolerated.
        config = await _get_zotero_config(pool)

    # Partial config: plaintext keys must be present.
    assert config["api_key"] == "plaintext-api-key"
    assert config["user_id"] == "55555"
    # The failed non-critical key must be absent from the config.
    assert "last_library_version" not in config

    # A warning must be logged mentioning the non-critical key.
    assert any("last_library_version" in r.message for r in caplog.records), (
        f"Expected warning for last_library_version; got: {[r.message for r in caplog.records]}"
    )


async def test_get_zotero_config_critical_decrypt_failure_raises(caplog):
    """Decrypt failure on api_key (a critical key) must raise ZoteroConfigDecryptError.

    Also verifies that a WARNING log is emitted with the expected text about
    the api_key and operator responsibility to re-save in Settings.
    """
    import logging

    import pytest

    from paper_ingestion.integrations.zotero_service import (
        ZoteroConfigDecryptError,
        _get_zotero_config,
    )

    rows = [
        # Critical key with corrupted ciphertext.
        FakeRecord(
            {
                "key": "zotero.api_key",
                "value": None,
                "encrypted_value": b"bad-ciphertext-critical",
            }
        ),
        FakeRecord(
            {
                "key": "zotero.user_id",
                "value": "12345",
                "encrypted_value": None,
            }
        ),
    ]

    conn = _make_conn(fetch=rows)
    pool = _make_pool(conn)

    # Capture WARNING and higher severity logs
    caplog.set_level(logging.WARNING)

    with pytest.raises(ZoteroConfigDecryptError):
        await _get_zotero_config(pool)

    # Verify that a WARNING was logged with the expected content
    warning_records = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert any(
        "api_key" in rec.message and "operator must re-save" in rec.message
        for rec in warning_records
    ), (
        f"Expected WARNING log with 'api_key' and 'operator must re-save', "
        f"got: {[rec.message for rec in warning_records]}"
    )


# ---------------------------------------------------------------------------
# poll_zotero_library with polling_user_id=None must not upsert version
# ---------------------------------------------------------------------------


async def test_poll_library_persists_version_for_null_user_no_skip_log(caplog):
    """When polling_user_id is None, last_library_version IS persisted (NULL-user row).

    Previously the upsert was skipped for anonymous cron polls, pinning the cursor
    at 0 and re-polling the entire library every cycle. The NULLS NOT DISTINCT
    index makes the NULL-user upsert well-defined, so the cursor now advances.
    """
    import logging

    new_item = _zotero_item(key="NULLUSER1", doi="")
    upsert_conn = _make_conn(fetchrow=FakeRecord({"id": 101, "is_insert": True}))
    version_conn = _make_conn()

    # _make_poll_pool prepends a config conn; remaining conns go to poll body.
    pool = _make_poll_pool(upsert_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        # version 99 != 0 → triggers the upsert
        mock_client.fetch_items_since = AsyncMock(return_value=([new_item], 99))

        import jarvis_common.task_registry as task_registry

        mock_analyze_task = MagicMock()
        mock_analyze_task.defer_async = AsyncMock()
        with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_analyze_task}):
            with caplog.at_level(
                logging.INFO, logger="paper_ingestion.integrations.zotero_service"
            ):
                result = await poll_zotero_library(
                    db_pool=pool, http_client=http, polling_user_id=None
                )

    # The version_conn MUST have had execute called with last_library_version,
    # and the user_id arg must be None (the NULL-user row).
    version_persist_calls = [
        c for c in version_conn.execute.call_args_list if "zotero.last_library_version" in str(c)
    ]
    assert len(version_persist_calls) == 1, (
        "last_library_version must be upserted once for polling_user_id=None; "
        f"found: {version_persist_calls}"
    )
    assert version_persist_calls[0].args[2] is None, (
        f"NULL-user cursor must persist with user_id=None; got {version_persist_calls[0].args!r}"
    )

    # The old skip log must be gone.
    assert not any("skipping last_library_version" in r.message for r in caplog.records), (
        f"No skip log expected now that the NULL-user cursor persists; got: "
        f"{[r.message for r in caplog.records]}"
    )

    assert result["status"] == "ok"
    assert result["version_to"] == 99


# ---------------------------------------------------------------------------
# push_highlight_to_zotero — one-way spatial-highlight push
# ---------------------------------------------------------------------------


# The Section-3 worked-example stored rect: US-Letter page 3, denormalizes to
# Zotero rect [[72.0, 690.0, 300.0, 705.0]] against a (612, 792) page.
def _worked_rect() -> dict:
    coords = {"x0": 0.1176, "y0": 0.1098, "x1": 0.4902, "y1": 0.1287}
    return {"boundingRect": dict(coords), "rects": [dict(coords)]}


def _highlight_row(
    *,
    paper_id: int = 7,
    page: int = 3,
    note: str | None = "interesting",
    color: str | None = "#34D399",
    quote: str | None = "a quoted span",
    zotero_annotation_key: str | None = None,
    zotero_item_key: str | None = "ITEM1234",
    zotero_attachment_key: str | None = "ATTACH1",
    rect: dict | None = None,
):
    """Build the joined paper_highlights + paper_user_zotero_links row push_highlight expects.

    The ``zotero_item_key`` / ``zotero_attachment_key`` columns now come from the
    per-user link table (LEFT JOIN ``l``), not the global papers row — the record
    keys are identical so the mock models the joined result either way.

    Defaults ``zotero_attachment_key`` to a resolved key so the ensure-attachment
    step short-circuits (the attachment lifecycle has its own dedicated tests).
    """
    return FakeRecord(
        {
            "paper_id": paper_id,
            "page": page,
            "rect": rect if rect is not None else _worked_rect(),
            "note": note,
            "color": color,
            "quote": quote,
            "zotero_annotation_key": zotero_annotation_key,
            "zotero_item_key": zotero_item_key,
            "zotero_attachment_key": zotero_attachment_key,
        }
    )


# Page-size resolver patch target: a (612, 792) US-Letter page for page 3.
def _patch_page_sizes(sizes: dict[int, tuple[float, float]] | None = None):
    """Patch the off-disk page-size resolver with a fixed mapping."""
    return patch(
        "paper_ingestion.integrations._zotero_highlights._get_page_sizes",
        AsyncMock(return_value=sizes if sizes is not None else {3: (612.0, 792.0)}),
    )


async def test_push_highlight_parents_on_attachment_not_bibliographic():
    """The annotation parents on the PDF ATTACHMENT key, never the bibliographic item."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_attachment_key="ATTACH1"))
    persist_conn = _make_conn()
    pool = _make_pool(config_conn, load_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes(),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock(return_value={"successful": {"0": {"key": "ANN123"}}})

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "ok"
    assert result["zotero_annotation_key"] == "ANN123"

    mock_zotero.create_item.assert_awaited_once()
    item_data = mock_zotero.create_item.await_args.args[0]
    assert item_data["itemType"] == "annotation"
    # The central correction: parent is the ATTACHMENT key, NOT the bibliographic key.
    assert item_data["parentItem"] == "ATTACH1"
    assert item_data["parentItem"] != "ITEM1234"
    assert item_data["annotationType"] == "highlight"
    assert item_data["annotationText"] == "a quoted span"
    assert item_data["annotationComment"] == "interesting"
    assert item_data["annotationPageLabel"] == "3"
    assert item_data["annotationColor"] == "#34D399"

    # Load query is user-scoped (tenancy); key is persisted on the highlight row.
    load_sql = load_conn.fetchrow.await_args.args[0]
    assert "h.user_id = $2" in load_sql
    # Zotero linkage is read per-user from paper_user_zotero_links ($3 = resolved owner).
    assert "paper_user_zotero_links l" in load_sql
    assert load_conn.fetchrow.await_args.args[1:] == (55, 42, 42)
    assert any("ANN123" in str(c) for c in persist_conn.execute.call_args_list)


async def test_push_highlight_emits_denormalized_position_and_sort_index():
    """The annotation carries a string-encoded annotationPosition + annotationSortIndex.

    The stored Section-3 worked-example rect on a (612, 792) page must decode to
    ``{"pageIndex": 2, "rects": [[72.0, 690.0, 300.0, 705.0]]}``.
    """
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_attachment_key="ATTACH1"))
    persist_conn = _make_conn()
    pool = _make_pool(config_conn, load_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes({3: (612.0, 792.0)}),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock(return_value={"successful": {"0": {"key": "ANN123"}}})

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "ok"
    item_data = mock_zotero.create_item.await_args.args[0]

    # annotationPosition is a JSON STRING, not a native object.
    raw_position = item_data["annotationPosition"]
    assert isinstance(raw_position, str)
    position = json.loads(raw_position)
    assert position == {"pageIndex": 2, "rects": [[72.0, 690.0, 300.0, 705.0]]}

    # annotationSortIndex: zero-padded pageIndex|0|yTop, with the page-2 field.
    sort_index = item_data["annotationSortIndex"]
    assert re.fullmatch(r"\d{5}\|\d{6}\|\d{5}", sort_index)
    assert sort_index.split("|")[0] == "00002"
    # The third field is the bounding top edge in PDF points (705 from the rect above).
    assert sort_index.split("|")[2] == "00705"


async def test_push_highlight_default_color_when_unset():
    """A highlight with no color falls back to Zotero's default highlight color."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(
        fetchrow=_highlight_row(color=None, note=None, quote=None, zotero_attachment_key="ATTACH1")
    )
    persist_conn = _make_conn()
    pool = _make_pool(config_conn, load_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes(),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock(return_value={"successful": {"0": {"key": "ANN9"}}})

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "ok"
    item_data = mock_zotero.create_item.await_args.args[0]
    assert item_data["annotationColor"] == "#ffd400"
    assert item_data["annotationText"] == ""
    assert item_data["annotationComment"] == ""


async def test_push_highlight_already_synced_is_noop():
    """A highlight that already carries a Zotero key is a no-op (no create_item)."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_annotation_key="EXISTING"))
    pool = _make_pool(config_conn, load_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock()

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "already_synced"
    assert result["zotero_annotation_key"] == "EXISTING"
    mock_zotero.create_item.assert_not_called()


async def test_push_highlight_not_linked_is_noop():
    """Paper not in Zotero (zotero_item_key NULL) → not_linked no-op, no create_item."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_item_key=None))
    pool = _make_pool(config_conn, load_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock()

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "not_linked"
    mock_zotero.create_item.assert_not_called()


async def test_push_highlight_tenancy_scoped_to_owner():
    """A user cannot push another user's highlight — the load is scoped by user_id."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    # The user-scoped query returns no row for a highlight owned by someone else.
    load_conn = _make_conn(fetchrow=None)
    pool = _make_pool(config_conn, load_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock()

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=99
        )

    assert result["status"] == "not_found"
    mock_zotero.create_item.assert_not_called()
    load_sql = load_conn.fetchrow.await_args.args[0]
    assert "h.user_id = $2" in load_sql
    assert load_conn.fetchrow.await_args.args[1:] == (55, 99, 99)


async def test_push_highlight_unique_violation_treated_as_synced():
    """A concurrent double-push collides on the partial unique index → already_synced."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row())
    persist_conn = _make_conn()
    persist_conn.execute = AsyncMock(side_effect=asyncpg.UniqueViolationError("duplicate key"))
    pool = _make_pool(config_conn, load_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes(),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock(return_value={"successful": {"0": {"key": "ANN123"}}})

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "already_synced"
    assert result["zotero_annotation_key"] == "ANN123"


async def test_push_highlight_disabled_when_no_credentials():
    """Missing Zotero credentials → disabled no-op, never touches the highlights table."""
    config_conn = _make_conn(fetch=_zotero_disabled_config_rows())
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_cm(config_conn))
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "disabled"
    mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# Attachment lifecycle — find-or-create the parent PDF attachment
# ---------------------------------------------------------------------------


async def test_push_highlight_reuses_existing_pdf_attachment():
    """An existing imported_file PDF child is reused — no upload, parent is its key."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_attachment_key=None))
    persist_conn = _make_conn()
    pool = _make_pool(config_conn, load_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes(),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.get_item_children = AsyncMock(
            return_value=[
                {
                    "key": "ATTACHX",
                    # md5 present == the attachment holds a stored, openable file.
                    "data": {
                        "contentType": "application/pdf",
                        "linkMode": "imported_file",
                        "md5": "d41d8cd98f00b204e9800998ecf8427e",
                    },
                },
            ]
        )
        mock_zotero.upload_attachment = AsyncMock()
        mock_zotero.create_item = AsyncMock(return_value={"successful": {"0": {"key": "ANN1"}}})

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "ok"
    mock_zotero.get_item_children.assert_awaited_once()
    # Reuse path: the PDF bytes are NOT re-uploaded.
    mock_zotero.upload_attachment.assert_not_called()
    item_data = mock_zotero.create_item.await_args.args[0]
    assert item_data["parentItem"] == "ATTACHX"
    # The discovered key is persisted on the paper row.
    assert any("ATTACHX" in str(c) for c in load_conn.execute.call_args_list)


async def test_push_highlight_skips_fileless_orphan_attachment():
    """A PDF child with no stored file (no md5) is NOT reused — a failed prior

    upload can leave a fileless attachment item; parenting annotations to it
    would point the reader at an unopenable file. The export must fall through to
    re-create/upload instead, so the orphan key is never adopted.
    """
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_attachment_key=None))
    persist_conn = _make_conn()
    pool = _make_pool(config_conn, load_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes(),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.get_item_children = AsyncMock(
            return_value=[
                {
                    "key": "ORPHAN",  # imported_file PDF item but md5 absent => no file
                    "data": {"contentType": "application/pdf", "linkMode": "imported_file"},
                },
            ]
        )
        mock_zotero.upload_attachment = AsyncMock()
        mock_zotero.create_item = AsyncMock(return_value={"successful": {"0": {"key": "ANN1"}}})

        # No PDF on disk in this test, so the re-create path stops at pdf_unavailable —
        # the point is that the orphan was skipped rather than adopted (which would be "ok").
        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "pdf_unavailable"
    assert not any("ORPHAN" in str(c) for c in load_conn.execute.call_args_list)
    mock_zotero.create_item.assert_not_called()


async def test_push_highlight_creates_and_uploads_attachment_when_absent(tmp_path):
    """No persisted/found attachment → create an imported_file item + upload the PDF."""
    pdf_path = tmp_path / "7.pdf"
    pdf_path.write_bytes(b"%PDF-1.7 fake")
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_attachment_key=None))
    persist_conn = _make_conn()
    pool = _make_pool(config_conn, load_conn, persist_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        patch(
            "paper_ingestion.integrations._zotero_highlights._paper_pdf_path",
            return_value=pdf_path,
        ),
        _patch_page_sizes(),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.get_item_children = AsyncMock(return_value=[])  # no PDF child exists
        mock_zotero.upload_attachment = AsyncMock()
        mock_zotero.create_item = AsyncMock(
            side_effect=[
                {"successful": {"0": {"key": "ATTACHNEW"}}},  # attachment item create
                {"successful": {"0": {"key": "ANN1"}}},  # annotation create
            ]
        )

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "ok"
    # Attachment uploaded once, to the newly created attachment key.
    mock_zotero.upload_attachment.assert_awaited_once_with("ATTACHNEW", str(pdf_path))
    # The attachment-create body declares an annotatable imported_file PDF.
    attach_body = mock_zotero.create_item.await_args_list[0].args[0]
    assert attach_body["itemType"] == "attachment"
    assert attach_body["linkMode"] == "imported_file"
    assert attach_body["parentItem"] == "ITEM1234"
    # The annotation parents on the new attachment, not the bibliographic item.
    annotation_body = mock_zotero.create_item.await_args_list[1].args[0]
    assert annotation_body["parentItem"] == "ATTACHNEW"


async def test_push_highlight_quota_exceeded_maps_to_failure(tmp_path):
    """A 413 on upload (storage quota) surfaces as a quota_exceeded status, no raise."""
    pdf_path = tmp_path / "7.pdf"
    pdf_path.write_bytes(b"%PDF-1.7 fake")
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_attachment_key=None))
    pool = _make_pool(config_conn, load_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    req = httpx.Request("POST", "https://api.zotero.org/users/123456/items/ATTACHNEW/file")
    too_large = httpx.HTTPStatusError(
        "quota", request=req, response=httpx.Response(413, request=req)
    )

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        patch(
            "paper_ingestion.integrations._zotero_highlights._paper_pdf_path",
            return_value=pdf_path,
        ),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.get_item_children = AsyncMock(return_value=[])
        mock_zotero.create_item = AsyncMock(
            return_value={"successful": {"0": {"key": "ATTACHNEW"}}}
        )
        mock_zotero.upload_attachment = AsyncMock(side_effect=too_large)

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "quota_exceeded"


async def test_push_highlight_pdf_unavailable_when_page_sizes_missing():
    """An absent on-disk PDF (no page sizes) → pdf_unavailable, no annotation created."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_attachment_key="ATTACH1"))
    pool = _make_pool(config_conn, load_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes({}),  # PDF missing on disk
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock()

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "pdf_unavailable"
    mock_zotero.create_item.assert_not_called()


async def test_push_highlight_push_failed_when_no_key_returned():
    """A create response without successful.0.key maps to push_failed (no persist)."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    load_conn = _make_conn(fetchrow=_highlight_row(zotero_attachment_key="ATTACH1"))
    pool = _make_pool(config_conn, load_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes(),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock(return_value={"successful": {}, "failed": {"0": {}}})

        result = await push_highlight_to_zotero(
            highlight_id=55, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "push_failed"


# ---------------------------------------------------------------------------
# push_highlights_for_paper — per-paper batch export
# ---------------------------------------------------------------------------


def _paper_zotero_row(
    *,
    paper_id: int = 7,
    zotero_item_key: str | None = "ITEM1234",
    zotero_attachment_key: str | None = "ATTACH1",
):
    # Models the per-paper SELECT joined to paper_user_zotero_links (l.zotero_*);
    # the result column names are unchanged so the same keys model the link row.
    return FakeRecord(
        {
            "id": paper_id,
            "zotero_item_key": zotero_item_key,
            "zotero_attachment_key": zotero_attachment_key,
        }
    )


def _batch_highlight_rows(n: int = 2):
    return [
        FakeRecord(
            {
                "id": 100 + i,
                "page": 3,
                "rect": _worked_rect(),
                "note": f"n{i}",
                "color": "#34D399",
                "quote": f"q{i}",
            }
        )
        for i in range(n)
    ]


async def test_push_highlights_for_paper_exports_all_unsynced():
    """Two unsynced highlights → two annotations, each parented on the attachment key."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    paper_conn = _make_conn(fetchrow=_paper_zotero_row())
    highlights_conn = _make_conn(fetch=_batch_highlight_rows(2))
    ensure_conn = _make_conn()
    persist0 = _make_conn()
    persist1 = _make_conn()
    pool = _make_pool(config_conn, paper_conn, highlights_conn, ensure_conn, persist0, persist1)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes(),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock(
            side_effect=[
                {"successful": {"0": {"key": "ANN1"}}},
                {"successful": {"0": {"key": "ANN2"}}},
            ]
        )

        result = await push_highlights_for_paper(
            paper_id=7, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["exported"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == 0
    assert result["status"] == "ok"
    assert mock_zotero.create_item.await_count == 2
    for call in mock_zotero.create_item.await_args_list:
        assert call.args[0]["parentItem"] == "ATTACH1"
    # The unsynced-only filter is in the highlights query.
    hl_sql = highlights_conn.fetch.await_args.args[0]
    assert "zotero_annotation_key IS NULL" in hl_sql


async def test_push_highlights_for_paper_idempotent_when_all_synced():
    """No unsynced highlights → zero create_item calls, ok with exported=0."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    paper_conn = _make_conn(fetchrow=_paper_zotero_row())
    highlights_conn = _make_conn(fetch=[])  # everything already carries a key
    pool = _make_pool(config_conn, paper_conn, highlights_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock()

        result = await push_highlights_for_paper(
            paper_id=7, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "ok"
    assert result["exported"] == 0
    mock_zotero.create_item.assert_not_called()


async def test_push_highlights_for_paper_not_linked():
    """A paper with no zotero_item_key short-circuits to not_linked."""
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    paper_conn = _make_conn(fetchrow=_paper_zotero_row(zotero_item_key=None))
    pool = _make_pool(config_conn, paper_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock()

        result = await push_highlights_for_paper(
            paper_id=7, db_pool=pool, http_client=http, owner_user_id=42
        )

    assert result["status"] == "not_linked"
    mock_zotero.create_item.assert_not_called()


# ---------------------------------------------------------------------------
# ZoteroClient.upload_attachment — 3-stage upload, mocked at the transport
# ---------------------------------------------------------------------------


@respx.mock
async def test_upload_attachment_three_stage_transport_sequence(tmp_path):
    """Authorize → S3 PUT-bytes → register, with the documented header/body shape."""
    from paper_ingestion.integrations.zotero_client import ZOTERO_API_BASE, ZoteroClient

    pdf = tmp_path / "7.pdf"
    pdf_bytes = b"%PDF-1.7\nfake pdf body\n"
    pdf.write_bytes(pdf_bytes)

    base = f"{ZOTERO_API_BASE}/users/123456"
    file_url = f"{base}/items/ATTACH1/file"
    s3_url = "https://zotero-uploads.s3.example.com/put"

    def _file_endpoint(request):
        # Same URL serves Stage 1 (authorize) and Stage 3 (register) — distinguish by body.
        if b"upload=" in request.content:
            return httpx.Response(204)  # Stage 3 register success
        return httpx.Response(
            200,
            json={
                "url": s3_url,
                "contentType": "application/octet-stream",
                "prefix": "PFX",
                "suffix": "SFX",
                "uploadKey": "UPKEY42",
            },
        )

    file_route = respx.post(file_url).mock(side_effect=_file_endpoint)
    s3_route = respx.post(s3_url).mock(return_value=httpx.Response(201))

    client = ZoteroClient(api_key="test_key", user_id="123456", http_client=httpx.AsyncClient())
    await client.upload_attachment("ATTACH1", str(pdf))

    assert file_route.call_count == 2  # authorize + register
    assert s3_route.call_count == 1

    stage1 = file_route.calls[0].request
    stage3 = file_route.calls[1].request
    s3 = s3_route.calls[0].request

    assert stage1.headers["If-None-Match"] == "*"
    assert b"params=1" in stage1.content
    assert stage1.headers["Zotero-API-Key"] == "test_key"
    # Stage-1 fingerprints the bytes: md5 hex, byte length, and mtime in MS.
    stage1_body = parse_qs(stage1.content.decode())
    assert stage1_body["md5"][0] == hashlib.md5(pdf_bytes, usedforsecurity=False).hexdigest()
    assert int(stage1_body["filesize"][0]) == len(pdf_bytes)
    assert int(stage1_body["mtime"][0]) == int(pdf.stat().st_mtime * 1000)
    assert b"upload=UPKEY42" in stage3.content
    assert stage3.headers["If-None-Match"] == "*"

    # Stage 2 (S3) carries NO Zotero auth header; body is prefix + bytes + suffix.
    assert "Zotero-API-Key" not in s3.headers
    assert s3.content == b"PFX" + pdf_bytes + b"SFX"
    assert s3.headers["content-type"] == "application/octet-stream"


@respx.mock
async def test_upload_attachment_exists_short_circuits(tmp_path):
    """A Stage-1 {"exists": 1} response means the bytes are already stored — skip 2 & 3."""
    from paper_ingestion.integrations.zotero_client import ZOTERO_API_BASE, ZoteroClient

    pdf = tmp_path / "7.pdf"
    pdf.write_bytes(b"already-uploaded")

    base = f"{ZOTERO_API_BASE}/users/123456"
    file_url = f"{base}/items/ATTACH1/file"
    s3_url = "https://zotero-uploads.s3.example.com/put"

    file_route = respx.post(file_url).mock(return_value=httpx.Response(200, json={"exists": 1}))
    s3_route = respx.post(s3_url).mock(return_value=httpx.Response(201))

    client = ZoteroClient(api_key="test_key", user_id="123456", http_client=httpx.AsyncClient())
    await client.upload_attachment("ATTACH1", str(pdf))

    assert file_route.call_count == 1  # only authorize; no register
    assert not s3_route.called


# ---------------------------------------------------------------------------
# Highlight-export authz: safety invariant + view-level job re-validation
# ---------------------------------------------------------------------------


async def test_push_highlights_for_paper_scopes_export_to_owner_user():
    """No cross-user leak: the per-paper export binds the highlights load to the
    caller's user_id, so a co-owner's highlights on the same shared paper are
    never read or pushed.

    This is the invariant that makes view-level export authz safe: even when a
    public-source paper carries highlights from several users, an exporter only
    ever sees their own. The query parameters (paper_id, user_id) are asserted
    directly — the WHERE clause is keyed to ``user_id = $2`` so binding $2 to the
    caller (here user 99) excludes any other user's rows (e.g. user 42).
    """
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    paper_conn = _make_conn(fetchrow=_paper_zotero_row())
    # The DB returns only user 99's two unsynced highlights because the load is
    # scoped by user_id; a co-owner's rows on the same paper are filtered out.
    highlights_conn = _make_conn(fetch=_batch_highlight_rows(2))
    ensure_conn = _make_conn()
    persist0 = _make_conn()
    persist1 = _make_conn()
    pool = _make_pool(config_conn, paper_conn, highlights_conn, ensure_conn, persist0, persist1)
    http = AsyncMock(spec=httpx.AsyncClient)

    with (
        patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client,
        _patch_page_sizes(),
    ):
        mock_zotero = mock_client.return_value
        mock_zotero.create_item = AsyncMock(
            side_effect=[
                {"successful": {"0": {"key": "ANN1"}}},
                {"successful": {"0": {"key": "ANN2"}}},
            ]
        )

        result = await push_highlights_for_paper(
            paper_id=7, db_pool=pool, http_client=http, owner_user_id=99
        )

    assert result["exported"] == 2
    # Load-bearing: the highlights load is parameterized to the caller (user 99)
    # as positional $2. Another user's highlights (user_id != 99) cannot match.
    assert highlights_conn.fetch.await_args.args[1:] == (7, 99)
    assert mock_zotero.create_item.await_count == 2


async def test_push_highlights_job_allows_persisted_public_paper():
    """Job re-validation accepts a persisted-public paper.

    Highlight loading remains scoped to the caller, so public paper visibility
    cannot expose another user's annotations.
    """
    from paper_ingestion.integrations.zotero_service import _zotero_push_highlights_job

    visible_conn = _make_conn(
        fetchrow=FakeRecord({"source_type": "arxiv"})
    )
    pool = _make_pool(visible_conn)
    http = AsyncMock(spec=httpx.AsyncClient)
    ctx = AsyncMock()
    sentinel = {"paper_id": 7, "status": "ok", "exported": 0}

    with patch(
        "paper_ingestion.integrations._zotero_jobs.push_highlights_for_paper",
        AsyncMock(return_value=sentinel),
    ) as mock_push:
        result = await _zotero_push_highlights_job(pool, http, {"paper_id": 7, "user_id": 42}, ctx)

    assert result == sentinel
    mock_push.assert_awaited_once_with(7, pool, http, owner_user_id=42)


async def test_push_highlights_job_404s_foreign_private_paper():
    """Job authz parity (deny side): a private-origin (LOCAL) paper owned by another
    user and not in the caller's library is still rejected with 404 at job
    re-validation — view-level authz only widens access to the shared public
    corpus, never to private uploads."""
    import pytest
    from fastapi import HTTPException

    from paper_ingestion.integrations.zotero_service import _zotero_push_highlights_job

    private_conn = _make_conn(fetchrow=None)
    pool = _make_pool(private_conn)
    http = AsyncMock(spec=httpx.AsyncClient)
    ctx = AsyncMock()

    with patch(
        "paper_ingestion.integrations._zotero_jobs.push_highlights_for_paper",
        AsyncMock(),
    ) as mock_push:
        with pytest.raises(HTTPException) as exc_info:
            await _zotero_push_highlights_job(pool, http, {"paper_id": 7, "user_id": 42}, ctx)

    assert exc_info.value.status_code == 404
    mock_push.assert_not_awaited()


async def test_push_highlights_job_single_user_skips_visibility_check():
    """Single-user mode (user_id=None): the view-level visibility check is skipped
    (no-op parity with the prior assert_paper_ownership(None)), and the export still
    runs. Removing the ``if user_id is not None`` guard would run
    assert_paper_pdf_visible(conn, paper_id, None) and spuriously 404 the export."""
    from paper_ingestion.integrations.zotero_service import _zotero_push_highlights_job

    # No conn supplied: the visibility branch is skipped, so pool.acquire() must
    # never be called. A regression that drops the guard would acquire here.
    pool = _make_pool()
    http = AsyncMock(spec=httpx.AsyncClient)
    ctx = AsyncMock()
    sentinel = {"paper_id": 7, "status": "ok", "exported": 0}

    with (
        patch(
            "paper_ingestion.routers.pdfs.assert_paper_pdf_visible",
            AsyncMock(),
        ) as mock_visible,
        patch(
            "paper_ingestion.integrations._zotero_jobs.push_highlights_for_paper",
            AsyncMock(return_value=sentinel),
        ) as mock_push,
    ):
        result = await _zotero_push_highlights_job(
            pool, http, {"paper_id": 7, "user_id": None}, ctx
        )

    assert result == sentinel
    # Single-user mode: the visibility check is skipped entirely.
    mock_visible.assert_not_awaited()
    # The export still proceeds, scoped to owner_user_id=None.
    mock_push.assert_awaited_once_with(7, pool, http, owner_user_id=None)


async def test_zotero_push_job_revalidates_ownership_before_push():
    """zotero.push re-validates ownership at execution time, then delegates the push."""
    from paper_ingestion.integrations.zotero_service import _zotero_push_job

    conn = _make_conn()
    pool = _make_pool(conn)
    http = AsyncMock(spec=httpx.AsyncClient)
    ctx = AsyncMock()

    with (
        patch("jarvis_common.db_helpers.assert_paper_ownership", AsyncMock()) as mock_assert,
        patch(
            "paper_ingestion.integrations._zotero_jobs.push_paper_to_zotero",
            AsyncMock(),
        ) as mock_push,
    ):
        result = await _zotero_push_job(pool, http, {"paper_id": 7, "user_id": 42}, ctx)

    mock_assert.assert_awaited_once_with(conn, 7, 42)
    mock_push.assert_awaited_once_with(7, pool, http, owner_user_id=42)
    assert result == {"paper_id": 7, "status": "pushed"}


async def test_zotero_resync_job_revalidates_ownership_before_resync():
    """zotero.resync re-validates ownership at execution time, then delegates the resync."""
    from paper_ingestion.integrations.zotero_service import _zotero_resync_job

    conn = _make_conn()
    pool = _make_pool(conn)
    http = AsyncMock(spec=httpx.AsyncClient)
    ctx = AsyncMock()

    with (
        patch("jarvis_common.db_helpers.assert_paper_ownership", AsyncMock()) as mock_assert,
        patch(
            "paper_ingestion.integrations._zotero_jobs.resync_paper_to_zotero",
            AsyncMock(),
        ) as mock_resync,
    ):
        result = await _zotero_resync_job(pool, http, {"paper_id": 7, "user_id": 42}, ctx)

    mock_assert.assert_awaited_once_with(conn, 7, 42)
    mock_resync.assert_awaited_once_with(7, pool, http, owner_user_id=42)
    assert result == {"paper_id": 7, "status": "resynced"}


async def test_zotero_sync_annotations_job_revalidates_ownership_before_sync():
    """zotero.sync_annotations re-validates ownership at execution time, then delegates."""
    from paper_ingestion.integrations.zotero_service import _zotero_sync_annotations_job

    conn = _make_conn()
    pool = _make_pool(conn)
    http = AsyncMock(spec=httpx.AsyncClient)
    ctx = AsyncMock()
    sentinel = {"paper_id": 7, "status": "ok", "imported": 3}

    with (
        patch("jarvis_common.db_helpers.assert_paper_ownership", AsyncMock()) as mock_assert,
        patch(
            "paper_ingestion.integrations._zotero_jobs.sync_annotations_for_paper",
            AsyncMock(return_value=sentinel),
        ) as mock_sync,
    ):
        result = await _zotero_sync_annotations_job(pool, http, {"paper_id": 7, "user_id": 42}, ctx)

    mock_assert.assert_awaited_once_with(conn, 7, 42)
    mock_sync.assert_awaited_once_with(7, pool, http, owner_user_id=42)
    assert result == sentinel


def test_parse_zotero_item_builds_authors_url_fallback_and_skips_jarvis_origin():
    from paper_ingestion.integrations.zotero_service import (
        _parse_zotero_item,
        _ZoteroLibraryNamespace,
    )

    namespace = _ZoteroLibraryNamespace("user", "123456")

    # jarvis-origin skip uses data["extra"]
    assert (
        _parse_zotero_item(
            {"extra": "jarvis_paper_id=42", "key": "ABC"}, {}, namespace
        )
        is None
    )

    # key resolved from data first; fallback to outer_item["key"]
    parsed_with_data_key = _parse_zotero_item(
        {
            "key": "ITEMKEY",
            "title": "Attention Is All You Need",
            "DOI": "10.1/x",
            "creators": [{"firstName": "Ada", "lastName": "Lovelace"}, {"lastName": "Hopper"}],
        },
        {"key": "OUTER"},  # outer_item; data["key"] takes precedence
        namespace,
    )
    assert parsed_with_data_key is not None
    assert parsed_with_data_key.item_key == "ITEMKEY"
    assert parsed_with_data_key.authors == ["Ada Lovelace", "Hopper"]
    assert (
        parsed_with_data_key.url == "https://www.zotero.org/items/ITEMKEY"
    )  # fallback when no url
    assert parsed_with_data_key.metadata == {"zotero_item_key": "ITEMKEY", "doi": "10.1/x"}

    # key falls back to outer_item when data has no "key"
    parsed_outer_key = _parse_zotero_item(
        {"title": "Fallback title"},
        {"key": "OUTERKEY"},
        namespace,
    )
    assert parsed_outer_key is not None
    assert parsed_outer_key.item_key == "OUTERKEY"


def test_zotero_external_ids_are_scoped_to_the_remote_library():
    """The same item key in two remote libraries creates distinct identities."""
    from paper_ingestion.integrations.zotero_service import (
        _parse_zotero_item,
        _ZoteroLibraryNamespace,
    )

    item = {"key": "SHARED", "title": "Shared key"}
    personal = _parse_zotero_item(
        item,
        {},
        _ZoteroLibraryNamespace("user", "123456"),
    )
    group = _parse_zotero_item(
        item,
        {},
        _ZoteroLibraryNamespace("group", "987654"),
    )

    assert personal is not None
    assert group is not None
    assert personal.paper_create.external_id == "zotero:user:123456:SHARED"
    assert group.paper_create.external_id == "zotero:group:987654:SHARED"
    assert personal.paper_create.external_id != group.paper_create.external_id


async def test_legacy_zotero_identity_is_renamed_only_for_one_matching_namespace():
    """Every linked owner must resolve to the current namespace before rename."""
    from paper_ingestion.integrations._zotero_poll import (
        _migrate_unambiguous_legacy_identity,
        _ZoteroLibraryNamespace,
    )

    conn = _make_conn()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                FakeRecord(
                    {
                        "id": 17,
                        "linked_user_ids": [1, 2],
                        "destination_exists": False,
                    }
                )
            ],
            [
                FakeRecord({"key": "zotero.library_type", "value": "user"}),
                FakeRecord({"key": "zotero.user_id", "value": "123456"}),
            ],
            [
                FakeRecord({"key": "zotero.library_type", "value": "user"}),
                FakeRecord({"key": "zotero.user_id", "value": "123456"}),
            ],
        ]
    )
    conn.execute = AsyncMock(return_value="UPDATE 1")

    await _migrate_unambiguous_legacy_identity(
        conn,
        item_key="SHARED",
        namespace=_ZoteroLibraryNamespace("user", "123456"),
    )

    conn.execute.assert_awaited_once()
    assert conn.execute.await_args is not None
    assert conn.execute.await_args.args[1:] == (
        17,
        "zotero:user:123456:SHARED",
        "zotero:SHARED",
    )


async def test_legacy_zotero_identity_stays_private_when_linked_namespaces_disagree():
    """Ambiguous legacy rows are preserved instead of merging remote libraries."""
    from paper_ingestion.integrations._zotero_poll import (
        _migrate_unambiguous_legacy_identity,
        _ZoteroLibraryNamespace,
    )

    conn = _make_conn()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                FakeRecord(
                    {
                        "id": 17,
                        "linked_user_ids": [1, 2],
                        "destination_exists": False,
                    }
                )
            ],
            [
                FakeRecord({"key": "zotero.library_type", "value": "user"}),
                FakeRecord({"key": "zotero.user_id", "value": "123456"}),
            ],
            [
                FakeRecord({"key": "zotero.library_type", "value": "group"}),
                FakeRecord({"key": "zotero.group_id", "value": 987654}),
            ],
        ]
    )

    await _migrate_unambiguous_legacy_identity(
        conn,
        item_key="SHARED",
        namespace=_ZoteroLibraryNamespace("user", "123456"),
    )

    conn.execute.assert_not_awaited()
