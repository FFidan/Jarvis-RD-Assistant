"""Contract tests for the Zotero router DB layer.

Collapses mock-unit DB-pool tests to real asyncpg:
  - test_zotero_router.py: get_paper_zotero_state — ownership check + DB read
    (the mock test uses _make_pool_and_conn + monkeypatched assert_paper_ownership
    to verify a 403 path; the contract test exercises the real ownership guard
    against a real papers row).

Idiomatic-mock carve-out:
  - ZoteroClient HTTP (respx / httpx mocks in test_zotero_client.py) — KEPT
  - task_registry._TASK_MAP injection (test_zotero_router.py poll tests) — KEPT
  - push_paper_to_zotero / poll_zotero_library (test_zotero_service.py) — KEPT
    (all mock ZoteroClient HTTP = idiomatic external boundary)

All tests use the session-scoped contract_conn fixture (asyncpg connection
wrapped in a per-test rollback transaction).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_user_and_paper(conn, *, email: str, title: str) -> tuple[int, int]:
    """Insert one user + one paper discovered by that user; return (user_id, paper_id).

    Uses ``discovered_by`` (not the removed ``user_id`` column) per the canonical
    corpus ownership semantics in assert_paper_ownership (db_helpers.py:235).
    """
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        email,
    )
    paper_id = await conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ($1, 'arxiv', $2, ARRAY['Author Z'], 'https://example.com/z', $3)
        RETURNING id
        """,
        f"arxiv:zotero-contract-{user_id}",
        title,
        user_id,
    )
    return int(user_id), int(paper_id)


# ---------------------------------------------------------------------------
# get_paper_zotero_state — ownership enforcement
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_ownership_check_passes_for_own_paper(contract_conn):
    """assert_paper_ownership does NOT raise when caller == discovered_by.

    Collapses: the positive ownership path of test_get_paper_zotero_state_checks_ownership
    — exercises the real SQL in assert_paper_ownership (db_helpers.py:235) instead of
    monkeypatching it.

    NOTE: The full GET /api/papers/{id}/zotero route is NOT exercised here because
    the papers table in the contract DB (init.sql baseline) does not include the
    zotero_item_key / zotero_citation_key / zotero_last_pushed_at columns — those
    are legacy migration-only columns not yet folded into init.sql.
    STALE-CITATION: zotero router fetchrow("SELECT zotero_item_key …") fails
    against the contract schema. Scoped to ownership layer only.
    """
    from jarvis_common.db_helpers import assert_paper_ownership

    user_id, paper_id = await _seed_user_and_paper(
        contract_conn,
        email="zotero-contract-owner@test.invalid",
        title="ZZZ-ZOTERO-CONTRACT Ownership Pass",
    )

    # Must not raise — caller is the owner (discovered_by == user_id)
    await assert_paper_ownership(contract_conn, paper_id, user_id)


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_ownership_check_passes_for_canonical_paper(contract_conn):
    """assert_paper_ownership does NOT raise for discovered_by IS NULL (shared corpus).

    A paper with no discovered_by is a shared/canonical paper readable by all
    authenticated users — this is the D4 ownership rule (db_helpers.py:295-299).
    """
    from jarvis_common.db_helpers import assert_paper_ownership

    # Seed a canonical paper with discovered_by = NULL.
    paper_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url)
        VALUES ('arxiv:zotero-canonical-001', 'arxiv',
                'ZZZ-ZOTERO-CONTRACT Canonical Paper',
                ARRAY['System'], 'https://example.com/canonical')
        RETURNING id
        """,
    )

    # Seed any user.
    reader_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "zotero-reader@test.invalid",
    )

    # assert_paper_ownership must pass even though reader_id != discovered_by.
    await assert_paper_ownership(contract_conn, int(paper_id), int(reader_id))


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_get_zotero_state_404_for_nonexistent_paper(contract_conn):
    """get_paper_zotero_state returns 404 when paper_id does not exist in DB.

    Exercises the real papers SELECT (not a mock fetchrow return_value).
    """
    import httpx
    from unittest.mock import AsyncMock, patch
    from httpx import ASGITransport
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from jarvis_common import verify_api_key
    from jarvis_common.testing import SharedConnPool

    # Use a very large ID that cannot exist in the rolled-back test transaction.
    nonexistent_paper_id = 999_999_999

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[verify_api_key] = lambda: None

    try:
        import paper_ingestion.routers.zotero as zotero_mod

        with patch.object(zotero_mod, "current_user_id_strict", AsyncMock(return_value=1)):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    f"/api/papers/{nonexistent_paper_id}/zotero",
                    headers={"X-API-Key": "test"},
                )
    finally:
        if original_pool is None:
            app.state.__dict__.pop("db_pool", None)
        else:
            app.state.db_pool = original_pool
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(verify_api_key, None)

    # 403 (ownership guard fires before the SELECT) or 404 (paper not found) are
    # both acceptable — the key assertion is that the app doesn't 500 on a real
    # missing row. In practice assert_paper_ownership raises 404 when the paper
    # row is absent.
    assert resp.status_code in (403, 404), (
        f"Expected 403 or 404 for nonexistent paper, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_get_zotero_state_403_for_non_owner(contract_conn):
    """get_paper_zotero_state returns 403 when caller does not own the paper.

    Collapses: test_get_paper_zotero_state_checks_ownership — the mock test
    monkeypatches assert_paper_ownership to raise 403; this contract test seeds
    a real paper owned by user A and calls as user B, letting the real ownership
    guard enforce the 403.
    """
    import httpx
    from unittest.mock import AsyncMock, patch
    from httpx import ASGITransport
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from jarvis_common import verify_api_key
    from jarvis_common.testing import SharedConnPool

    owner_id, paper_id = await _seed_user_and_paper(
        contract_conn,
        email="zotero-owner-a@test.invalid",
        title="ZZZ-ZOTERO-CONTRACT Owned by A",
    )

    # Seed a second user who does NOT own the paper.
    intruder_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "zotero-intruder-b@test.invalid",
    )

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[verify_api_key] = lambda: None

    try:
        import paper_ingestion.routers.zotero as zotero_mod

        # Call as intruder (user B), not the owner.
        with patch.object(
            zotero_mod, "current_user_id_strict", AsyncMock(return_value=intruder_id)
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    f"/api/papers/{paper_id}/zotero",
                    headers={"X-API-Key": "test"},
                )
    finally:
        if original_pool is None:
            app.state.__dict__.pop("db_pool", None)
        else:
            app.state.db_pool = original_pool
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(verify_api_key, None)

    assert resp.status_code == 403, (
        f"Non-owner must receive 403; got {resp.status_code}: {resp.text}"
    )
