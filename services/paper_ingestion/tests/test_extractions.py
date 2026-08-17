"""RBAC tests for extraction-template CUD endpoints (PI-B).

``extraction_templates`` is an instance-global table (no ``user_id`` column),
so create/update/delete must be admin-only. Any authenticated non-admin tenant
mutating it would break extraction for every user (e.g. deleting the default
template).

- no session         → 401 on the list endpoint
- non-admin session  → 403 on each CUD op
- admin session      → success
- read endpoint (GET /api/extraction-templates) requires a signed-in session

Schema guard (CFG-EXTPL-1): a live-PG test asserts ``extraction_templates``
has NO ``user_id`` column so that accidental per-user column addition is caught
before it causes a multi-tenancy design violation (requires a schema migration
+ design review to introduce).
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import pytest
from jarvis_common.testing_auth import SignedIdentityMiddleware
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
from jarvis_common.testing_contract_apps import make_contract_client as _client

from tests.conftest import FakeRecord, _make_pool_and_conn
from tests.migration_helpers import apply_fresh_init


def _template_row(id=1, name="Default Template", is_default=True):
    return FakeRecord(
        id=id,
        name=name,
        description=None,
        fields=[{"name": "method", "label": "Method", "description": "d", "type": "text"}],
        is_default=is_default,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture()
def _app(request):
    """Full app with mocked DB, signed identity, and limiter disabled.

    ``request.param`` (parametrized via indirect) is the simulated session
    role: ``None``/``"user"`` exercise the rejection path and ``"admin"``
    exercises the success path through the production identity middleware.
    """
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    user_role = getattr(request, "param", None)

    mock_pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_template_row()]
    conn.fetchrow.return_value = _template_row(id=3, name="New Template")
    conn.execute.return_value = "DELETE 1"

    with patch_pi_test_app(
        mock_pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=True,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides={verify_api_key: lambda: None},
        ),
    ) as patched_app:
        yield (
            SignedIdentityMiddleware(
                patched_app,
                audience="research",
                user_id=1 if user_role is not None else None,
                role=user_role,
            ),
            conn,
        )


_VALID_BODY = {
    "name": "T",
    "fields": [{"name": "f", "label": "F", "description": "d", "type": "text"}],
}


# ---------------------------------------------------------------------------
# Non-admin → 403 on every CUD op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_create_template_rejects_non_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.post("/api/extraction-templates", json=_VALID_BODY)
    assert resp.status_code == 403, resp.text
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_update_template_rejects_non_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.put("/api/extraction-templates/1", json={"name": "X"})
    assert resp.status_code == 403, resp.text
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_delete_template_rejects_non_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.delete("/api/extraction-templates/1")
    assert resp.status_code == 403, resp.text
    conn.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", [None], indirect=True)
async def test_create_template_rejects_no_session(_app):
    """API-key-only caller (no session ⇒ no user_role) is not an admin."""
    app, conn = _app
    async with _client(app) as c:
        resp = await c.post("/api/extraction-templates", json=_VALID_BODY)
    assert resp.status_code == 403, resp.text
    conn.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# Admin → success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["admin"], indirect=True)
async def test_create_template_allows_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.post("/api/extraction-templates", json=_VALID_BODY)
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "New Template"


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["admin"], indirect=True)
async def test_delete_template_allows_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.delete("/api/extraction-templates/1")
    assert resp.status_code == 204, resp.text
    conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Read endpoint: requires a session, but is not admin-gated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", [None], indirect=True)
async def test_list_templates_requires_session(_app):
    """GET /api/extraction-templates without a session returns 401."""
    app, _conn = _app
    async with _client(app) as c:
        resp = await c.get("/api/extraction-templates")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_list_templates_allowed_for_authenticated_user(_app):
    """GET /api/extraction-templates with a valid session returns 200 (not admin-gated)."""
    app, conn = _app
    async with _client(app) as c:
        resp = await c.get("/api/extraction-templates")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Schema guard: CFG-EXTPL-1 — extraction_templates is system-global (no user_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.live_pg
async def test_extraction_templates_are_global_not_per_user(live_pg_dsn: str) -> None:
    """extraction_templates has no user_id column — templates are global/system-scoped.

    Guards against accidental addition of a per-user column which would require
    a schema migration + multi-tenancy design review (CFG-EXTPL-1).  The query in
    ``extract_fields_for_paper`` (SELECT ... WHERE id = $1) is intentionally
    user-predicate-free; this test locks that contract at the schema level.
    """
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await apply_fresh_init(pool)
        async with pool.acquire() as conn:
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'extraction_templates'"
                )
            }
        assert "user_id" not in cols, (
            "extraction_templates is intentionally global; "
            "do not add user_id without a migration + multi-tenancy design review"
        )
        # Confirm expected system-global columns are present
        assert "id" in cols
        assert "name" in cols
        assert "fields" in cols
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# build_extraction_prompt: no pre-truncation; wrap_delimited handles it
# Prompt-shape: system carries rules, user carries data only
# ---------------------------------------------------------------------------


def test_build_extraction_prompt_no_instruction_head():
    """build_extraction_prompt returns only data — no instruction prose.

    The extraction rules now live in _SYSTEM_EXTRACTION (system role).
    The user-role prompt must carry only the data fields so untrusted paper
    text cannot escape into the instruction layer.
    """
    from paper_ingestion.extraction.core import build_extraction_prompt

    fields = [{"name": "method", "label": "Method", "description": "desc", "type": "text"}]
    prompt = build_extraction_prompt(fields, "Test Title", "Some paper text.")

    assert "You are" not in prompt
    assert "RULES:" not in prompt
    assert "Do NOT invent" not in prompt
    assert "PAPER TITLE:" in prompt
    assert "FIELDS TO EXTRACT:" in prompt
    assert "PAPER TEXT:" in prompt
    assert "<paper_text>" in prompt


def test_system_extraction_contains_rules():
    """_SYSTEM_EXTRACTION carries the extraction rules in the system constant."""
    from paper_ingestion.extraction.core import _SYSTEM_EXTRACTION

    assert "You are" in _SYSTEM_EXTRACTION
    assert "RULES:" in _SYSTEM_EXTRACTION
    assert "Do NOT invent" in _SYSTEM_EXTRACTION
    assert "verbatim" in _SYSTEM_EXTRACTION.lower()


def test_build_extraction_prompt_accepts_long_text_without_pre_truncation():
    """build_extraction_prompt truncates via wrap_delimited, not pre-truncation.

    Passing text longer than 15000 chars must not raise and the returned
    prompt must contain the wrapped tag (truncation done inside wrap_delimited).
    """
    from paper_ingestion.extraction.core import build_extraction_prompt

    fields = [{"name": "f", "label": "F", "description": "d", "type": "text"}]
    long_text = "A" * 20000
    prompt = build_extraction_prompt(fields, "Title", long_text)
    assert "<paper_text>" in prompt
    assert len(prompt) < 20000 + 500
