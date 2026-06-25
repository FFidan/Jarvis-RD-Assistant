"""Extraction templates contract tests — target rows A35, A36, A37, A38, A40.

Survivor-of: test_extraction_endpoints.py, test_extractions.py mock-unit assertions
    for list_templates, create_template, update_template, delete_template,
    get_paper_extractions.
Carve-out: LLM (extract_paper) is exempt — mocked at the boundary; require_admin
    bypassed via dependency_overrides for admin-gated write endpoints.
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
    Survivor-of: test_extraction_endpoints.py, test_extractions.py.
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
    Survivor-of: test_extraction_endpoints.py, test_extractions.py.
    """
    template_name = "contract-test-template-phase-b"
    async with _make_client(_pi_app_admin, contract_two_users.cookie_a) as c:
        resp = await c.post(
            "/api/extraction-templates",
            json={
                "name": template_name,
                "description": "contract test template",
                "fields": [
                    {
                        "name": "field1",
                        "label": "Field 1",
                        "description": "test field",
                        "type": "text",
                    }
                ],
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
                "fields": [
                    {
                        "name": "field1",
                        "label": "Field 1",
                        "description": "duplicate field",
                        "type": "text",
                    }
                ],
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
    Survivor-of: test_extraction_endpoints.py, test_extractions.py.
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
    Survivor-of: test_extraction_endpoints.py, test_extractions.py.
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
    Survivor-of: test_extraction_endpoints.py.
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


# ---------------------------------------------------------------------------
# LLM-sidecar contracts: decomposition + extraction + verifier-mandatory
#
# These contracts replace mock-unit patches of call_llm_structured in:
#   test_decomposition.py (~260 LOC)
#   test_extraction.py (~895 LOC partial)
#   test_entity_extractor_verification.py (~160 LOC)
#   test_verifier_mandatory.py (366 LOC)
# ---------------------------------------------------------------------------


async def test_extraction_w2_decomposition_happy_path(
    pi_contract_app_with_litellm_sidecar,
    contract_conn,
):
    """decompose_query returns 2-4 sub-queries from the faux LLM response.

    # Verified: services/paper_ingestion/paper_ingestion/rag/decomposition.py:25-102
    # Survivor-of: test_decomposition.py mock-unit assertions patching call_llm_structured
    """
    import json
    from paper_ingestion.rag.decomposition import decompose_query

    app, faux = pi_contract_app_with_litellm_sidecar

    sub_queries = ["What is attention?", "How do transformers scale?", "What are benchmarks used?"]
    faux.add_response("fast", json.dumps(sub_queries))

    result = await decompose_query(
        "How does attention mechanism scale in transformer models across multiple benchmarks?",
        model="fast",
        openai_client=app.state.openai_client,
    )

    assert isinstance(result, list), "decompose_query must return a list"
    assert 1 <= len(result) <= 4, f"Expected 1-4 sub-queries, got {len(result)}"
    assert all(isinstance(q, str) and q.strip() for q in result), (
        "All sub-queries must be non-empty strings"
    )


async def test_extraction_w2_nested_decomposition_recursive(
    pi_contract_app_with_litellm_sidecar,
    contract_conn,
):
    """decompose_query degrades gracefully to [original_question] when LLM errors.

    # Verified: services/paper_ingestion/paper_ingestion/rag/decomposition.py:100-102
    # Survivor-of: test_decomposition.py mock-unit fallback path assertions
    """
    from paper_ingestion.rag.decomposition import decompose_query

    app, faux = pi_contract_app_with_litellm_sidecar

    # Inject a 502 error — decompose_query must catch it and fall back.
    faux.add_error("fast", 502, "upstream unavailable")

    question = "What datasets are used in neural architecture search?"
    result = await decompose_query(
        question,
        model="fast",
        openai_client=app.state.openai_client,
    )

    # On any exception, decompose_query falls back to [original_question].
    assert result == [question], (
        f"decompose_query must degrade to [original_question] on LLM error; got {result}"
    )


async def test_extraction_w2_verifier_mandatory_blocks_unverified(
    pi_contract_app_with_litellm_sidecar,
    contract_conn,
):
    """extract_fields_for_paper discards field values whose quotes fail verification.

    # Verified: services/paper_ingestion/paper_ingestion/extraction/core.py:211-231
    # Survivor-of: test_extraction.py PI-CORE-007 mock-unit assertions on verifier discard
    """
    from paper_ingestion.extraction.core import extract_fields_for_paper

    app, faux = pi_contract_app_with_litellm_sidecar

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role)"
        " VALUES ('w2-extv-verify@contract.test', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)"
        " VALUES ('w2-extv-01', 'arxiv', 'W2 Verifier Paper', ARRAY['Author V'],"
        " 'http://w2v', $1) RETURNING id",
        user_id,
    )
    await contract_conn.execute(
        "INSERT INTO paper_chunks (paper_id, chunk_index, content)"
        " VALUES ($1, 0, 'The model achieves 94.2% accuracy on GLUE.')",
        paper_id,
    )
    template_id = await contract_conn.fetchval(
        "INSERT INTO extraction_templates (name, fields) VALUES ($1, $2::jsonb) RETURNING id",
        "w2-verifier-template",
        [
            {
                "name": "accuracy",
                "label": "Accuracy",
                "type": "text",
                "description": "Model accuracy",
            }
        ],
    )

    # LLM returns a quote that does NOT appear in the source text → verifier discards it.
    import json

    faux.add_response(
        "smart",
        json.dumps(
            {
                "accuracy": {
                    "value": "95.0%",
                    "quote": "This hallucinated quote is not in the source text at all.",
                }
            }
        ),
    )

    from jarvis_common.verify import QuoteVerifier

    result = await extract_fields_for_paper(
        http_client=None,  # type: ignore[arg-type]
        db_pool=app.state.db_pool,
        paper_id=paper_id,
        template_id=template_id,
        embedder=None,
        verifier=QuoteVerifier(),
        openai_client=app.state.openai_client,
    )

    accuracy_field = result.extractions.get("accuracy")
    assert accuracy_field is not None, "accuracy field must be present in extraction result"
    assert accuracy_field.value is None, (
        "Unverified quote must cause value to be discarded (confidence 0.0)"
    )
    assert accuracy_field.confidence == 0.0, (
        f"Unverified extraction must have confidence=0.0; got {accuracy_field.confidence}"
    )


