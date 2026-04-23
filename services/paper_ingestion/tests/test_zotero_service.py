"""Tests for zotero_service: push_paper_to_zotero and resync_paper_to_zotero.

Uses AsyncMock / MagicMock for db_pool and httpx.AsyncClient.
ZoteroClient methods are patched at the class level.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from paper_ingestion.integrations.zotero_service import (
    poll_zotero_library,
    push_paper_to_zotero,
    resync_paper_to_zotero,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Minimal asyncpg.Record substitute."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


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
    return conn


def _zotero_enabled_config_rows():
    """Simulate user_config rows for an enabled Zotero config."""
    return [
        FakeRecord({"key": "zotero.enabled", "value": True}),
        FakeRecord({"key": "zotero.api_key", "value": "test_api_key"}),
        FakeRecord({"key": "zotero.user_id", "value": "123456"}),
        FakeRecord({"key": "zotero.library_type", "value": "user"}),
    ]


def _zotero_disabled_config_rows():
    return [FakeRecord({"key": "zotero.enabled", "value": False})]


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


# ---------------------------------------------------------------------------
# Test: disabled
# ---------------------------------------------------------------------------


async def test_push_paper_not_configured():
    """push_paper_to_zotero returns early when Zotero is disabled."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=_zotero_disabled_config_rows())

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)
        # ZoteroClient should never be instantiated
        mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# Test: no project links
# ---------------------------------------------------------------------------


async def test_push_paper_no_project_links():
    """push_paper_to_zotero returns early when paper has no project links."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=_zotero_enabled_config_rows())

    paper = _paper_row(project_ids=None)  # NULL → empty list in service
    paper_conn = _make_conn(fetchrow=paper)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=paper_conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)
        mock_client.return_value.create_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: happy path (new item created)
# ---------------------------------------------------------------------------


async def test_push_paper_happy_path():
    """push_paper_to_zotero creates item, stores key, and attempts BBT key fetch."""
    paper = _paper_row(project_ids=[10])
    project = _project_row(col_key=None)  # no pre-existing collection key
    topic_rows: list = []

    # Connection sequence:
    # 1. acquire() for fetchrow(paper)
    # 2. acquire() for fetch(topic_rows)
    # 3. acquire() for fetchrow(project)
    # 4. acquire() for execute(UPDATE projects …)
    # 5. acquire() for execute(UPDATE papers zotero_item_key)
    # 6. acquire() for execute(UPDATE papers zotero_citation_key)

    conn1 = _make_conn(fetchrow=paper)
    conn2 = _make_conn(fetch=topic_rows)
    conn3 = _make_conn(fetchrow=project)
    conn4 = _make_conn()
    conn5 = _make_conn()
    conn6 = _make_conn()

    def _cm(conn):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=_zotero_enabled_config_rows())
    pool.acquire = MagicMock(
        side_effect=[_cm(conn1), _cm(conn2), _cm(conn3), _cm(conn4), _cm(conn5), _cm(conn6)]
    )

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

        # Check that the key was persisted
        assert any("ABCD1234" in str(c) for c in conn5.execute.call_args_list)


# ---------------------------------------------------------------------------
# Test: DOI deduplication — reuse existing Zotero item
# ---------------------------------------------------------------------------


async def test_push_paper_doi_dedupe():
    """push_paper_to_zotero reuses existing Zotero item found by DOI search."""
    paper = _paper_row(project_ids=[10], doi="10.1234/test")
    conn1 = _make_conn(fetchrow=paper)

    # After DOI dedupe, we only need the persist-key connection
    conn2 = _make_conn()

    def _cm(conn):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=_zotero_enabled_config_rows())
    pool.acquire = MagicMock(side_effect=[_cm(conn1), _cm(conn2)])

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        mock_zotero = mock_client.return_value
        mock_zotero.search_by_doi = AsyncMock(
            return_value={"key": "EXISTING_KEY", "data": {"DOI": "10.1234/test"}}
        )
        mock_zotero.create_item = AsyncMock()
        mock_zotero.fetch_bbt_citation_key = AsyncMock(return_value=None)

        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)

        # create_item must NOT be called when DOI match is found
        mock_zotero.create_item.assert_not_called()
        # The existing key should be persisted
        assert any("EXISTING_KEY" in str(c) for c in conn2.execute.call_args_list)


# ---------------------------------------------------------------------------
# Test: BBT fallback — push still succeeds when BBT returns None
# ---------------------------------------------------------------------------


async def test_push_paper_bbt_fallback():
    """push_paper_to_zotero succeeds even when BBT returns None (non-fatal)."""
    paper = _paper_row(project_ids=[10])
    project = _project_row(col_key="PRECOLL")
    topic_rows: list = []

    conn1 = _make_conn(fetchrow=paper)
    conn2 = _make_conn(fetch=topic_rows)
    conn3 = _make_conn(fetchrow=project)
    conn4 = _make_conn()  # UPDATE papers zotero_item_key

    def _cm(conn):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=_zotero_enabled_config_rows())
    pool.acquire = MagicMock(side_effect=[_cm(conn1), _cm(conn2), _cm(conn3), _cm(conn4)])

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
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)

        mock_zotero.create_item.assert_called_once()
        # zotero_citation_key update should NOT be called when bbt_key is None
        # (service only calls it when bbt_key is truthy)
        # conn4 holds the zotero_item_key update — citation_key update is skipped
        assert conn4.execute.called


# ---------------------------------------------------------------------------
# Test: resync clears key then re-pushes
# ---------------------------------------------------------------------------


async def test_resync_clears_and_repushes():
    """resync_paper_to_zotero clears zotero_item_key before delegating to push."""
    clear_conn = _make_conn()

    def _cm(conn):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=conn)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_cm(clear_conn))

    http = AsyncMock(spec=httpx.AsyncClient)

    # Patch push_paper_to_zotero to confirm it's called after clear
    with patch(
        "paper_ingestion.integrations.zotero_service.push_paper_to_zotero",
        new=AsyncMock(),
    ) as mock_push:
        await resync_paper_to_zotero(paper_id=42, db_pool=pool, http_client=http)

        # The clear execute must be called with NULL
        clear_conn.execute.assert_called_once()
        sql_call = clear_conn.execute.call_args
        assert "NULL" in sql_call[0][0]
        assert 42 in sql_call[0]

        # push must be called afterwards
        mock_push.assert_called_once_with(42, pool, http)


# ---------------------------------------------------------------------------
# Helpers for poll tests
# ---------------------------------------------------------------------------


def _zotero_poll_enabled_config_rows():
    """user_config rows for an enabled Zotero config with polling on."""
    return [
        FakeRecord({"key": "zotero.enabled", "value": True}),
        FakeRecord({"key": "zotero.api_key", "value": "test_api_key"}),
        FakeRecord({"key": "zotero.user_id", "value": "123456"}),
        FakeRecord({"key": "zotero.library_type", "value": "user"}),
        FakeRecord({"key": "zotero.poll_enabled", "value": True}),
        FakeRecord({"key": "zotero.last_library_version", "value": 0}),
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
    """Pool mock where pool.fetch returns config rows and acquire() uses conn_returns."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=config_rows or _zotero_poll_enabled_config_rows())

    def _cm(rv):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=rv)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    pool.acquire = MagicMock(side_effect=[_cm(rv) for rv in conn_returns])
    return pool


