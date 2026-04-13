"""Tests for QuoteVerifier integration in entity extraction (C1 fix).

These tests verify that:
1. Hallucinated evidence causes the KG edge to be dropped (strict-skip policy).
2. A verified evidence quote causes the matched_text to be persisted.
3. Rows with evidence that passes QuoteVerifier are actually inserted.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models import VerificationResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Create a mock pool whose acquire() context manager returns *conn*."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _chunk_row(
    *,
    content: str = "Neural ODEs are a continuous-depth model.",
    page_number: int = 3,
) -> dict:
    """Return a dict that satisfies row_to_chunk_response's field access."""
    return {
        "id": 11,
        "chunk_index": 0,
        "content": content,
        "page_number": page_number,
        "start_char": 0,
        "end_char": len(content),
        "embedding_id": None,
        "created_at": datetime.now(tz=UTC),
        "paper_id": 1,
    }


def _unverified() -> VerificationResult:
    return VerificationResult(quote="made-up text", verified=False)


_DEFAULT_EVIDENCE = "Neural ODEs are a continuous-depth model."


def _verified(text: str = _DEFAULT_EVIDENCE, page: int = 3) -> VerificationResult:
    return VerificationResult(
        quote=text,
        verified=True,
        match_type="exact",
        match_score=1.0,
        matched_text=text,
        page_number=page,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_rejects_hallucinated_evidence():
    """Relationships with evidence that fails verification must be dropped.

    The function should NOT call conn.fetchval for the INSERT, and
    dropped_relationships must be > 0 in the returned stats.
    """
    from app.entity_extractor import extract_entities_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Neural ODE Paper"}
    mock_conn.fetch.return_value = [_chunk_row()]
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetchval = AsyncMock(return_value=None)
    pool = _make_pool(mock_conn)

    llm_result = {
        "entities": [
            {"name": "Neural ODE", "type": "method", "description": "continuous depth"},
            {"name": "CIFAR-10", "type": "dataset", "description": "image dataset"},
        ],
        "relationships": [
            {
                "source": "Neural ODE",
                "target": "CIFAR-10",
                "type": "evaluated_on",
                "confidence": 0.9,
                # evidence is fabricated — will fail QuoteVerifier
                "evidence": "This completely made-up sentence was never in the paper.",
            }
        ],
    }

    with (
        patch("app.entity_extractor.call_llm", AsyncMock(return_value=llm_result)),
        patch(
            "app.entity_extractor._find_or_create_entity",
            AsyncMock(side_effect=[(1, False), (2, False)]),
        ),
        patch("app.entity_extractor.QuoteVerifier.verify_quote", return_value=_unverified()),
    ):
        result = await extract_entities_for_paper(AsyncMock(), pool, paper_id=1)

    # Relationship was dropped — fetchval for INSERT should never have been called
    mock_conn.fetchval.assert_not_awaited()
    assert result.relationships_added == 0
    assert result.dropped_relationships > 0


@pytest.mark.asyncio
async def test_extraction_persists_page_number_from_chunk():
    """Verified evidence must use matched_text (from verifier) in the INSERT.

    We assert that conn.fetchval is called with matched_text as the evidence
    parameter, confirming the canonicalised quote is persisted, not the raw
    LLM string.
    """
    from app.entity_extractor import extract_entities_for_paper

    chunk_content = "Neural ODEs are a continuous-depth model."
    matched_text = chunk_content  # exact match

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Neural ODE Paper"}
    mock_conn.fetch.return_value = [_chunk_row(content=chunk_content, page_number=3)]
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetchval = AsyncMock(return_value=1)  # INSERT succeeds
    pool = _make_pool(mock_conn)

    llm_result = {
        "entities": [
            {"name": "Neural ODE", "type": "method", "description": "continuous depth"},
            {"name": "CIFAR-10", "type": "dataset", "description": "image dataset"},
        ],
        "relationships": [
            {
                "source": "Neural ODE",
                "target": "CIFAR-10",
                "type": "evaluated_on",
                "confidence": 0.85,
                "evidence": chunk_content,
            }
        ],
    }

    with (
        patch("app.entity_extractor.call_llm", AsyncMock(return_value=llm_result)),
        patch(
            "app.entity_extractor._find_or_create_entity",
            AsyncMock(side_effect=[(1, False), (2, False)]),
        ),
        patch(
            "app.entity_extractor.QuoteVerifier.verify_quote",
            return_value=_verified(text=matched_text, page=3),
        ),
    ):
        result = await extract_entities_for_paper(AsyncMock(), pool, paper_id=1)

    assert result.relationships_added == 1
    assert result.dropped_relationships == 0

    # The INSERT call (fetchval) must have received matched_text, not the raw evidence
    insert_call_args = mock_conn.fetchval.await_args
    assert insert_call_args is not None
    positional_args = insert_call_args.args
    # Signature: (SQL, source_id, target_id, rel_type, paper_id, evidence_quote, confidence)
    # evidence_quote is arg index 5 (0-based after the SQL string)
    assert positional_args[5] == matched_text


@pytest.mark.asyncio
async def test_extraction_keeps_verified_rows():
    """Relationships whose evidence passes QuoteVerifier are persisted.

    A good-faith quote that matches a chunk should result in
    relationships_added == 1 and dropped_relationships == 0.
    """
    from app.entity_extractor import extract_entities_for_paper

    evidence_text = "Neural ODEs are a continuous-depth model."

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Neural ODE Paper"}
    mock_conn.fetch.return_value = [_chunk_row(content=evidence_text)]
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetchval = AsyncMock(return_value=1)
    pool = _make_pool(mock_conn)

    llm_result = {
        "entities": [
            {"name": "Neural ODE", "type": "method", "description": "continuous depth"},
            {"name": "ImageNet", "type": "dataset", "description": "large image dataset"},
        ],
        "relationships": [
            {
                "source": "Neural ODE",
                "target": "ImageNet",
                "type": "used_on",
                "confidence": 0.95,
                "evidence": evidence_text,
            }
        ],
    }

    with (
        patch("app.entity_extractor.call_llm", AsyncMock(return_value=llm_result)),
        patch(
            "app.entity_extractor._find_or_create_entity",
            AsyncMock(side_effect=[(1, False), (2, False)]),
        ),
        patch(
            "app.entity_extractor.QuoteVerifier.verify_quote",
            return_value=_verified(text=evidence_text),
        ),
    ):
        result = await extract_entities_for_paper(AsyncMock(), pool, paper_id=1)

    assert result.relationships_added == 1
    assert result.dropped_relationships == 0
    # One fetchval call for the relationship INSERT
    mock_conn.fetchval.assert_awaited_once()
