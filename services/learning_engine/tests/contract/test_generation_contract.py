"""Generation + export contract tests — A198, A199, A200.

Idiomatic-mock carve-out applies:
- card_generator (LLM) and task_registry (procrastinate) kept as mocks per spec.
- anki_exporter (file generation) kept as mock.

Contract-tested behavior:
- A198 GET /api/export/anki/{deck_id}  — 404 for non-owner deck (exporter mocked)
- A199 POST /api/generate              — 404 for non-owner deck (task_registry mocked)
- A200 POST /api/generate/batch        — 404 for non-owner deck (task_registry mocked)
"""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


from jarvis_common.testing_contract_apps import (
    DEFAULT_CONTRACT_API_KEY,
    make_contract_client as _client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# §A198 — GET /api/export/anki/{deck_id} — 404 for non-owner deck
# ---------------------------------------------------------------------------


async def test_export_anki_non_owner_deck_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot export user A's deck — 404 (IDOR guard).

    anki_exporter is kept mocked (file-generation boundary); the contract test
    exercises the real ``SELECT * FROM decks WHERE id = $1 AND user_id = $2``
    ownership check before the exporter is called.
    """
    deck_id_a = contract_two_users.deck_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/export/anki/{deck_id_a}")

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} exporting user A's deck {deck_id_a} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A199 — POST /api/generate — 404 for non-owner deck (task_registry mocked)
# ---------------------------------------------------------------------------


async def test_generate_cards_non_owner_deck_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot enqueue generation for user A's deck — 404 (IDOR guard).

    task_registry (procrastinate defer_async) is kept mocked per idiomatic
    carve-out.  The contract exercises the real deck ownership check:
    ``SELECT id FROM decks WHERE id = $1 AND user_id = $2``.
    """
    import jarvis_common.task_registry as task_registry

    deck_id_a = contract_two_users.deck_id_a
    paper_id_a = contract_two_users.paper_id_a

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate": mock_task}):
        async with _client(_le_app, contract_two_users.cookie_b) as c:
            resp = await c.post(
                "/api/generate",
                json={"paper_id": paper_id_a, "deck_id": deck_id_a, "max_cards": 3},
            )

    # RD-DA-001 fix: paper-ownership extractor now fires before deck-ownership,
    # so non-owner paper returns 403 instead of 404. Both are valid IDOR rejections.
    assert resp.status_code in (403, 404), (
        f"IDOR: user B got {resp.status_code} generating cards for user A's deck "
        f"{deck_id_a} (expected 403 or 404). Body: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# §A200 — POST /api/generate/batch — IDOR rejection for non-owner deck
# ---------------------------------------------------------------------------


async def test_batch_generate_non_owner_deck_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot enqueue batch generation for user A's deck — 404 (IDOR guard).

    task_registry kept mocked per idiomatic carve-out.  Contract exercises the
    real ``SELECT id FROM decks WHERE id = $1 AND user_id = $2`` check.
    """
    import jarvis_common.task_registry as task_registry

    deck_id_a = contract_two_users.deck_id_a

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate_batch": mock_task}):
        async with _client(_le_app, contract_two_users.cookie_b) as c:
            resp = await c.post(
                "/api/generate/batch",
                json={"deck_id": deck_id_a, "max_per_paper": 5},
            )

    # RD-DA-001 fix: ownership-related rejection may be 403 (paper-ownership)
    # or 404 (deck-ownership); both are valid IDOR rejections.
    assert resp.status_code in (403, 404), (
        f"IDOR: user B got {resp.status_code} batch-generating for user A's deck "
        f"{deck_id_a} (expected 403 or 404). Body: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# §A201 — generate_cards_core re-validates paper ownership (RD-DA-001 depth)
# ---------------------------------------------------------------------------


async def test_generate_cards_core_revalidates_paper_ownership(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """RD-DA-001 defense-in-depth: generate_cards_core raises 403 when the
    caller's user_id does not own the requested paper_id.

    This guards the worker entry point directly: even if a job were somehow
    enqueued with a mismatched paper_id, the core helper must re-validate
    ownership before reading paper data.

    Setup: seed a deck owned by user B so the deck check passes, then supply
    user A's paper_id so the paper ownership check fires and returns 403.
    """
    from fastapi import HTTPException

    from learning_engine.routers.generation import generate_cards_core

    paper_id_a = contract_two_users.paper_id_a
    user_b_id = contract_two_users.user_b_id

    # Seed a deck owned by user B within the contract transaction.
    deck_id_b = await contract_conn.fetchval(
        "INSERT INTO decks (user_id, name, description) VALUES ($1, $2, $3) RETURNING id",
        user_b_id,
        "contract-test-deck-b",
        "temp deck for RD-DA-001 test",
    )

    # Reach into the _le_app fixture to get the SharedConnPool already wired.
    shared = _le_app.state.db_pool

    with pytest.raises(HTTPException) as exc_info:
        await generate_cards_core(
            pool=shared,
            http_client=_le_app.state.http_client,
            paper_id=paper_id_a,
            deck_id=deck_id_b,
            max_cards=3,
            user_id=user_b_id,
        )

    assert exc_info.value.status_code == 403, (
        f"RD-DA-001 depth: generate_cards_core returned {exc_info.value.status_code} "
        f"instead of 403 for user_b accessing paper_a. Detail: {exc_info.value.detail}"
    )


# ---------------------------------------------------------------------------
# §A202 — POST /api/generate without session → 401 (current_user_id_strict)
# ---------------------------------------------------------------------------


async def test_generate_endpoint_no_session_returns_401(_le_app, _configure_api_key):
    """POST /api/generate with API key but no session cookie → 401.

    generate_cards uses current_user_id_strict; no session = no resolved user_id → 401.
    Documents that the generation endpoint is session-gated (not API-key-only).
    # Verified: services/learning_engine/learning_engine/routers/generation.py:307
    """
    import jarvis_common.task_registry as task_registry

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate": mock_task}):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_le_app),
            base_url="http://test",
            headers={"X-API-Key": DEFAULT_CONTRACT_API_KEY},
        ) as c:
            resp = await c.post(
                "/api/generate",
                json={"paper_id": 1, "deck_id": 1, "max_cards": 3},
            )

    assert resp.status_code == 401, (
        f"Expected 401 for API-key-only generate; got {resp.status_code}: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# §A203 — POST /api/generate with invalid payload → 422 (validation)
# ---------------------------------------------------------------------------


async def test_generate_endpoint_missing_paper_id_returns_422(
    contract_two_users, _le_app, _configure_api_key
):
    """POST /api/generate missing required paper_id field → 422 (Pydantic discriminator).

    GenerateCardsRequest requires paper_id: int; omitting it triggers FastAPI's
    request body validation before any auth or DB query fires.
    # Verified: services/learning_engine/learning_engine/models.py:99-104
    """
    import jarvis_common.task_registry as task_registry

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock()

    with patch.dict(task_registry._TASK_MAP, {"card.generate": mock_task}):
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp = await c.post(
                "/api/generate",
                json={"deck_id": contract_two_users.deck_id_a, "max_cards": 3},
            )

    assert resp.status_code == 422, (
        f"Expected 422 for missing paper_id; got {resp.status_code}: {resp.text[:300]}"
    )
    mock_task.defer_async.assert_not_awaited()


# ---------------------------------------------------------------------------
# W2.4 — LLM-sidecar contracts: card generation + anki export
#
# These contracts replace mock-unit patches of call_llm_structured in:
#   test_card_generator.py (291 LOC)
#   test_generation.py (5.9K)
# ---------------------------------------------------------------------------


async def test_generation_w2_card_gen_happy_path_via_faux_litellm(
    le_contract_app_with_litellm_sidecar,
    contract_conn,
):
    """CardGenerator.generate_cards produces cards when faux LiteLLM returns valid output.

    # Verified: services/learning_engine/learning_engine/card_generator.py:252-313
    # Survivor-of: test_card_generator.py mock-unit assertions patching call_llm_structured
    """
    from learning_engine.card_generator import CardGenerator
    from learning_engine.card_models import CardGenerationOutput, CardOutput
    from jarvis_common.llm_client import LiteLLMConfig

    import httpx

    app, faux = le_contract_app_with_litellm_sidecar

    chunks = [
        {
            "id": 1,
            "content": "Neural networks improve retrieval quality significantly.",
            "page_number": 1,
        }
    ]
    scripted = CardGenerationOutput(
        cards=[
            CardOutput(
                card_type="concept",
                front="What improves retrieval quality?",
                back="Neural networks improve retrieval quality.",
                evidence_quote="Neural networks improve retrieval quality significantly.",
                page_number=1,
            )
        ]
    )
    faux.add_pydantic_response("smart", scripted)

    cg = CardGenerator(
        http_client=httpx.AsyncClient(),
        litellm_config=LiteLLMConfig(base_url=faux.url),
    )
    result = await cg.generate_cards(
        title="Retrieval Paper",
        authors=["Author X"],
        chunks=chunks,
        openai_client=app.state.openai_client,
        paper_id=None,
        max_cards=1,
        model="smart",
    )

    assert result["total_count"] >= 1, "LLM returned at least 1 card"
    assert result["verified_count"] >= 1, "Evidence quote is in source text — must verify"
    assert result["cards"][0]["card_type"] == "concept"
    assert result["confidence"] in ("HIGH", "MEDIUM")


async def test_generation_w2_card_gen_instructor_retry_exception_handled(
    le_contract_app_with_litellm_sidecar,
    contract_conn,
):
    """CardGenerator._call_llm_for_cards returns None when the LLM returns malformed JSON.

    # Verified: services/learning_engine/learning_engine/card_generator.py:108-121
    # Survivor-of: test_card_generator.py mock-unit InstructorRetryException tests
    """
    from learning_engine.card_generator import CardGenerator
    from jarvis_common.llm_client import LiteLLMConfig
    import httpx

    app, faux = le_contract_app_with_litellm_sidecar

    # Enqueue malformed JSON — Instructor will fail to parse it and raise
    # InstructorRetryException (or ValidationError), which _call_llm_for_cards catches.
    faux.add_response("smart", '{"cards": "this-is-wrong-not-a-list"}')

    cg = CardGenerator(
        http_client=httpx.AsyncClient(),
        litellm_config=LiteLLMConfig(base_url=faux.url),
    )
    result = await cg.generate_cards(
        title="Error Paper",
        authors=["Author Y"],
        chunks=[{"id": 1, "content": "Some content.", "page_number": 1}],
        openai_client=app.state.openai_client,
        paper_id=None,
        max_cards=3,
        model="smart",
    )

    # _call_llm_for_cards returned None → _empty_result() → 0 cards, LOW confidence
    assert result["total_count"] == 0, "Malformed LLM output must produce empty result"
    assert result["confidence"] == "LOW"


async def test_generation_w2_anki_export_shape_correct(
    le_contract_app_with_litellm_sidecar,
    contract_conn,
):
    """GET /api/export/anki/{deck_id} returns .apkg bytes for a deck with cards.

    # Verified: services/learning_engine/learning_engine/routers/export.py:17-80
    # Survivor-of: test_export.py mock-unit assertions on AnkiExporter.export_deck
    """
    from jarvis_common.testing_contract_apps import make_contract_client

    app, faux = le_contract_app_with_litellm_sidecar

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('w2-anki@contract.test', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)"
        " VALUES ('w2-anki-01', 'arxiv', 'W2 Anki Paper', ARRAY['Author Z'], 'http://w2a', $1)"
        " RETURNING id",
        user_id,
    )
    deck_id = await contract_conn.fetchval(
        "INSERT INTO decks (user_id, name) VALUES ($1, 'Anki Export Test Deck') RETURNING id",
        user_id,
    )
    await contract_conn.execute(
        "INSERT INTO cards (deck_id, paper_id, card_type, front, back, user_id)"
        " VALUES ($1, $2, 'concept', 'What is BERT?', 'A bidirectional encoder.', $3)",
        deck_id,
        paper_id,
        user_id,
    )

    session_id = await contract_conn.fetchval(
        "INSERT INTO sessions (user_id, expires_at) VALUES ($1, NOW() + INTERVAL '1 hour') RETURNING id",
        user_id,
    )
    cookie = str(session_id)

    async with make_contract_client(app, cookie) as c:
        resp = await c.get(f"/api/export/anki/{deck_id}")

    assert resp.status_code == 200, (
        f"Expected 200 for anki export, got {resp.status_code}: {resp.text[:300]}"
    )
    content_type = resp.headers.get("content-type", "")
    assert "octet-stream" in content_type or "zip" in content_type, (
        f"Expected binary content-type for .apkg; got {content_type}"
    )
    assert len(resp.content) > 0, ".apkg response must have non-zero bytes"


async def test_generation_w2_deck_create_persists_cards(
    le_contract_app_with_litellm_sidecar,
    contract_conn,
):
    """generate_cards_core persists LLM-generated cards to DB under real transaction.

    # Verified: services/learning_engine/learning_engine/routers/generation.py:43-192
    # Survivor-of: test_generation.py mock-unit assertions on generate_cards_core DB inserts
    """
    from learning_engine.routers.generation import generate_cards_core
    from learning_engine.card_models import CardGenerationOutput, CardOutput
    from jarvis_common.llm_client import LiteLLMConfig
    from learning_engine.card_generator import CardGenerator
    import httpx

    app, faux = le_contract_app_with_litellm_sidecar

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role)"
        " VALUES ('w2-deck-persist@contract.test', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        "INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)"
        " VALUES ('w2-deck-01', 'arxiv', 'W2 Deck Paper', ARRAY['Author Q'], 'http://w2d', $1)"
        " RETURNING id",
        user_id,
    )
    await contract_conn.execute(
        "INSERT INTO paper_chunks (paper_id, chunk_index, content)"
        " VALUES ($1, 0, 'Attention mechanism enables parallel processing in transformers.')",
        paper_id,
    )
    deck_id = await contract_conn.fetchval(
        "INSERT INTO decks (user_id, name) VALUES ($1, 'W2 Persist Deck') RETURNING id",
        user_id,
    )

    scripted = CardGenerationOutput(
        cards=[
            CardOutput(
                card_type="concept",
                front="What enables parallel processing in transformers?",
                back="The attention mechanism.",
                evidence_quote="Attention mechanism enables parallel processing in transformers.",
                page_number=1,
            )
        ]
    )
    faux.add_pydantic_response("smart", scripted)

    cg = CardGenerator(
        http_client=httpx.AsyncClient(),
        litellm_config=LiteLLMConfig(base_url=faux.url),
    )
    result = await generate_cards_core(
        pool=app.state.db_pool,
        http_client=httpx.AsyncClient(),
        paper_id=paper_id,
        deck_id=deck_id,
        max_cards=1,
        card_generator=cg,
        user_id=user_id,
    )

    assert result["cards_created"] >= 1, "At least one card must be persisted to DB"
    count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM cards WHERE deck_id = $1 AND paper_id = $2", deck_id, paper_id
    )
    assert count >= 1, "Card row must exist in DB after generate_cards_core"
