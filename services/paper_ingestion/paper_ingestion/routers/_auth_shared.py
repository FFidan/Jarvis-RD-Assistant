"""Helpers shared by the magic-link flows in ``auth``, ``account`` and ``admin``.

Sign-in links, invite links and email-change links are all minted from the same
``magic_link_tokens`` table, so the cooldown probe and the verify-URL derivation
must stay identical across the three routers.
"""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import Request
from jarvis_common.settings import get_core_settings

MAGIC_LINK_COOLDOWN = timedelta(minutes=2)


async def magic_link_on_cooldown(conn, user_id: int, *, email_change: bool) -> bool:
    """Return True when a token of the requested kind was minted within the cooldown.

    ``email_change`` selects the pending-email tokens; login/invite links carry a
    NULL ``pending_email``. The two kinds are probed separately so an email-change
    link never suppresses a sign-in link, or vice versa.
    """
    predicate = "IS NOT NULL" if email_change else "IS NULL"
    recent = await conn.fetchval(
        "SELECT created_at FROM magic_link_tokens"
        f" WHERE user_id = $1 AND pending_email {predicate}"
        " ORDER BY created_at DESC LIMIT 1",
        user_id,
    )
    return (
        recent is not None and datetime.now(UTC) - recent.replace(tzinfo=UTC) < MAGIC_LINK_COOLDOWN
    )


def build_verify_link(
    request: Request, token: str, *, logger: logging.Logger, link_kind: str
) -> str:
    """Construct the ``/auth/verify`` URL the recipient clicks.

    Honours ``APP_BASE_URL`` when set; otherwise derives the URL from the
    incoming request, whose scheme and host ProxyHeadersMiddleware has already
    replaced with the public-facing values.

    The misconfiguration warning is emitted through the caller's ``logger`` and
    names its ``link_kind``, so operators keep the per-flow log signal they
    filter production logs by.
    """
    from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

    base = get_paper_ingestion_settings().app_base_url
    fragment = urlencode({"token": token})
    if base:
        return f"{base.rstrip('/')}/auth/verify#{fragment}"
    if get_core_settings().environment == "production":
        logger.warning(
            "APP_BASE_URL is unset in production; the %s is derived from the "
            "request origin and may be wrong behind a tunnel or proxy. Set APP_BASE_URL "
            "to the public URL.",
            link_kind,
        )
    verify_url = str(request.url.replace(path="/auth/verify", query=""))
    return f"{verify_url}#{fragment}"
