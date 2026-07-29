"""notes domain contract tests.

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

import asyncio
import uuid
from typing import Any

import pytest
from starlette.requests import Request

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


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
# (row A62 PARTIAL-IDOR)
# ---------------------------------------------------------------------------


async def test_a62_update_note_owner_gets_200(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A62: PUT /api/notes/{id} owner updates note text.

    Verified: notes.py:127-165 update_note at HEAD d21aaea8.
    Survivor-of: test_notes.py mock-unit tests.
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
# (row A64 PARTIAL-IDOR)
# ---------------------------------------------------------------------------


async def test_a64_delete_note_owner_gets_204(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A64: DELETE /api/notes/{id} removes row from DB for owner.

    Verified: notes.py:313-339 delete_note at HEAD d21aaea8.
    Survivor-of: test_notes.py mock-unit tests.
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
    row = await contract_conn.fetchrow("SELECT id FROM paper_notes WHERE id = $1", note_id)
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
    Survivor-of: test_notes.py IDOR cross-user list mock tests.
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
    Survivor-of: test_notes.py create-note mock-unit DB-side assertions.
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
    Survivor-of: test_notes.py highlight source mock tests.
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


async def test_note_is_retained_and_remains_stale_after_source_replacement_and_edit(
    contract_two_users,
    contract_conn,
    _pi_app_with_pool,
    _configure_api_key,
):
    """A source replacement marks retained notes stale; editing does not rebind them."""
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        created = await c.post(
            f"/api/papers/{paper_id}/notes",
            json={"user_note": "retained note", "highlight_text": "old source"},
        )
    assert created.status_code == 201, created.text[:300]
    note_id = created.json()["id"]
    assert created.json()["stale"] is False

    await contract_conn.execute(
        "UPDATE papers SET content_generation = content_generation + 1 WHERE id = $1",
        paper_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        listed = await c.get(f"/api/papers/{paper_id}/notes")
        edited = await c.put(
            f"/api/notes/{note_id}",
            json={"user_note": "edited but still old-source"},
        )

    retained = next(note for note in listed.json() if note["id"] == note_id)
    assert retained["stale"] is True
    assert edited.status_code == 200, edited.text[:300]
    assert edited.json()["stale"] is True
    row = await contract_conn.fetchrow(
        "SELECT user_note, content_generation FROM paper_notes WHERE id = $1",
        note_id,
    )
    assert row["user_note"] == "edited but still old-source"
    assert row["content_generation"] == 0


async def test_note_creation_serializes_with_source_replacement(
    _contract_pool,
):
    """A note stamps the source generation before a blocked replacement can proceed."""
    from jarvis_common.testing import SharedConnPool

    from paper_ingestion.models import NoteCreate
    from paper_ingestion.routers.notes import create_note

    token = uuid.uuid4().hex
    async with _contract_pool.acquire() as seed_conn:
        user_id = await seed_conn.fetchval(
            "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
            f"note-race-{token}@contract.test",
        )
        paper_id = await seed_conn.fetchval(
            """
            INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
            VALUES ($1, 'arxiv', 'Note race', ARRAY['A'], $2, $3)
            RETURNING id
            """,
            f"note-race-{token}",
            f"https://example.test/note-race-{token}",
            user_id,
        )
        await seed_conn.execute(
            "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
            user_id,
            paper_id,
        )

    note_insert_started = asyncio.Event()
    release_note_insert = asyncio.Event()

    class GatedConnection:
        def __init__(self, connection: Any) -> None:
            self._connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

        async def fetchval(self, query: str, *args: object) -> Any:
            if "INSERT INTO paper_notes" in query:
                note_insert_started.set()
                await release_note_insert.wait()
            return await self._connection.fetchval(query, *args)

    create_task: asyncio.Task[Any] | None = None
    replacement_task: asyncio.Task[None] | None = None
    try:
        async with _contract_pool.acquire() as note_conn:
            note_backend_pid = await note_conn.fetchval("SELECT pg_backend_pid()")
            create_task = asyncio.create_task(
                create_note(
                    request=Request(
                        {
                            "type": "http",
                            "method": "POST",
                            "path": f"/api/papers/{paper_id}/notes",
                            "headers": [],
                            "client": ("127.0.0.1", 0),
                        }
                    ),
                    paper_id=paper_id,
                    body=NoteCreate(user_note="serialized note"),
                    db_pool=SharedConnPool(GatedConnection(note_conn)),
                    user_id=user_id,
                )
            )
            try:
                await asyncio.wait_for(note_insert_started.wait(), timeout=5)

                replacement_started = asyncio.Event()
                replacement_acquired = asyncio.Event()
                replacement_backend_pid: int | None = None

                async def replace_source() -> None:
                    nonlocal replacement_backend_pid
                    async with _contract_pool.acquire() as replacement_conn:
                        replacement_backend_pid = int(
                            await replacement_conn.fetchval("SELECT pg_backend_pid()")
                        )
                        replacement_started.set()
                        async with replacement_conn.transaction():
                            await replacement_conn.execute(
                                "UPDATE papers SET content_generation = "
                                "content_generation + 1 WHERE id = $1",
                                paper_id,
                            )
                            replacement_acquired.set()

                replacement_task = asyncio.create_task(replace_source())
                await asyncio.wait_for(replacement_started.wait(), timeout=5)
                assert replacement_backend_pid is not None

                blocked = False
                async with _contract_pool.acquire() as observer_conn:
                    for _ in range(100):
                        blocked = bool(
                            await observer_conn.fetchval(
                                "SELECT $1 = ANY(pg_blocking_pids($2))",
                                note_backend_pid,
                                replacement_backend_pid,
                            )
                        )
                        if blocked or replacement_task.done():
                            break
                        await asyncio.sleep(0.01)
                assert blocked
                assert not replacement_acquired.is_set()

                release_note_insert.set()
                created = await asyncio.wait_for(create_task, timeout=5)
                await asyncio.wait_for(replacement_task, timeout=5)
            finally:
                release_note_insert.set()
                pending_tasks = [
                    task
                    for task in (create_task, replacement_task)
                    if task is not None and not task.done()
                ]
                for task in pending_tasks:
                    task.cancel()
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)

        async with _contract_pool.acquire() as verify_conn:
            row = await verify_conn.fetchrow(
                """
                SELECT pn.content_generation AS note_generation, p.content_generation AS paper_generation
                  FROM paper_notes pn
                  JOIN papers p ON p.id = pn.paper_id
                 WHERE pn.id = $1
                """,
                created.id,
            )
        assert created.stale is False
        assert row["note_generation"] == 0
        assert row["paper_generation"] == 1
    finally:
        async with _contract_pool.acquire() as cleanup_conn:
            await cleanup_conn.execute("DELETE FROM paper_notes WHERE paper_id = $1", paper_id)
            await cleanup_conn.execute("DELETE FROM user_library WHERE paper_id = $1", paper_id)
            await cleanup_conn.execute("DELETE FROM papers WHERE id = $1", paper_id)
            await cleanup_conn.execute("DELETE FROM users WHERE id = $1", user_id)
