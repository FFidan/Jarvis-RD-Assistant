"""Extraction templates contract tests — Phase B target rows A35, A36, A37, A38, A40.

Survivor-of: test_extraction_endpoints.py, test_extractions.py mock-unit assertions
    for list_templates, create_template, update_template, delete_template,
    get_paper_extractions.
Carve-out: LLM (extract_paper) is exempt — mocked at the boundary; require_admin
    bypassed via dependency_overrides for admin-gated write endpoints.
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

_TEST_API_KEY = "extraction-contract-key-phase-b-do-not-use-in-prod"


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
async def _pi_app_admin(contract_conn):
    """PI app wired to contract conn + require_admin bypassed (admin-gated template writes)."""
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.auth import require_admin
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared

    removed_override = app.dependency_overrides.pop(
        current_user_id_strict_with_owner_override, None
    )
    had_override = removed_override is not None

    async def _allow_admin():
        return None

    app.dependency_overrides[require_admin] = _allow_admin

    yield app

    app.dependency_overrides.pop(require_admin, None)
    if original_pool is None:
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
    else:
        app.state.db_pool = original_pool

    if had_override:
        app.dependency_overrides[current_user_id_strict_with_owner_override] = removed_override


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    """PI app wired to contract conn — no admin bypass (for non-admin 403 tests)."""
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared

    removed_override = app.dependency_overrides.pop(
        current_user_id_strict_with_owner_override, None
    )
    had_override = removed_override is not None

    yield app

    if original_pool is None:
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
    else:
        app.state.db_pool = original_pool

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
# A35: GET /api/extraction-templates — global list (no user scoping)
# ---------------------------------------------------------------------------


async def test_a35_list_templates_returns_global_list(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A35: GET /api/extraction-templates returns global list.

    Verified: extractions.py:56-81 list_templates at HEAD d21aaea8.
    Survivor-of (future Phase C): test_extraction_endpoints.py, test_extractions.py.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/extraction-templates")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, list), f"Expected list response, got {type(body).__name__}"


# ---------------------------------------------------------------------------
# A36: POST /api/extraction-templates — admin creates template; 403 for non-admin
# ---------------------------------------------------------------------------


async def test_a36_create_template_admin_persists_to_db(
    contract_two_users,
    _pi_app_admin,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A36: admin POST creates template row in DB.

    Verified: extractions.py:84-122 create_template at HEAD d21aaea8.
    Survivor-of (future Phase C): test_extraction_endpoints.py, test_extractions.py.
    """
    template_name = "contract-test-template-phase-b"
    async with _make_client(_pi_app_admin, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/extraction-templates",
            json={
                "name": template_name,
                "description": "Phase B contract test template",
                "fields": [{"name": "field1", "description": "test field", "field_type": "text"}],
                "is_default": False,
            },
        )

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body["name"] == template_name
    assert "id" in body

    # Verify DB row
    row = await contract_conn.fetchrow(
        "SELECT name FROM extraction_templates WHERE name = $1",
        template_name,
    )
    assert row is not None, f"Template {template_name!r} not found in DB after creation"


async def test_a36_create_template_duplicate_name_returns_409(
    contract_two_users,
    _pi_app_admin,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A36: duplicate template name returns 409.

    Verified: extractions.py:112-113 UniqueViolationError handler at HEAD d21aaea8.
    """
    dup_name = "contract-dup-template-phase-b"
    await contract_conn.execute(
        "INSERT INTO extraction_templates (name, description, fields, is_default) VALUES ($1, $2, $3::jsonb, FALSE)",
        dup_name,
        "dup desc",
        [],
    )

    async with _make_client(_pi_app_admin, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/extraction-templates",
            json={
                "name": dup_name,
                "description": "dup",
                "fields": [],
                "is_default": False,
            },
        )

    assert resp.status_code == 409, (
        f"Expected 409 for duplicate template name, got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A37: PUT /api/extraction-templates/{id} — admin updates template fields in DB
# ---------------------------------------------------------------------------


async def test_a37_update_template_persists_changes(
    contract_two_users,
    _pi_app_admin,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A37: admin PUT updates template description in DB.

    Verified: extractions.py:125-196 update_template at HEAD d21aaea8.
    Survivor-of (future Phase C): test_extraction_endpoints.py, test_extractions.py.
    """
    template_id = await contract_conn.fetchval(
        "INSERT INTO extraction_templates (name, description, fields, is_default) VALUES ($1, $2, $3::jsonb, FALSE) RETURNING id",
        "contract-update-tmpl",
        "original desc",
        [],
    )

    async with _make_client(_pi_app_admin, contract_two_users.cookie_a) as c:
        resp = await c.put(
            f"/api/extraction-templates/{template_id}",
            json={"description": "updated desc"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    row = await contract_conn.fetchrow(
        "SELECT description FROM extraction_templates WHERE id = $1", template_id
    )
    assert row is not None
    assert row["description"] == "updated desc", (
        f"Expected 'updated desc', got {row['description']!r}"
    )


# ---------------------------------------------------------------------------
# A38: DELETE /api/extraction-templates/{id} — admin deletes template row
# ---------------------------------------------------------------------------


async def test_a38_delete_template_removes_row_from_db(
    contract_two_users,
    _pi_app_admin,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A38: admin DELETE removes template row from DB.

    Verified: extractions.py:199-221 delete_template at HEAD d21aaea8.
    Survivor-of (future Phase C): test_extraction_endpoints.py, test_extractions.py.
    """
    template_id = await contract_conn.fetchval(
        "INSERT INTO extraction_templates (name, description, fields, is_default) VALUES ($1, $2, $3::jsonb, FALSE) RETURNING id",
        "contract-delete-tmpl",
        "to be deleted",
        [],
    )

    async with _make_client(_pi_app_admin, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/extraction-templates/{template_id}")

    assert resp.status_code == 204, f"Expected 204, got {resp.status_code}: {resp.text[:300]}"
    row = await contract_conn.fetchrow(
        "SELECT id FROM extraction_templates WHERE id = $1", template_id
    )
    assert row is None, f"Template {template_id} must be deleted from DB"


async def test_a38_delete_template_nonexistent_returns_404(
    contract_two_users,
    _pi_app_admin,
    _configure_api_key,
):
    """Covers map row A38: DELETE non-existent template returns 404.

    Verified: extractions.py:220-221 result == 'DELETE 0' check at HEAD d21aaea8.
    """
    async with _make_client(_pi_app_admin, contract_two_users.cookie_a) as c:
        resp = await c.delete("/api/extraction-templates/9999999")

    assert resp.status_code == 404, (
        f"Expected 404 for non-existent template, got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A40: GET /api/papers/{paper_id}/extractions — scoped to owner's paper
# ---------------------------------------------------------------------------


async def test_a40_get_paper_extractions_owner_gets_list(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A40: GET /api/papers/{id}/extractions returns list for owner.

    Verified: extractions.py:257 get_paper_extractions at HEAD d21aaea8.
    Survivor-of (future Phase C): test_extraction_endpoints.py.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/papers/{paper_id}/extractions")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, list), f"Expected list, got {type(body).__name__}"


async def test_a40_get_paper_extractions_user_b_gets_403_404(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A40: user B denied access to user A's paper extractions.

    Verified: extractions.py:238-240 assert_paper_ownership at HEAD d21aaea8.
    """
    paper_id = contract_two_users.paper_id_a
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/papers/{paper_id}/extractions")

    assert resp.status_code in (403, 404), (
        f"User B should get 403/404 for user A's extractions; got {resp.status_code}"
    )
