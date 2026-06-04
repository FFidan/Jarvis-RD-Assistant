"""Contract tests for the paper.analyze auto-enqueue side-effect on save paths.

Covers three entry points exercised via the ASGI app:
  - PUT  /api/papers/{id}/save           (papers_lifecycle.py::save_paper)
  - POST /api/pulse/rate  rating="save"  (pulse.py::rate_card)
  - POST /api/papers/batch-save          (papers_detail.py::batch_save_papers)

Each path defers ``paper.analyze`` guarded by a ``paper_chunks`` existence check
so already-processed papers are skipped.  The batch path relies on in-job
idempotency and does NOT add a per-paper pre-check query.

Verified:
  papers_lifecycle.py:41-69  — save_paper chunk-exists guard + defer
  pulse.py:220-284           — rate_card "save" branch + defer
  papers_detail.py:163-217   — batch_save_papers per-paper defer (no pre-check)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import jarvis_common.task_registry as task_registry
import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool
from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
    patch_app_state,
    patch_dependency_overrides,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# App fixture — mirrors conftest._pi_app_with_pool but defined locally so
# this file is self-contained and the loop_scope is explicit.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _autoenqueue_app(contract_conn):
    """paper_ingestion app wired to the contract conn pool.

    Removes the autouse ``current_user_id_strict_with_owner_override`` override
    so that session-cookie auth (contract_two_users) works.
    """
    from jarvis_common import current_user_id_strict_with_owner_override
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    with (
        patch_app_state(app, {"db_pool": shared}),
        patch_dependency_overrides(
            app, remove_overrides={current_user_id_strict_with_owner_override}
        ),
    ):
        yield app


# ---------------------------------------------------------------------------
# Helper — build a (mock_task, mock_defer) pair for patch.dict injection.
# KIND_TO_TASK is a MappingProxyType over task_registry._TASK_MAP; patching
# _TASK_MAP is visible to the handler via KIND_TO_TASK at call time.
# Verified: libs/jarvis_common/jarvis_common/task_registry.py (KIND_TO_TASK)
# ---------------------------------------------------------------------------


def _mock_analyze_task() -> tuple[MagicMock, AsyncMock]:
    mock_task = MagicMock()
    mock_defer = AsyncMock()
    mock_task.defer_async = mock_defer
    return mock_task, mock_defer


# ---------------------------------------------------------------------------
# Test 1 — PUT /api/papers/{id}/save: enqueues when paper has no chunks
#
# Fixture paper_id_a has state='to_read' (allowed by save_paper) and no
# paper_chunks row (seeded by _seed_resources without any chunk data).
# Verified: papers_lifecycle.py:54 — allowed=("inbox","done","to_read","reading")
# Verified: papers_lifecycle.py:57-68 — chunk-exists guard + defer_async call
# ---------------------------------------------------------------------------


async def test_save_enqueues_analyze_when_unprocessed(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/save: defers paper.analyze when no paper_chunks row exists.

    Verified: papers_lifecycle.py:57-68 — fetchval EXISTS + defer_async when False.
    """
    paper_id = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id

    # Ensure no paper_chunks row exists for this paper (it shouldn't by default,
    # but delete any that might exist from other tests touching the same paper).
    await contract_conn.execute(
        "DELETE FROM paper_chunks WHERE paper_id = $1",
        paper_id,
    )

    mock_task, mock_defer = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as c:
            resp = await c.put(f"/api/papers/{paper_id}/save")

    assert resp.status_code == 200, (
        f"Expected 200 from PUT /save; got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json() == {"status": "ok", "paper_id": paper_id}, (
        f"Unexpected response body: {resp.json()}"
    )
    mock_defer.assert_awaited_once()  # Verified: papers_lifecycle.py:64
    call_kwargs = mock_defer.await_args.kwargs
    assert call_kwargs.get("paper_id") == paper_id, (
        f"defer_async must receive paper_id={paper_id}; got kwargs={call_kwargs}"
    )
    assert call_kwargs.get("user_id") == user_a_id, (
        f"defer_async must receive user_id={user_a_id}; got kwargs={call_kwargs}"
    )
    assert "job_id" in call_kwargs, (
        f"defer_async must receive job_id kwarg; got kwargs={call_kwargs}"
    )


# ---------------------------------------------------------------------------
# Test 2 — PUT /api/papers/{id}/save: skips enqueue when already processed
#
# Inserts a minimal paper_chunks row for paper_id_a; the EXISTS check returns
# True → defer_async must NOT be called.
# Verified: papers_lifecycle.py:57-60 — already_processed guard skips the block
# ---------------------------------------------------------------------------


