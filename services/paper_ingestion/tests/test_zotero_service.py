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
    sync_annotations_for_paper,
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
    # Support `async with conn.transaction():` — transaction() must return a sync
    # callable that itself returns an async context manager.
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=None)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    return conn


def _zotero_enabled_config_rows():
    """Simulate user_config rows for an enabled Zotero config."""
    return [
        FakeRecord({"key": "zotero.enabled", "value": True}),
        FakeRecord({"key": "zotero.api_key", "value": "test_api_key"}),
        FakeRecord({"key": "zotero.user_id", "value": "123456"}),
        FakeRecord({"key": "zotero.library_type", "value": "user"}),
    ]


def _zotero_enabled_with_annotations_rows():
    rows = _zotero_enabled_config_rows()
    rows.append(FakeRecord({"key": "zotero.sync_annotations_enabled", "value": True}))
    return rows


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


def _cm(conn):
    """Wrap a connection in an async context manager mock."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Test: disabled
# ---------------------------------------------------------------------------


async def test_push_paper_not_configured():
    """push_paper_to_zotero returns early when Zotero is disabled."""
    # PI-010: _get_zotero_config now uses acquire() — config conn is first acquire().
    config_conn = _make_conn(fetch=_zotero_disabled_config_rows())
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_cm(config_conn))

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
    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())
    paper = _paper_row(project_ids=None)  # NULL → empty list in service
    paper_conn = _make_conn(fetchrow=paper)

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=[_cm(config_conn), _cm(paper_conn)])

    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client:
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)
        mock_client.return_value.create_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: happy path (new item created)
# ---------------------------------------------------------------------------


async def test_push_paper_happy_path():
    """push_paper_to_zotero creates item, stores key, and attempts BBT key fetch.

    PI-EDGE-013: push now acquires a single connection for all sub-queries.
    Connection sequence:
      1. acquire() for _get_zotero_config fetch (config_conn)
      2. acquire() for entire push body (push_conn — handles paper fetch, topics,
         project fetch, project key update, paper key update, BBT key update)
    """
    paper = _paper_row(project_ids=[10])
    project = _project_row(col_key=None)  # no pre-existing collection key
    topic_rows: list = []

    config_conn = _make_conn(fetch=_zotero_enabled_config_rows())

    # Single connection used for all push sub-queries; configure it to return
    # appropriate values for successive fetchrow / fetch calls.
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

        # Check that the zotero_item_key was persisted via execute
        assert any("ABCD1234" in str(c) for c in push_conn.execute.call_args_list)


# ---------------------------------------------------------------------------
# Test: DOI deduplication — reuse existing Zotero item
# ---------------------------------------------------------------------------


async def test_push_paper_doi_dedupe():
    """push_paper_to_zotero reuses existing Zotero item found by DOI search.

    PI-EDGE-013: push now acquires a single connection for all sub-queries.
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

        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)

        # create_item must NOT be called when DOI match is found
        mock_zotero.create_item.assert_not_called()
        # The existing key should be persisted
        assert any("EXISTING_KEY" in str(c) for c in push_conn.execute.call_args_list)


# ---------------------------------------------------------------------------
# Test: BBT fallback — push still succeeds when BBT returns None
# ---------------------------------------------------------------------------


