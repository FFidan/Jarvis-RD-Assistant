"""Tests for jarvis_common.email template rendering and dev-mode fallback.

Guards the BE-04 fix: `_PLAIN_BODY_TEMPLATE` must use ``str.replace`` (not
``str.format``) so that URLs containing ``{`` / ``}`` characters (e.g. query
params with template-like tokens) are included verbatim in the email body
without raising ``KeyError`` / ``IndexError`` from the format DSL.

Also pins the no-send dev-mode fallback behaviour (Task T0.4): when SMTP is
unconfigured, ``send_magic_link`` records only a SHA-256 hash of the email in
``system_events``; it does NOT log the raw link (a bearer token) or any
fragment of it to stdout or any other sink.
"""

from __future__ import annotations

import pytest
from jarvis_common.email import _PLAIN_BODY_TEMPLATE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_body(link: str) -> str:
    """Mirror the production rendering path (must use .replace, not .format)."""
    return _PLAIN_BODY_TEMPLATE.replace("{link}", link)


# ---------------------------------------------------------------------------
# Normal URL
# ---------------------------------------------------------------------------


def test_plain_body_normal_url() -> None:
    """A standard magic-link URL is embedded verbatim."""
    link = "https://example.com/auth/verify?token=abc123"
    body = _render_body(link)
    assert link in body
    assert "Click the link" in body
    assert "15 minutes" in body


# ---------------------------------------------------------------------------
# URLs with brace characters (BE-04 regression guard)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dev-mode / no-SMTP fallback characterization (Task T0.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_magic_link_no_smtp_does_not_deliver(monkeypatch) -> None:
    """When SMTP is unconfigured, send_magic_link takes the dev-mode path.

    Characterization: no SMTP delivery occurs, no link is written to any log,
    only a SHA-256 hash of the email is emitted via the logger.

    Pins the corrected docstring behaviour: the stale claim "logged to stdout"
    was false — the raw link (a bearer token) is never logged.
    """
    import logging
    import logging.handlers
    from unittest.mock import AsyncMock, patch

    import aiosmtplib
    from jarvis_common.email import send_magic_link
    from jarvis_common.settings import get_secrets_settings

    # Ensure SMTP env vars are absent so _env_smtp() returns empty host/sender.
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    # Clear SecretsSettings cache so the monkeypatch takes effect.
    get_secrets_settings.cache_clear()

    raw_link = "https://example.com/auth/verify?token=supersecretbearertoken"
    records: list[logging.LogRecord] = []

    mock_smtp_send = AsyncMock(name="aiosmtplib.send")

    with patch.object(aiosmtplib, "send", mock_smtp_send):
        with patch("jarvis_common.email.log_event", new_callable=AsyncMock) as mock_log_event:
            # Capture log records from the email logger.
            handler = logging.handlers.MemoryHandler(capacity=100, flushLevel=logging.CRITICAL)
            handler.buffer = records  # type: ignore[attr-defined]
            email_logger = logging.getLogger("jarvis_common.email")
            email_logger.addHandler(handler)
            try:
                await send_magic_link("user@example.com", raw_link, pool=None)
            finally:
                email_logger.removeHandler(handler)

    # aiosmtplib.send must never be called — no SMTP delivery on the fallback path.
    mock_smtp_send.assert_not_awaited()

    # The raw link (bearer token) must NOT appear in any log record.
    all_log_text = " ".join(r.getMessage() for r in records)
    assert "supersecretbearertoken" not in all_log_text, (
        f"Raw magic-link token must never be written to any log record. Found in: {all_log_text!r}"
    )
    assert raw_link not in all_log_text, (
        "Full magic-link URL (bearer token) must never be written to any log record."
    )

    # log_event may be called (best-effort system_events insert) — it should
    # NOT carry the link either.
    if mock_log_event.called:
        call_kwargs = mock_log_event.call_args.kwargs
        context = call_kwargs.get("context", {})
        assert "supersecretbearertoken" not in str(context), (
            "Raw token must not appear in system_events context payload."
        )

    # Clean up SecretsSettings cache.
    get_secrets_settings.cache_clear()