async def test_save_skips_analyze_when_already_processed(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/save: does NOT defer paper.analyze when paper_chunks row exists.

    Verified: papers_lifecycle.py:57-60 — chunk EXISTS → skip defer block.
    """
    paper_id = contract_two_users.paper_id_a

    # Insert a minimal paper_chunks row so EXISTS returns true.
    # Required NOT NULL columns: chunk_index, content. paper_id is FK → papers.id.
    # Verified: db/init.sql:701-713 — paper_chunks DDL (chunk_index NOT NULL, content NOT NULL).
    await contract_conn.execute(
        """INSERT INTO paper_chunks (paper_id, chunk_index, content)
           VALUES ($1, 0, 'contract-test-chunk')
           ON CONFLICT (paper_id, chunk_index) DO NOTHING""",
        paper_id,
    )

    mock_task, mock_defer = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as c:
            resp = await c.put(f"/api/papers/{paper_id}/save")

    assert resp.status_code == 200, (
        f"Expected 200 from PUT /save (already processed); got {resp.status_code}: {resp.text[:300]}"
    )
    mock_defer.assert_not_awaited()  # Verified: papers_lifecycle.py:57-60


# ---------------------------------------------------------------------------
# Test 3b — PUT /api/papers/{id}/save: enqueue failure is best-effort
#
# save_paper wraps the defer_async call in try/except and logs via
# logger.exception — the response must still be 200 even when the broker
# is down.  Mirrors test_star_zotero_push_trigger.py::test_star_enqueue_failure_is_best_effort.
# Verified: papers_lifecycle.py:60-68 — try/except around defer_async
# ---------------------------------------------------------------------------


async def test_save_analyze_enqueue_failure_is_best_effort(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/save: 200 even when paper.analyze defer_async raises.

    Verified: papers_lifecycle.py:60-68 — enqueue wrapped in try/except; RuntimeError swallowed.
    """
    paper_id = contract_two_users.paper_id_a

    # Ensure no paper_chunks row so the enqueue path is taken.
    await contract_conn.execute(
        "DELETE FROM paper_chunks WHERE paper_id = $1",
        paper_id,
    )

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(side_effect=RuntimeError("broker down"))
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as c:
            resp = await c.put(f"/api/papers/{paper_id}/save")

    # Save must succeed even though enqueue failed.
    assert resp.status_code == 200, (
        f"Expected 200 even with broker down; got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json() == {"status": "ok", "paper_id": paper_id}
    mock_task.defer_async.assert_awaited_once()  # attempt was made


# ---------------------------------------------------------------------------
# Test 3c — POST /api/pulse/rate rating="save": skips enqueue when already processed
#
# Inserts a minimal paper_chunks row for paper_id_a; the EXISTS check returns
# True → defer_async must NOT be called.  Mirrors test_save_skips_analyze_when_already_processed.
# Verified: pulse.py:258-263 — should_analyze=False when chunks exist → skip defer
# ---------------------------------------------------------------------------


async def test_pulse_rate_save_skips_when_already_processed(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """POST /api/pulse/rate rating='save': does NOT defer paper.analyze when paper_chunks exists.

    Verified: pulse.py:258-263 — chunk EXISTS guard → should_analyze=False → skip defer.
    """
    paper_id = contract_two_users.paper_id_a

    # Insert a minimal paper_chunks row so EXISTS returns true.
    # Verified: db/init.sql — paper_chunks DDL (chunk_index NOT NULL, content NOT NULL).
    await contract_conn.execute(
        """INSERT INTO paper_chunks (paper_id, chunk_index, content)
           VALUES ($1, 0, 'contract-test-chunk-pulse')
           ON CONFLICT (paper_id, chunk_index) DO NOTHING""",
        paper_id,
    )

    mock_task, mock_defer = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as c:
            resp = await c.post(
                "/api/pulse/rate",
                json={"paper_id": paper_id, "rating": "save"},
            )

    assert resp.status_code == 200, (
        f"Expected 200 from POST /api/pulse/rate (already processed); got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body.get("status") == "ok", f"Expected status='ok'; got {body}"
    mock_defer.assert_not_awaited()  # Verified: pulse.py chunk-exists guard


# ---------------------------------------------------------------------------
# Test 3 — POST /api/pulse/rate rating="save": enqueues when no chunks
#
# The fixture paper_id_a is already linked to user A's pulse deck
# (seeded by _seed_resources → pulse_cards + pulse_decks), so the deck
# membership guard passes.  No paper_chunks → should_analyze=True → defer.
# Verified: pulse.py:241-283 — deck guard + "save" branch + defer_async
# ---------------------------------------------------------------------------


async def test_pulse_rate_save_enqueues_analyze(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """POST /api/pulse/rate rating='save': defers paper.analyze when no paper_chunks row.

    Verified: pulse.py:258-263 — save branch upserts state + chunk-exists guard.
    Verified: pulse.py:277-283 — should_analyze=True → defer_async called.
    """
    paper_id = contract_two_users.paper_id_a

    # Ensure no paper_chunks row (deck guard passes because pulse_card exists for this paper).
    await contract_conn.execute(
        "DELETE FROM paper_chunks WHERE paper_id = $1",
        paper_id,
    )

    mock_task, mock_defer = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as c:
            # Request body shape: {"paper_id": int, "rating": str}
            # Verified: pulse.py:220-225 — PulseRateRequest body parameter
            # Verified: test_pulse_contract.py:165 — {"paper_id": ..., "rating": "open"} shape
            resp = await c.post(
                "/api/pulse/rate",
                json={"paper_id": paper_id, "rating": "save"},
            )

    assert resp.status_code == 200, (
        f"Expected 200 from POST /api/pulse/rate; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body.get("status") == "ok", f"Expected status='ok'; got {body}"
    mock_defer.assert_awaited_once()  # Verified: pulse.py:279
    call_kwargs = mock_defer.await_args.kwargs
    assert call_kwargs.get("paper_id") == paper_id, (
        f"defer_async must receive paper_id={paper_id}; got kwargs={call_kwargs}"
    )
    assert call_kwargs.get("user_id") == contract_two_users.user_a_id, (
        f"defer_async must receive user_id; got kwargs={call_kwargs}"
    )
    assert "job_id" in call_kwargs, (
        f"defer_async must receive job_id kwarg; got kwargs={call_kwargs}"
    )


# ---------------------------------------------------------------------------
# Test 4 — POST /api/papers/batch-save: enqueues analyze per saved paper
#
# batch_save_papers defers paper.analyze for every paper it upserts — no
# pre-check (relies on in-job idempotency).
# Verified: papers_detail.py:208-216 — per-item defer_async loop
# ---------------------------------------------------------------------------


async def test_batch_save_enqueues_analyze_per_paper(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """POST /api/papers/batch-save: defers paper.analyze once per saved paper.

    Verified: papers_detail.py:208-216 — per-item defer_async in post-transaction loop.
    Request body shape: list of PaperCreate objects.
    Verified: papers_detail.py:163-167 — list[PaperCreate] Body() parameter.
    Verified: test_papers_contract.py:598-611 — existing batch-save contract test shape.
    """
    payload = [
        {
            "external_id": "autoenqueue-contract-test-ext-001",
            "source_type": "arxiv",
            "title": "AutoEnqueue Contract Test Paper One",
            "authors": ["Test Author"],
            "url": "https://autoenqueue.contract.test/001",
        },
        {
            "external_id": "autoenqueue-contract-test-ext-002",
            "source_type": "arxiv",
            "title": "AutoEnqueue Contract Test Paper Two",
            "authors": ["Test Author"],
            "url": "https://autoenqueue.contract.test/002",
        },
    ]

    mock_task, mock_defer = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/papers/batch-save", json=payload)

    assert resp.status_code == 200, (
        f"Expected 200 from POST /api/papers/batch-save; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert isinstance(body, list) and len(body) == 2, (
        f"Expected list of 2 PaperResponse; got: {body!r}"
    )

    # One defer per saved paper — Verified: papers_detail.py:208-216
    assert mock_defer.await_count == 2, (
        f"Expected defer_async called twice (once per paper); got {mock_defer.await_count} calls"
    )
    saved_ids = {p["id"] for p in body}
    deferred_paper_ids = {call.kwargs.get("paper_id") for call in mock_defer.await_args_list}
    assert deferred_paper_ids == saved_ids, (
        f"defer_async paper_ids {deferred_paper_ids} must match saved paper ids {saved_ids}"
    )
    user_a_id = contract_two_users.user_a_id
    for call in mock_defer.await_args_list:
        assert call.kwargs.get("user_id") == user_a_id, (
            f"defer_async must receive correct user_id={user_a_id}; got {call.kwargs}"
        )
        assert "job_id" in call.kwargs, f"defer_async must receive job_id kwarg; got {call.kwargs}"
