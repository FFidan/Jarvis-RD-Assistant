"""Tests for extraction template and extraction table endpoints.

Covers:
- Templates: GET /api/extraction-templates, POST, PUT (not found), DELETE
- Table missing: UndefinedTableError -> 503, naming the table the query reads
- Extraction table: JSON format, CSV format
- Paper extractions: GET /api/papers/{id}/extractions
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import asyncpg.exceptions
import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _template_row(id=1, name="Default Template", description=None, is_default=True):
    return FakeRecord(
        id=id,
        name=name,
        description=description,
        fields=[
            {"name": "method", "label": "Method", "description": "Method used", "type": "text"}
        ],
        is_default=is_default,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked DB pool and disabled auth."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict, require_admin
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    mock_http = AsyncMock()
    with patch_pi_test_app(
        mock_pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=False,
            override_db_dependency=True,
            disable_limiter=True,
            state_overrides={"http_client": mock_http},
            dependency_overrides={
                verify_api_key: lambda: None,
                # Template CUD is admin-gated (PI-B). These tests exercise
                # CRUD/404/503 logic, not the auth gate, so run them as an
                # admin caller.
                require_admin: lambda: None,
                # Extraction read endpoints (table, paper extractions) resolve
                # the caller via Depends(current_user_id_strict); override it
                # to a fixed user.
                current_user_id_strict: lambda: 1,
            },
        ),
    ):
        yield app, conn, mock_http


# ---------------------------------------------------------------------------
# Tests: Template CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_templates(_app):
    """GET /api/extraction-templates returns a list of templates."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        _template_row(id=1, name="Template A"),
        _template_row(id=2, name="Template B", is_default=False),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/extraction-templates")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["name"] == "Template A"


@pytest.mark.asyncio
async def test_create_template(_app):
    """POST /api/extraction-templates creates a new template."""
    app, conn, _ = _app
    conn.fetchrow.return_value = _template_row(id=3, name="New Template")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/extraction-templates",
            json={
                "name": "New Template",
                "fields": [
                    {
                        "name": "method",
                        "label": "Method",
                        "description": "Method used",
                        "type": "text",
                    }
                ],
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "New Template"


@pytest.mark.asyncio
async def test_update_template_not_found(_app):
    """PUT /api/extraction-templates/{id} returns 404 for missing template."""
    app, conn, _ = _app
    conn.fetchrow.return_value = None  # not found

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/extraction-templates/999",
            json={
                "name": "Updated Name",
            },
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_template(_app):
    """DELETE /api/extraction-templates/{id} returns 204 on success."""
    app, conn, _ = _app
    conn.execute.return_value = "DELETE 1"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/extraction-templates/1")

    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Tests: Table missing -> 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_template_table_missing_503(_app):
    """POST /api/extraction-templates returns 503 naming the table and a followable remedy."""
    app, conn, _ = _app
    conn.fetchrow.side_effect = asyncpg.exceptions.UndefinedTableError(
        'relation "extraction_templates" does not exist'
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/extraction-templates",
            json={
                "name": "Template",
                "fields": [{"name": "f", "label": "F", "description": "d", "type": "text"}],
            },
        )

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail.startswith("extraction_templates table not found")
    assert "db/init.sql" in detail
    assert "db/migrations/README.md" in detail
    # The squashed baseline deleted the numbered migration files, so naming one
    # would send an operator after a file that cannot be applied.
    assert "migration 011" not in detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "params"),
    [
        ("/api/papers/42/extractions", None),
        ("/api/extractions/table", {"template_id": 1}),
        ("/api/extractions/table", {"template_id": 1, "paper_ids": "1,2"}),
    ],
)
async def test_paper_extractions_missing_503_names_that_table(_app, url, params):
    """Endpoints reading paper_extractions name it, not the template table."""
    app, conn, _ = _app
    # One row serves whichever lookup runs first: the ownership check reads
    # id/is_visible, the comparison table reads fields.
    conn.fetchrow.return_value = FakeRecord(
        id=42,
        is_visible=True,
        fields=[{"name": "method", "label": "Method", "description": "d", "type": "text"}],
    )
    conn.fetch.side_effect = asyncpg.exceptions.UndefinedTableError(
        'relation "paper_extractions" does not exist'
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(url, params=params)

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail.startswith("paper_extractions table not found")
    assert "db/migrations/README.md" in detail


# ---------------------------------------------------------------------------
# Tests: Extraction table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_table_json(_app):
    """GET /api/extractions/table returns JSON format by default."""
    app, conn, _ = _app
    # template fields
    conn.fetchrow.return_value = FakeRecord(
        fields=[{"name": "method", "label": "Method", "description": "d", "type": "text"}],
    )
    # extraction rows
    conn.fetch.return_value = [
        FakeRecord(
            paper_id=1,
            paper_title="Paper A",
            extractions={
                "method": {
                    "value": "GAN",
                    "verified": True,
                    "confidence": 0.9,
                    "quote": None,
                    "chunk_id": None,
                    "page_number": None,
                }
            },
        ),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/extractions/table", params={"template_id": 1})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["paper_title"] == "Paper A"


@pytest.mark.asyncio
async def test_extraction_table_csv(_app):
    """GET /api/extractions/table?format=csv returns CSV format."""
    app, conn, _ = _app
    conn.fetchrow.return_value = FakeRecord(
        fields=[{"name": "method", "label": "Method", "description": "d", "type": "text"}],
    )
    conn.fetch.return_value = [
        FakeRecord(
            paper_id=1,
            paper_title="Paper A",
            extractions={
                "method": {
                    "value": "GAN",
                    "verified": True,
                    "confidence": 0.9,
                    "quote": None,
                    "chunk_id": None,
                    "page_number": None,
                }
            },
        ),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/extractions/table", params={"template_id": 1, "format": "csv"}
        )

    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")
    content = resp.text
    assert "Paper" in content
    assert "Method" in content


# ---------------------------------------------------------------------------
# Tests: Paper extractions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paper_extractions_found(_app):
    """GET /api/papers/{id}/extractions returns extractions for a paper."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(
            id=1,
            paper_id=42,
            template_id=1,
            extractions={
                "method": {
                    "value": "CNN",
                    "verified": True,
                    "confidence": 0.85,
                    "quote": None,
                    "chunk_id": None,
                    "page_number": None,
                }
            },
            extraction_model="smart",
            created_at=datetime.now(UTC),
        ),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/42/extractions")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["paper_id"] == 42
    assert "method" in body[0]["extractions"]
