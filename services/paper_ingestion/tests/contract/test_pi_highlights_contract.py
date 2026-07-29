"""Raw-PDF endpoint + spatial-highlight CRUD contract tests (P7b.1).

Live-PG, real-auth. Covers:
  GET /api/pdfs/{id}                       — persisted visibility + guards
  POST/GET /api/papers/{id}/highlights     — per-user CRUD + tenancy
  PATCH/DELETE /api/highlights/{id}        — owner-only mutation (opaque 404)

The PDF storage dir is redirected to tmp_path so FileResponse serves fixture
files. Visibility mirrors the snapshots endpoint: persisted-public papers are
served to authenticated users; private papers require caller-library
membership, with an opaque 404 (never 403) for everything out of scope.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool
from jarvis_common.testing_contract_apps import (
    PITestAppOptions,
    make_contract_client as _make_client,
    patch_pi_test_app,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_MIN_PDF_BYTES = (
    b"%PDF-1.4\n%\xc3\xa4\xc3\xbc\n1 0 obj\n<<>>\nendobj\nxref\n0 1\ntrailer<<>>\n%%EOF\n"
)

_RECT = {
    "boundingRect": {"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4},
    "rects": [{"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4}],
}


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn, tmp_path, monkeypatch):
    """PI app wired to the contract conn; PDF storage redirected to tmp_path."""
    import paper_ingestion.routers.pdfs as _pdfs_mod

    monkeypatch.setattr(_pdfs_mod, "PDF_STORAGE_PATH", str(tmp_path))

    shared = SharedConnPool(contract_conn)
    with patch_pi_test_app(
        shared,
        options=PITestAppOptions(remove_owner_override=True),
    ) as app:
        yield app


async def _seed_local_paper(conn, user_id: int, external_id: str) -> int:
    """Insert a LOCAL (uploaded) paper owned by *user_id*, NOT in any library."""
    return await _seed_private_paper(conn, user_id, external_id, "local")


async def _seed_private_paper(conn, user_id: int, external_id: str, source_type: str) -> int:
    """Insert a private-origin paper (``local``/``zotero``) discovered by *user_id*.

    Not added to any library; used to exercise the ownership-scoped visibility
    gate for non-public sources.
    """
    return int(
        await conn.fetchval(
            """
            INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
            VALUES ($1, $2, 'Private Paper', ARRAY['A'], $3, $4)
            RETURNING id
            """,
            external_id,
            source_type,
            f"{source_type}://{external_id}",
            user_id,
        )
    )


async def _seed_public_paper(conn, external_id: str) -> int:
    """Insert a persisted-public scholarly paper with no library membership."""
    return int(
        await conn.fetchval(
            """INSERT INTO papers (
                   external_id, source_type, title, authors, url, visibility_scope
               )
               VALUES ($1, 'arxiv', 'Public Paper', ARRAY['A'], $2, 'public')
               RETURNING id""",
            external_id,
            f"https://example.test/{external_id}",
        )
    )


async def _record_stored_pdf(conn, tmp_path, paper_id: int) -> None:
    """Give a seeded paper a stored PDF: the file and the pointer that claims it.

    These routes read a paper through the pointer, not the storage directory,
    so a seeded row only behaves like a readable paper once both exist —
    which is how every writer publishes them. Seeding them together keeps the
    404 cases in this module attributable to visibility alone.
    """
    stored_pdf = tmp_path / f"{paper_id}.pdf"
    stored_pdf.write_bytes(_MIN_PDF_BYTES)
    await conn.execute(
        "UPDATE papers SET pdf_downloaded = TRUE, pdf_local_path = $1 WHERE id = $2",
        str(stored_pdf),
        paper_id,
    )


# ---------------------------------------------------------------------------
# GET /api/pdfs/{id}
# ---------------------------------------------------------------------------


async def test_pdf_public_source_served_to_non_owner(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """A persisted-public paper's PDF is served to another authenticated user."""
    paper_id = await _seed_public_paper(contract_conn, "hl-public")
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/pdfs/{paper_id}")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")


