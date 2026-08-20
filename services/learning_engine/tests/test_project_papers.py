"""Contract tests for project↔paper linking.

``link_paper`` must hand the automatic Zotero push to the paper_ingestion
worker that owns the ``zotero.push`` handler. Because that handler is absent
from learning_engine's in-process task registry, the enqueue goes through the
shared procrastinate app by name and MUST target ``queue="paper_ingestion"`` —
the queue paper_ingestion's worker consumes. A registry lookup that skips, or a
wrong queue, silently drops the push; both are asserted against here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from jarvis_common import task_registry
from jarvis_common.testing_contract_apps import make_contract_client as _client

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_research_library_command_is_exact_and_acknowledged(monkeypatch, tmp_path) -> None:
    """Learning requests one route-bound Research mutation and validates its reply."""
    import learning_engine.routers.project_papers as project_papers

    token_file = tmp_path / "learning-token"
    token_file.write_text("learning-secret\n", encoding="utf-8")
    monkeypatch.setattr(
        project_papers,
        "get_learning_engine_settings",
        lambda: SimpleNamespace(
            platform_api_url="http://platform.test",
            paper_ingestion_url="http://research.test",
            learning_service_token_file=token_file,
        ),
    )
    authorize = AsyncMock(return_value={"X-Jarvis-Identity": "signed"})
    monkeypatch.setattr(project_papers, "authorize_service_command", authorize)
    response = MagicMock()
    response.json.return_value = {"acknowledged": True}
    client = AsyncMock()
    client.post.return_value = response
    request = MagicMock()
    request.app.state.http_client = client

    await project_papers._add_to_research_library(request, user_id=7, paper_id=42)

    command = authorize.await_args.kwargs["command"]
    assert command.audience == "research"
    assert command.method == "POST"
    assert command.path == "/internal/domains/library"
    assert command.user_id == 7
    client.post.assert_awaited_once_with(
        "http://research.test/internal/domains/library",
        headers={"X-Jarvis-Identity": "signed"},
        json={
            "request_id": command.request_id,
            "user_id": 7,
            "paper_id": 42,
        },
        timeout=10.0,
    )


async def test_research_library_command_rejects_malformed_acknowledgement(
    monkeypatch, tmp_path
) -> None:
    """Learning reports deterministic unavailability for a malformed owner reply."""
    import learning_engine.routers.project_papers as project_papers

    token_file = tmp_path / "learning-token"
    token_file.write_text("learning-secret", encoding="utf-8")
    monkeypatch.setattr(
        project_papers,
        "get_learning_engine_settings",
        lambda: SimpleNamespace(
            platform_api_url="http://platform.test",
            paper_ingestion_url="http://research.test",
            learning_service_token_file=token_file,
        ),
    )
    monkeypatch.setattr(
        project_papers,
        "authorize_service_command",
        AsyncMock(return_value={"X-Jarvis-Identity": "signed"}),
    )
    response = MagicMock()
    response.json.return_value = {"acknowledged": False}
    request = MagicMock()
    request.app.state.http_client = AsyncMock()
    request.app.state.http_client.post.return_value = response

    with pytest.raises(HTTPException) as exc_info:
        await project_papers._add_to_research_library(request, user_id=7, paper_id=42)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Paper library is temporarily unavailable"


async def _star_paper(conn, paper_id: int, user_id: int) -> None:
    """Star a paper so ``link_paper`` takes the zotero.push branch."""
    await conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, starred) VALUES ($1, $2, TRUE) "
        "ON CONFLICT (paper_id, user_id) DO UPDATE SET starred = TRUE",
        paper_id,
        user_id,
    )


async def _insert_zotero_link(conn, paper_id: int, user_id: int, item_key: str) -> None:
    """Insert a per-user Zotero link so the push-trigger reads the per-user table."""
    await conn.execute(
        "INSERT INTO paper_user_zotero_links (paper_id, user_id, zotero_item_key) "
        "VALUES ($1, $2, $3) ON CONFLICT (paper_id, user_id) DO UPDATE SET zotero_item_key = $3",
        paper_id,
        user_id,
        item_key,
    )


async def test_link_paper_enqueues_zotero_push_on_paper_ingestion_queue(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
    _research_library_command,
    monkeypatch,
):
    """Linking a starred paper defers zotero.push by name on the paper_ingestion
    queue with the handler's arg names — not a registry lookup that drops it.
    """
    project_id = contract_two_users.project_id_a
    paper_id = contract_two_users.paper_id_a
    user_id = contract_two_users.user_a_id

    await _star_paper(contract_conn, paper_id, user_id)

    deferrer = MagicMock()
    deferrer.defer_async = AsyncMock(return_value=1)
    configure_task = MagicMock(return_value=deferrer)
    monkeypatch.setattr(task_registry.app, "configure_task", configure_task)

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(f"/api/projects/{project_id}/papers/{paper_id}")

    assert resp.status_code == 201, f"link must succeed: {resp.status_code}: {resp.text[:300]}"
    assert resp.json() == {"project_id": project_id, "paper_id": paper_id}

    # The push is enqueued cross-service on paper_ingestion's queue, not skipped.
    # Reverting to the old registry-lookup-and-skip leaves this at zero calls.
    configure_task.assert_called_once_with(name="zotero.push", queue="paper_ingestion")
    deferrer.defer_async.assert_awaited_once()
    kwargs = deferrer.defer_async.await_args.kwargs
    assert kwargs["user_id"] == user_id
    assert kwargs["paper_id"] == paper_id
    assert isinstance(kwargs["job_id"], str) and kwargs["job_id"]
    # Deferring by name bypasses the registry facade that normally attaches the
    # propagation entry, so without it the push starts a trace of its own and
    # cannot be joined to the request that linked the paper.
    assert kwargs["_jarvis_telemetry"]["correlation_id"] is not None


async def test_link_paper_enqueue_failure_is_observable_and_returns_201(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
    _research_library_command,
    monkeypatch,
    caplog,
):
    """A defer failure after the link commits stays loud (ERROR log) and the
    endpoint still returns 201 — the link itself is already persisted.
    """
    import logging as _logging

    project_id = contract_two_users.project_id_a
    paper_id = contract_two_users.paper_id_a

    await _star_paper(contract_conn, paper_id, contract_two_users.user_a_id)

    deferrer = MagicMock()
    deferrer.defer_async = AsyncMock(side_effect=RuntimeError("connector not open"))
    monkeypatch.setattr(task_registry.app, "configure_task", MagicMock(return_value=deferrer))

    with caplog.at_level(_logging.ERROR, logger="learning_engine.routers.project_papers"):
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp = await c.post(f"/api/projects/{project_id}/papers/{paper_id}")

    assert resp.status_code == 201, f"link must survive enqueue failure: {resp.text[:300]}"
    error_messages = [r.getMessage() for r in caplog.records if r.levelno >= _logging.ERROR]
    assert any("zotero.push" in m for m in error_messages), (
        f"enqueue failure must be logged at ERROR; got {error_messages}"
    )


async def test_link_paper_fires_push_via_requesting_users_zotero_link(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
    _research_library_command,
    monkeypatch,
):
    """link_paper fires zotero.push when the requesting user has a paper_user_zotero_links row.

    Uses a fresh, unstarred paper so the ONLY push trigger is the per-user zotero link.

    Regression proof: revert the LEFT JOIN to read p.zotero_item_key (global) instead of
    l.zotero_item_key → papers.zotero_item_key is NULL, paper not starred → configure_task
    never called → configure_task.assert_called_once_with raises → RED.
    """
    user_id = contract_two_users.user_a_id
    project_id = contract_two_users.project_id_a

    # Fresh paper owned by user A, not starred — zotero link is the sole push trigger.
    fresh_paper_id = await contract_conn.fetchval(
        "INSERT INTO papers "
        "(external_id, source_type, title, authors, url, discovered_by) "
        "VALUES ('iso-zotero-pos-test', 'arxiv', 'Zotero Pos Test Paper', "
        "ARRAY['T. Author'], 'https://example.test/zotero-pos', $1) RETURNING id",
        user_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_id,
        fresh_paper_id,
    )
    await _insert_zotero_link(contract_conn, fresh_paper_id, user_id, "ZKEY-USER-A-001")

    deferrer = MagicMock()
    deferrer.defer_async = AsyncMock(return_value=1)
    configure_task = MagicMock(return_value=deferrer)
    monkeypatch.setattr(task_registry.app, "configure_task", configure_task)

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(f"/api/projects/{project_id}/papers/{fresh_paper_id}")

    assert resp.status_code == 201, f"link must succeed: {resp.status_code}: {resp.text[:300]}"
    # Verified: push triggered solely by user A's per-user zotero link row.
    configure_task.assert_called_once_with(name="zotero.push", queue="paper_ingestion")


async def test_link_paper_no_push_when_only_other_user_has_zotero_link(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
    _research_library_command,
    monkeypatch,
):
    """link_paper does NOT fire zotero.push when only a different user holds the zotero link.

    Uses a fresh paper (not the seeded paper_id_a which is always starred for user A) so the
    no-push branch is cleanly reachable.

    Regression proof: drop AND l.user_id=$2 from the paper_user_zotero_links join →
    user B's row leaks into user A's query → configure_task called spuriously →
    configure_task.assert_not_called raises → RED.
    """
    user_a_id = contract_two_users.user_a_id
    project_id = contract_two_users.project_id_a

    # Fresh paper owned by user A, not starred, no zotero link for user A.
    fresh_paper_id = await contract_conn.fetchval(
        "INSERT INTO papers "
        "(external_id, source_type, title, authors, url, discovered_by) "
        "VALUES ('iso-zotero-neg-test', 'arxiv', 'Zotero Isolation Test Paper', "
        "ARRAY['T. Author'], 'https://example.test/zotero-neg', $1) RETURNING id",
        user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_a_id,
        fresh_paper_id,
    )
    # Only user B gets a zotero link; user A has no link and has not starred this paper.
    await _insert_zotero_link(
        contract_conn, fresh_paper_id, contract_two_users.user_b_id, "ZKEY-USER-B-001"
    )

    configure_task = MagicMock()
    monkeypatch.setattr(task_registry.app, "configure_task", configure_task)

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(f"/api/projects/{project_id}/papers/{fresh_paper_id}")

    assert resp.status_code == 201, f"link must succeed: {resp.status_code}: {resp.text[:300]}"
    # Verified: push not triggered — user B's link must not leak into user A's query.
    configure_task.assert_not_called()