# ---------------------------------------------------------------------------
# E4 Poll tests
# ---------------------------------------------------------------------------


async def test_poll_library_disabled():
    """Returns disabled status when zotero.enabled is false."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=_zotero_disabled_config_rows())
    http = AsyncMock(spec=httpx.AsyncClient)

    result = await poll_zotero_library(db_pool=pool, http_client=http)

    assert result["status"] == "disabled"


async def test_poll_library_poll_disabled():
    """Returns poll_disabled status when zotero.poll_enabled is false."""
    config_rows = [
        FakeRecord({"key": "zotero.enabled", "value": True}),
        FakeRecord({"key": "zotero.api_key", "value": "key"}),
        FakeRecord({"key": "zotero.user_id", "value": "123"}),
        FakeRecord({"key": "zotero.poll_enabled", "value": False}),
    ]
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=config_rows)
    http = AsyncMock(spec=httpx.AsyncClient)

    result = await poll_zotero_library(db_pool=pool, http_client=http)

    assert result["status"] == "poll_disabled"


async def test_poll_library_skips_jarvis_origin():
    """Items with jarvis_paper_id= in Extra are skipped (not enqueued)."""
    jarvis_item = _zotero_item(key="JARVIS01", extra="jarvis_paper_id=42")
    # version conn: persist new version
    version_conn = _make_conn()
    pool = _make_poll_pool(version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([jarvis_item], 5))

        with patch("paper_ingestion.integrations.zotero_service.jobs_lib") as mock_jobs:
            mock_jobs.enqueue = AsyncMock()
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    # JARVIS-originated item must not be enqueued
    mock_jobs.enqueue.assert_not_called()
    assert result["status"] == "ok"
    assert result["new_items"] == 0
    assert result["enqueued"] == 0


async def test_poll_library_enqueues_new_items():
    """New items without jarvis origin are enqueued as paper.process jobs."""
    new_item = _zotero_item(key="NEWITEM1", title="New Paper", doi="")
    # No DOI → no DB lookup; one enqueue call; one version-persist conn
    version_conn = _make_conn()
    pool = _make_poll_pool(version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([new_item], 10))

        with patch("paper_ingestion.integrations.zotero_service.jobs_lib") as mock_jobs:
            mock_jobs.enqueue = AsyncMock(return_value="job-uuid-1")
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    mock_jobs.enqueue.assert_called_once()
    call_args = mock_jobs.enqueue.call_args
    assert call_args[0][1] == "paper.process"
    payload = call_args[0][2]
    assert payload["source_override"] == "zotero"
    assert payload["zotero_item_key"] == "NEWITEM1"
    assert result["enqueued"] == 1
    assert result["new_items"] == 1


async def test_poll_library_updates_version():
    """zotero.last_library_version updated in user_config after poll."""
    item = _zotero_item(key="VER0001", doi="")
    version_conn = _make_conn()
    pool = _make_poll_pool(version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        # Return a newer version (42) than the current (0)
        mock_client.fetch_items_since = AsyncMock(return_value=([item], 42))

        with patch("paper_ingestion.integrations.zotero_service.jobs_lib") as mock_jobs:
            mock_jobs.enqueue = AsyncMock(return_value="job-uuid-2")
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    # The version-persist connection should have had execute called
    version_conn.execute.assert_called_once()
    sql, version_arg = version_conn.execute.call_args[0][:2]
    assert "zotero.last_library_version" in sql
    assert "42" in str(version_arg)
    assert result["version_to"] == 42