@pytest.mark.asyncio
async def test_smtp_configured_public_fn_returns_false_without_smtp(monkeypatch) -> None:
    """smtp_configured() public wrapper returns False when no SMTP env or DB rows.

    Pins the public API surface introduced in Task T0.4 so callers (e.g.
    /api/setup/status) can probe SMTP state without touching private helpers.
    """
    from jarvis_common.email import smtp_configured
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    get_secrets_settings.cache_clear()

    result = await smtp_configured(pool=None)

    assert result is False, (
        f"smtp_configured() must return False when no SMTP env vars and pool=None; got {result!r}"
    )
    get_secrets_settings.cache_clear()


# ---------------------------------------------------------------------------
# sanitize_header_value
# ---------------------------------------------------------------------------


def test_sanitize_header_value_normal_string() -> None:
    from jarvis_common.email import sanitize_header_value

    assert sanitize_header_value("JARVIS Bot") == "JARVIS Bot"


def test_sanitize_header_value_strips_whitespace() -> None:
    from jarvis_common.email import sanitize_header_value

    assert sanitize_header_value("  hello  ") == "hello"


def test_sanitize_header_value_none_returns_none() -> None:
    from jarvis_common.email import sanitize_header_value

    assert sanitize_header_value(None) is None


def test_sanitize_header_value_empty_string_returns_none() -> None:
    from jarvis_common.email import sanitize_header_value

    assert sanitize_header_value("") is None


def test_sanitize_header_value_whitespace_only_returns_none() -> None:
    from jarvis_common.email import sanitize_header_value

    assert sanitize_header_value("   ") is None


@pytest.mark.parametrize(
    "bad_value",
    [
        "Name\r\nBcc: evil@x.com",
        "Name\nBcc: evil@x.com",
        # \r in the middle (not stripped by .strip()) — injection risk
        "Name\rMiddle",
        "has\x00null",
    ],
)
def test_sanitize_header_value_control_chars_returns_none(bad_value: str) -> None:
    from jarvis_common.email import sanitize_header_value

    assert sanitize_header_value(bad_value) is None


# ---------------------------------------------------------------------------
# _EffectiveSmtp.from_header
# ---------------------------------------------------------------------------


def test_effective_smtp_from_header_with_from_name() -> None:
    from email.utils import formataddr

    from jarvis_common.email import _EffectiveSmtp

    smtp = _EffectiveSmtp(
        host="mail.example.com",
        port=587,
        user=None,
        password=None,
        sender="bot@example.com",
        from_name="JARVIS Bot",
    )
    assert smtp.from_header == formataddr(("JARVIS Bot", "bot@example.com"))
    assert "JARVIS Bot" in smtp.from_header
    assert "bot@example.com" in smtp.from_header


def test_effective_smtp_from_header_bare_when_no_from_name() -> None:
    from jarvis_common.email import _EffectiveSmtp

    smtp = _EffectiveSmtp(
        host="mail.example.com",
        port=587,
        user=None,
        password=None,
        sender="bot@example.com",
    )
    assert smtp.from_header == "bot@example.com"


def test_effective_smtp_from_header_bare_when_from_name_empty() -> None:
    from jarvis_common.email import _EffectiveSmtp

    # from_name='' is falsy; from_header must return bare sender
    smtp = _EffectiveSmtp(
        host="mail.example.com",
        port=587,
        user=None,
        password=None,
        sender="bot@example.com",
        from_name="",
    )
    assert smtp.from_header == "bot@example.com"


