"""Pydantic models for journal entry CRUD endpoints (Phase 2 My Day)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class JournalPrompts(BaseModel):
    first_move: str | None = None
    worked: str | None = None
    blocked: str | None = None


class JournalEntryCreate(BaseModel):
    date: date
    prompts: JournalPrompts


class JournalEntryResponse(BaseModel):
    id: int
    date: date
    prompts: JournalPrompts
    created_at: datetime
    updated_at: datetime
