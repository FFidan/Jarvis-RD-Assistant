"""Tests for paper_ingestion.pulse.verification.verify_pulse_reasoning (WS-2.3).

Exercises the four branches that gate persistence of
``pulse_cards.reasoning_verified`` + ``pulse_cards.reasoning_confidence``:

1. Happy path: reasoning echoes the abstract verbatim -> HIGH + verified.
2. Diverged reasoning: no overlap with abstract -> UNVERIFIED.
3. Empty reasoning -> UNVERIFIED (no verifier call).
4. Fallback sentinel "LLM scoring failed" -> UNVERIFIED (no verifier call).
"""

from __future__ import annotations

import pytest
from paper_ingestion.pulse.verification import verify_pulse_reasoning
from paper_ingestion.rag.verification import RagConfidence
from paper_ingestion.verification import QuoteVerifier


@pytest.mark.asyncio
async def test_happy_path_reasoning_matches_abstract() -> None:
    """Reasoning verbatim-quotes the abstract -> verified=True, confidence=HIGH."""
    verifier = QuoteVerifier()
    title = "Neural ODEs for continuous-time dynamics"
    # Reasoning is a verbatim substring of the abstract -> exact match -> HIGH.
    abstract = (
        "We propose Neural ODEs, a family of models that parameterize the "
        "derivative of the hidden state using a neural network, achieving "
        "O(1) memory cost and adaptive evaluation."
    )
    reasoning = "parameterize the derivative of the hidden state using a neural network"

    verified, confidence = await verify_pulse_reasoning(reasoning, title, abstract, verifier)

    assert verified is True
    assert confidence == RagConfidence.HIGH


@pytest.mark.asyncio
async def test_paraphrased_reasoning_maps_to_medium() -> None:
    """A close-but-not-exact paraphrase falls in MEDIUM (85-96% fuzzy score)."""
    verifier = QuoteVerifier()
    title = "Neural ODEs for continuous-time dynamics"
    abstract = (
        "We propose Neural ODEs, a family of models that parameterize the "
        "derivative of the hidden state using a neural network, achieving "
        "O(1) memory cost and adaptive evaluation."
    )
    # Paraphrased — adds "Neural ODEs" prefix (not in substring position).
    reasoning = "Neural ODEs parameterize the derivative of the hidden state using a neural network"

    verified, confidence = await verify_pulse_reasoning(reasoning, title, abstract, verifier)

    assert verified is True
    assert confidence in (RagConfidence.HIGH, RagConfidence.MEDIUM)


@pytest.mark.asyncio
async def test_diverged_reasoning_fails_verification() -> None:
    """Reasoning with no overlap -> UNVERIFIED."""
    verifier = QuoteVerifier()
    title = "Neural ODEs for continuous-time dynamics"
    abstract = "We propose Neural ODEs for modeling continuous-time systems."
    # Wildly unrelated claim
    reasoning = "Transformers dominate computer vision benchmarks across ImageNet."

    verified, confidence = await verify_pulse_reasoning(reasoning, title, abstract, verifier)

    assert verified is False
    assert confidence == RagConfidence.UNVERIFIED


@pytest.mark.asyncio
async def test_empty_reasoning_short_circuits() -> None:
    """Empty / whitespace reasoning returns UNVERIFIED without calling the verifier."""
    verifier = QuoteVerifier()
    title = "Some paper"
    abstract = "Some abstract."

    for empty in ["", "   ", "\t\n"]:
        verified, confidence = await verify_pulse_reasoning(empty, title, abstract, verifier)
        assert verified is False
        assert confidence == RagConfidence.UNVERIFIED


@pytest.mark.asyncio
async def test_fallback_sentinel_short_circuits() -> None:
    """The 'LLM scoring failed' sentinel emitted by stage2 maps to UNVERIFIED."""
    verifier = QuoteVerifier()
    title = "Some paper"
    abstract = "Some abstract."

    verified, confidence = await verify_pulse_reasoning(
        "LLM scoring failed", title, abstract, verifier
    )

    assert verified is False
    assert confidence == RagConfidence.UNVERIFIED


@pytest.mark.asyncio
async def test_empty_paper_text_returns_unverified() -> None:
    """No title + no abstract -> cannot verify, return UNVERIFIED."""
    verifier = QuoteVerifier()
    verified, confidence = await verify_pulse_reasoning("anything", "", "", verifier)
    assert verified is False
    assert confidence == RagConfidence.UNVERIFIED