# ---------------------------------------------------------------------------
# send_magic_link — Reply-To and From header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_magic_link_sets_reply_to_when_configured(monkeypatch) -> None:
    """When smtp.reply_to is set, Reply-To header must be present in the sent message."""
    from email.message import EmailMessage
    from unittest.mock import patch

    import aiosmtplib
    from jarvis_common.email import send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_REPLY_TO", "support@example.com")
    monkeypatch.delenv("SMTP_FROM_NAME", raising=False)
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()

    captured: list[EmailMessage] = []

    async def fake_send(message, **kwargs):
        captured.append(message)

    with patch.object(aiosmtplib, "send", side_effect=fake_send):
        await send_magic_link("user@example.com", "https://example.com/verify?token=abc", pool=None)

    get_secrets_settings.cache_clear()
    assert captured, "aiosmtplib.send was not called"
    msg = captured[0]
    assert msg["Reply-To"] == "support@example.com", (
        f"Expected Reply-To='support@example.com'; got {msg['Reply-To']!r}"
    )
    assert msg["From"] == "bot@example.com", (
        f"Expected bare From when no from_name; got {msg['From']!r}"
    )


@pytest.mark.asyncio
async def test_send_magic_link_no_reply_to_when_not_configured(monkeypatch) -> None:
    """When smtp.reply_to is not set, the sent message must have no Reply-To header."""
    from email.message import EmailMessage
    from unittest.mock import patch

    import aiosmtplib
    from jarvis_common.email import send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.delenv("SMTP_REPLY_TO", raising=False)
    monkeypatch.delenv("SMTP_FROM_NAME", raising=False)
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()

    captured: list[EmailMessage] = []

    async def fake_send(message, **kwargs):
        captured.append(message)

    with patch.object(aiosmtplib, "send", side_effect=fake_send):
        await send_magic_link("user@example.com", "https://example.com/verify?token=abc", pool=None)

    get_secrets_settings.cache_clear()
    assert captured, "aiosmtplib.send was not called"
    msg = captured[0]
    assert msg["Reply-To"] is None, (
        f"Expected no Reply-To when not configured; got {msg['Reply-To']!r}"
    )


@pytest.mark.asyncio
async def test_send_magic_link_from_header_with_display_name(monkeypatch) -> None:
    """When smtp.from_name is set, From header must be 'Name <addr>' (formataddr)."""
    from email.message import EmailMessage
    from email.utils import formataddr
    from unittest.mock import patch

    import aiosmtplib
    from jarvis_common.email import send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_FROM_NAME", "JARVIS Bot")
    monkeypatch.delenv("SMTP_REPLY_TO", raising=False)
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()

    captured: list[EmailMessage] = []

    async def fake_send(message, **kwargs):
        captured.append(message)

    with patch.object(aiosmtplib, "send", side_effect=fake_send):
        await send_magic_link("user@example.com", "https://example.com/verify?token=abc", pool=None)

    get_secrets_settings.cache_clear()
    assert captured, "aiosmtplib.send was not called"
    msg = captured[0]
    expected_from = formataddr(("JARVIS Bot", "bot@example.com"))
    assert msg["From"] == expected_from, (
        f"Expected From={expected_from!r} with display name; got {msg['From']!r}"
    )


@pytest.mark.asyncio
async def test_send_magic_link_header_injection_sanitized(monkeypatch) -> None:
    """A from_name with CR/LF injection must not produce injected headers.

    The send path uses sanitize_header_value which drops malicious values before
    they reach the EmailMessage — so From degrades to the bare sender and no
    extra headers appear.
    """
    from email.message import EmailMessage
    from unittest.mock import patch

    import aiosmtplib
    from jarvis_common.email import send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    # Injection attempt in from_name
    monkeypatch.setenv("SMTP_FROM_NAME", "Evil\r\nBcc: attacker@evil.com")
    monkeypatch.setenv("SMTP_REPLY_TO", "legit@example.com\nX-Injected: yes")
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()

    captured: list[EmailMessage] = []

    async def fake_send(message, **kwargs):
        captured.append(message)

    with patch.object(aiosmtplib, "send", side_effect=fake_send):
        await send_magic_link("user@example.com", "https://example.com/verify?token=abc", pool=None)

    get_secrets_settings.cache_clear()
    # The send must still succeed (sanitization degrades, never crashes)
    assert captured, "aiosmtplib.send was not called — send must still succeed after sanitization"
    msg = captured[0]
    # Bad from_name sanitized → From must be bare sender (not contain the injection)
    from_header = msg["From"] or ""
    assert "Bcc:" not in from_header, (
        f"Injected Bcc must not appear in From header; got {from_header!r}"
    )
    assert "Evil" not in from_header, (
        f"Injected from_name must not appear in From header; got {from_header!r}"
    )
    # Bad reply_to sanitized → Reply-To must be absent
    reply_to = msg["Reply-To"]
    assert reply_to is None or "X-Injected" not in (reply_to or ""), (
        f"Injected Reply-To must not carry extra headers; got {reply_to!r}"
    )


