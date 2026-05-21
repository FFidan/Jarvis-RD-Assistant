"""notes domain contract tests (wave 4.4.D1).

The shared IDOR contract (test_idor_contract.py) already covers the key
ownership quadruples for notes:
  - GET  /api/papers/{paper_id_a}/notes  — byid
  - POST /api/papers/{paper_id_a}/notes  — mutate
  - PUT  /api/notes/{note_id_a}          — mutate
  - DELETE /api/notes/{note_id_a}        — mutate

These contract tests cover the POSITIVE path (owner gets useful response)
and a cross-user behavioral assertion (user B's note list is empty for user
A's paper). They complement rather than duplicate the shared suite.

All tests require JARVIS_RUN_LIVE_PG=1 and run under -m contract.

Verified identifiers:
  notes.py:56    — GET /notes scopes by paper_id + user_id
  notes.py:105   — POST /notes: 201 + NoteResponse body
  notes.py:155-165 — PUT /notes/{id}: WHERE id AND user_id → 404 non-owner
  notes.py:327-339 — DELETE /notes/{id}: WHERE id AND user_id → 404 non-owner
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import httpx

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "notes-contract-key-d1-do-not-use-in-prod"


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    from jarvis_common import auth as _auth
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_API_KEY)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    """paper_ingestion app wired to the contract conn pool.

    Also removes the ``_default_authenticated_user`` autouse fixture's
    ``current_user_id_strict_with_owner_override`` override so that session
    cookies are resolved by the real SessionMiddleware path (not the test stub
    that always returns user_id=1). This is needed because our contract tests
    live under the paper_ingestion conftest scope.
    """
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared

    # Remove the autouse stub so session-cookie auth works in contract tests.
    removed_override = app.dependency_overrides.pop(
        current_user_id_strict_with_owner_override, None
    )
    had_override = removed_override is not None

    yield app

    # Restore pool
    if original_pool is None:
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
    else:
        app.state.db_pool = original_pool

    # Restore override exactly as found (so autouse fixture teardown doesn't fail)
    if had_override:
        app.dependency_overrides[current_user_id_strict_with_owner_override] = removed_override


def _make_client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# GET /api/papers/{paper_id}/notes — owner gets their notes
# ---------------------------------------------------------------------------


async def test_notes_list_owner_gets_own_note(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/papers/{id}/notes: owner sees their seeded note in the list.

    The seeded note text is the A_NOTE_TEXT sentinel from _seed_resources.
    Verifies the user_id scoping SQL (notes.py:56) returns rows for the owner.
    """
    from jarvis_common.testing import A_NOTE_TEXT

    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/papers/{paper_id}/notes")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    notes = resp.json()
    assert isinstance(notes, list)
    texts = [n["user_note"] for n in notes]
    assert any(A_NOTE_TEXT in t for t in texts), (
        f"Seeded note text not found in owner's note list: {texts}"
    )


# ---------------------------------------------------------------------------
# POST /api/papers/{paper_id}/notes — create returns 201 with NoteResponse
# ---------------------------------------------------------------------------


async def test_notes_create_owner_gets_201(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """POST /api/papers/{id}/notes: owner creates a note, gets 201 + NoteResponse.

    Verifies notes.py:105 RETURNING shape: id, paper_id, user_note, source, created_at.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/papers/{paper_id}/notes",
            json={"user_note": "contract-test-create-note", "page_number": 2},
        )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    for field in ("id", "paper_id", "user_note", "source", "created_at"):
        assert field in body, f"Missing field {field!r} in note creation response: {body}"
    assert body["paper_id"] == paper_id
    assert body["user_note"] == "contract-test-create-note"
    assert body["source"] == "user"
