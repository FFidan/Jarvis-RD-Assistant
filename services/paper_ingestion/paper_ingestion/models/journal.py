"""Pydantic models for journal entry CRUD endpoints (Phase 2 My Day)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class JournalPrompts(BaseModel):
    first_move: str | None = None
    worked: str | None = None
    blocked: str | None = None
    # UI_v3 EOD "shutdown ritual" (spec §3.10 / §4.3): one optional free-note
    # escape hatch ("anything else") so EOD never feels like a cage and the
    # live free-form Journal use case stays covered. Stored in the existing
    # ``prompts`` JSONB column — NO migration required (additive JSONB key).
    note: str | None = None


class JournalEntryCreate(BaseModel):
    date: date
    prompts: JournalPrompts


class JournalEntryResponse(BaseModel):
    id: int
    date: date
    prompts: JournalPrompts
    created_at: datetime
    updated_at: datetime