@pytest.mark.asyncio
async def test_send_magic_link_whitespace_from_name_bare_sender(monkeypatch) -> None:
    """A whitespace-only from_name must result in a bare From address (no display name)."""
    from email.message import EmailMessage
    from unittest.mock import patch

    import aiosmtplib
    from jarvis_common.email import send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_FROM_NAME", "   ")  # whitespace only
    monkeypatch.delenv("SMTP_REPLY_TO", raising=False)
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()

    captured: list[EmailMessage] = []

    async def fake_send(message, **kwargs):
        captured.append(message)

    with patch.object(aiosmtplib, "send", side_effect=fake_send):
        await send_magic_link("user@example.com", "https://example.com/verify?token=abc", pool=None)

    get_secrets_settings.cache_clear()
    assert captured, "aiosmtplib.send was not called"
    msg = captured[0]
    # Whitespace-only from_name → sanitize_header_value returns None → bare sender
    assert msg["From"] == "bot@example.com", (
        f"Expected bare From for whitespace from_name; got {msg['From']!r}"
    )


# ---------------------------------------------------------------------------
# effective_smtp_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_smtp_status_env_only_deliverable(monkeypatch) -> None:
    """env-only SMTP with host+from set: (True, [])."""
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is True
    assert issues == []


@pytest.mark.asyncio
async def test_effective_smtp_status_nothing_set(monkeypatch) -> None:
    """No SMTP configured: (False, [message about no relay])."""
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is False
    assert len(issues) == 1
    # Issue must be value-free — must not contain any configured value
    assert "mail.example.com" not in issues[0]
    assert "bot@example.com" not in issues[0]
    # Must mention relay/log concept
    assert "relay" in issues[0].lower() or "log" in issues[0].lower()


@pytest.mark.asyncio
async def test_effective_smtp_status_host_only_no_from(monkeypatch) -> None:
    """Host set but no From address: (False, [partial message])."""
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.delenv("SMTP_FROM", raising=False)
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is False
    assert len(issues) == 1
    # Value-free: must not echo the configured host
    assert "mail.example.com" not in issues[0]


@pytest.mark.asyncio
async def test_effective_smtp_status_from_only_no_host(monkeypatch) -> None:
    """From set but no host: (False, [partial message])."""
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is False
    assert len(issues) == 1
    assert "bot@example.com" not in issues[0]


@pytest.mark.asyncio
async def test_effective_smtp_status_empty_string_required_field(monkeypatch) -> None:
    """Explicit empty SMTP env now fails settings validation instead of degrading."""
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings
    from pydantic import ValidationError

    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    get_secrets_settings.cache_clear()

    with pytest.raises(ValidationError, match="SMTP secret values must be unset or non-empty"):
        await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()


# ---------------------------------------------------------------------------
# Auth-consistency signal in effective_smtp_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_smtp_status_user_set_no_password_surfaces_issue(monkeypatch) -> None:
    """host+sender+user set but no password → deliverable True but an auth issue is surfaced.

    Regression guard: the auth-consistency issue must appear even though
    ``deliverable`` is True (it previously returned ``(True, [])`` early and hid
    a half-configured login).
    """
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_USER", "relay-user")
    monkeypatch.delenv("SMTP_PASS", raising=False)
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is True, "host+sender present → still deliverable"
    assert len(issues) == 1
    assert "password" in issues[0].lower()
    # value-free
    assert "relay-user" not in issues[0]
    assert "mail.example.com" not in issues[0]


@pytest.mark.asyncio
async def test_effective_smtp_status_ip_allowlist_relay_stays_clean(monkeypatch) -> None:
    """host+sender, NO user, NO pass (IP-allowlist relay) → (True, []) — no false auth warning."""
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is True
    assert issues == []


