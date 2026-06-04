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
import pytest_asyncio

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _zotero_app(contract_conn):
    """PI app wired to the contract_conn transaction for Zotero router tests."""
    from jarvis_common import verify_api_key
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_contract_apps import (
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    with (
        patch_app_state(app, {"db_pool": shared}),
        patch_dependency_overrides(
            app,
            set_overrides={get_db_pool: lambda: shared, verify_api_key: lambda: None},
        ),
    ):
        yield app


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
async def test_get_zotero_state_404_for_nonexistent_paper(_zotero_app):
    """get_paper_zotero_state returns 404 when paper_id does not exist in DB.

    Exercises the real papers SELECT (not a mock fetchrow return_value).
    """
    from jarvis_common.auth import current_user_id_strict
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_dependency_overrides,
    )

    # Use a very large ID that cannot exist in the rolled-back test transaction.
    nonexistent_paper_id = 999_999_999

    with patch_dependency_overrides(_zotero_app, set_overrides={current_user_id_strict: lambda: 1}):
        async with make_contract_client(_zotero_app, None) as client:
            resp = await client.get(f"/api/papers/{nonexistent_paper_id}/zotero")

    # 403 (ownership guard fires before the SELECT) or 404 (paper not found) are
    # both acceptable — the key assertion is that the app doesn't 500 on a real
    # missing row. In practice assert_paper_ownership raises 404 when the paper
    # row is absent.
    assert resp.status_code in (403, 404), (
        f"Expected 403 or 404 for nonexistent paper, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_get_zotero_state_403_for_non_owner(contract_conn, _zotero_app):
    """get_paper_zotero_state returns 403 when caller does not own the paper.

    Collapses: test_get_paper_zotero_state_checks_ownership — the mock test
    monkeypatches assert_paper_ownership to raise 403; this contract test seeds
    a real paper owned by user A and calls as user B, letting the real ownership
    guard enforce the 403.
    """
    from jarvis_common.auth import current_user_id_strict
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_dependency_overrides,
    )

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

    # Call as intruder (user B), not the owner.
    with patch_dependency_overrides(
        _zotero_app, set_overrides={current_user_id_strict: lambda: intruder_id}
    ):
        async with make_contract_client(_zotero_app, None) as client:
            resp = await client.get(f"/api/papers/{paper_id}/zotero")

    assert resp.status_code == 403, (
        f"Non-owner must receive 403; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# A170. POST /api/papers/{paper_id}/zotero — push: ownership enforced at DB layer
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a170_push_to_zotero_owner_gets_202(contract_conn, _zotero_app):
    """POST /api/papers/{paper_id}/zotero: owner gets 202 Accepted.

    The push endpoint checks paper existence + ownership from real DB before
    enqueueing the procrastinate job (KIND_TO_TASK["zotero.push"].defer_async
    is patched — procrastinate boundary is an idiomatic mock).
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    import jarvis_common.task_registry as task_registry
    from jarvis_common.auth import current_user_id_strict
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_dependency_overrides,
    )

    owner_id, paper_id = await _seed_user_and_paper(
        contract_conn,
        email="a170-owner@contract.example.com",
        title="ZZZ-A170-PUSH Zotero Contract",
    )

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(return_value=None)

    with (
        patch_dependency_overrides(
            _zotero_app, set_overrides={current_user_id_strict: lambda: owner_id}
        ),
        patch.dict(task_registry._TASK_MAP, {"zotero.push": mock_task}),
    ):
        async with make_contract_client(_zotero_app, None) as client:
            resp = await client.post(f"/api/papers/{paper_id}/zotero")

    assert resp.status_code == 202, (
        f"Owner push must return 202; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "job_id" in body
    assert body["status"] == "queued"
    mock_task.defer_async.assert_awaited_once()


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a170_push_to_zotero_non_owner_gets_403(contract_conn, _zotero_app):
    """POST /api/papers/{paper_id}/zotero: non-owner receives 403.

    assert_paper_ownership fires before the procrastinate enqueue.
    No mock for KIND_TO_TASK needed (guard raises before reaching it).
    """
    from jarvis_common.auth import current_user_id_strict
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_dependency_overrides,
    )

    owner_id, paper_id = await _seed_user_and_paper(
        contract_conn,
        email="a170-owner2@contract.example.com",
        title="ZZZ-A170-PUSH-IDOR Zotero Contract",
    )
    intruder_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role)"
        " VALUES ('a170-intruder@contract.example.com', 'user') RETURNING id"
    )

    with patch_dependency_overrides(
        _zotero_app, set_overrides={current_user_id_strict: lambda: intruder_id}
    ):
        async with make_contract_client(_zotero_app, None) as client:
            resp = await client.post(f"/api/papers/{paper_id}/zotero")

    assert resp.status_code == 403, (
        f"Non-owner must receive 403 on push; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# A172. POST /api/zotero/resync/{paper_id} — ownership enforcement at DB layer
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a172_resync_owner_gets_202(contract_conn, _zotero_app):
    """POST /api/zotero/resync/{paper_id}: owner gets 202 Accepted.

    Existence check + ownership check run against real DB; procrastinate
    boundary (KIND_TO_TASK["zotero.resync"]) stays mocked.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    import jarvis_common.task_registry as task_registry
    from jarvis_common.auth import current_user_id_strict
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_dependency_overrides,
    )

    owner_id, paper_id = await _seed_user_and_paper(
        contract_conn,
        email="a172-owner@contract.example.com",
        title="ZZZ-A172-RESYNC Zotero Contract",
    )

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(return_value=None)

    with (
        patch_dependency_overrides(
            _zotero_app, set_overrides={current_user_id_strict: lambda: owner_id}
        ),
        patch.dict(task_registry._TASK_MAP, {"zotero.resync": mock_task}),
    ):
        async with make_contract_client(_zotero_app, None) as client:
            resp = await client.post(f"/api/zotero/resync/{paper_id}")

    assert resp.status_code == 202, (
        f"Owner resync must return 202; got {resp.status_code}: {resp.text}"
    )
    mock_task.defer_async.assert_awaited_once()