# ---------------------------------------------------------------------------
# M6.5 — dynamic-extraction adversarial tripwire (schema-echo defense-in-depth)
#
# A schema-object echo has top-level keys (type/properties/required/...) that do
# NOT match template field names, so every template field resolves to None — no
# schema keyword ever reaches the DB as a field value. all-None is LEGITIMATE
# (a paper with no extractable fields), so the request must fail SAFE (null
# values, confidence 0.0), not 500 and not a hard all-None rejection.
# ---------------------------------------------------------------------------


@pytest.mark.nightly_smoke
async def test_extraction_w2_schema_object_echo_no_key_corruption(
    pi_contract_app_with_litellm_sidecar,
    contract_conn,
):
    """A schema-object echo resolves every template field to None with confidence 0.0
    and never lets a JSON-schema keyword leak into a field value/quote.

    # Verified: services/paper_ingestion/paper_ingestion/extraction/core.py:227-233
    # (getattr(llm_result, field_name) on schema-object keys -> None per field)
    """
    import json

    from jarvis_common.verify import QuoteVerifier
    from paper_ingestion.extraction.core import extract_fields_for_paper
    from paper_ingestion.extraction.dynamic_models import _build_extraction_response_model

    app, faux = pi_contract_app_with_litellm_sidecar

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role)"
        " VALUES ('w2-extv-schema-echo@contract.test', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)"
        " VALUES ('w2-extv-schema-01', 'arxiv', 'W2 Extraction Schema Echo', ARRAY['Author E'],"
        " 'http://w2ese', $1) RETURNING id",
        user_id,
    )
    await contract_conn.execute(
        "INSERT INTO paper_chunks (paper_id, chunk_index, content)"
        " VALUES ($1, 0, 'The model achieves 94.2% accuracy on GLUE.')",
        paper_id,
    )
    template_id = await contract_conn.fetchval(
        "INSERT INTO extraction_templates (name, fields) VALUES ($1, $2::jsonb) RETURNING id",
        "w2-schema-echo-template",
        [
            {"name": "accuracy", "label": "Accuracy", "type": "text", "description": "Accuracy"},
            {"name": "dataset", "label": "Dataset", "type": "text", "description": "Dataset"},
        ],
    )

    # The LLM echoes its own JSON schema object instead of extracted values.
    response_model = _build_extraction_response_model(("accuracy", "dataset"))
    faux.add_response("smart", json.dumps(response_model.model_json_schema()))

    result = await extract_fields_for_paper(
        http_client=None,  # type: ignore[arg-type]
        db_pool=app.state.db_pool,
        paper_id=paper_id,
        template_id=template_id,
        embedder=None,
        verifier=QuoteVerifier(),
        openai_client=app.state.openai_client,
    )

    schema_keywords = {"type", "properties", "required", "$defs", "title", "ExtractedFieldOutput"}
    for field_name in ("accuracy", "dataset"):
        field = result.extractions.get(field_name)
        assert field is not None, f"{field_name} must be present in the extraction result"
        assert field.value is None, (
            f"{field_name}: schema-echo must resolve to a null value (all-None is legitimate)"
        )
        assert field.confidence == 0.0, f"{field_name}: a null extraction must carry confidence 0.0"
        assert field.value not in schema_keywords, (
            f"{field_name}: a JSON-schema keyword must never leak into a field value"
        )
        assert field.quote is None or field.quote not in schema_keywords, (
            f"{field_name}: a JSON-schema keyword must never leak into a field quote"
        )