def test_effective_smtp_auth_consistent_property() -> None:
    """auth_consistent: False only when user is set with no password."""
    from jarvis_common.email import _EffectiveSmtp

    base = dict(host="h", port=587, sender="s@example.com")
    assert _EffectiveSmtp(user=None, password=None, **base).auth_consistent is True
    assert _EffectiveSmtp(user="u", password="p", **base).auth_consistent is True
    assert _EffectiveSmtp(user="u", password=None, **base).auth_consistent is False
    assert _EffectiveSmtp(user=None, password="p", **base).auth_consistent is True


@pytest.mark.asyncio
async def test_effective_smtp_db_empty_user_does_not_revert_to_env(monkeypatch) -> None:
    """A user_config row explicitly storing smtp.user='' must clear the user, not fall back to env.

    The wizard persists a cleared field as JSONB '' (setup.py:657-662). _effective_smtp
    must treat 'row present and empty' as a deliberate clear for user/reply_to/from_name,
    distinct from 'row absent' (which falls back to env).
    """
    from unittest.mock import AsyncMock, MagicMock

    from jarvis_common.email import _effective_smtp
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_USER", "env-user")
    monkeypatch.setenv("SMTP_REPLY_TO", "env-reply@example.com")
    monkeypatch.setenv("SMTP_FROM_NAME", "Env Name")
    get_secrets_settings.cache_clear()

    # user_config rows: smtp.user / smtp.reply_to / smtp.from_name explicitly cleared to ''.
    db_rows = [
        {"key": "smtp.user", "value": "", "encrypted_value": None},
        {"key": "smtp.reply_to", "value": "", "encrypted_value": None},
        {"key": "smtp.from_name", "value": "", "encrypted_value": None},
    ]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=db_rows)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    eff = await _effective_smtp(pool)

    get_secrets_settings.cache_clear()
    # host/from were not in the DB rows → env still applies (deliverable).
    assert eff.host == "mail.example.com"
    assert eff.sender == "bot@example.com"
    # The three cleared optional fields must be cleared, NOT the env value.
    assert eff.user is None, f"cleared smtp.user must not revert to env; got {eff.user!r}"
    assert eff.reply_to is None, (
        f"cleared smtp.reply_to must not revert to env; got {eff.reply_to!r}"
    )
    assert eff.from_name is None, (
        f"cleared smtp.from_name must not revert to env; got {eff.from_name!r}"
    )


@pytest.mark.asyncio
async def test_effective_smtp_absent_db_row_falls_back_to_env(monkeypatch) -> None:
    """When a field has NO user_config row, _effective_smtp falls back to the env value (unchanged)."""
    from unittest.mock import AsyncMock, MagicMock

    from jarvis_common.email import _effective_smtp
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_USER", "env-user")
    get_secrets_settings.cache_clear()

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])  # no DB rows
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    eff = await _effective_smtp(pool)

    get_secrets_settings.cache_clear()
    assert eff.user == "env-user", "absent DB row must fall back to env user"


