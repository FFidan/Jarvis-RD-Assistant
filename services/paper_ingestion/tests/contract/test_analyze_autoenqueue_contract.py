"""Contract tests for the paper.analyze auto-enqueue side-effect on save paths.

Covers three entry points exercised via the ASGI app:
  - PUT  /api/papers/{id}/save           (papers_lifecycle.py::save_paper)
  - POST /api/pulse/rate  rating="save"  (pulse.py::rate_card)
  - POST /api/papers/batch-save          (papers_detail.py::batch_save_papers)

Each path defers ``paper.analyze`` only when a paper has a usable PDF source and
has no ``paper_chunks`` yet. Papers without either a remote PDF URL or a local
PDF path are saved normally without scheduling a job that must fail.

Verified:
  papers_service.py::find_papers_needing_analysis — shared source/chunk gate
  papers_lifecycle.py::save_paper                 — per-paper Save consumer
  pulse.py::rate_card                             — Pulse Save consumer
  papers_detail.py::batch_save_papers             — deduplicated batch consumer
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import jarvis_common.task_registry as task_registry
import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool
from jarvis_common.testing_auth import SignedIdentityMiddleware
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

    Removes the autouse identity overrides so session-cookie auth
    (contract_two_users) works.
    """
    from jarvis_common import (
        current_user_id_strict_with_owner_override,
        get_current_user_id,
    )
    from paper_ingestion.main import app

    shared = SharedConnPool(
        contract_conn,
        session_authorization="jarvis_research_runtime",
    )
    with (
        patch_app_state(app, {"db_pool": shared}),
        patch_dependency_overrides(
            app,
            remove_overrides={
                current_user_id_strict_with_owner_override,
                get_current_user_id,
            },
        ),
    ):
        yield SignedIdentityMiddleware(
            app,
            audience="research",
            session_pool=shared.with_session_authorization("jarvis_platform_runtime"),
        )


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
# Test 1 — PUT /api/papers/{id}/save: enqueues with a PDF source and no chunks
#
# Fixture paper_id_a has state='to_read' (allowed by save_paper) and no
# paper_chunks row (seeded by _seed_resources without any chunk data).
# Verified: papers_lifecycle.py:54 — allowed=("inbox","done","to_read","reading")
# The shared helper owns source/chunk eligibility; this route owns the defer.
# ---------------------------------------------------------------------------


