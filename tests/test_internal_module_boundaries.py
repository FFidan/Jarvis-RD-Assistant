"""Keep retired internal compatibility surfaces from returning unnoticed."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import jarvis_common.auth as auth
from paper_ingestion.routers import papers
from paper_ingestion.services import scheduler_effects
from telegram_bot.config import BotConfig

_ROOT = Path(__file__).resolve().parents[1]


def test_retired_modules_are_not_importable() -> None:
    retired = (
        "paper_ingestion." + "migrations_runner",
        "paper_ingestion.services." + "settings_service",
    )
    for module_name in retired:
        assert importlib.util.find_spec(module_name) is None


def test_retired_symbols_are_absent_from_their_former_owners() -> None:
    assert not hasattr(auth, "current_" + "user_id")
    assert not hasattr(scheduler_effects, "apply_" + "zotero_cron")
    assert not hasattr(scheduler_effects, "_apply_" + "cron_reschedule")
    assert not hasattr(BotConfig, "telegram_" + "chat_id")
    assert not hasattr(papers, "star_" + "paper")
    assert not hasattr(papers, "submit_" + "feedback")


def test_retired_telegram_environment_row_is_absent_from_compose() -> None:
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "TELEGRAM_" + "CHAT_ID" not in compose