# ---------------------------------------------------------------------------
# TENANT-01: per-user extraction isolation
# ---------------------------------------------------------------------------


async def test_tenant01_per_user_extraction_isolation(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """TENANT-01: user A and user B extracting the same (paper, template) produce
    separate rows; each user's GET endpoints return only their own data.

    Verified:
      extractions.py:267-272 get_paper_extractions WHERE user_id = $2
      extractions.py:400-408 get_extraction_table WHERE pe.user_id = $2
    """
    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    # Insert a shared paper that both users have in their library.
    shared_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('tenant01-shared', 'arxiv', 'Shared Paper TENANT-01',
                   ARRAY['Author X'], 'https://tenant01.test/', $1)
           RETURNING id""",
        user_a_id,
    )
    for uid in (user_a_id, user_b_id):
        await contract_conn.execute(
            "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
            uid,
            shared_paper_id,
        )

    # Create a template.
    template_id = await contract_conn.fetchval(
        """INSERT INTO extraction_templates (name, description, fields, is_default)
           VALUES ('tenant01-tmpl', 'isolation test', $1::jsonb, FALSE) RETURNING id""",
        [{"name": "finding", "label": "Finding", "type": "text", "description": "key finding"}],
    )

    # Seed one extraction row per user directly (bypasses LLM).
    await contract_conn.execute(
        """INSERT INTO paper_extractions (paper_id, template_id, extractions, extraction_model, user_id)
           VALUES ($1, $2, $3::jsonb, 'test-model', $4)""",
        shared_paper_id,
        template_id,
        {
            "finding": {
                "value": "result-A",
                "verified": True,
                "confidence": 0.9,
                "quote": None,
                "chunk_id": None,
                "page_number": None,
            }
        },
        user_a_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_extractions (paper_id, template_id, extractions, extraction_model, user_id)
           VALUES ($1, $2, $3::jsonb, 'test-model', $4)""",
        shared_paper_id,
        template_id,
        {
            "finding": {
                "value": "result-B",
                "verified": True,
                "confidence": 0.8,
                "quote": None,
                "chunk_id": None,
                "page_number": None,
            }
        },
        user_b_id,
    )

    # Verify DB has two distinct rows.
    rows = await contract_conn.fetch(
        "SELECT user_id FROM paper_extractions WHERE paper_id = $1 AND template_id = $2",
        shared_paper_id,
        template_id,
    )
    assert len(rows) == 2, f"Expected 2 extraction rows (one per user), got {len(rows)}"
    row_user_ids = {r["user_id"] for r in rows}
    assert row_user_ids == {user_a_id, user_b_id}, (
        f"Expected rows for both users; got user_ids={row_user_ids}"
    )

    # GET /api/papers/{P}/extractions as user A → only A's row.
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_a = await c.get(f"/api/papers/{shared_paper_id}/extractions")
    assert resp_a.status_code == 200, (
        f"User A paper extractions: {resp_a.status_code} {resp_a.text[:200]}"
    )
    body_a = resp_a.json()
    assert len(body_a) == 1, f"User A must see exactly 1 extraction row; got {len(body_a)}"
    assert body_a[0]["extractions"]["finding"]["value"] == "result-A", (
        f"User A must see result-A; got {body_a[0]['extractions']}"
    )

    # GET /api/papers/{P}/extractions as user B → only B's row.
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_b = await c.get(f"/api/papers/{shared_paper_id}/extractions")
    assert resp_b.status_code == 200, (
        f"User B paper extractions: {resp_b.status_code} {resp_b.text[:200]}"
    )
    body_b = resp_b.json()
    assert len(body_b) == 1, f"User B must see exactly 1 extraction row; got {len(body_b)}"
    assert body_b[0]["extractions"]["finding"]["value"] == "result-B", (
        f"User B must see result-B; got {body_b[0]['extractions']}"
    )

    # GET /api/extractions/table?template_id=T as user A → only A's paper row.
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp_ta = await c.get(f"/api/extractions/table?template_id={template_id}")
    assert resp_ta.status_code == 200, f"User A table: {resp_ta.status_code} {resp_ta.text[:200]}"
    table_a = resp_ta.json()
    assert len(table_a) == 1, f"User A table must have 1 row; got {len(table_a)}"
    assert table_a[0]["extractions"]["finding"]["value"] == "result-A", (
        f"User A table must contain result-A; got {table_a[0]['extractions']}"
    )

    # GET /api/extractions/table?template_id=T as user B → only B's paper row.
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp_tb = await c.get(f"/api/extractions/table?template_id={template_id}")
    assert resp_tb.status_code == 200, f"User B table: {resp_tb.status_code} {resp_tb.text[:200]}"
    table_b = resp_tb.json()
    assert len(table_b) == 1, f"User B table must have 1 row; got {len(table_b)}"
    assert table_b[0]["extractions"]["finding"]["value"] == "result-B", (
        f"User B table must contain result-B; got {table_b[0]['extractions']}"
    )
