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

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

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


# ---------------------------------------------------------------------------
# A62: PUT /api/notes/{note_id} — update persists and 404 for non-owner
# (Phase B extension — row A62 PARTIAL-IDOR)
# ---------------------------------------------------------------------------


async def test_a62_update_note_owner_gets_200(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A62: PUT /api/notes/{id} owner updates note text.

    Verified: notes.py:127-165 update_note at HEAD d21aaea8.
    Survivor-of (future Phase C): test_notes.py mock-unit tests.
    """
    # First create a note to update
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        create_resp = await c.post(
            f"/api/papers/{paper_id}/notes",
            json={"user_note": "original-text-for-update", "page_number": 1},
        )
    assert create_resp.status_code == 201, f"Setup failed: {create_resp.text[:200]}"
    note_id = create_resp.json()["id"]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.put(
            f"/api/notes/{note_id}",
            json={"user_note": "updated-text-contract-test"},
        )

    assert resp.status_code == 200, f"Owner expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body["user_note"] == "updated-text-contract-test", (
        f"Expected updated text, got: {body.get('user_note')!r}"
    )


async def test_a62_update_note_user_b_gets_404(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A62: PUT /api/notes/{id} non-owner gets 404.

    Verified: notes.py:155-165 WHERE id AND user_id → 404 non-owner at HEAD d21aaea8.
    """
    # Create a note as user A
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        create_resp = await c.post(
            f"/api/papers/{paper_id}/notes",
            json={"user_note": "note-for-b-update-test"},
        )
    assert create_resp.status_code == 201
    note_id = create_resp.json()["id"]

    # User B tries to update user A's note
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.put(
            f"/api/notes/{note_id}",
            json={"user_note": "b-overwrite-attempt"},
        )

    assert resp.status_code == 404, (
        f"Non-owner should get 404; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A64: DELETE /api/notes/{note_id} — deletes from DB and 404 for non-owner
# (Phase B extension — row A64 PARTIAL-IDOR)
# ---------------------------------------------------------------------------


async def test_a64_delete_note_owner_gets_204(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A64: DELETE /api/notes/{id} removes row from DB for owner.

    Verified: notes.py:313-339 delete_note at HEAD d21aaea8.
    Survivor-of (future Phase C): test_notes.py mock-unit tests.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        create_resp = await c.post(
            f"/api/papers/{paper_id}/notes",
            json={"user_note": "note-to-delete"},
        )
    assert create_resp.status_code == 201
    note_id = create_resp.json()["id"]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/notes/{note_id}")

    assert resp.status_code in (200, 204), (
        f"Owner expected 200/204, got {resp.status_code}: {resp.text[:300]}"
    )
    row = await contract_conn.fetchrow("SELECT id FROM notes WHERE id = $1", note_id)
    assert row is None, f"Note {note_id} must be deleted from DB"


async def test_a64_delete_note_user_b_gets_404(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A64: DELETE /api/notes/{id} non-owner gets 404.

    Verified: notes.py:327-339 WHERE id AND user_id at HEAD d21aaea8.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        create_resp = await c.post(
            f"/api/papers/{paper_id}/notes",
            json={"user_note": "note-for-b-delete-test"},
        )
    assert create_resp.status_code == 201
    note_id = create_resp.json()["id"]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/notes/{note_id}")

    assert resp.status_code == 404, (
        f"Non-owner should get 404; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# E1.PI extensions — highlight idempotency, note list IDOR (user B empty), create DB persistence
#
# Verified: notes.py:56 (GET scopes by paper_id + user_id)
# Verified: notes.py:105 (POST 201 + NoteResponse RETURNING shape)
# Verified: notes.py:155-165 (PUT WHERE id AND user_id — 404 non-owner)
# ---------------------------------------------------------------------------


async def test_e1_notes_user_b_sees_empty_list_for_user_a_paper(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """GET /api/papers/{paper_id}/notes: user B's list is empty for user A's paper.

    Behavioral assertion: even if user B's paper_id happens to match user A's
    paper, no notes cross the user_id boundary.
    Verified: notes.py:56 WHERE paper_id = $1 AND user_id = $2 scope.
    Survivor-of (Phase E2): test_notes.py IDOR cross-user list mock tests.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/papers/{paper_id}/notes")

    assert resp.status_code in (200, 403, 404), (
        f"Unexpected status {resp.status_code}: {resp.text[:200]}"
    )
    if resp.status_code == 200:
        notes = resp.json()
        assert isinstance(notes, list)
        # User B must see no notes for user A's paper (user_id scoping)
        assert len(notes) == 0, (
            f"User B must see empty list for user A's paper; got {len(notes)} notes"
        )


async def test_e1_notes_create_persists_to_db(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """POST /api/papers/{id}/notes: created note is retrievable from DB with correct user_id.

    Stronger than mock-unit: verifies the DB row exists after the POST.
    Verified: notes.py:105 INSERT INTO paper_notes ... RETURNING id, paper_id, user_note, source, created_at.
    Survivor-of (Phase E2): test_notes.py create-note mock-unit DB-side assertions.
    """
    paper_id = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/papers/{paper_id}/notes",
            json={"user_note": "e1-db-persistence-test-note"},
        )
    assert resp.status_code == 201, f"Expected 201; got {resp.status_code}: {resp.text[:200]}"
    note_id = resp.json()["id"]

    row = await contract_conn.fetchrow(
        "SELECT user_id, user_note, source FROM paper_notes WHERE id = $1",
        note_id,
    )
    assert row is not None, f"Note row {note_id} must exist in paper_notes after POST"
    assert row["user_id"] == user_a_id, (
        f"Note must be owned by user_a_id={user_a_id}; got user_id={row['user_id']}"
    )
    assert row["user_note"] == "e1-db-persistence-test-note"
    assert row["source"] == "user"


async def test_e1_notes_highlight_source_idempotency(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """POST /api/papers/{id}/notes twice with source='highlight' creates 2 distinct rows.

    Highlights are NOT idempotent at the endpoint level — each POST creates a new
    row. This verifies the schema allows multiple highlight rows per (paper, user).
    Verified: notes.py:105 (no UNIQUE constraint on highlight rows — unlimited inserts).
    Survivor-of (Phase E2): test_notes.py highlight source mock tests.
    """
    paper_id = contract_two_users.paper_id_a

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        r1 = await c.post(
            f"/api/papers/{paper_id}/notes",
            json={"user_note": "highlight-dup-test", "source": "highlight"},
        )
    assert r1.status_code == 201, f"First highlight POST failed: {r1.text[:200]}"
    id1 = r1.json()["id"]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        r2 = await c.post(
            f"/api/papers/{paper_id}/notes",
            json={"user_note": "highlight-dup-test", "source": "highlight"},
        )
    assert r2.status_code == 201, f"Second highlight POST failed: {r2.text[:200]}"
    id2 = r2.json()["id"]

    assert id1 != id2, (
        "Two highlight POSTs with same text must create two distinct note rows (no idempotency guard)"
    )
    count = await contract_conn.fetchval(
        "SELECT count(*) FROM paper_notes WHERE id = ANY($1::int[])",
        [id1, id2],
    )
    assert count == 2, f"Both rows must exist in DB; got count={count}"
