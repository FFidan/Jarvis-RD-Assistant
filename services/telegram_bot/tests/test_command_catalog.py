"""Exact parity checks for Telegram command registration, menu, and help."""

from __future__ import annotations

import html
import re
from pathlib import Path
from unittest.mock import MagicMock

from telegram.ext import CommandHandler
from telegram_bot.command_catalog import (
    COMMAND_CATALOG,
    command_spec,
    menu_command_specs,
    standard_command_specs,
)
from telegram_bot.formatters import format_help
from telegram_bot.handlers.commands.registry import register_command_handlers
from telegram_bot.handlers.review_handler import get_review_conversation_handler

REPO_ROOT = Path(__file__).resolve().parents[3]


def _handler_commands(handler: CommandHandler) -> set[str]:
    return set(handler.commands)


def test_standard_registry_matches_the_catalog_exactly() -> None:
    app = MagicMock()

    register_command_handlers(app)

    registered = set()
    for call in app.add_handler.call_args_list:
        registered.update(_handler_commands(call.args[0]))
    assert registered == {spec.name for spec in standard_command_specs()}


def test_review_and_cancel_have_review_only_ownership() -> None:
    conversation = get_review_conversation_handler()
    review_commands = set()
    for handler in conversation.entry_points:
        if isinstance(handler, CommandHandler):
            review_commands.update(_handler_commands(handler))
    cancel_commands = set()
    for handler in conversation.fallbacks:
        if isinstance(handler, CommandHandler):
            cancel_commands.update(_handler_commands(handler))

    assert review_commands == {"review"}
    assert cancel_commands == {"cancel"}
    assert command_spec("review").owner == "review"
    assert command_spec("cancel").owner == "review"
    assert command_spec("cancel").include_in_menu is False


def test_menu_and_help_are_exact_catalog_projections() -> None:
    menu_names = {spec.name for spec in menu_command_specs()}
    help_text = format_help()

    assert menu_names == {spec.name for spec in COMMAND_CATALOG} - {"cancel"}
    for spec in COMMAND_CATALOG:
        rendered = f"/{html.escape(spec.usage)} — {html.escape(spec.description)}"
        assert help_text.count(rendered) == 1
    assert "/papers [query]" in help_text
    assert "/focus [minutes]" in help_text
    assert "/cancel — Cancel the current flashcard review" in help_text


def test_manual_command_table_matches_catalog_usage_exactly() -> None:
    manual = (REPO_ROOT / "docs/manual/telegram.md").read_text(encoding="utf-8")
    command_table = manual.split("## Commands", 1)[1].split("### Inline actions", 1)[0]
    documented = re.findall(r"^\| `/(.+?)` \|", command_table, re.MULTILINE)

    assert documented == [spec.usage for spec in COMMAND_CATALOG]