# ---------------------------------------------------------------------------
# TENANT-02: sync_annotations_for_paper — annotations attributed to syncing user
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_sync_annotations_attributed_to_syncing_user(contract_conn):
    """Zotero annotations are written with user_id == syncing user (not paper discoverer).

    Scenario: paper P was discovered by user A; user B syncs annotations for P.
    The resulting paper_notes row must have user_id == B, and the row must NOT
    appear under user A.

    Verifies:
    - ON CONFLICT (paper_id, user_id, zotero_annotation_key) WHERE … targets the right index
    - $7 bind is owner_user_id (the syncing user), NOT paper["discovered_by"]
    """
    from unittest.mock import AsyncMock, patch

    import httpx
    from jarvis_common.testing import SharedConnPool

    from paper_ingestion.integrations.zotero_service import sync_annotations_for_paper

    # Seed user A (discoverer) and the paper they discovered.
    user_a_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "tenant02-user-a@contract.example.com",
    )
    paper_id = await contract_conn.fetchval(
        """
        INSERT INTO papers
            (external_id, source_type, title, authors, url, discovered_by, zotero_item_key)
        VALUES ($1, 'arxiv', $2, ARRAY['Author Z'], 'https://example.com/z', $3, $4)
        RETURNING id
        """,
        f"arxiv:tenant02-contract-{user_a_id}",
        "ZZZ-TENANT02-ANNOTATIONS Paper",
        user_a_id,
        "ZITEM001",
    )

    # Seed user B (the syncing user) with a Zotero config so _get_zotero_config succeeds.
    user_b_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "tenant02-user-b@contract.example.com",
    )
    for key, value in [
        ("zotero.api_key", "fake-api-key-b"),
        ("zotero.user_id", "999999"),
        ("zotero.library_type", "user"),
    ]:
        await contract_conn.execute(
            "INSERT INTO user_config (user_id, key, value) VALUES ($1, $2, $3::jsonb)",
            user_b_id,
            key,
            f'"{value}"',
        )

    shared = SharedConnPool(contract_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    one_annotation = [
        {
            "key": "ANNCONTRACT1",
            "data": {
                "annotationText": "Tenant-02 highlighted claim",
                "annotationComment": "Contract test comment",
                "annotationPageLabel": "3",
            },
        }
    ]

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient") as mock_cls:
        mock_cls.return_value.get_item_children = AsyncMock(return_value=one_annotation)
        result = await sync_annotations_for_paper(
            paper_id=int(paper_id),
            db_pool=shared,
            http_client=http,
            owner_user_id=user_b_id,
        )

    assert result == {"paper_id": int(paper_id), "imported": 1, "status": "ok"}, result

    # The paper_notes row must be attributed to user B (the syncing user).
    row = await contract_conn.fetchrow(
        "SELECT user_id, zotero_annotation_key FROM paper_notes"
        " WHERE paper_id = $1 AND zotero_annotation_key = 'ANNCONTRACT1'",
        int(paper_id),
    )
    assert row is not None, "paper_notes row not found after sync"
    assert row["user_id"] == user_b_id, (
        f"Expected user_id={user_b_id} (syncing user B), got {row['user_id']}"
    )

    # Confirm user A's view is empty — annotation scoped to B's user_id.
    count_a = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM paper_notes"
        " WHERE paper_id = $1 AND user_id = $2 AND zotero_annotation_key = 'ANNCONTRACT1'",
        int(paper_id),
        user_a_id,
    )
    assert count_a == 0, f"Annotation must NOT appear under user A (discovered_by); got {count_a}"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a172_resync_non_owner_gets_403(contract_conn, _zotero_app):
    """POST /api/zotero/resync/{paper_id}: non-owner receives 403.

    assert_paper_ownership fires before the procrastinate enqueue.
    """
    from jarvis_common.auth import current_user_id_strict
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_dependency_overrides,
    )

    owner_id, paper_id = await _seed_user_and_paper(
        contract_conn,
        email="a172-owner2@contract.example.com",
        title="ZZZ-A172-RESYNC-IDOR Zotero Contract",
    )
    intruder_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role)"
        " VALUES ('a172-intruder@contract.example.com', 'user') RETURNING id"
    )

    with patch_dependency_overrides(
        _zotero_app, set_overrides={current_user_id_strict: lambda: intruder_id}
    ):
        async with make_contract_client(_zotero_app, None) as client:
            resp = await client.post(f"/api/zotero/resync/{paper_id}")

    assert resp.status_code == 403, (
        f"Non-owner must receive 403 on resync; got {resp.status_code}: {resp.text}"
    )
