"""Tests for QuoteVerifier integration in entity extraction (C1 fix).

These tests verify that:
1. Hallucinated evidence causes the KG edge to be dropped (strict-skip policy).
2. A verified evidence quote causes the matched_text to be persisted.
3. Rows with evidence that passes QuoteVerifier are actually inserted.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.extraction.kg_models import (
    KGEntityCandidate,
    KGExtractionOutput,
    KGRelationshipCandidate,
)
from paper_ingestion.models import VerificationResult


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
    from paper_ingestion.extraction.entities import extract_entities_for_paper

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Neural ODE Paper"}
    mock_conn.fetch.return_value = [_chunk_row()]
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetchval = AsyncMock(return_value=None)
    pool, _ = make_pool_and_conn(conn=mock_conn)

    kg_result = KGExtractionOutput(
        entities=[
            KGEntityCandidate(name="Neural ODE", type="method", description="continuous depth"),
            KGEntityCandidate(name="CIFAR-10", type="dataset", description="image dataset"),
        ],
        relationships=[
            KGRelationshipCandidate(
                source="Neural ODE",
                target="CIFAR-10",
                type="evaluates",
                evidence="This completely made-up sentence was never in the paper.",
                confidence=0.9,
            )
        ],
    )

    with (
        patch(
            "paper_ingestion.extraction.entities.call_llm_structured",
            AsyncMock(return_value=kg_result),
        ),
        patch(
            "paper_ingestion.extraction.entities._find_or_create_entity",
            AsyncMock(side_effect=[(1, False), (2, False)]),
        ),
        patch(
            "paper_ingestion.extraction.entities.QuoteVerifier.verify_quote",
            return_value=_unverified(),
        ),
    ):
        result = await extract_entities_for_paper(
            AsyncMock(), pool, paper_id=1, openai_client=MagicMock()
        )

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
    from paper_ingestion.extraction.entities import extract_entities_for_paper

    chunk_content = "Neural ODEs are a continuous-depth model."
    matched_text = chunk_content  # exact match

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Neural ODE Paper"}
    mock_conn.fetch.return_value = [_chunk_row(content=chunk_content, page_number=3)]
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetchval = AsyncMock(return_value=1)  # INSERT succeeds
    pool, _ = make_pool_and_conn(conn=mock_conn)

    kg_result = KGExtractionOutput(
        entities=[
            KGEntityCandidate(name="Neural ODE", type="method", description="continuous depth"),
            KGEntityCandidate(name="CIFAR-10", type="dataset", description="image dataset"),
        ],
        relationships=[
            KGRelationshipCandidate(
                source="Neural ODE",
                target="CIFAR-10",
                type="evaluates",
                evidence=chunk_content,
                confidence=0.85,
            )
        ],
    )

    with (
        patch(
            "paper_ingestion.extraction.entities.call_llm_structured",
            AsyncMock(return_value=kg_result),
        ),
        patch(
            "paper_ingestion.extraction.entities._find_or_create_entity",
            AsyncMock(side_effect=[(1, False), (2, False)]),
        ),
        patch(
            "paper_ingestion.extraction.entities.QuoteVerifier.verify_quote",
            return_value=_verified(text=matched_text, page=3),
        ),
    ):
        result = await extract_entities_for_paper(
            AsyncMock(), pool, paper_id=1, openai_client=MagicMock()
        )

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
    from paper_ingestion.extraction.entities import extract_entities_for_paper

    evidence_text = "Neural ODEs are a continuous-depth model."

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Neural ODE Paper"}
    mock_conn.fetch.return_value = [_chunk_row(content=evidence_text)]
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetchval = AsyncMock(return_value=1)
    pool, _ = make_pool_and_conn(conn=mock_conn)

    kg_result = KGExtractionOutput(
        entities=[
            KGEntityCandidate(name="Neural ODE", type="method", description="continuous depth"),
            KGEntityCandidate(name="ImageNet", type="dataset", description="large image dataset"),
        ],
        relationships=[
            KGRelationshipCandidate(
                source="Neural ODE",
                target="ImageNet",
                type="used_on",
                evidence=evidence_text,
                confidence=0.95,
            )
        ],
    )

    with (
        patch(
            "paper_ingestion.extraction.entities.call_llm_structured",
            AsyncMock(return_value=kg_result),
        ),
        patch(
            "paper_ingestion.extraction.entities._find_or_create_entity",
            AsyncMock(side_effect=[(1, False), (2, False)]),
        ),
        patch(
            "paper_ingestion.extraction.entities.QuoteVerifier.verify_quote",
            return_value=_verified(text=evidence_text),
        ),
    ):
        result = await extract_entities_for_paper(
            AsyncMock(), pool, paper_id=1, openai_client=MagicMock()
        )

    assert result.relationships_added == 1
    assert result.dropped_relationships == 0
    # One fetchval call for the relationship INSERT
    mock_conn.fetchval.assert_awaited_once()


# ---------------------------------------------------------------------------
# δ2 — full-text verifier: saved_by_full_text_verify counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_uses_full_text_for_verification():
    """verify_quote must be called with the full un-truncated text.

    BE-C01 regression: previously full_text was truncated to 12000 chars before
    being passed to verify_quote, so evidence beyond char 12000 was silently
    dropped.  Now verify_quote receives the original full_text.

    We simulate a paper whose evidence appears beyond the 12000-char mark by
    providing a long chunk whose content appears after the first 12000 chars.
    """
    from paper_ingestion.extraction.entities import extract_entities_for_paper

    # Create content that pushes evidence past the 12000-char LLM cap
    padding = "A" * 12100
    evidence_text = "This evidence appears beyond the context window cap."
    # The chunk content starts with padding, evidence at position ~12100
    long_content = padding + " " + evidence_text

    chunk = {
        "id": 11,
        "chunk_index": 0,
        "content": long_content,
        "page_number": 5,
        "start_char": 0,
        "end_char": len(long_content),
        "embedding_id": None,
        "created_at": __import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        "paper_id": 1,
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {"id": 1, "title": "Long Paper"}
    mock_conn.fetch.return_value = [chunk]
    mock_conn.execute = AsyncMock(return_value="UPDATE 1")
    mock_conn.fetchval = AsyncMock(return_value=1)
    pool, _ = make_pool_and_conn(conn=mock_conn)

    kg_result = KGExtractionOutput(
        entities=[
            KGEntityCandidate(name="Method A", type="method", description="a method"),
            KGEntityCandidate(name="Dataset B", type="dataset", description="a dataset"),
        ],
        relationships=[
            KGRelationshipCandidate(
                source="Method A",
                target="Dataset B",
                type="evaluates",
                evidence=evidence_text,
                confidence=0.9,
            )
        ],
    )

    # The verifier returns success; evidence_text is present in full_text at pos > 12000
    verified_result = _verified(text=evidence_text, page=5)

    with (
        patch(
            "paper_ingestion.extraction.entities.call_llm_structured",
            AsyncMock(return_value=kg_result),
        ),
        patch(
            "paper_ingestion.extraction.entities._find_or_create_entity",
            AsyncMock(side_effect=[(1, False), (2, False)]),
        ),
        patch(
            "paper_ingestion.extraction.entities.QuoteVerifier.verify_quote",
            return_value=verified_result,
        ),
    ):
        result = await extract_entities_for_paper(
            AsyncMock(), pool, paper_id=1, openai_client=MagicMock()
        )

    # Relationship was added — full-text verify succeeded
    assert result.relationships_added == 1
    assert result.dropped_relationships == 0
    # saved_by_full_text_verify must be > 0 because the evidence is beyond char 12000
    assert result.saved_by_full_text_verify >= 1