async def test_save_enqueues_analyze_when_unprocessed(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/save: defers analysis for an unprocessed PDF source.

    The source/chunk gate and defer payload are both exercised through ASGI.
    """
    paper_id = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id

    # Ensure no paper_chunks row exists for this paper (it shouldn't by default,
    # but delete any that might exist from other tests touching the same paper).
    await contract_conn.execute(
        "DELETE FROM paper_chunks WHERE paper_id = $1",
        paper_id,
    )
    await contract_conn.execute(
        "UPDATE papers SET pdf_url = 'https://arxiv.org/pdf/2608.00001' WHERE id = $1",
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
    mock_defer.assert_awaited_once()
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
# Inserts a minimal paper_chunks row for paper_id_a, making it ineligible.
# ---------------------------------------------------------------------------


async def test_save_skips_analyze_when_already_processed(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/save: does NOT defer paper.analyze when paper_chunks row exists.

    Existing chunks make the paper ineligible even when it has a PDF source.
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
    mock_defer.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 3b — PUT /api/papers/{id}/save: enqueue failure is best-effort
#
# save_paper wraps the defer_async call in try/except and logs via
# logger.exception — the response must still be 200 even when the broker
# is down.  Mirrors test_star_zotero_push_trigger.py::test_star_enqueue_failure_is_best_effort.
# The route deliberately treats scheduling as best-effort after saving state.
# ---------------------------------------------------------------------------


async def test_save_analyze_enqueue_failure_is_best_effort(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """PUT /api/papers/{id}/save: 200 even when paper.analyze defer_async raises.

    A scheduling failure does not roll back the successful Save transition.
    """
    paper_id = contract_two_users.paper_id_a

    # Ensure no paper_chunks row so the enqueue path is taken.
    await contract_conn.execute(
        "DELETE FROM paper_chunks WHERE paper_id = $1",
        paper_id,
    )
    await contract_conn.execute(
        "UPDATE papers SET pdf_url = 'https://arxiv.org/pdf/2608.00002' WHERE id = $1",
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
# Existing chunks make the Pulse paper ineligible. Mirrors the per-paper Save test.
# ---------------------------------------------------------------------------


async def test_pulse_rate_save_skips_when_already_processed(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """POST /api/pulse/rate rating='save': does NOT defer paper.analyze when paper_chunks exists.

    The shared helper returns no scheduling candidate once chunks exist.
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
    mock_defer.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 3 — POST /api/pulse/rate rating="save": enqueues when no chunks
#
# The fixture paper_id_a is already linked to user A's pulse deck
# (seeded by _seed_resources → pulse_cards + pulse_decks), so the deck
# membership guard passes. A PDF source plus no paper_chunks permits the defer.
# The request crosses the deck guard, Save transition, shared gate, and defer.
# ---------------------------------------------------------------------------


async def test_pulse_rate_save_enqueues_analyze(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """POST /api/pulse/rate rating='save': defers an unprocessed PDF source.

    The route receives one candidate from the shared source/chunk gate.
    """
    paper_id = contract_two_users.paper_id_a

    # Ensure no paper_chunks row (deck guard passes because pulse_card exists for this paper).
    await contract_conn.execute(
        "DELETE FROM paper_chunks WHERE paper_id = $1",
        paper_id,
    )
    await contract_conn.execute(
        "UPDATE papers SET pdf_url = 'https://arxiv.org/pdf/2608.00003' WHERE id = $1",
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
    mock_defer.assert_awaited_once()
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
# Test 4 — POST /api/papers/batch-save: enqueues analyzable, unchunked papers
#
# batch_save_papers issues one shared eligibility query, then defers once per
# unique analyzable paper.
# ---------------------------------------------------------------------------


async def test_batch_save_enqueues_analyze_for_unchunked_papers(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """POST /api/papers/batch-save: defers only analyzable papers without chunks.

    Two papers are saved; one is pre-seeded with a paper_chunks row.
    Asserts: unchunked paper IS enqueued, already-chunked paper is NOT.

    The request exercises both deduplication and shared eligibility through ASGI.
    """
    payload = [
        {
            "external_id": "autoenqueue-contract-test-ext-001",
            "source_type": "arxiv",
            "title": "AutoEnqueue Contract Test Paper One",
            "authors": ["Test Author"],
            "url": "https://autoenqueue.contract.test/001",
            "pdf_url": "https://arxiv.org/pdf/2608.00004",
        },
        {
            "external_id": "autoenqueue-contract-test-ext-002",
            "source_type": "arxiv",
            "title": "AutoEnqueue Contract Test Paper Two",
            "authors": ["Test Author"],
            "url": "https://autoenqueue.contract.test/002",
            "pdf_url": "https://arxiv.org/pdf/2608.00005",
        },
    ]

    # First save both papers to get their ids (no mock — we need real ids).
    mock_task_first, _ = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task_first}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as c:
            setup_resp = await c.post("/api/papers/batch-save", json=payload)
    assert setup_resp.status_code == 200
    body_setup = setup_resp.json()
    assert len(body_setup) == 2
    paper_id_by_ext = {p["external_id"]: p["id"] for p in body_setup}
    chunked_id = paper_id_by_ext["autoenqueue-contract-test-ext-001"]
    unchunked_id = paper_id_by_ext["autoenqueue-contract-test-ext-002"]

    # Pre-seed a chunk for ext-001 so its id appears in paper_chunks.
    await contract_conn.execute(
        """INSERT INTO paper_chunks (paper_id, chunk_index, content)
           VALUES ($1, 0, 'contract-test-chunk-batch')
           ON CONFLICT (paper_id, chunk_index) DO NOTHING""",
        chunked_id,
    )
    # Ensure ext-002 has no chunks.
    await contract_conn.execute(
        "DELETE FROM paper_chunks WHERE paper_id = $1",
        unchunked_id,
    )

    # Re-save the same two papers; the batch check should skip chunked_id.
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

    # Only the unchunked paper must be enqueued.
    assert mock_defer.await_count == 1, (
        f"Expected defer_async called once (only for unchunked paper); got {mock_defer.await_count} calls"
    )
    deferred_paper_ids = {call.kwargs.get("paper_id") for call in mock_defer.await_args_list}
    assert deferred_paper_ids == {unchunked_id}, (
        f"Only unchunked paper {unchunked_id} should be enqueued; got {deferred_paper_ids}"
    )
    assert chunked_id not in deferred_paper_ids, (
        f"Already-chunked paper {chunked_id} must NOT be re-enqueued"
    )
    user_a_id = contract_two_users.user_a_id
    for call in mock_defer.await_args_list:
        assert call.kwargs.get("user_id") == user_a_id, (
            f"defer_async must receive correct user_id={user_a_id}; got {call.kwargs}"
        )
        assert "job_id" in call.kwargs, f"defer_async must receive job_id kwarg; got {call.kwargs}"


async def test_batch_save_duplicate_entries_enqueue_analyze_once(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """POST /api/papers/batch-save: duplicate external_id entries defer one analyze.

    A payload naming the same external_id twice resolves to one canonical
    paper, so the enqueue loop must defer exactly one paper.analyze job for
    it — not one per duplicate entry.

    The deduplicated candidate set must produce only one defer.
    """
    entry = {
        "external_id": "autoenqueue-contract-dup-ext",
        "source_type": "arxiv",
        "title": "AutoEnqueue Duplicate Entry Paper",
        "authors": ["Test Author"],
        "url": "https://autoenqueue.contract.test/dup",
        "pdf_url": "https://arxiv.org/pdf/2608.00006",
    }
    mock_task, mock_defer = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as c:
            resp = await c.post("/api/papers/batch-save", json=[entry, dict(entry)])

    assert resp.status_code == 200, (
        f"Expected 200 from POST /api/papers/batch-save; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert isinstance(body, list) and len(body) == 2, (
        f"Both duplicate entries must still be echoed; got: {body!r}"
    )
    assert mock_defer.await_count == 1, (
        f"Expected exactly one defer_async for the duplicated paper; got {mock_defer.await_count}"
    )
    deferred_paper_ids = [call.kwargs.get("paper_id") for call in mock_defer.await_args_list]
    assert deferred_paper_ids == [body[0]["id"]], (
        f"The single deferred job must target the canonical paper; got {deferred_paper_ids}"
    )


async def test_save_without_pdf_source_does_not_enqueue_analyze(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """Saving metadata-only discovery must not schedule a doomed analysis job."""
    paper_id = contract_two_users.paper_id_a
    await contract_conn.execute("DELETE FROM paper_chunks WHERE paper_id = $1", paper_id)
    await contract_conn.execute(
        "UPDATE papers SET pdf_url = NULL, pdf_local_path = NULL WHERE id = $1",
        paper_id,
    )

    mock_task, mock_defer = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as client:
            response = await client.put(f"/api/papers/{paper_id}/save")

    assert response.status_code == 200
    mock_defer.assert_not_awaited()


async def test_pulse_save_without_pdf_source_does_not_enqueue_analyze(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
    contract_conn,
):
    """Pulse Save preserves metadata-only cards without creating a failed job."""
    paper_id = contract_two_users.paper_id_a
    await contract_conn.execute("DELETE FROM paper_chunks WHERE paper_id = $1", paper_id)
    await contract_conn.execute(
        "UPDATE papers SET pdf_url = '   ', pdf_local_path = '   ' WHERE id = $1",
        paper_id,
    )

    mock_task, mock_defer = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as client:
            response = await client.post(
                "/api/pulse/rate",
                json={"paper_id": paper_id, "rating": "save"},
            )

    assert response.status_code == 200
    mock_defer.assert_not_awaited()


async def test_batch_save_without_pdf_source_does_not_enqueue_analyze(
    contract_two_users,
    _autoenqueue_app,
    _configure_api_key,
):
    """Batch Save must not analyze a bibliographic record with no PDF source."""
    payload = [
        {
            "external_id": "autoenqueue-contract-pdfless",
            "source_type": "pubmed",
            "title": "Metadata-only discovery",
            "authors": ["Test Author"],
            "url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
        }
    ]

    mock_task, mock_defer = _mock_analyze_task()
    with patch.dict(task_registry._TASK_MAP, {"paper.analyze": mock_task}):
        async with _make_client(_autoenqueue_app, contract_two_users.cookie_a) as client:
            response = await client.post("/api/papers/batch-save", json=payload)

    assert response.status_code == 200
    assert len(response.json()) == 1
    mock_defer.assert_not_awaited()
