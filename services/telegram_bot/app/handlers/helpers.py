"""Shared helper functions for Telegram bot handlers.

Provides common utilities for accessing bot configuration, database pool,
HTTP client, and authorisation checks.
"""

from __future__ import annotations

import asyncpg
import httpx
from telegram import Update
from telegram.ext import ContextTypes

from app.config import BotConfig


def _get_config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    """Return the shared ``BotConfig`` instance.

    Thin accessor kept as a named function (rather than inlined dict lookup)
    so that tests can patch ``helpers._get_config`` in one place instead of
    mocking ``context.application.bot_data`` at every call site.
    """
    return context.application.bot_data["config"]


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> asyncpg.Pool:
    """Return the shared asyncpg connection pool.

    Thin accessor kept as a named function for mockability in tests.
    """
    return context.application.bot_data["db_pool"]


def _get_http(context: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    """Return the shared httpx async client.

    Thin accessor kept as a named function for mockability in tests.
    """
    return context.application.bot_data["http_client"]


def _auth_check(update: Update, config: BotConfig) -> bool:
    """Check whether the incoming chat is authorised."""
    chat = update.effective_chat
    return chat is not None and chat.id == config.telegram_chat_id
