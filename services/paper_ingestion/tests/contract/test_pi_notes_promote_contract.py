"""Notes promote endpoint contract tests — Cluster 3.

Covers POST /api/notes/{id}/promote (Zotero-only verified evidence flow) plus
the zotero-rejection paths on PUT/DELETE /api/notes/{id}. Replaces mock-unit
tests in services/paper_ingestion/tests/test_notes.py with survivor citations:

  test_promote_zotero_note_verifies_highlight    → N-01
  test_promote_zotero_note_author_succeeds       → N-01
  test_promote_returns_404_for_missing_note_id   → N-02
  test_promote_note_403_for_other_user           → N-03
  test_promote_zotero_note_rejects_non_author    → N-03
  test_promote_already_verified_is_idempotent    → N-04
  test_update_zotero_note_is_rejected            → N-05
  test_update_zotero_note_other_user_gets_404_not_403 → N-05
  test_update_own_zotero_note_still_gets_403     → N-05
  test_delete_zotero_note_is_rejected            → N-06
  test_delete_zotero_note_other_user_gets_404_not_403 → N-06
  test_delete_own_zotero_note_still_gets_403     → N-06

The plan notes "needs faux-Qdrant" but promote_zotero_note reads paper_chunks
from PostgreSQL — NOT Qdrant. No sidecar fixture needed; just seed the
paper_chunks table.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool_and_verifier(contract_conn):
    """PI app wired to contract conn + real QuoteVerifier on app.state.verifier."""
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_contract_apps import patch_app_state, patch_dependency_overrides
    from jarvis_common.verify import QuoteVerifier
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    with (
        patch_app_state(
            app,
            {"db_pool": shared, "embedder": None, "verifier": QuoteVerifier()},
        ),
        patch_dependency_overrides(
            app, remove_overrides={current_user_id_strict_with_owner_override}
        ),
    ):
        yield app


async def _seed_zotero_note_with_chunk(
    conn,
    paper_id: int,
    user_id: int,
    *,
    highlight: str = "The transformer architecture is foundational",
) -> int:
    """Seed a zotero note + matching paper_chunk so verify_quote can succeed."""
    await conn.execute(
        """
        INSERT INTO paper_chunks (paper_id, chunk_index, content, page_number, start_char, end_char)
        VALUES ($1, 0, $2, 1, 0, $3)
        """,
        paper_id,
        f"This paragraph contains the quote. {highlight}. Other content follows.",
        100,
    )
    note_id = await conn.fetchval(
        """
        INSERT INTO paper_notes (paper_id, user_id, source, user_note, highlight_text, page_number,
                                  verification_status)
        VALUES ($1, $2, 'zotero', '', $3, 1, 'unverified')
        RETURNING id
        """,
        paper_id,
        user_id,
        highlight,
    )
    return int(note_id)


# ---------------------------------------------------------------------------
# N-01: happy path — promote succeeds + persists verified state
# ---------------------------------------------------------------------------


async def test_n01_promote_happy_path_verified(
    contract_two_users, contract_conn, _pi_app_with_pool_and_verifier, _configure_api_key
):
    """POST /api/notes/{id}/promote: zotero note with matching chunk → verified.

    # Verified: services/paper_ingestion/paper_ingestion/routers/notes.py:190
    # (promote_zotero_note: verifier.verify_quote → UPDATE status='verified' + promoted_at=NOW()).
    """
    paper_id_a = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id
    note_id = await _seed_zotero_note_with_chunk(contract_conn, paper_id_a, user_a_id)

    async with _make_client(_pi_app_with_pool_and_verifier, contract_two_users.cookie_a) as c:
        resp = await c.post(f"/api/notes/{note_id}/promote")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body.get("verification_status") == "verified", (
        f"Expected verification_status='verified'; got {body.get('verification_status')!r}"
    )
    promoted_at = await contract_conn.fetchval(
        "SELECT promoted_at FROM paper_notes WHERE id = $1", note_id
    )
    assert promoted_at is not None, "promoted_at should be set after successful verification"


# ---------------------------------------------------------------------------
# N-02: 404 for nonexistent note id
# ---------------------------------------------------------------------------


async def test_n02_promote_404_missing_note(
    contract_two_users, _pi_app_with_pool_and_verifier, _configure_api_key
):
    """POST /api/notes/{nonexistent_id}/promote returns 404.

    # Verified: services/paper_ingestion/paper_ingestion/routers/notes.py:190
    # (fetchrow returns None → HTTPException 404).
    """
    async with _make_client(_pi_app_with_pool_and_verifier, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/notes/9999999/promote")

    assert resp.status_code == 404, resp.text[:300]


# ---------------------------------------------------------------------------
# N-03: 404 for non-author (user B promoting user A's note)
# ---------------------------------------------------------------------------


async def test_n03_promote_404_non_author(
    contract_two_users, contract_conn, _pi_app_with_pool_and_verifier, _configure_api_key
):
    """User B promoting user A's zotero note returns 404 (the SELECT WHERE user_id scoping).

    # Verified: services/paper_ingestion/paper_ingestion/routers/notes.py:190
    # (the SELECT WHERE id AND user_id returns None for non-owner → 404).
    """
    paper_id_a = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id
    note_id = await _seed_zotero_note_with_chunk(contract_conn, paper_id_a, user_a_id)

    async with _make_client(_pi_app_with_pool_and_verifier, contract_two_users.cookie_b) as c:
        resp = await c.post(f"/api/notes/{note_id}/promote")

    assert resp.status_code == 404, (
        f"User B should get 404 promoting user A's note; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# N-04: idempotent — already-verified note returns 200 without re-running verification
# ---------------------------------------------------------------------------


async def test_n04_promote_idempotent_already_verified(
    contract_two_users, contract_conn, _pi_app_with_pool_and_verifier, _configure_api_key
):
    """Already-verified note returns 200 immediately (no re-verification or DB write).

    # Verified: services/paper_ingestion/paper_ingestion/routers/notes.py:190
    # (idempotency guard: verification_status=='verified' AND promoted_at NOT NULL → return early).
    """
    paper_id_a = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id
    note_id = await contract_conn.fetchval(
        """
        INSERT INTO paper_notes (paper_id, user_id, source, user_note, highlight_text, page_number,
                                  verification_status, promoted_at)
        VALUES ($1, $2, 'zotero', '', 'already verified', 5, 'verified', NOW())
        RETURNING id
        """,
        paper_id_a,
        user_a_id,
    )

    async with _make_client(_pi_app_with_pool_and_verifier, contract_two_users.cookie_a) as c:
        resp = await c.post(f"/api/notes/{note_id}/promote")

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body.get("verification_status") == "verified"


# ---------------------------------------------------------------------------
# N-05: PUT /api/notes/{id} rejects zotero notes
# ---------------------------------------------------------------------------


async def test_n05_update_zotero_note_rejected(
    contract_two_users, contract_conn, _pi_app_with_pool_and_verifier, _configure_api_key
):
    """PUT /api/notes/{id} on a zotero note returns 400 for owner; 404 for non-owner.

    # Verified: services/paper_ingestion/paper_ingestion/routers/notes.py:127
    # (update_note: zotero source rejection).
    """
    paper_id_a = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id
    note_id = await contract_conn.fetchval(
        """
        INSERT INTO paper_notes (paper_id, user_id, source, user_note, highlight_text)
        VALUES ($1, $2, 'zotero', '', 'highlight')
        RETURNING id
        """,
        paper_id_a,
        user_a_id,
    )

    # Owner sees 400/403 — cannot modify zotero notes
    async with _make_client(_pi_app_with_pool_and_verifier, contract_two_users.cookie_a) as c:
        resp_a = await c.put(f"/api/notes/{note_id}", json={"user_note": "tampered"})
    assert resp_a.status_code in (400, 403), (
        f"Owner PUT on zotero note: expected 400/403; got {resp_a.status_code}: {resp_a.text[:200]}"
    )

    # Non-owner gets 404
    async with _make_client(_pi_app_with_pool_and_verifier, contract_two_users.cookie_b) as c:
        resp_b = await c.put(f"/api/notes/{note_id}", json={"user_note": "tampered"})
    assert resp_b.status_code == 404, (
        f"Non-owner PUT on zotero note: expected 404; got {resp_b.status_code}"
    )


# ---------------------------------------------------------------------------
# N-06: DELETE /api/notes/{id} rejects zotero notes (mirror of N-05)
# ---------------------------------------------------------------------------


async def test_n06_delete_zotero_note_rejected(
    contract_two_users, contract_conn, _pi_app_with_pool_and_verifier, _configure_api_key
):
    """DELETE /api/notes/{id} on a zotero note returns 400 for owner; 404 for non-owner.

    # Verified: services/paper_ingestion/paper_ingestion/routers/notes.py:313
    # (delete_note: zotero source rejection).
    """
    paper_id_a = contract_two_users.paper_id_a
    user_a_id = contract_two_users.user_a_id
    note_id = await contract_conn.fetchval(
        """
        INSERT INTO paper_notes (paper_id, user_id, source, user_note, highlight_text)
        VALUES ($1, $2, 'zotero', '', 'highlight')
        RETURNING id
        """,
        paper_id_a,
        user_a_id,
    )

    async with _make_client(_pi_app_with_pool_and_verifier, contract_two_users.cookie_a) as c:
        resp_a = await c.delete(f"/api/notes/{note_id}")
    assert resp_a.status_code in (400, 403), (
        f"Owner DELETE on zotero note: expected 400/403; got {resp_a.status_code}"
    )

    async with _make_client(_pi_app_with_pool_and_verifier, contract_two_users.cookie_b) as c:
        resp_b = await c.delete(f"/api/notes/{note_id}")
    assert resp_b.status_code == 404, (
        f"Non-owner DELETE on zotero note: expected 404; got {resp_b.status_code}"
    )
