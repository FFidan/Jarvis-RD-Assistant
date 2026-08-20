"""Daily briefing: the shared section gather and the scheduled morning overview."""

import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

import httpx
from telegram import Bot

from telegram_bot import services_client
from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_morning_briefing
from telegram_bot.notification_policy import ScheduledNotificationPolicy
from telegram_bot.platform_client import list_user_pairings
from telegram_bot.vocabulary import is_not_done

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BriefingSections:
    """The briefing sections as they were actually read for one user.

    A count is ``None`` when its backend read failed, so the briefing can say
    the number is unknown rather than present an outage as a real zero. The
    list sections use the same marker for the same reason: an unread list is
    ``None`` and says so, while an empty list stays empty and renders as no
    section at all. Conflating the two would report an outage as "nothing to
    do".
    """

    new_papers_count: int | None
    inbox_total: int | None
    due_cards: int | None
    open_tasks: list[dict[str, Any]] | None
    milestones: list[dict[str, Any]] | None


async def _count_or_unavailable(gather: Awaitable[int], section: str, user_id: int) -> int | None:
    """Await one count gather, returning ``None`` when the backend cannot be read."""
    try:
        return await gather
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("briefing: %s count failed for user_id=%s", section, user_id)
        return None


async def gather_briefing_sections(
    http_client: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> BriefingSections:
    """Read every briefing section, degrading each one independently.

    A transient failure on one gather must not suppress the rest of the
    briefing, so each section is read on its own and reports its own outcome.
    Both the ``/briefing`` command and the scheduled send read through here, so
    the two surfaces cannot drift apart.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    config : BotConfig
        Bot configuration.
    user_id : int
        DB user PK for scoping all per-user queries.
    """
    new_papers_count = await _count_or_unavailable(
        services_client.fetch_new_paper_count(http_client, config, user_id), "new-paper", user_id
    )
    inbox_total = await _count_or_unavailable(
        services_client.fetch_inbox_count(http_client, config, user_id), "inbox", user_id
    )
    due_cards = await _count_or_unavailable(
        services_client.fetch_due_card_count(http_client, config, user_id), "due-card", user_id
    )

    open_tasks: list[dict[str, Any]] | None = None
    try:
        # One page, then My Day's own rule: the endpoint filters on a single
        # status, so it cannot express the not-done set on its own.
        all_tasks = await services_client.fetch_tasks(
            http_client, config, user_id, limit=services_client.MAX_TASK_PAGE_SIZE
        )
        open_tasks = [task for task in all_tasks if is_not_done(task)]
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("briefing: tasks fetch failed for user_id=%s", user_id)

    milestones: list[dict[str, Any]] | None = None
    try:
        milestones = await services_client.fetch_upcoming_milestones(
            http_client, config, user_id, within_days=7
        )
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("briefing: milestones fetch failed for user_id=%s", user_id)

    return BriefingSections(new_papers_count, inbox_total, due_cards, open_tasks, milestones)


async def _run_briefing_for_chat(
    http_client: httpx.AsyncClient,
    bot: Bot,
    config: BotConfig,
    chat_id: int,
    user_id: int,
) -> None:
    """Send the daily briefing to a single chat.

    All product data comes from :func:`gather_briefing_sections`, the same
    gather the ``/briefing`` command runs.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    chat_id : int
        Target Telegram chat ID.
    user_id : int
        DB user PK for scoping all per-user queries.
    """
    sections = await gather_briefing_sections(http_client, config, user_id)
    message = format_morning_briefing(
        sections.new_papers_count,
        sections.inbox_total,
        sections.due_cards,
        sections.open_tasks,
        sections.milestones,
    )

    try:
        await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
        logger.info("Daily briefing sent to chat_id=%d user_id=%s", chat_id, user_id)
    except Exception:  # noqa: BLE001 — top-level send; must not crash the scheduler
        logger.exception("Failed to send daily briefing to chat_id=%d", chat_id)


async def run_daily_briefing(
    http_client: httpx.AsyncClient,
    platform_client: httpx.AsyncClient,
    bot: Bot,
    config: BotConfig,
    *,
    delivery_policy: ScheduledNotificationPolicy | None = None,
) -> None:
    """Send a combined morning briefing with papers, cards, and tasks.

    Iterates ``telegram_user_pairings`` and delivers per-user briefings.
    Skips with a warning when no pairings exist.  Each pairing's REST gather
    is wrapped so one user's backend error does not abort the whole run.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    platform_client : httpx.AsyncClient
        Scoped Platform client used to list active pairings.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    """
    pairings = await list_user_pairings(platform_client, config)
    if not pairings:
        logger.warning(
            "daily_briefing skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        if delivery_policy is not None and await delivery_policy.suppresses(
            pairing.user_id, "daily_summary"
        ):
            continue
        try:
            await _run_briefing_for_chat(http_client, bot, config, pairing.chat_id, pairing.user_id)
        except Exception:
            logger.exception(
                "Failed to build daily briefing for user_id=%s (chat_id=%d)",
                pairing.user_id,
                pairing.chat_id,
            )
            continue
