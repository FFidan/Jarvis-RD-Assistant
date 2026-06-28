"""Pydantic models for structured LLM output in contradiction detection."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ContradictionClassification(BaseModel):
    """Structured LLM output for a single paper-pair claim assessment.

    Attributes
    ----------
    is_contradiction : bool
        Whether the pair of findings constitutes a genuine contradiction
        (equivalent to ``stance == "opposes"``).
    stance : str
        How the two findings relate on their shared claim: ``"supports"``
        (they agree), ``"opposes"`` (they conflict — a contradiction), or
        ``"neutral"`` (they differ in scope/method/dataset or are unrelated).
        Only ``"supports"`` and ``"opposes"`` are persisted.
    claim_topic : str
        Short normalized noun phrase naming the shared claim both findings
        address, used to cluster related findings in the consensus view.
    contradiction_type : str
        Category of the contradiction: ``"direct"`` (factual opposition),
        ``"methodological"``, ``"result"``, or ``"interpretation"``.
    explanation : str
        Human-readable explanation (10–400 characters).
    quote_a : str
        Verbatim supporting quote from paper A.  Required when ``stance`` is
        ``"supports"`` or ``"opposes"``.
    quote_b : str
        Verbatim supporting quote from paper B.  Required when ``stance`` is
        ``"supports"`` or ``"opposes"``.
    confidence : float
        LLM self-reported confidence in the classification (0.0–1.0).
    """

    is_contradiction: bool
    stance: Literal["supports", "opposes", "neutral"]
    claim_topic: str = Field(
        default="",
        max_length=200,
        description="Short normalized noun phrase naming the shared claim "
        "(e.g. 'effect of caffeine on memory').",
    )
    contradiction_type: Literal["direct", "methodological", "result", "interpretation"] = "direct"
    explanation: str = Field(min_length=10, max_length=400)
    quote_a: str = Field(default="")
    quote_b: str = Field(default="")
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _quotes_required_when_persisted(self) -> ContradictionClassification:
        # A persisted stance ('supports'/'opposes') must carry verbatim quotes
        # from both papers: the downstream QuoteVerifier and the NOT NULL quote
        # columns would otherwise drop the row. 'neutral' is never persisted.
        needs_quotes = self.is_contradiction or self.stance in ("supports", "opposes")
        if needs_quotes and (not self.quote_a.strip() or not self.quote_b.strip()):
            raise ValueError("a supports/opposes stance requires non-empty quote_a and quote_b")
        return self
