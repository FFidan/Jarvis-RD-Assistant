"""Pydantic output models for the weekly research summary generator.

Used by ``call_llm_structured`` (Instructor) in ``weekly_summary.py`` to
produce validated, typed output instead of raw dicts.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ThemeOutput(BaseModel):
    theme: str = Field(min_length=10, max_length=300)
    supporting_papers: list[int] = Field(min_length=1)
    notes: str | None = Field(default=None, max_length=500)


class WeeklyDigestOutput(BaseModel):
    themes: list[ThemeOutput] = Field(default_factory=list, max_length=5)
    summary: str = Field(min_length=20, max_length=600)
