"""Shared test fixtures for telegram_bot tests.

Loaded automatically by pytest before any test file in this directory.
Module stubs MUST be at module level (not in fixtures) because they need
to be installed before any ``import app.*`` triggers transitive imports
of telegram and apscheduler, which are only available inside Docker.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# 1. Path setup
# ---------------------------------------------------------------------------
_SERVICE_ROOT = str(Path(__file__).resolve().parents[1])
if _SERVICE_ROOT not in sys.path:
    sys.path.insert(0, _SERVICE_ROOT)

# ---------------------------------------------------------------------------
# 2. Telegram + APScheduler module stubs
#    Guards ensure existing per-file stubs are not overwritten.
# ---------------------------------------------------------------------------
for _mod_name in (
    "telegram",
    "telegram.ext",
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers",
    "apscheduler.triggers.cron",
):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

# Set attributes on stubs that existing tests rely on at import time.
_tg = sys.modules["telegram"]
_tg.Update = MagicMock
_tg.InlineKeyboardButton = lambda *a, **kw: MagicMock()
_tg.InlineKeyboardMarkup = lambda *a, **kw: MagicMock()
_tg.BotCommand = lambda cmd, desc: (cmd, desc)


# Message must be a real class so isinstance(query.message, Message) works in handlers.
class _StubMessage:
    """Minimal stub for telegram.Message used across handler tests."""


_tg.Message = _StubMessage

_tg_ext = sys.modules["telegram.ext"]
_tg_ext.Application = MagicMock
_tg_ext.CommandHandler = MagicMock
_tg_ext.CallbackQueryHandler = MagicMock
_tg_ext.ContextTypes = MagicMock()
_tg_ext.ContextTypes.DEFAULT_TYPE = MagicMock
_tg_ext.ConversationHandler = MagicMock()
_tg_ext.ConversationHandler.END = -1
