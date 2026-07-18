"""Tests for brace-escape isolation fix from verification corpus.

Ensures that literal { and } characters in paper text (e.g. math notation like
{x ∈ ℝⁿ}) do not leak into the verification corpus as escaped {{ / }} sequences,
which would cause substring matching to fail when the LLM returns a verbatim quote
containing those same braces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from jarvis_common.llm_client import LiteLLMConfig
from learning_engine.card_generator import CardGenerator
from learning_engine.card_models import CardGenerationOutput, CardOutput


def _make_generator() -> tuple[CardGenerator, AsyncMock]:
    """Build a CardGenerator with a mocked HTTP client."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    generator = CardGenerator(
        http_client=http_client,
        litellm_config=LiteLLMConfig(base_url="http://litellm:4000"),
    )
    return generator, http_client


def _make_openai_client() -> MagicMock:
    """Return a mock openai.AsyncOpenAI client."""
    return MagicMock()


@pytest.mark.asyncio
async def test_card_with_brace_quote_passes_verification() -> None:
    """A card whose evidence_quote contains literal { } must survive verification.

    brace-escape regression: before the fix, full_text was escaped ({{ / }}) before being
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
        return_value=CardGenerationOutput(
            cards=[
                CardOutput(
                    card_type="concept",
                    front="What is the feasible set?",
                    back="The set of all x satisfying Ax = b within ℝⁿ.",
                    evidence_quote="x ∈ {x ∈ ℝⁿ | Ax = b}",
                    page_number=1,
                )
            ]
        )
    )

    result = await generator.generate_cards(
        title="Convex Optimisation",
        authors=["Author A"],
        chunks=chunks,
        openai_client=_make_openai_client(),
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

    brace-escape regression (format side): before the fix the raw full_text was fed directly
    into CARD_GENERATION_PROMPT.format(...), causing KeyError on any token that
    looks like a format placeholder.

    After the fix, the escaped copy is used for .format() while the raw copy is
    kept for verification; this test confirms no exception is raised.
    """
    generator, _ = _make_generator()

    # Simulate the LLM returning None (parse failure / all quotes fail)
    generator._call_llm_for_cards = AsyncMock(return_value=None)

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
        openai_client=_make_openai_client(),
        paper_id=None,
        abstract="Abstract without braces.",
    )

    # With a None LLM result the pipeline returns _empty_result()
    assert isinstance(result, dict)
    assert "cards" in result


@pytest.mark.asyncio
async def test_generate_cards_prompt_preserves_raw_braces() -> None:
    """generate_cards must not double literal { } braces in the LLM prompt.

    brace-escape regression (prompt side): before the fix, full_text was
    escaped ({{ / }}) before being substituted as the ``text=`` VALUE of
    ``_CARD_DATA_TEMPLATE.format(...)``. ``str.format()`` never re-parses
    braces inside substituted values, so the doubled braces reached the LLM
    verbatim, corrupting LaTeX / set-notation paper text.
    """
    generator, _ = _make_generator()
    generator._call_llm_for_cards = AsyncMock(return_value=None)

    chunks = [
        {
            "id": 1,
            "content": "loss = {alpha}/2 for {x | Ax = b}",
            "page_number": 1,
        }
    ]

    await generator.generate_cards(
        title="t",
        authors=["a"],
        chunks=chunks,
        openai_client=_make_openai_client(),
        paper_id=1,
    )

    prompt = generator._call_llm_for_cards.call_args.args[0]
    assert "{alpha}" in prompt
    assert "{{alpha}}" not in prompt
    assert "{x | Ax = b}" in prompt
    assert "{{x | Ax = b}}" not in prompt


@pytest.mark.asyncio
async def test_generate_cards_digest_branch_preserves_raw_braces() -> None:
    """The reduce-stage digest branch must not double literal { } braces either.

    brace-escape regression (digest side): when a paper exceeds one context
    window, generate_cards substitutes ``summary_text`` (not the raw chunk join)
    as the ``text=`` VALUE of ``_CARD_DATA_TEMPLATE.format(...)``. Like the
    short-text path, that value must reach the LLM with its braces intact —
    ``str.format()`` never re-parses substituted values, so a pre-escaped
    ``{{beta}}`` would corrupt LaTeX / set-notation digests.
    """
    generator, _ = _make_generator()
    generator._call_llm_for_cards = AsyncMock(return_value=None)

    # A tiny context window pins the char budget to its floor; oversized
    # brace-free filler pushes full_text past it so the digest branch fires
    # (not the truncation fallback). The digest branch substitutes summary_text,
    # so the raw chunk marker must be absent while summary braces survive.
    chunk_marker = "RAW_CHUNK_MARKER_UNIQUE"
    chunks = [
        {"id": 1, "content": "A " * 4000 + chunk_marker, "page_number": 1},
    ]
    summary_text = "Digest with set notation {beta} preserved."

    await generator.generate_cards(
        title="t",
        authors=["a"],
        chunks=chunks,
        openai_client=_make_openai_client(),
        paper_id=1,
        summary_text=summary_text,
        num_ctx=2048,
    )

    prompt = generator._call_llm_for_cards.call_args.args[0]
    # summary_text reaching the prompt confirms the digest branch fired: the
    # else branch would substitute the raw chunk join (chunk_marker) instead.
    assert chunk_marker not in prompt
    assert "{beta}" in prompt
    assert "{{beta}}" not in prompt
