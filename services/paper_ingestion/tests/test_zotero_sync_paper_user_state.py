"""Tests for paper_user_state INSERT in poll_zotero_library (C6).

Strategy: handler-mock (no live_pg).

Rationale
---------
The `paper_user_state` INSERT executed by `poll_zotero_library` is a
single SQL statement with a simple ON CONFLICT DO NOTHING predicate.
A live-PG test would add ~45 s of Docker startup per CI run and test the
same *structural* property that can be verified cheaply by inspecting what
SQL is passed to `conn.execute`.  The handler-mock approach:

  - Patches `ZoteroClient.fetch_items_since` so no real HTTP occurs.
  - Uses an AsyncMock `conn` to capture every `execute` call in order.
  - Asserts SQL text, parameters, and call-position relative to
    `upsert_paper` and the ``zotero_item_key`` UPDATE.

Three scenarios per plan §8 C6:

1. Fresh sync inserts state='to_read', starred=FALSE.
2. Re-sync of same item does NOT add a second row (ON CONFLICT DO NOTHING).
3. User-modified state is NOT overwritten by a subsequent sync.

Grounded against:
  services/paper_ingestion/paper_ingestion/integrations/zotero_service.py
  lines 530–551 (the INSERT + UPDATE block inside the upsert try-block).
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_zotero_item(key: str = "AAAA0001") -> dict[str, Any]:
    """Minimal Zotero API item structure consumed by poll_zotero_library."""
    return {
        "key": key,
        "data": {
            "key": key,
            "title": f"Test paper {key}",
            "abstractNote": "Abstract text",
            "url": "https://example.com/paper",
            "DOI": "",
            "creators": [{"firstName": "Jane", "lastName": "Doe"}],
            "extra": "",  # No 'jarvis_paper_id=' marker → not skipped
        },
    }


def _cfg_rows(
    *,
    api_key: str = "zotero_api_key_abc",
    user_id: str = "12345",
    library_type: str = "user",
    enabled: bool = True,
    poll_enabled: bool = True,
    last_version: int = 0,
) -> list[dict[str, Any]]:
    """Return asyncpg-style rows for _get_zotero_config."""
    return [
        {"key": "zotero.enabled", "value": enabled, "encrypted_value": None},
        {"key": "zotero.poll_enabled", "value": poll_enabled, "encrypted_value": None},
        {"key": "zotero.api_key", "value": api_key, "encrypted_value": None},
        {"key": "zotero.user_id", "value": user_id, "encrypted_value": None},
        {"key": "zotero.library_type", "value": library_type, "encrypted_value": None},
        {
            "key": "zotero.last_library_version",
            "value": last_version,
            "encrypted_value": None,
        },
    ]


def _make_pool_capturing_conn(execute_returns: list[Any] | None = None):
    """Build a mock asyncpg pool whose conn.execute() captures calls in order.

    Parameters
    ----------
    execute_returns:
        Optional list of return values for successive conn.execute() calls.
        Defaults to None for each call (asyncpg execute returns None).

    Returns (pool, conn_mock).  conn.execute.call_args_list records every
    (sql, *params) invocation in the order they happened.
    """
    conn = AsyncMock()
    # execute always returns None in asyncpg
    conn.execute = AsyncMock(return_value=None)
    # fetchrow for upsert_paper must return a row with 'id'
    conn.fetchrow = AsyncMock(return_value={"id": 42, "is_insert": True})
    # fetch for _get_zotero_config (first pool.acquire context)
    conn.fetch = AsyncMock(return_value=_cfg_rows())

    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


# ---------------------------------------------------------------------------
# Shared patch context — patches ZoteroClient + paper_analyze.defer_async
# ---------------------------------------------------------------------------


def _patch_zotero_client(items: list[dict], new_version: int = 10):
    """Patch ZoteroClient so fetch_items_since returns (items, new_version)."""
    client_instance = AsyncMock()
    client_instance.fetch_items_since = AsyncMock(return_value=(items, new_version))
    client_cls = MagicMock(return_value=client_instance)
    return patch(
        "paper_ingestion.integrations.zotero_client.ZoteroClient",
        client_cls,
    )


# ---------------------------------------------------------------------------
# Scenario 1 — fresh sync inserts state='to_read', starred=FALSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_sync_inserts_to_read_state() -> None:
    """First poll of a new Zotero item must write state='to_read', starred=FALSE.

    Verifies:
      - conn.execute is called with the INSERT INTO paper_user_state SQL.
      - SQL contains ON CONFLICT (paper_id, user_id) DO NOTHING.
      - Parameters are (paper_id=42, user_id=None, confirmed by placeholder order).
    """
    from paper_ingestion.integrations.zotero_service import poll_zotero_library

    item = _make_zotero_item("AAAA0001")
    pool, conn = _make_pool_capturing_conn()
    http_client = AsyncMock()

    with (
        _patch_zotero_client([item]),
        patch(
            "paper_ingestion.integrations.zotero_service.paper_analyze.defer_async",
            AsyncMock(),
        ),
    ):
        result = await poll_zotero_library(pool, http_client)

    assert result["status"] == "ok"
    assert result["enqueued"] == 1

    # Find the paper_user_state INSERT among all execute calls
    all_calls = conn.execute.call_args_list
    pus_calls = [
        c for c in all_calls if "paper_user_state" in (c.args[0] if c.args else "").lower()
    ]
    assert pus_calls, (
        "Expected at least one conn.execute() call referencing paper_user_state; "
        f"got execute calls: {[c.args[0] for c in all_calls if c.args]}"
    )

    insert_call = pus_calls[0]
    sql: str = insert_call.args[0]

    # SQL structure assertions
    assert re.search(r"INSERT\s+INTO\s+paper_user_state", sql, re.IGNORECASE), (
        "SQL must be an INSERT INTO paper_user_state"
    )
    assert "ON CONFLICT (paper_id, user_id) DO NOTHING" in sql, (
        "SQL must contain ON CONFLICT (paper_id, user_id) DO NOTHING"
    )
    assert "'to_read'" in sql, "SQL must hard-code 'to_read' as the state literal"
    assert "FALSE" in sql, "SQL must hard-code FALSE as the starred literal"

    # Parameter assertions: $1 = paper_id (42), $2 = user_id (None)
    params = insert_call.args[1:]
    assert len(params) == 2, f"Expected 2 params ($1 paper_id, $2 user_id), got {params}"
    assert params[0] == 42, f"$1 must be paper_id=42, got {params[0]}"
    assert params[1] is None, f"$2 must be user_id=None (single-tenant), got {params[1]}"


# ---------------------------------------------------------------------------
# Scenario 2 — re-sync of same item does NOT add a second row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_same_item_does_not_duplicate_state_row() -> None:
    """ON CONFLICT DO NOTHING must be present so a second sync is a no-op.

    This test verifies the SQL carries the conflict clause, which is what
    guarantees idempotency in the DB.  The mock always fires execute(), but
    the SQL shape means the DB will silently skip duplicates.  We verify:
      - Two poll_zotero_library calls each fire exactly one paper_user_state INSERT.
      - Both INSERT calls carry the identical parameters.
      - The conflict clause is present on both (no variant was introduced).
    """
    from paper_ingestion.integrations.zotero_service import poll_zotero_library

    item = _make_zotero_item("AAAA0002")

    # --- First sync ---
    pool1, conn1 = _make_pool_capturing_conn()
    http_client = AsyncMock()

    with (
        _patch_zotero_client([item]),
        patch(
            "paper_ingestion.integrations.zotero_service.paper_analyze.defer_async",
            AsyncMock(),
        ),
    ):
        result1 = await poll_zotero_library(pool1, http_client)

    assert result1["status"] == "ok"

    pus_calls_1 = [
        c
        for c in conn1.execute.call_args_list
        if "paper_user_state" in (c.args[0] if c.args else "").lower()
    ]
    assert len(pus_calls_1) == 1, (
        f"First sync must fire exactly one paper_user_state INSERT; got {len(pus_calls_1)}"
    )

    # --- Second sync (same item, simulates re-poll) ---
    pool2, conn2 = _make_pool_capturing_conn()

    with (
        _patch_zotero_client([item]),
        patch(
            "paper_ingestion.integrations.zotero_service.paper_analyze.defer_async",
            AsyncMock(),
        ),
    ):
        result2 = await poll_zotero_library(pool2, http_client)

    assert result2["status"] == "ok"

    pus_calls_2 = [
        c
        for c in conn2.execute.call_args_list
        if "paper_user_state" in (c.args[0] if c.args else "").lower()
    ]
    assert len(pus_calls_2) == 1, (
        f"Second sync must fire exactly one paper_user_state INSERT; got {len(pus_calls_2)}"
    )

    # Both calls must carry the conflict clause
    for idx, c in enumerate([pus_calls_1[0], pus_calls_2[0]], start=1):
        sql = c.args[0]
        assert "ON CONFLICT (paper_id, user_id) DO NOTHING" in sql, (
            f"Sync {idx}: ON CONFLICT DO NOTHING must be present (guarantees idempotency)"
        )

    # Params must match (same paper_id returned by mock fetchrow in both pools)
    assert pus_calls_1[0].args[1:] == pus_calls_2[0].args[1:], (
        "INSERT params must be identical on first and second sync"
    )


# ---------------------------------------------------------------------------
# Scenario 3 — user-modified state is NOT overwritten by a subsequent sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resync_does_not_overwrite_user_modified_state() -> None:
    """ON CONFLICT DO NOTHING must prevent clobbering a user-set state.

    Scenario:
      1. First sync → INSERT state='to_read' (mock: succeeds silently, row count=1).
      2. User manually changes state to 'trash' (simulated: in real DB the row exists).
      3. Second sync → INSERT fires again with ON CONFLICT DO NOTHING → DB ignores it.

    This test verifies that the SQL statement will *never* use DO UPDATE, which
    is what would overwrite the user's choice.  We inspect:
      - The SQL does NOT contain 'DO UPDATE'.
      - The SQL does NOT contain 'SET state'.
      - The conflict clause is exactly 'ON CONFLICT (paper_id, user_id) DO NOTHING'.
    """
    from paper_ingestion.integrations.zotero_service import poll_zotero_library

    item = _make_zotero_item("AAAA0003")
    pool, conn = _make_pool_capturing_conn()
    http_client = AsyncMock()

    # Simulate second sync (state already exists in DB as 'trash').
    # From the mock's point of view the conn.execute runs the same INSERT SQL
    # each time — the DB side-effects differ but the SQL shape is what we test.
    with (
        _patch_zotero_client([item]),
        patch(
            "paper_ingestion.integrations.zotero_service.paper_analyze.defer_async",
            AsyncMock(),
        ),
    ):
        result = await poll_zotero_library(pool, http_client)

    assert result["status"] == "ok"

    pus_calls = [
        c
        for c in conn.execute.call_args_list
        if "paper_user_state" in (c.args[0] if c.args else "").lower()
    ]
    assert pus_calls, "Expected at least one paper_user_state execute call"

    for c in pus_calls:
        sql: str = c.args[0]
        assert "DO UPDATE" not in sql.upper(), (
            f"SQL must NOT contain 'DO UPDATE' — that would overwrite user state. Got SQL:\n{sql}"
        )
        assert "SET state" not in sql.lower(), (
            f"SQL must NOT contain 'SET state' — that would overwrite user state. Got SQL:\n{sql}"
        )
        assert "ON CONFLICT (paper_id, user_id) DO NOTHING" in sql, (
            "Conflict clause must be exactly ON CONFLICT (paper_id, user_id) DO NOTHING"
        )


# ---------------------------------------------------------------------------
# Scenario 4 — INSERT call ordering: after upsert_paper, before item_key UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_user_state_insert_order() -> None:
    """INSERT paper_user_state must execute AFTER upsert_paper and BEFORE the
    zotero_item_key UPDATE.

    Per the spec: B7 runs INSERT after upsert_paper, before the existing
    zotero_item_key UPDATE.  We verify call ordering by inspecting the SQL
    text of successive conn.execute() calls:

      1. upsert_paper → conn.fetchrow (INSERT INTO papers ... RETURNING ...)
      2. conn.execute → INSERT INTO paper_user_state ...   ← must come first
      3. conn.execute → UPDATE papers SET zotero_item_key  ← must come after
    """
    from paper_ingestion.integrations.zotero_service import poll_zotero_library

    item = _make_zotero_item("AAAA0004")
    pool, conn = _make_pool_capturing_conn()
    http_client = AsyncMock()

    with (
        _patch_zotero_client([item]),
        patch(
            "paper_ingestion.integrations.zotero_service.paper_analyze.defer_async",
            AsyncMock(),
        ),
    ):
        await poll_zotero_library(pool, http_client)

    # Collect ordered execute() calls that have SQL text as first arg
    execute_sqls: list[str] = [c.args[0] for c in conn.execute.call_args_list if c.args]

    # Find indices of the two SQL statements we care about
    pus_index: int | None = None
    item_key_index: int | None = None

    for idx, sql in enumerate(execute_sqls):
        if "paper_user_state" in sql.lower() and "INSERT" in sql.upper():
            if pus_index is None:
                pus_index = idx
        if "zotero_item_key" in sql.lower() and "UPDATE" in sql.upper():
            if item_key_index is None:
                item_key_index = idx

    assert pus_index is not None, (
        f"No INSERT INTO paper_user_state found in execute calls: {execute_sqls}"
    )
    assert item_key_index is not None, (
        f"No UPDATE ... zotero_item_key found in execute calls: {execute_sqls}"
    )
    assert pus_index < item_key_index, (
        f"INSERT paper_user_state (index {pus_index}) must come BEFORE "
        f"UPDATE zotero_item_key (index {item_key_index}). "
        f"Execute order: {execute_sqls}"
    )
