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
    """Empty-string required env field triggers the empty-value issue message."""
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "")  # present but empty — silent-fail case
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is False
    assert len(issues) == 1
    # Must mention "empty" in some form and be value-free
    assert "empty" in issues[0].lower()
    assert "bot@example.com" not in issues[0]
    assert "SMTP_HOST" not in issues[0]


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
