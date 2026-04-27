"""Tests for H1 fix: brace-escape isolation from verification corpus.

Ensures that literal { and } characters in paper text (e.g. math notation like
{x ∈ ℝⁿ}) do not leak into the verification corpus as escaped {{ / }} sequences,
which would cause substring matching to fail when the LLM returns a verbatim quote
containing those same braces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from jarvis_common.llm_client import LiteLLMConfig
from learning_engine.card_generator import CardGenerator


def _make_generator() -> tuple[CardGenerator, AsyncMock]:
    """Build a CardGenerator with a mocked HTTP client."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    generator = CardGenerator(
        http_client=http_client,
        litellm_config=LiteLLMConfig(base_url="http://litellm:4000", api_key="secret"),
    )
    return generator, http_client


@pytest.mark.asyncio
async def test_card_with_brace_quote_passes_verification() -> None:
    """A card whose evidence_quote contains literal { } must survive verification.

    H1 regression: before the fix, full_text was escaped ({{ / }}) before being
    passed to _verify_raw_cards.  The LLM returns quotes with literal { / }, so
    the normalized substring match failed and all math-heavy cards were discarded,
    leaving only the abstract fallback.

    After the fix, full_text (raw) is passed to verification, so the quote
    "{x ∈ ℝⁿ | Ax = b}" is found via substring match and the card is kept.
    """
    generator, _ = _make_generator()

    paper_text = "Let x ∈ {x ∈ ℝⁿ | Ax = b} be the feasible set."
    chunks = [{"id": 1, "content": paper_text, "page_number": 1}]

    # LLM returns a card whose quote is a verbatim substring of paper_text,
    # including literal braces (as the LLM would actually return them).
    generator._call_llm_for_cards = AsyncMock(
        return_value=[
            {
                "card_type": "concept",
                "front": "What is the feasible set?",
                "back": "The set of all x satisfying Ax = b within ℝⁿ.",
                "evidence_quote": "{x ∈ ℝⁿ | Ax = b}",
                "page_number": 1,
            }
        ]
    )

    result = await generator.generate_cards(
        title="Convex Optimisation",
        authors=["Author A"],
        chunks=chunks,
        paper_id=None,
        abstract="Abstract text.",
    )

    # The card should be verified and kept — NOT discarded and replaced by the abstract fallback
    assert result["verified_count"] == 1, (
        "Card with brace quote should pass verification; got abstract fallback instead"
    )
    assert result["total_count"] == 1
    assert len(result["cards"]) == 1
    # Confirm this is NOT the abstract fallback card
    assert result["cards"][0].get("evidence", {}).get("verified") is not False, (
        "Verified card should not carry verified=False (that flag belongs to abstract fallbacks)"
    )
    assert result["cards"][0]["front"] == "What is the feasible set?"


@pytest.mark.asyncio
async def test_full_text_with_braces_does_not_crash_format() -> None:
    """Paper text containing literal { x } must not raise KeyError from str.format().

    H1 regression (format side): before the fix the raw full_text was fed directly
    into CARD_GENERATION_PROMPT.format(...), causing KeyError on any token that
    looks like a format placeholder.

    After the fix, the escaped copy is used for .format() while the raw copy is
    kept for verification; this test confirms no exception is raised.
    """
    generator, _ = _make_generator()

    # Simulate the LLM returning an empty list (all quotes fail, or none generated)
    generator._call_llm_for_cards = AsyncMock(return_value=[])

    chunks_with_braces = [
        {
            "id": 1,
            "content": "The gradient ∇f(x) vanishes at every {x} in the constraint set.",
            "page_number": 2,
        }
    ]

    # Must not raise KeyError (or any other exception) despite braces in paper text
    result = await generator.generate_cards(
        title="Optimisation with Braces",
        authors=["Dr. Brace"],
        chunks=chunks_with_braces,
        paper_id=None,
        abstract="Abstract without braces.",
    )

    # With an empty LLM result the pipeline falls back to the abstract card
    assert isinstance(result, dict)
    assert "cards" in result
