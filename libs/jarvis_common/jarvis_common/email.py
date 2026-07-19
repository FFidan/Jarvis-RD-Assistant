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
import os
import time
from dataclasses import dataclass
from email.utils import formataddr
from enum import StrEnum, auto

import asyncpg

from jarvis_common.crypto import resolve_secret_row
from jarvis_common.event_log import log_event
from jarvis_common.net import _reject_non_public_host
from jarvis_common.settings import get_core_settings, get_secrets_settings

logger = logging.getLogger(__name__)

# Socket-operation timeout for the live magic-link send. Kept >= the setup
# wizard's SMTP_TEST_TIMEOUT_SECONDS (10s) so a slow-but-valid relay that
# passed the test-send is not cut off on the real send, while still bounding
# a hung connection well under aiosmtplib's 60s default.
SMTP_SEND_TIMEOUT_SECONDS = 30.0


def smtp_tls_flags(port: int) -> tuple[bool, bool]:
    """(use_tls, start_tls) by port: 465 implicit TLS; 587 STARTTLS; else plaintext."""
    return port == 465, port == 587


def sanitize_header_value(value: str | None) -> str | None:
    """Strip a header-bound value and drop it if it carries control characters.

    Reply-To / From display-name are admin config; the API validates them, but
    the DB system row or env var could be written via another surface. A CR/LF/
    NUL (or any non-printable) in a header value would break header construction
    or enable header injection, so such values are rejected defensively here —
    callers fall back to the bare sender. Empty/whitespace-only → ``None``.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if any(c in v for c in ("\r", "\n", "\x00")) or not v.isprintable():
        logger.warning("dropping SMTP header value containing control characters")
        return None
    return v


@dataclass(frozen=True)
class _EffectiveSmtp:
    """Resolved SMTP relay config: DB (``user_config``) layered over env."""

    host: str
    port: int
    user: str | None
    password: str | None
    sender: str
    reply_to: str | None = None
    from_name: str | None = None

    @property
    def deliverable(self) -> bool:
        """True iff host + sender are present (the minimum to send an envelope)."""
        return bool(self.host) and bool(self.sender)

    @property
    def auth_consistent(self) -> bool:
        """False iff a username is set but no password resolved.

        IP-allowlist relays (no user, no pass) are consistent. A username with a
        password is consistent. The only misconfiguration is a half-set login
        (user without a resolvable password) — the relay will reject the AUTH.
        This is independent of ``deliverable`` (host + sender can be fine while
        the AUTH credentials are half-configured).
        """
        return not (bool(self.user) and not self.password)

    @property
    def from_header(self) -> str:
        """RFC-safe ``From`` value: ``"Name" <addr>`` when a display name is set.

        ``from_name`` is sanitized at read time, but compose defensively: a bad
        value degrades to the bare sender rather than breaking the send.
        """
        if self.from_name:
            try:
                return formataddr((self.from_name, self.sender))
            except (TypeError, ValueError):
                logger.warning("invalid SMTP from_name; using bare sender", exc_info=True)
        return self.sender


def _env_smtp() -> _EffectiveSmtp:
    """Read SMTP from process-cached env (``SecretsSettings``)."""
    s = get_secrets_settings()
    return _EffectiveSmtp(
        host=s.smtp_host.get_secret_value() if s.smtp_host else "",
        port=int(s.smtp_port.get_secret_value()) if s.smtp_port else 587,
        user=s.smtp_user.get_secret_value() if s.smtp_user else None,
        password=s.smtp_pass.get_secret_value() if s.smtp_pass else None,
        sender=s.smtp_from.get_secret_value() if s.smtp_from else "",
        reply_to=sanitize_header_value(
            s.smtp_reply_to.get_secret_value() if s.smtp_reply_to else None
        ),
        from_name=sanitize_header_value(
            s.smtp_from_name.get_secret_value() if s.smtp_from_name else None
        ),
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

    keys = (
        "smtp.host",
        "smtp.port",
        "smtp.user",
        "smtp.from",
        "smtp.pass",
        "smtp.reply_to",
        "smtp.from_name",
    )
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

    def _optional_field(key: str, env_value: str | None) -> str | None:
        """Resolve an optional field that the wizard can explicitly clear.

        A user_config row present with an empty value is a deliberate clear
        (setup.py persists '' for a cleared field) — return None and do NOT fall
        back to env. Only an ABSENT row falls back to the env value.
        """
        if key not in by_key:
            return env_value
        value = by_key[key]["value"]
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
    user = _optional_field("smtp.user", env.user)
    sender = _plain("smtp.from") or env.sender
    reply_to = sanitize_header_value(_optional_field("smtp.reply_to", env.reply_to))
    from_name = sanitize_header_value(_optional_field("smtp.from_name", env.from_name))

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

    return _EffectiveSmtp(
        host=host,
        port=port,
        user=user,
        password=password,
        sender=sender,
        reply_to=reply_to,
        from_name=from_name,
    )


async def _smtp_configured(pool: asyncpg.Pool | None = None) -> bool:
    """Return True iff SMTP is configured via the DB OR env.

    SMTP_USER and SMTP_PASS are intentionally NOT required (some relays use
    IP-allowlist auth). HOST and FROM are the minimum to compose + deliver an
    envelope. The wizard-written ``user_config`` rows count the same as env.
    """
    return (await _effective_smtp(pool)).deliverable


async def _required_smtp_empty_string(pool: asyncpg.Pool | None) -> bool:
    """True iff a required SMTP field is present but empty.

    ``SecretsSettings`` rejects explicit empty SMTP values, so this probe reads
    raw env values to keep the status endpoint diagnostic instead of crashing.
    """
    for env_name in ("SMTP_HOST", "SMTP_FROM"):
        if os.environ.get(env_name) == "":
            return True
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT value FROM user_config WHERE key = ANY($1::text[]) AND user_id IS NULL",
                ["smtp.host", "smtp.from"],
            )
    except Exception:  # noqa: BLE001 — DB unreachable → cannot assert empty
        logger.debug("smtp empty-string probe DB read failed", exc_info=True)
        return False
    return any(r["value"] is not None and str(r["value"]) == "" for r in rows)


# Reachability-probe cache: effective ``"host:port"`` -> (monotonic expiry,
# (reachable, issue)). Keyed on host:port so a config change to a different
# relay is a new key and re-probes naturally. TTL is kept well above any
# status-poll cadence so repeated polls never reconnect. ``time.monotonic()``
# (not wall clock) so a system clock step cannot expire or extend an entry.
_REACHABILITY_TTL_SECONDS = 600.0
_reachability_cache: dict[str, tuple[float, tuple[bool, str | None]]] = {}

# A liveness connect should be fast; cap it well under the 30s send timeout so an
# unreachable/hung relay cannot block the (unauthenticated, cached) status poll
# for long — this cost is paid at most once per TTL per host:port.
_REACHABILITY_PROBE_TIMEOUT_SECONDS = 8.0

# Value-free, operator-facing (mirrors the ``effective_smtp_status`` issue
# style): it never embeds the host, port, or any credential.
_UNREACHABLE_ISSUE = (
    "The mail server is not accepting connections — sign-in emails may not be delivered."
)


async def _probe_relay(eff: _EffectiveSmtp) -> tuple[bool, str | None]:
    """Open a connection + EHLO to the effective relay; never raise.

    A liveness check only: connect + EHLO + QUIT — no mail is sent and AUTH is
    NOT attempted (AUTH would put credentials on the wire and risk lockouts).
    Reuses ``send_magic_link``'s SSRF guard + TLS-flag construction, but with a
    short liveness timeout (a relay too slow to connect within it is treated as
    failing — the user will not wait that long for a sign-in link anyway).
    Returns ``(reachable, issue)`` with a value-free issue string on any failure.
    """
    # SSRF parity with the live send: refuse a non-public relay unless the
    # operator opted in. A send would be skipped for such a host anyway, so it
    # is effectively unreachable for delivery — and this keeps the probe from
    # opening an outbound connection to an internal address.
    if not get_core_settings().allow_private_smtp_host:
        try:
            await _reject_non_public_host(eff.host)
        except Exception:  # noqa: BLE001 — unresolved/non-public → treat as unreachable
            logger.debug("smtp reachability probe: host rejected", exc_info=True)
            return False, _UNREACHABLE_ISSUE

    import aiosmtplib  # noqa: PLC0415

    use_tls, start_tls = smtp_tls_flags(eff.port)
    client = aiosmtplib.SMTP(
        hostname=eff.host,
        port=eff.port,
        use_tls=use_tls,
        start_tls=start_tls,
        timeout=_REACHABILITY_PROBE_TIMEOUT_SECONDS,
    )
    try:
        await client.connect()
        await client.ehlo()
    except Exception:  # noqa: BLE001 — any connect/EHLO failure → unreachable, never propagate
        logger.debug("smtp reachability probe failed", exc_info=True)
        return False, _UNREACHABLE_ISSUE
    # Reachable: the relay accepted a connection and EHLO. QUIT is best-effort
    # cleanup — a QUIT failure must NOT flip a reachable relay to unreachable.
    try:
        await client.quit()
    except Exception:  # noqa: BLE001 — best-effort disconnect
        logger.debug("smtp reachability probe quit failed (non-fatal)", exc_info=True)
    return True, None


async def probe_smtp_reachable(pool: asyncpg.Pool | None = None) -> tuple[bool, str | None]:
    """Return ``(reachable, issue)`` for the EFFECTIVE relay, cached per host:port.

    When the relay is not ``deliverable`` (host/sender missing) there is nothing
    to probe → ``(False, None)`` and NO connection is attempted. Otherwise a
    lightweight, cached, short-timeout liveness probe (connect + EHLO, no mail,
    no AUTH) runs and its result is cached for ``_REACHABILITY_TTL_SECONDS`` so
    repeated status polls never reconnect. Never raises; the issue string is
    value-free (no host/port/credentials).

    Safe on any authenticated status/admin/setup surface. It must NEVER be
    called inline on the unauthenticated ``request-link`` path — that path's
    shape and timing are the anti-enumeration defense.
    """
    eff = await _effective_smtp(pool)
    if not eff.deliverable:
        return False, None

    cache_key = f"{eff.host}:{eff.port}"
    now = time.monotonic()
    cached = _reachability_cache.get(cache_key)
    if cached is not None and now < cached[0]:
        return cached[1]

    result = await _probe_relay(eff)
    _reachability_cache[cache_key] = (now + _REACHABILITY_TTL_SECONDS, result)
    return result


async def effective_smtp_status(pool: asyncpg.Pool | None = None) -> tuple[bool, list[str]]:
    """Return ``(deliverable, issues)`` for the EFFECTIVE relay (DB over env).

    ``deliverable`` mirrors ``_EffectiveSmtp.deliverable`` of the resolved
    config, so an env-only deployment reports healthy (no false warning).
    ``issues`` are value-free, operator-facing strings for the settings UI;
    they never embed a configured value.
    """
    eff = await _effective_smtp(pool)

    issues: list[str] = []
    # Auth-consistency is independent of deliverability: host + sender can be
    # present (deliverable) while a username is set with no resolvable password,
    # which the relay will reject at AUTH. Surface it even when deliverable, so
    # the early-return below does not hide a half-configured login.
    if not eff.auth_consistent:
        issues.append(
            "A mail-server username is set but no password is configured — "
            "the relay will reject sign-in emails."
        )

    if eff.deliverable:
        # Deliverable is presence-only; a configured relay can still be down or
        # unreachable. Surface that as an additional value-free issue (does NOT
        # flip deliverable). The probe is cached + short-timeout, so this adds no
        # login-page latency; it never runs on the unauthenticated request-link path.
        reachable, reach_issue = await probe_smtp_reachable(pool)
        if not reachable and reach_issue is not None:
            issues.append(reach_issue)
        return True, issues

    if await _required_smtp_empty_string(pool):
        issues.append(
            "A required SMTP field is set to an empty value — sign-in links will not be delivered."
        )
    elif eff.host and not eff.sender:
        issues.append("The mail server is set but the From address is missing.")
    elif eff.sender and not eff.host:
        issues.append("The From address is set but the mail server (host) is missing.")
    else:
        issues.append(
            "No mail relay is configured — sign-in links are written to the server log, "
            "not emailed."
        )
    return False, issues


def _dev_mode() -> bool:
    return get_core_settings().dev_smtp_log_only


def _hash_email(email: str) -> str:
    """Return a SHA-256 hex digest of the email address for safe logging.

    Use this wherever an email must appear in a log record instead of the raw
    address (or any derived secret such as a magic-link token).
    """
    return hashlib.sha256(email.lower().encode("utf-8")).hexdigest()


_PLAIN_BODY_TEMPLATE = (
    "Click the link below to sign in. The link expires in 15 minutes.\n"
    "\n"
    "{link}\n"
    "\n"
    "If you didn't request this, ignore this email.\n"
)


class MagicLinkDelivery(StrEnum):
    """Outcome of a ``send_magic_link`` call so callers act on truth, not inference."""

    DELIVERED = auto()
    DROPPED_UNCONFIGURED = auto()
    DROPPED_DEV_LOG_ONLY = auto()
    DROPPED_PRIVATE_HOST = auto()
    FAILED = auto()


async def send_magic_link(
    email: str,
    link: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> MagicLinkDelivery:
    """Deliver a magic-link email and report the outcome as a ``MagicLinkDelivery``.

    Never raises for a delivery problem: an unconfigured relay, dev-log-only
    mode, a rejected private host, and a failed send each return a distinct
    enum member so callers can surface a manual link instead of inferring
    delivery from mere config presence.

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

    dev_log_only = _dev_mode()
    if dev_log_only or not smtp.deliverable:
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
        return (
            MagicLinkDelivery.DROPPED_DEV_LOG_ONLY
            if dev_log_only
            else MagicLinkDelivery.DROPPED_UNCONFIGURED
        )

    # Real SMTP path. Lazy import so jarvis_common doesn't pull aiosmtplib
    # in test environments that don't need it.
    from email.message import EmailMessage  # noqa: PLC0415

    import aiosmtplib  # noqa: PLC0415

    message = EmailMessage()
    try:
        message["From"] = smtp.from_header
    except (TypeError, ValueError):
        logger.warning("invalid From header; using bare sender", exc_info=True)
        message["From"] = smtp.sender
    message["To"] = email
    message["Subject"] = "Sign in to JARVIS"
    if smtp.reply_to:
        try:
            message["Reply-To"] = smtp.reply_to
        except (TypeError, ValueError):
            logger.warning("invalid Reply-To header; omitting", exc_info=True)
    message.set_content(_PLAIN_BODY_TEMPLATE.replace("{link}", link))

    use_tls, start_tls = smtp_tls_flags(smtp.port)

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
            return MagicLinkDelivery.DROPPED_PRIVATE_HOST

    try:
        await aiosmtplib.send(
            message,
            hostname=smtp.host,
            port=smtp.port,
            username=smtp.user,
            password=smtp.password,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=SMTP_SEND_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # Make delivery failures observable (the Logs Live tab surfaces this),
        # then return FAILED so callers react without an exception. The
        # unauthenticated /request-link response stays unchanged
        # (anti-enumeration). Never log the raw recipient or bearer-token link.
        logger.warning(
            "magic_link_delivery_failed email_hash=%s error_class=%s",
            _hash_email(email),
            type(exc).__name__,
        )
        if pool is not None:
            try:
                await log_event(
                    pool=pool,
                    level="error",
                    category="auth",
                    source="auth",
                    message="magic_link_delivery_failed",
                    context={
                        "email_hash": _hash_email(email),
                        "error_class": type(exc).__name__,
                    },
                )
            except Exception:  # noqa: BLE001 — observability is best-effort
                logger.debug("system_events emit failed (non-fatal)", exc_info=True)
        return MagicLinkDelivery.FAILED

    logger.info("magic_link_sent email_hash=%s", _hash_email(email))
    if pool is not None:
        try:
            await log_event(
                pool=pool,
                level="info",
                category="auth",
                source="auth",
                message="magic_link_sent",
                context={"email_hash": _hash_email(email)},
            )
        except Exception:  # noqa: BLE001 — observability is best-effort
            logger.debug("system_events emit failed (non-fatal)", exc_info=True)
    return MagicLinkDelivery.DELIVERED


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


__all__ = ["probe_smtp_reachable", "send_magic_link", "smtp_configured"]
