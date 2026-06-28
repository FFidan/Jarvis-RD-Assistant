"""Raw-PDF endpoint + spatial-highlight CRUD contract tests (P7b.1).

Live-PG, real-auth. Covers:
  GET /api/pdfs/{id}                       — shared-corpus visibility + guards
  POST/GET /api/papers/{id}/highlights     — per-user CRUD + tenancy
  PATCH/DELETE /api/highlights/{id}        — owner-only mutation (opaque 404)

The PDF storage dir is redirected to tmp_path so FileResponse serves fixture
files. Visibility mirrors the snapshots endpoint: public-source papers are
served to any authenticated user; LOCAL papers are scoped to the caller's
library, with an opaque 404 (never 403) for everything out of scope.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from jarvis_common.testing_contract_apps import make_contract_client as _make_client

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
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from jarvis_common.testing_contract_apps import patch_app_state, patch_dependency_overrides

    import paper_ingestion.routers.pdfs as _pdfs_mod
    from paper_ingestion.main import app

    monkeypatch.setattr(_pdfs_mod, "PDF_STORAGE_PATH", str(tmp_path))

    shared = SharedConnPool(contract_conn)
    with (
        patch_app_state(app, {"db_pool": shared}),
        patch_dependency_overrides(
            app, remove_overrides={current_user_id_strict_with_owner_override}
        ),
    ):
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


# ---------------------------------------------------------------------------
# GET /api/pdfs/{id}
# ---------------------------------------------------------------------------


async def test_pdf_public_source_served_to_non_owner(
    contract_two_users, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """A public-source paper's PDF is served (200) to a non-owner (shared corpus)."""
    paper_id = contract_two_users.paper_id_a  # source_type='arxiv', discovered_by=A
    (tmp_path / f"{paper_id}.pdf").write_bytes(_MIN_PDF_BYTES)

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
    (tmp_path / f"{paper_id}.pdf").write_bytes(_MIN_PDF_BYTES)

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
    (tmp_path / f"{paper_id}.pdf").write_bytes(_MIN_PDF_BYTES)

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
    (tmp_path / f"{paper_id}.pdf").write_bytes(_MIN_PDF_BYTES)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/pdfs/{paper_id}")

    assert resp.status_code == 404, (
        f"unauthorized ZOTERO paper must be an opaque 404; got {resp.status_code}"
    )


async def test_pdf_zotero_owned_by_discoverer_served_200(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key, tmp_path
):
    """A ZOTERO paper is served (200) to the caller who discovered it."""
    paper_id = await _seed_private_paper(
        contract_conn, contract_two_users.user_a_id, "hl-zotero-owned", "zotero"
    )
    (tmp_path / f"{paper_id}.pdf").write_bytes(_MIN_PDF_BYTES)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/pdfs/{paper_id}")

    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")


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
    (tmp_path / f"{paper_id}.pdf").write_bytes(_MIN_PDF_BYTES)

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
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """Owner can create, list, update (note/color), and delete a highlight."""
    paper_id = contract_two_users.paper_id_a
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


async def test_highlights_tenancy_isolation(
    contract_two_users, _pi_app_with_pool, _configure_api_key
):
    """User B cannot list, patch, or delete user A's highlight (opaque 404s)."""
    paper_id = contract_two_users.paper_id_a  # public source: visible to both users

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        created = await c.post(
            f"/api/papers/{paper_id}/highlights", json={"page": 1, "rect": _RECT}
        )
        assert created.status_code == 201, created.text[:300]
        hid = created.json()["id"]

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        # B can view the shared-corpus PDF but sees none of A's highlights.
        listed = await c.get(f"/api/papers/{paper_id}/highlights")
        assert listed.status_code == 200, listed.text[:300]
        assert listed.json() == [], f"IDOR leak: B saw A's highlights: {listed.json()}"

        patched = await c.patch(f"/api/highlights/{hid}", json={"note": "hijack"})
        assert patched.status_code == 404, f"B patched A's highlight: {patched.status_code}"

        deleted = await c.delete(f"/api/highlights/{hid}")
        assert deleted.status_code == 404, f"B deleted A's highlight: {deleted.status_code}"


async def test_highlights_create_on_unauthorized_local_paper_404(
    contract_two_users, contract_conn, _pi_app_with_pool, _configure_api_key
):
    """User B cannot create a highlight on a LOCAL paper not in B's library."""
    paper_id = await _seed_local_paper(
        contract_conn, contract_two_users.user_a_id, "hl-local-create"
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.post(f"/api/papers/{paper_id}/highlights", json={"page": 1, "rect": _RECT})

    assert resp.status_code == 404, (
        f"create on unauthorized LOCAL paper must be opaque 404; got {resp.status_code}"
    )
