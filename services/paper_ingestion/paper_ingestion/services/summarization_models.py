"""Pydantic models for structured LLM output in paper summarization.

These models define the contract between the LLM and ``generate_paper_summary``.
Used by ``call_llm_structured`` (via Instructor) to guarantee the response
shape — eliminating the need for ``json.loads`` + ``dict.get`` chains and
the malformed-JSON failure modes that came with them.
"""

from __future__ import annotations

from pydantic import BaseModel


class KeyFindingOutput(BaseModel):
    """A single LLM-claimed finding with a verbatim supporting quote."""

    finding: str
    quote: str
    page_number: int | None = None


class SummarizationOutput(BaseModel):
    """Full LLM summary payload returned for a single paper.

    The ``key_findings`` list is later filtered against the source text by
    ``QuoteVerifier`` — unverified findings are discarded before persistence.
    """

    tldr: str = ""
    summary_brief: str = ""
    summary_detailed: str = ""
    key_findings: list[KeyFindingOutput] = []
    methodology: str | None = None
    limitations: str | None = None
    relevance_notes: str | None = None
