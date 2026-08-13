"""Central delivery policy for scheduled Telegram notifications."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, get_args

import httpx

from telegram_bot import services_client
from telegram_bot.config import BotConfig

logger = logging.getLogger(__name__)

ScheduledNotificationKind = Literal[
    "daily_summary",
    "paper_digest",
    "review_reminder",
    "deadline_warning",
    "research_pulse",
    "author_alert",
]

SCHEDULED_NOTIFICATION_KINDS = frozenset(get_args(ScheduledNotificationKind))


@dataclass(frozen=True, slots=True)
class ScheduledNotificationPolicy:
    """Suppress non-exempt scheduled delivery while focus is open.

    A lookup failure fails closed. The bot explicitly promises that scheduled
    notifications are paused, so uncertain focus state must not become an
    unsolicited delivery. Focus-completion and operator-failure notices are
    control messages and never pass through this policy.
    """

    http_client: httpx.AsyncClient
    config: BotConfig

    async def suppresses(
        self,
        user_id: int,
        kind: ScheduledNotificationKind,
    ) -> bool:
        if kind not in SCHEDULED_NOTIFICATION_KINDS:
            raise ValueError("Unknown scheduled notification kind")
        try:
            session = await services_client.fetch_active_focus_session(
                self.http_client,
                self.config,
                user_id,
            )
        except Exception:
            logger.warning(
                "Suppressing scheduled Telegram delivery because focus state is unavailable"
            )
            return True
        return session is not None and session.state in {"active", "paused"}