async def test_push_paper_bbt_fallback():
    """push_paper_to_zotero succeeds even when BBT returns None (non-fatal).

    PI-EDGE-013: push now acquires a single connection for all sub-queries.
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
        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)

        mock_zotero.create_item.assert_called_once()
        # zotero_citation_key update should NOT be called when bbt_key is None
        # (service only calls it when bbt_key is truthy)
        # push_conn.execute should have been called for zotero_item_key update only
        assert push_conn.execute.called


# ---------------------------------------------------------------------------
# Test: resync clears key then re-pushes
# ---------------------------------------------------------------------------


async def test_resync_clears_and_repushes():
    """resync_paper_to_zotero clears zotero_item_key before delegating to push."""
    clear_conn = _make_conn()

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
    """Pool mock where config is fetched via acquire() and subsequent acquire() uses conn_returns.

    PI-010: _get_zotero_config now uses acquire() — config conn is prepended automatically.
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
    """Returns disabled status when zotero.enabled is false."""
    config_conn = _make_conn(fetch=_zotero_disabled_config_rows())
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_cm(config_conn))
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

        with patch("paper_ingestion.integrations.zotero_service.jobs_lib") as mock_jobs:
            mock_jobs.enqueue = AsyncMock()
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    # JARVIS-originated item must not be enqueued
    mock_jobs.enqueue.assert_not_called()
    assert result["status"] == "ok"
    assert result["new_items"] == 0
    assert result["enqueued"] == 0


async def test_poll_library_enqueues_new_items():
    """New items without jarvis origin are upserted and enqueued as paper.analyze jobs.

    PI-002: poll now calls upsert_paper → enqueues paper.analyze with paper_id.
    """
    new_item = _zotero_item(key="NEWITEM1", title="New Paper", doi="")
    # No DOI → no DOI-lookup conn needed.
    # upsert conn: fetchrow returns the upserted paper row; execute for zotero_item_key update.
    upsert_conn = _make_conn(fetchrow=FakeRecord({"id": 99}))
    # version conn: persist new version (10 != 0)
    version_conn = _make_conn()
    pool = _make_poll_pool(upsert_conn, version_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.fetch_items_since = AsyncMock(return_value=([new_item], 10))

        with patch("paper_ingestion.integrations.zotero_service.jobs_lib") as mock_jobs:
            mock_jobs.enqueue = AsyncMock(return_value="job-uuid-1")
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    mock_jobs.enqueue.assert_called_once()
    call_args = mock_jobs.enqueue.call_args
    # PI-002: job kind is now paper.analyze, payload has paper_id
    assert call_args[0][1] == "paper.analyze"
    payload = call_args[0][2]
    assert payload["paper_id"] == 99
    assert result["enqueued"] == 1
    assert result["new_items"] == 1


async def test_poll_library_updates_version():
    """zotero.last_library_version updated in user_config after poll."""
    item = _zotero_item(key="VER0001", doi="")
    # upsert conn for the item
    upsert_conn = _make_conn(fetchrow=FakeRecord({"id": 55}))
    # version conn: persists new version
    version_conn = _make_conn()
    pool = _make_poll_pool(upsert_conn, version_conn)
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


# ---------------------------------------------------------------------------
# Annotation sync
# ---------------------------------------------------------------------------


async def test_sync_annotations_for_paper_imports_zotero_highlights_idempotently():
    """Zotero annotation children are upserted into paper_notes by annotation key."""
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

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.get_item_children = AsyncMock(return_value=annotations)

        result = await sync_annotations_for_paper(
            paper_id=7,
            db_pool=pool,
            http_client=http,
        )

    assert result == {"paper_id": 7, "imported": 2, "status": "ok"}
    assert persist_conn.execute.await_count == 2
    first_sql = persist_conn.execute.await_args_list[0].args[0]
    assert "ON CONFLICT (paper_id, zotero_annotation_key)" in first_sql
    assert "verification_status" in first_sql
    assert "promoted_at" in first_sql
    assert persist_conn.execute.await_args_list[0].args[1:6] == (
        7,
        "zotero",
        "ANN1",
        "Worth citing",
        "Important highlighted claim",
    )
    assert persist_conn.execute.await_args_list[1].args[5] is None


# ---------------------------------------------------------------------------
# _get_zotero_config — decrypt roundtrip tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PI-EDGE-003: per-sync enqueue cap
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
    upsert_conns = [_make_conn(fetchrow=FakeRecord({"id": 1000 + i})) for i in range(25)]

    pool = _make_poll_pool(*upsert_conns)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        # Library version advances to 999 on the Zotero side.
        mock_client.fetch_items_since = AsyncMock(return_value=(items, 999))

        with patch("paper_ingestion.integrations.zotero_service.jobs_lib") as mock_jobs:
            mock_jobs.enqueue = AsyncMock(return_value="job-uuid")
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    # Exactly MAX_ENQUEUE_PER_SYNC jobs must have been enqueued.
    assert mock_jobs.enqueue.call_count == MAX_ENQUEUE_PER_SYNC, (
        f"Expected {MAX_ENQUEUE_PER_SYNC} enqueued jobs, got {mock_jobs.enqueue.call_count}"
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
                "key": "zotero.enabled",
                "value": True,
                "encrypted_value": None,
            }
        ),
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
    assert config["enabled"] is True
    assert config["user_id"] == "123456"
    assert config["library_type"] == "user"