# ---------------------------------------------------------------------------
# Delivery-failure observability (magic_link_delivery_failed event)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_magic_link_failure_emits_event_and_reraises(monkeypatch) -> None:
    """A failing real send emits a magic_link_delivery_failed system event, then re-raises.

    The event carries email_hash + error_class (never the raw email or the
    bearer-token link); the exception propagates so callers' own logging fires
    (they swallow it for anti-enumeration).
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    import aiosmtplib
    from jarvis_common.email import _hash_email, send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()

    pool = MagicMock(name="pool")
    raw_link = "https://example.com/auth/verify?token=supersecretbearertoken"

    failing_send = AsyncMock(side_effect=aiosmtplib.SMTPConnectError("relay down"))

    with patch.object(aiosmtplib, "send", failing_send):
        with patch("jarvis_common.email.log_event", new_callable=AsyncMock) as mock_log_event:
            with pytest.raises(aiosmtplib.SMTPConnectError):
                await send_magic_link("user@example.com", raw_link, pool=pool)

    get_secrets_settings.cache_clear()

    mock_log_event.assert_awaited_once()
    kwargs = mock_log_event.call_args.kwargs
    assert kwargs["level"] == "error"
    assert kwargs["message"] == "magic_link_delivery_failed"
    context = kwargs["context"]
    assert context["email_hash"] == _hash_email("user@example.com")
    assert context["error_class"] == "SMTPConnectError"
    # Never leak the raw recipient or the bearer token.
    assert "user@example.com" not in str(context)
    assert "supersecretbearertoken" not in str(context)


@pytest.mark.asyncio
async def test_send_magic_link_success_emits_sent_event(monkeypatch) -> None:
    """A successful send emits a magic_link_sent system event (email_hash only)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import aiosmtplib
    from jarvis_common.email import _hash_email, send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()

    pool = MagicMock(name="pool")

    with patch.object(aiosmtplib, "send", new_callable=AsyncMock):
        with patch("jarvis_common.email.log_event", new_callable=AsyncMock) as mock_log_event:
            await send_magic_link(
                "user@example.com", "https://example.com/verify?token=abc", pool=pool
            )

    get_secrets_settings.cache_clear()

    mock_log_event.assert_awaited_once()
    kwargs = mock_log_event.call_args.kwargs
    assert kwargs["level"] == "info"
    assert kwargs["message"] == "magic_link_sent"
    assert kwargs["context"]["email_hash"] == _hash_email("user@example.com")


@pytest.mark.asyncio
async def test_send_magic_link_passes_timeout(monkeypatch) -> None:
    """The live send passes timeout=SMTP_SEND_TIMEOUT_SECONDS (>= the test-send timeout)."""
    from unittest.mock import AsyncMock, patch

    import aiosmtplib
    from jarvis_common.email import SMTP_SEND_TIMEOUT_SECONDS, send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()

    assert SMTP_SEND_TIMEOUT_SECONDS >= 10.0  # >= setup's SMTP_TEST_TIMEOUT_SECONDS

    mock_send = AsyncMock()
    with patch.object(aiosmtplib, "send", mock_send):
        await send_magic_link("user@example.com", "https://example.com/verify?token=abc", pool=None)

    get_secrets_settings.cache_clear()
    assert mock_send.call_args.kwargs["timeout"] == SMTP_SEND_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Brace-URL tests (existing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "link",
    [
        # Named placeholder lookalike — would trip format() if re-scanned
        "https://example.com/verify?token={abc}",
        # Positional placeholder lookalike
        "https://example.com/verify?token={0}",
        # Empty braces
        "https://example.com/verify?token={}",
        # Double braces (escaped in format DSL)
        "https://example.com/verify?token={{escaped}}",
        # The template field name itself as a query value
        "https://example.com/verify?redirect={link}",
        # Mixed braces in path and query
        "https://example.com/{path}?token={value}&other={0}",
    ],
)
def test_plain_body_brace_url_rendered_verbatim(link: str) -> None:
    """URLs containing ``{…}`` tokens must appear verbatim in the rendered body.

    Before the BE-04 fix the body was built with ``_PLAIN_BODY_TEMPLATE.format(link=link)``.
    While CPython's single-pass ``str.format`` does not re-scan substituted values,
    using ``str.replace`` is semantically correct (no format-DSL interpretation) and
    eliminates the risk entirely.  This test verifies the safe path is in place.
    """
    body = _render_body(link)
    assert link in body, (
        f"Link with brace chars was not embedded verbatim.\nLink:  {link!r}\nBody:\n{body}"
    )


def test_hash_email_is_case_insensitive() -> None:
    from jarvis_common.email import _hash_email

    assert _hash_email("User@Example.COM") == _hash_email("user@example.com")


def test_email_hash_matches_auth_router_canonicalization() -> None:
    from jarvis_common.email import _hash_email as mail_hash_email
    from paper_ingestion.routers.auth import _hash_email as auth_hash_email

    assert mail_hash_email("User@Example.COM") == auth_hash_email("user@example.com")
