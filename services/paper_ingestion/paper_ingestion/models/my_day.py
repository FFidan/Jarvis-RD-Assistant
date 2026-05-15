"""Pydantic models for the on-the-fly § Yesterday rollup (UI_v3 My-Day).

Spec ``docs/superpowers/specs/2026-05-15-my-day-parity-pomodoro-design.md``
§3.2 / §4.2: § Yesterday is an **on-the-fly query**, NOT a materialized
daily-rollup job (YAGNI; the stated rollup blocker is removed). Derived from
``tasks`` (completed / deferred) and ``daily_log`` (focus hours, cards
reviewed) filtered to yesterday's boundary in the caller's local day.

Note: ``from __future__ import annotations`` is intentionally absent — this
module is imported by ``routers/my_day.py`` which uses these as response
models alongside FastAPI body models (see that file's PydanticUserError note).
"""

from datetime import date

from pydantic import BaseModel


class YesterdayTask(BaseModel):
    """A task surfaced in § Yesterday — completed (Check) or deferred (carry over)."""

    id: int
    title: str
    status: str


class YesterdaySummary(BaseModel):
    """The § Yesterday card payload (prototype ``v5-calm-ritual-v2.jsx:72-91``).

    ``focused_hours`` / ``cards_reviewed`` / ``tasks_done`` populate the header
    note ``{focused}h focused · {cards} cards · {n} tasks done``. ``completed``
    renders with a Check icon; ``deferred`` renders strikethrough with the
    ``carry over →`` action that re-opens a deferred task into today.
    """

    date: date
    focused_hours: float
    cards_reviewed: int
    tasks_done: int
    completed: list[YesterdayTask]
    deferred: list[YesterdayTask]
