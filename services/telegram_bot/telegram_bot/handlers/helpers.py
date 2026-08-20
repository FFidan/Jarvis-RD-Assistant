"""Shared helper functions for Telegram bot handlers.

Provides common utilities for scoped HTTP clients, authorisation checks, and
running a handler's slow follow-up work outside the update loop.
"""

from __future__ import annotations

import logging
from collections.abc import Coroutine
from typing import Any

import httpx
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from telegram_bot.config import BotConfig
from telegram_bot.platform_client import resolve_pairing

logger = logging.getLogger(__name__)


def get_config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    """Return the shared ``BotConfig`` instance.

    Thin accessor kept as a named function (rather than inlined dict lookup)
    so that tests can patch ``helpers._get_config`` in one place instead of
    mocking ``context.application.bot_data`` at every call site.
    """
    return context.application.bot_data["config"]


def get_http(context: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    """Return the shared httpx async client.

    Thin accessor kept as a named function for mockability in tests.
    """
    return context.application.bot_data["http_client"]


def get_platform_http(context: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    """Return the scoped Platform HTTP client.

    Parameters
    ----------
    context : ContextTypes.DEFAULT_TYPE
        Telegram handler context whose application owns shared clients.

    Returns
    -------
    httpx.AsyncClient
        Client carrying Telegram's dedicated service credential.
    """
    return context.application.bot_data["platform_client"]


def get_jarvis_user_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return the JARVIS user-id cached by ``auth_required`` in ``context.user_data``.

    Returns ``None`` only when ``context.user_data`` is unavailable; for an
    authorised (paired) chat the decorator always stashes a real integer id.
    """
    return context.user_data.get("jarvis_user_id") if context.user_data is not None else None


async def auth_check(
    update: Update,
    config: BotConfig,
    platform_client: httpx.AsyncClient,
) -> tuple[bool, int | None]:
    """Check whether the incoming chat is paired and return its user_id.

    Platform pairing is the sole bot-identity mechanism.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    config : BotConfig
        Runtime configuration containing the Platform origin.
    platform_client : httpx.AsyncClient
        Scoped Platform client.

    Returns
    -------
    tuple[bool, int | None]
        ``(True, user_id)`` when Platform resolves the private chat, otherwise
        ``(False, None)``.

    Invariant: ``authorized is True`` ⟺ ``user_id is not None``.

    """
    chat = update.effective_chat
    if chat is None:
        return False, None
    # Identity is bound to chat_id, so a group/supergroup pairing would let every
    # member act as the paired user. Only 1:1 private chats may hold an identity.
    if chat.type != ChatType.PRIVATE:
        return False, None
    try:
        pairing = await resolve_pairing(platform_client, config, chat.id)
    except (httpx.HTTPError, RuntimeError):
        logger.warning(
            "auth_check: Platform pairing lookup failed; denying request",
            exc_info=True,
        )
        return False, None
    if pairing is None:
        return False, None
    return True, pairing.user_id


async def _log_detached_failure(work: Coroutine[Any, Any, None], description: str) -> None:
    """Await detached work, logging whatever it fails on instead of raising it."""
    try:
        await work
    except Exception:
        logger.exception("Detached %s failed", description)


def run_detached(
    context: ContextTypes.DEFAULT_TYPE,
    work: Coroutine[Any, Any, None],
    *,
    description: str,
) -> None:
    """Run one handler's slow follow-up work outside the update loop.

    The application processes updates one at a time, so a handler that awaits a
    multi-second backend call stops the bot answering anyone at all — including
    the user who sent it. Such a handler answers immediately and hands the slow
    part here, and the work reports its own outcome to the chat when it ends.

    Parameters
    ----------
    context : ContextTypes.DEFAULT_TYPE
        Handler context whose application owns the detached task. The
        application awaits its outstanding tasks when it stops, so work
        scheduled here is not dropped by an ordinary shutdown.
    work : Coroutine
        The follow-up work, already carrying everything it needs — including
        the callable it answers the chat through.
    description : str
        What the work does, named in the log line if it fails.
    """
    context.application.create_task(_log_detached_failure(work, description))
