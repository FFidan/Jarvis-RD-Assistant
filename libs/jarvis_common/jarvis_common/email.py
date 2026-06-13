"""SMTP delivery for transactional emails (magic-link, future invites).

Ships ``send_magic_link`` and the public ``smtp_configured`` probe.
Plain-text only by design (better deliverability, simpler debugging,
no template engine dependency).

Dev-mode fallback: when SMTP is unconfigured (``smtp_configured`` returns
``False``) OR ``DEV_MODE=true``, the magic-link is NOT delivered anywhere
visible to end users.  It is NOT logged to stdout — only a SHA-256 hash of
the recipient email is recorded in ``system_events`` (category=auth,
message='magic_link_dev_mode') so the Logs Live tab can surface it.
The raw link (a bearer token) is never written to any log.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import asyncpg

from jarvis_common.crypto import resolve_secret_row
from jarvis_common.event_log import log_event
from jarvis_common.net import _reject_non_public_host
from jarvis_common.settings import get_core_settings, get_secrets_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EffectiveSmtp:
    """Resolved SMTP relay config: DB (``user_config``) layered over env."""

    host: str
    port: int
    user: str | None
    password: str | None
    sender: str

    @property
    def deliverable(self) -> bool:
        """True iff host + sender are present (the minimum to send an envelope)."""
        return bool(self.host) and bool(self.sender)


def _env_smtp() -> _EffectiveSmtp:
    """Read SMTP from process-cached env (``SecretsSettings``)."""
    s = get_secrets_settings()
    return _EffectiveSmtp(
        host=s.smtp_host.get_secret_value() if s.smtp_host else "",
        port=int(s.smtp_port.get_secret_value()) if s.smtp_port else 587,
        user=s.smtp_user.get_secret_value() if s.smtp_user else None,
        password=s.smtp_pass.get_secret_value() if s.smtp_pass else None,
        sender=s.smtp_from.get_secret_value() if s.smtp_from else "",
    )


async def _effective_smtp(pool: asyncpg.Pool | None) -> _EffectiveSmtp:
    """Return the SMTP config the sender should actually use.

    The first-run wizard writes SMTP to ``user_config`` (system-wide rows,
    ``user_id IS NULL``); ``smtp.pass`` is Fernet-encrypted via the same
    crypto helper ``_persist_config`` uses. We read those rows and layer them
    over the env (``SecretsSettings``) values per-field, so a wizard-saved
    relay takes effect WITHOUT a service restart + hand-edited .env. Any
    field absent/empty in the DB falls back to the env value.
    """
    env = _env_smtp()
    if pool is None:
        return env

    keys = ("smtp.host", "smtp.port", "smtp.user", "smtp.from", "smtp.pass")
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value, encrypted_value FROM user_config "
                "WHERE key = ANY($1::text[]) AND user_id IS NULL",
                list(keys),
            )
    except Exception:  # noqa: BLE001 — DB unreachable → fall back to env
        logger.debug("smtp user_config read failed; using env", exc_info=True)
        return env
    by_key = {r["key"]: r for r in rows}

    def _plain(key: str) -> str | None:
        row = by_key.get(key)
        if row is None:
            return None
        # asyncpg JSONB codec auto-decodes — value is already a scalar.
        value = row["value"]
        if value is None or str(value) == "":
            return None
        return str(value)

    host = _plain("smtp.host") or env.host
    port_raw = _plain("smtp.port")
    port = env.port
    if port_raw is not None:
        try:
            port = int(port_raw)
        except ValueError:
            port = env.port
    user = _plain("smtp.user") or env.user
    sender = _plain("smtp.from") or env.sender

    password = env.password
    pass_row = by_key.get("smtp.pass")
    if pass_row is not None and pass_row["encrypted_value"] is not None:
        try:
            decrypted = resolve_secret_row(pass_row)
        except Exception:  # noqa: BLE001 — bad key/tamper → keep env password
            logger.warning("smtp.pass decrypt failed; falling back to env", exc_info=True)
        else:
            if decrypted:
                password = decrypted

    return _EffectiveSmtp(host=host, port=port, user=user, password=password, sender=sender)


async def _smtp_configured(pool: asyncpg.Pool | None = None) -> bool:
    """Return True iff SMTP is configured via the DB OR env.

    SMTP_USER and SMTP_PASS are intentionally NOT required (some relays use
    IP-allowlist auth). HOST and FROM are the minimum to compose + deliver an
    envelope. The wizard-written ``user_config`` rows count the same as env.
    """
    return (await _effective_smtp(pool)).deliverable


def _dev_mode() -> bool:
    return get_core_settings().dev_smtp_log_only


def _hash_email(email: str) -> str:
    """Return a SHA-256 hex digest of the email address for safe logging.

    Use this wherever an email must appear in a log record instead of the raw
    address (or any derived secret such as a magic-link token).
    """
    return hashlib.sha256(email.encode()).hexdigest()


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
    """Deliver a magic-link email, or silently drop it when SMTP is unconfigured.

    Parameters
    ----------
    email:
        Recipient address.
    link:
        Fully-qualified URL the user clicks to verify (e.g.
        ``https://localhost:3001/auth/verify?token=...``). Constructed by the
        caller to avoid this module knowing about the front-end origin.
    pool:
        Optional asyncpg pool. When supplied and SMTP is unconfigured or
        ``DEV_SMTP_LOG_ONLY=true``, the event is persisted as a
        ``system_events`` row (category=auth, message='magic_link_dev_mode')
        so the Logs Live tab can surface it. Pass ``app.state.db_pool``
        from the request handler.

    Security note: the raw ``link`` (and the embedded token) is **never**
    logged.  When SMTP is unconfigured the link is silently dropped — only
    a SHA-256 hash of the recipient email is recorded so operators can
    correlate events without the log becoming a bearer-token store.

    """
    # Resolve the effective relay once: wizard-written user_config layered
    # over env. This is what makes a wizard-saved SMTP relay send mail with
    # NO restart / hand-edited .env — the DB is the durable source of truth.
    smtp = await _effective_smtp(pool)

    if _dev_mode() or not smtp.deliverable:
        # Always emit a structured log so docker-compose logs pick it up
        # even when the system_events insert fails (e.g. fresh DB).
        # NOTE: never log `link` or any fragment of it — it is a bearer token.
        logger.info(
            "magic_link_dev_mode email_hash=%s link_issued=true",
            _hash_email(email),
        )
        if pool is not None:
            try:
                await log_event(
                    pool=pool,
                    level="info",
                    category="auth",
                    source="auth",
                    message="magic_link_dev_mode",
                    context={"email_hash": _hash_email(email), "link_issued": True},
                )
            except Exception:  # noqa: BLE001 — best-effort dev affordance
                logger.debug("system_events emit failed (non-fatal)", exc_info=True)
        return

    # Real SMTP path. Lazy import so jarvis_common doesn't pull aiosmtplib
    # in test environments that don't need it.
    from email.message import EmailMessage  # noqa: PLC0415

    import aiosmtplib  # noqa: PLC0415

    message = EmailMessage()
    message["From"] = smtp.sender
    message["To"] = email
    message["Subject"] = "Sign in to JARVIS"
    message.set_content(_PLAIN_BODY_TEMPLATE.replace("{link}", link))

    # Port 465 → implicit TLS; everything else → STARTTLS where supported.
    use_tls = smtp.port == 465
    start_tls = not use_tls

    # SSRF guard: reject non-public SMTP hosts on the live send path unless the
    # operator has explicitly opted in (e.g. an internal corporate relay).
    if not get_core_settings().allow_private_smtp_host:
        try:
            await _reject_non_public_host(smtp.host)
        except ValueError:
            logger.warning(
                "SMTP host is non-public; set ALLOW_PRIVATE_SMTP_HOST=true for an internal relay. "
                "Magic-link send skipped."
            )
            return

    await aiosmtplib.send(
        message,
        hostname=smtp.host,
        port=smtp.port,
        username=smtp.user,
        password=smtp.password,
        use_tls=use_tls,
        start_tls=start_tls,
    )
    logger.info("magic_link_sent email_hash=%s", _hash_email(email))


async def smtp_configured(pool: asyncpg.Pool | None = None) -> bool:
    """Return ``True`` iff SMTP is configured via the DB OR process env.

    SMTP_USER and SMTP_PASS are intentionally NOT required (some relays use
    IP-allowlist auth). HOST and FROM are the minimum to compose and deliver an
    envelope. Wizard-written ``user_config`` rows are weighted the same as env.

    This is the public wrapper around the private ``_smtp_configured`` probe,
    exported for callers (e.g. ``/api/setup/status``) that need to surface
    SMTP readiness without coupling to internal helpers.  The function is
    always safe to call: a DB failure falls back to env, and ``None`` pool
    skips the DB entirely.
    """
    return await _smtp_configured(pool)


__all__ = ["send_magic_link", "smtp_configured"]
