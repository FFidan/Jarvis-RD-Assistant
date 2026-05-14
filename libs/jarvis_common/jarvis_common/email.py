"""SMTP delivery for transactional emails (magic-link, future invites).

Phase 2 WS-2A: ships only ``send_magic_link``. Plain-text only by design (better
deliverability, simpler debugging, no template engine dependency).

Dev-mode fallback: when any required SMTP env var is unset OR ``DEV_MODE=true``,
the link is logged to stdout AND written to ``system_events`` (category=auth,
message='magic_link_dev_mode') so the Logs Live tab catches it.
"""

from __future__ import annotations

import logging

import asyncpg

from jarvis_common.event_log import log_event
from jarvis_common.settings import get_core_settings, get_secrets_settings

logger = logging.getLogger(__name__)

_REQUIRED_SMTP_VARS = ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM")


def _smtp_configured() -> bool:
    """Return True iff every required SMTP env var has a non-empty value.

    SMTP_USER and SMTP_PASS are intentionally NOT required (some relays use
    IP-allowlist auth). HOST, PORT, and FROM are the minimum to compose +
    deliver an envelope.
    """
    s = get_secrets_settings()
    return all(getattr(s, name.lower()) is not None for name in _REQUIRED_SMTP_VARS)


def _dev_mode() -> bool:
    return get_core_settings().dev_mode


_PLAIN_BODY_TEMPLATE = (
    "Click the link below to sign in. The link expires in 15 minutes.\n"
    "\n"
    "{link}\n"
    "\n"
    "If you didn't request this, ignore this email.\n"
)


async def send_magic_link(
    email: str,
    link: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Deliver a magic-link email, or log it to stdout in dev mode.

    Parameters
    ----------
    email:
        Recipient address.
    link:
        Fully-qualified URL the user clicks to verify (e.g.
        ``https://localhost:3001/auth/verify?token=...``). Constructed by the
        caller to avoid this module knowing about the front-end origin.
    pool:
        Optional asyncpg pool. When supplied AND we're in dev-mode fallback,
        the link is also persisted as a ``system_events`` row so the Logs
        Live tab surfaces it. Pass ``app.state.db_pool`` from the request
        handler.
    """
    if _dev_mode() or not _smtp_configured():
        # Always emit a structured log so docker-compose logs pick it up
        # even when the system_events insert fails (e.g. fresh DB).
        logger.info(
            "magic_link_dev_mode email=%s link=%s",
            email,
            link,
        )
        if pool is not None:
            try:
                await log_event(
                    pool=pool,
                    level="info",
                    category="auth",
                    source="auth",
                    message="magic_link_dev_mode",
                    context={"email": email, "link": link},
                )
            except Exception:  # noqa: BLE001 — best-effort dev affordance
                logger.debug("system_events emit failed (non-fatal)", exc_info=True)
        return

    # Real SMTP path. Lazy import so jarvis_common doesn't pull aiosmtplib
    # in test environments that don't need it.
    from email.message import EmailMessage  # noqa: PLC0415

    import aiosmtplib  # noqa: PLC0415

    s = get_secrets_settings()
    host = s.smtp_host.get_secret_value() if s.smtp_host else ""
    port = int(s.smtp_port.get_secret_value()) if s.smtp_port else 587
    user = s.smtp_user.get_secret_value() if s.smtp_user else None
    password = s.smtp_pass.get_secret_value() if s.smtp_pass else None
    sender = s.smtp_from.get_secret_value() if s.smtp_from else ""

    message = EmailMessage()
    message["From"] = sender
    message["To"] = email
    message["Subject"] = "Sign in to JARVIS"
    message.set_content(_PLAIN_BODY_TEMPLATE.format(link=link))

    # Port 465 → implicit TLS; everything else → STARTTLS where supported.
    use_tls = port == 465
    start_tls = not use_tls

    await aiosmtplib.send(
        message,
        hostname=host,
        port=port,
        username=user,
        password=password,
        use_tls=use_tls,
        start_tls=start_tls,
    )
    logger.info("magic_link_sent email=%s", email)


__all__ = ["send_magic_link"]