# ---------------------------------------------------------------------------
# PI-EDGE-009: sync_annotations_for_paper — transaction rollback on mid-loop failure
# ---------------------------------------------------------------------------


async def test_sync_annotations_rolls_back_on_mid_loop_failure():
    """PI-EDGE-009: if conn.execute raises mid-loop, the whole transaction rolls back.

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
# PI-EDGE-011: _get_zotero_config — decrypt failure returns {} and logs warning
# ---------------------------------------------------------------------------


async def test_get_zotero_config_returns_empty_dict_on_decrypt_failure(caplog):
    """PI-EDGE-011: if decrypt_secret raises, _get_zotero_config returns {} and warns."""
    import logging

    from paper_ingestion.integrations.zotero_service import _get_zotero_config

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
            result = await _get_zotero_config(pool)

    assert result == {"_decrypt_error": True}, (
        f"Expected {{'_decrypt_error': True}} on decrypt failure, got {result}"
    )
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

        with patch("paper_ingestion.integrations.zotero_service.jobs_lib") as mock_jobs:
            mock_jobs.enqueue = AsyncMock()
            result = await poll_zotero_library(db_pool=pool, http_client=http)

    # Cursor must NOT have advanced — version_to should remain at last_version (0).
    assert result["version_to"] == 0, (
        f"Expected version_to=0 (cursor pinned), got {result['version_to']}"
    )
    assert result["status"] == "ok"
    # No jobs should have been enqueued for the failed item.
    mock_jobs.enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# H12: _get_zotero_config decrypt warning must not log exc string
# ---------------------------------------------------------------------------


async def test_get_zotero_config_does_not_log_exc_string(caplog):
    """H12: decrypt warning uses %r short_key form, not exc — no ciphertext leakage.

    The original exc object (which may contain ciphertext fragments) must NOT
    appear in any warning log record.  The short_key repr must appear.
    """
    import logging

    from paper_ingestion.integrations.zotero_service import _get_zotero_config

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
            result = await _get_zotero_config(pool)

    # Result must be the sentinel dict (H11).
    assert result == {"_decrypt_error": True}, f"Expected {{'_decrypt_error': True}}, got {result}"

    # The raw exc message string must NOT appear in any log record.
    for record in caplog.records:
        assert exc_message not in record.message, f"exc string leaked into log: {record.message!r}"

    # The short_key repr ('api_key') MUST appear in a warning record.
    assert any("api_key" in record.message for record in caplog.records), (
        f"Expected short_key in log warning; got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# PI-EDGE-013: push_paper_to_zotero — single connection acquisition
# ---------------------------------------------------------------------------


async def test_push_paper_to_zotero_acquires_single_connection():
    """PI-EDGE-013: push_paper_to_zotero acquires exactly one DB connection for the push body.

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

        await push_paper_to_zotero(paper_id=1, db_pool=pool, http_client=http)

    # Exactly 2 acquire() calls: config + push body.
    assert pool.acquire.call_count == 2, (
        f"Expected 2 pool.acquire() calls (config + push), got {pool.acquire.call_count}"
    )