async def test_pdf_local_unauthorized_is_opaque_404_not_403(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """A LOCAL paper not in the caller's library returns 404 (never 403)."""
    paper_id = await _seed_local_paper(contract_conn, contract_two_users.user_a_id, "hl-local-404")
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/pdfs/{paper_id}")

    assert resp.status_code == 404, (
        f"unauthorized LOCAL paper must be an opaque 404, not 403; got {resp.status_code}"
    )


async def test_pdf_local_in_owner_library_served_200(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """A LOCAL paper in the owner's library is served (200) to that owner."""
    paper_id = await _seed_local_paper(
        contract_conn, contract_two_users.user_a_id, "hl-local-owned"
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/pdfs/{paper_id}")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")


async def test_pdf_zotero_unauthorized_is_opaque_404(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """A ZOTERO (private-origin) paper is an opaque 404 for a non-discoverer.

    ZOTERO is not a public-corpus source, so a paper discovered by A with no
    library row must NOT be served to B via its enumerable integer id.
    """
    paper_id = await _seed_private_paper(
        contract_conn, contract_two_users.user_a_id, "hl-zotero-404", "zotero"
    )
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/pdfs/{paper_id}")

    assert resp.status_code == 404, (
        f"unauthorized ZOTERO paper must be an opaque 404; got {resp.status_code}"
    )


async def test_pdf_zotero_discoverer_without_library_denied(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """Discoverer attribution alone does not authorize a private ZOTERO PDF."""
    paper_id = await _seed_private_paper(
        contract_conn, contract_two_users.user_a_id, "hl-zotero-owned", "zotero"
    )
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/pdfs/{paper_id}")

    assert resp.status_code == 404, resp.text[:300]


async def test_pdf_private_in_caller_library_served_200(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """A private paper discovered by A but in B's library is served (200) to B."""
    paper_id = await _seed_private_paper(
        contract_conn, contract_two_users.user_a_id, "hl-zotero-inlib", "zotero"
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_b_id,
        paper_id,
    )
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/pdfs/{paper_id}")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")


async def test_pdf_unknown_id_404(contract_two_users, _pi_app_with_pool, _configure_api_key):
    """An unknown paper id returns an opaque 404."""
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/pdfs/99999999")
    assert resp.status_code == 404, resp.text[:300]


async def test_pdf_path_traversal_rejected(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """A traversal-style segment is rejected before any file access.

    ``paper_id`` is int-typed, so a non-numeric segment is rejected at the type
    boundary (422); the is_relative_to guard (→ 400) is defence-in-depth behind
    it. The contract is simply: traversal never reaches a file (no 200/500).
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/pdfs/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404, 422), resp.text[:300]


# ---------------------------------------------------------------------------
# Highlights CRUD round-trip (create -> list -> patch -> delete)
# ---------------------------------------------------------------------------


async def test_highlights_crud_roundtrip_scoped_per_user(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """Owner can create, list, update (note/color), and delete a highlight."""
    paper_id = contract_two_users.paper_id_a
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)
    create_body = {"page": 2, "rect": _RECT, "note": "first", "color": "yellow", "quote": "q"}

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        created = await c.post(f"/api/papers/{paper_id}/highlights", json=create_body)
        assert created.status_code == 201, created.text[:300]
        hl = created.json()
        hid = hl["id"]
        assert hl["page"] == 2
        assert hl["rect"]["boundingRect"]["x0"] == pytest.approx(0.1)
        assert hl["rect"]["rects"][0]["y1"] == pytest.approx(0.4)

        listed = await c.get(f"/api/papers/{paper_id}/highlights")
        assert listed.status_code == 200, listed.text[:300]
        assert [r["id"] for r in listed.json()] == [hid]

        patched = await c.patch(f"/api/highlights/{hid}", json={"note": "edited", "color": "green"})
        assert patched.status_code == 200, patched.text[:300]
        assert patched.json()["note"] == "edited"
        assert patched.json()["color"] == "green"

        deleted = await c.delete(f"/api/highlights/{hid}")
        assert deleted.status_code == 204, deleted.text[:300]

        empty = await c.get(f"/api/papers/{paper_id}/highlights")
        assert empty.json() == []


async def test_source_replacement_marks_only_preexisting_highlights_stale(
    contract_two_users,
    contract_conn,
    _pi_app_with_pool,
    _configure_api_key,
    tmp_path,
):
    """A replacement preserves annotations but distinguishes their document version."""
    from paper_ingestion.models import PaperCreate, SourceType
    from paper_ingestion.services.pdf_workflow import upsert_verified_public_paper

    original = PaperCreate(
        external_id="highlight-generation-paper",
        source_type=SourceType.ARXIV,
        title="Generation paper",
        authors=["A. Author"],
        url="https://arxiv.org/abs/2401.30001",
        pdf_url="https://arxiv.org/pdf/2401.30001v1.pdf",
    )
    paper = await upsert_verified_public_paper(
        contract_conn,
        original,
        discovered_by=contract_two_users.user_a_id,
    )
    paper_id = int(paper["id"])
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        first = await c.post(
            f"/api/papers/{paper_id}/highlights",
            json={"page": 1, "rect": _RECT, "quote": "Old document"},
        )
        assert first.status_code == 201, first.text[:300]

        replacement = original.model_copy(
            update={"pdf_url": "https://arxiv.org/pdf/2401.30001v2.pdf"}
        )
        await upsert_verified_public_paper(contract_conn, replacement)
        await _record_stored_pdf(contract_conn, tmp_path, paper_id)

        patched = await c.patch(
            f"/api/highlights/{first.json()['id']}",
            json={"note": "Kept from the old document"},
        )
        assert patched.status_code == 200, patched.text[:300]
        assert patched.json()["stale"] is True

        second = await c.post(
            f"/api/papers/{paper_id}/highlights",
            json={"page": 2, "rect": _RECT, "quote": "New document"},
        )
        assert second.status_code == 201, second.text[:300]
        listed = await c.get(f"/api/papers/{paper_id}/highlights")

    assert listed.status_code == 200, listed.text[:300]
    # Verified: services/paper_ingestion/paper_ingestion/routers/highlights.py:40
    # returns all user work and marks only a mismatched content generation stale.
    assert [(row["id"], row["stale"]) for row in listed.json()] == [
        (first.json()["id"], True),
        (second.json()["id"], False),
    ]
    assert (
        await contract_conn.fetchval(
            "SELECT count(*) FROM paper_highlights WHERE paper_id = $1",
            paper_id,
        )
        == 2
    )


async def test_highlights_tenancy_isolation(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """User B cannot list, patch, or delete user A's highlight (opaque 404s)."""
    paper_id = await _seed_public_paper(contract_conn, "hl-public-tenancy")
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        created = await c.post(
            f"/api/papers/{paper_id}/highlights", json={"page": 1, "rect": _RECT}
        )
        assert created.status_code == 201, created.text[:300]
        hid = created.json()["id"]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        # B can view the persisted-public paper but sees none of A's highlights.
        listed = await c.get(f"/api/papers/{paper_id}/highlights")
        assert listed.status_code == 200, listed.text[:300]
        assert listed.json() == [], f"IDOR leak: B saw A's highlights: {listed.json()}"

        patched = await c.patch(f"/api/highlights/{hid}", json={"note": "hijack"})
        assert patched.status_code == 404, f"B patched A's highlight: {patched.status_code}"

        deleted = await c.delete(f"/api/highlights/{hid}")
        assert deleted.status_code == 404, f"B deleted A's highlight: {deleted.status_code}"


async def test_highlights_create_on_unauthorized_local_paper_404(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """User B cannot create a highlight on a LOCAL paper not in B's library."""
    paper_id = await _seed_local_paper(
        contract_conn, contract_two_users.user_a_id, "hl-local-create"
    )
    await _record_stored_pdf(contract_conn, tmp_path, paper_id)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.post(f"/api/papers/{paper_id}/highlights", json={"page": 1, "rect": _RECT})

    assert resp.status_code == 404, (
        f"create on unauthorized LOCAL paper must be opaque 404; got {resp.status_code}"
    )


async def _seed_highlight_export_race(pool) -> tuple[int, int, int]:
    token = uuid.uuid4().hex
    async with pool.acquire() as conn:
        async with conn.transaction():
            user_id = await conn.fetchval(
                "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
                f"highlight-race-{token}@contract.test",
            )
            paper_id = await conn.fetchval(
                """
                INSERT INTO papers
                    (external_id, source_type, title, authors, url, discovered_by)
                VALUES ($1, 'arxiv', 'Highlight race', ARRAY['A'], $2, $3)
                RETURNING id
                """,
                f"highlight-race-{token}",
                f"https://example.test/{token}",
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO user_library (user_id, paper_id, added_via)
                VALUES ($1, $2, 'manual_save')
                """,
                user_id,
                paper_id,
            )
            await conn.execute(
                """
                INSERT INTO paper_user_zotero_links
                    (paper_id, user_id, zotero_item_key, zotero_attachment_key)
                VALUES ($1, $2, 'ITEM-RACE', 'ATTACH-RACE')
                """,
                paper_id,
                user_id,
            )
            highlight_id = await conn.fetchval(
                """
                INSERT INTO paper_highlights
                    (paper_id, user_id, page, rect, quote, content_generation)
                VALUES ($1, $2, 1, $3, 'race quote', 0)
                RETURNING id
                """,
                paper_id,
                user_id,
                _RECT,
            )
    return int(user_id), int(paper_id), int(highlight_id)


async def _delete_highlight_export_race(
    pool,
    *,
    user_id: int,
    paper_id: int,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM paper_highlights WHERE paper_id = $1", paper_id)
            await conn.execute(
                "DELETE FROM paper_user_zotero_links WHERE paper_id = $1",
                paper_id,
            )
            await conn.execute(
                "DELETE FROM user_library WHERE user_id = $1 AND paper_id = $2",
                user_id,
                paper_id,
            )
            await conn.execute("DELETE FROM papers WHERE id = $1", paper_id)
            await conn.execute("DELETE FROM users WHERE id = $1", user_id)


async def test_highlight_export_and_source_replacement_serialize_in_both_orders(
    _contract_pool,
    monkeypatch,
):
    """Zotero creation commits before replacement or is skipped after it wins."""
    import paper_ingestion.integrations._zotero_highlights as highlights_module

    user_id, paper_id, highlight_id = await _seed_highlight_export_race(_contract_pool)
    client = MagicMock()
    client.create_item = AsyncMock(return_value={"successful": {"0": {"key": "ANN-RACE"}}})
    monkeypatch.setattr(
        highlights_module,
        "_get_page_sizes",
        AsyncMock(return_value={1: (612.0, 792.0)}),
    )
    original_prepare = highlights_module._prepare_highlight_push
    try:
        source_locked = asyncio.Event()
        release_action = asyncio.Event()

        async def gated_prepare(conn, zotero_client, item_id, owner_id):
            prepared = await original_prepare(conn, zotero_client, item_id, owner_id)
            source_locked.set()
            await release_action.wait()
            return prepared

        monkeypatch.setattr(
            highlights_module,
            "_prepare_highlight_push",
            gated_prepare,
        )
        action = asyncio.create_task(
            highlights_module._push_one_highlight(
                _contract_pool,
                client,
                highlight_id,
                user_id,
            )
        )
        await asyncio.wait_for(source_locked.wait(), timeout=2)
        replacement_started = asyncio.Event()
        replacement_acquired = asyncio.Event()

        async def replacement_after_export() -> None:
            async with _contract_pool.acquire() as conn:
                async with conn.transaction():
                    replacement_started.set()
                    await conn.execute(
                        """
                        UPDATE papers
                        SET content_generation = content_generation + 1
                        WHERE id = $1
                        """,
                        paper_id,
                    )
                    replacement_acquired.set()

        replacement = asyncio.create_task(replacement_after_export())
        await asyncio.wait_for(replacement_started.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert not replacement_acquired.is_set()
        release_action.set()
        first = await asyncio.wait_for(action, timeout=2)
        await asyncio.wait_for(replacement, timeout=2)
        assert first["status"] == "ok"
        async with _contract_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT zotero_annotation_key FROM paper_highlights WHERE id = $1",
                    highlight_id,
                )
                == "ANN-RACE"
            )

        async with _contract_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE paper_highlights
                SET zotero_annotation_key = NULL,
                    content_generation = (
                        SELECT content_generation FROM papers WHERE id = $2
                    )
                WHERE id = $1
                """,
                highlight_id,
                paper_id,
            )
        client.create_item.reset_mock()
        monkeypatch.setattr(
            highlights_module,
            "_prepare_highlight_push",
            original_prepare,
        )
        replacement_locked = asyncio.Event()
        release_replacement = asyncio.Event()

        async def held_replacement() -> None:
            async with _contract_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE papers
                        SET content_generation = content_generation + 1
                        WHERE id = $1
                        """,
                        paper_id,
                    )
                    replacement_locked.set()
                    await release_replacement.wait()

        replacement = asyncio.create_task(held_replacement())
        await asyncio.wait_for(replacement_locked.wait(), timeout=2)
        action = asyncio.create_task(
            highlights_module._push_one_highlight(
                _contract_pool,
                client,
                highlight_id,
                user_id,
            )
        )
        await asyncio.sleep(0.05)
        assert not action.done()
        client.create_item.assert_not_called()
        release_replacement.set()
        await asyncio.wait_for(replacement, timeout=2)
        second = await asyncio.wait_for(action, timeout=2)
        assert second == {"highlight_id": highlight_id, "status": "stale_source"}
        client.create_item.assert_not_called()
        async with _contract_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT zotero_annotation_key FROM paper_highlights WHERE id = $1",
                    highlight_id,
                )
                is None
            )
    finally:
        await _delete_highlight_export_race(
            _contract_pool,
            user_id=user_id,
            paper_id=paper_id,
        )
