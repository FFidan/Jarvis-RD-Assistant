"""Pydantic output models for the weekly research summary generator.

Used by ``call_llm_structured`` (Instructor) in ``weekly_summary.py`` to
produce validated, typed output instead of raw dicts.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ThemeOutput(BaseModel):
    """A single cross-paper theme in the weekly digest.

    Attributes
    ----------
    theme : str
        One-sentence theme description (10–300 characters).
    supporting_papers : list[int]
        1-indexed positions of supporting papers referenced in the prompt
        context (e.g. ``[1, 3]`` means Paper 1 and Paper 3).
    notes : str | None
        Optional additional notes, contradictions, or open questions (max 500 chars).
    """

    theme: str = Field(min_length=10, max_length=300)
    supporting_papers: list[int] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=500)


class WeeklyDigestOutput(BaseModel):
    """Structured LLM output for the weekly research digest.

    Attributes
    ----------
    themes : list[ThemeOutput]
        Up to 5 cross-paper themes identified by the LLM.
    summary : str
        2–4 sentence executive summary (20–600 characters).
    """

    themes: list[ThemeOutput] = Field(default_factory=list, max_length=5)
    summary: str = Field(min_length=20, max_length=600)
