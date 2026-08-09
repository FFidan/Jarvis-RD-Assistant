"""Email rendering, delivery outcomes, and credential-safety tests.

Magic-link URLs containing brace characters must remain byte-for-byte intact.
When SMTP is unavailable or outbound use is quarantined, delivery is suppressed
without logging the raw link or recipient address.
"""

from __future__ import annotations

import socket

import pytest
from jarvis_common.email import _PLAIN_BODY_TEMPLATE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_body(link: str) -> str:
    """Mirror the production rendering path (must use .replace, not .format)."""
    return _PLAIN_BODY_TEMPLATE.replace("{link}", link)


@pytest.fixture(autouse=True)
def _fake_pinned_smtp_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep mail rendering tests deterministic; socket pinning has its own suite."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "jarvis_common.email.connect_pinned_socket",
        AsyncMock(return_value=socket.socket()),
    )


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
# URLs with brace characters
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Development-mode and unconfigured-SMTP behavior
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
async def test_send_magic_link_drops_without_loading_smtp_during_quarantine(
    monkeypatch, tmp_path
) -> None:
    """Quarantine is a non-raising delivery outcome and performs no SMTP work."""
    from unittest.mock import AsyncMock

    from jarvis_common import email as email_mod

    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.write_text("not json")
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    effective = AsyncMock()
    monkeypatch.setattr(email_mod, "_effective_smtp", effective)

    outcome = await email_mod.send_magic_link(
        "user@example.com", "https://example.com/auth/verify#token=secret"
    )

    assert outcome is email_mod.MagicLinkDelivery.DROPPED_QUARANTINED
    effective.assert_not_awaited()


@pytest.mark.asyncio
async def test_smtp_probe_reports_quarantine_without_loading_credentials(
    monkeypatch, tmp_path
) -> None:
    from unittest.mock import AsyncMock

    from jarvis_common import email as email_mod

    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    effective = AsyncMock()
    monkeypatch.setattr(email_mod, "_effective_smtp", effective)

    reachable, issue = await email_mod.probe_smtp_reachable()

    assert reachable is False
    assert issue == "Mail delivery is disabled until restored credentials are reviewed."
    effective.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_smtp_status_never_claims_bearer_links_are_logged(monkeypatch) -> None:
    """Operator guidance must point to the admin manual-link flow, not logs."""
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    for name in ("SMTP_HOST", "SMTP_FROM", "SMTP_USER", "SMTP_PASS"):
        monkeypatch.delenv(name, raising=False)
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(None)

    assert deliverable is False
    issue_text = " ".join(issues).lower()
    assert "server log" not in issue_text
    assert "stdout" not in issue_text
    assert "administrator" in issue_text
    assert "sign-in link" in issue_text

    get_secrets_settings.cache_clear()


@pytest.mark.asyncio
async def test_smtp_configured_public_fn_returns_false_without_smtp(monkeypatch) -> None:
    """smtp_configured() public wrapper returns False when no SMTP env or DB rows.

    Callers such as ``/api/setup/status`` can probe SMTP state without touching
    private helpers.
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
    from unittest.mock import AsyncMock

    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    # Reachability is probed separately; a reachable relay adds no issue.
    monkeypatch.setattr(
        "jarvis_common.email.probe_smtp_reachable", AsyncMock(return_value=(True, None))
    )
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
    """Explicit empty SMTP env is treated as unset and degrades diagnostically.

    An empty ``SMTP_HOST`` must NOT crash the ``SecretsSettings`` singleton;
    ``effective_smtp_status`` reports ``(False, issues)`` with a value-free
    "required field … empty value" diagnostic and never echoes a configured value.
    """
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is False
    assert any("empty value" in issue for issue in issues)
    # Value-free: no issue may echo the configured sender
    assert all("bot@example.com" not in issue for issue in issues)


@pytest.mark.asyncio
async def test_effective_smtp_status_whitespace_required_field_is_not_deliverable(
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock

    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "   ")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    probe = AsyncMock()
    monkeypatch.setattr("jarvis_common.email.probe_smtp_reachable", probe)
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is False
    assert issues and all("bot@example.com" not in issue for issue in issues)
    probe.assert_not_awaited()


def test_effective_smtp_deliverable_normalizes_required_values() -> None:
    from jarvis_common.email import _EffectiveSmtp

    base = {"port": 587, "user": None, "password": None}
    assert _EffectiveSmtp(host=" ", sender="bot@example.com", **base).deliverable is False
    assert _EffectiveSmtp(host="smtp.example.com", sender=" \t", **base).deliverable is False


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
    from unittest.mock import AsyncMock

    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_USER", "relay-user")
    monkeypatch.delenv("SMTP_PASS", raising=False)
    # Isolate the auth-consistency issue: reachable relay adds nothing.
    monkeypatch.setattr(
        "jarvis_common.email.probe_smtp_reachable", AsyncMock(return_value=(True, None))
    )
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
    from unittest.mock import AsyncMock

    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    monkeypatch.setattr(
        "jarvis_common.email.probe_smtp_reachable", AsyncMock(return_value=(True, None))
    )
    get_secrets_settings.cache_clear()

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    assert deliverable is True
    assert issues == []


# ---------------------------------------------------------------------------
# Reachability probe (probe_smtp_reachable) + its effect on effective_smtp_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_smtp_status_deliverable_but_unreachable_appends_value_free_issue(
    monkeypatch,
) -> None:
    """A deliverable relay that refuses a connection stays deliverable but gains a
    value-free reachability issue (host / port / credentials never leak)."""
    from unittest.mock import AsyncMock, MagicMock

    import aiosmtplib
    import jarvis_common.email as email_mod
    from jarvis_common.email import effective_smtp_status
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    # Skip the SSRF guard so the probe reaches the (mocked) connect.
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()
    email_mod._reachability_cache.clear()

    failing_client = MagicMock()
    failing_client.connect = AsyncMock(side_effect=aiosmtplib.SMTPConnectError("refused"))
    failing_client.ehlo = AsyncMock()
    failing_client.quit = AsyncMock()
    monkeypatch.setattr(aiosmtplib, "SMTP", MagicMock(return_value=failing_client))

    deliverable, issues = await effective_smtp_status(pool=None)

    get_secrets_settings.cache_clear()
    email_mod._reachability_cache.clear()
    assert deliverable is True, "deliverable is presence-based; unreachable must not flip it"
    assert len(issues) == 1, f"expected exactly the reachability issue; got {issues!r}"
    issue = issues[0]
    assert "connection" in issue.lower() or "mail server" in issue.lower()
    # Value-free: never echo the configured host / port / credentials.
    for leak in ("mail.example.com", "bot@example.com", "587"):
        assert leak not in issue, f"reachability issue leaked a configured value: {issue!r}"
    failing_client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_smtp_reachable_caches_within_ttl(monkeypatch) -> None:
    """Two probes within the TTL reconnect only once (result cached per host:port)."""
    from unittest.mock import AsyncMock, MagicMock

    import aiosmtplib
    import jarvis_common.email as email_mod
    from jarvis_common.email import probe_smtp_reachable
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()
    email_mod._reachability_cache.clear()

    client = MagicMock()
    client.connect = AsyncMock()
    client.ehlo = AsyncMock()
    client.quit = AsyncMock()
    monkeypatch.setattr(aiosmtplib, "SMTP", MagicMock(return_value=client))

    first = await probe_smtp_reachable(pool=None)
    second = await probe_smtp_reachable(pool=None)

    get_secrets_settings.cache_clear()
    email_mod._reachability_cache.clear()
    assert first == (True, None)
    assert second == (True, None)
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_smtp_reachable_not_deliverable_skips_connection(monkeypatch) -> None:
    """A not-deliverable config returns (False, None) and never opens a connection."""
    from unittest.mock import MagicMock

    import aiosmtplib
    import jarvis_common.email as email_mod
    from jarvis_common.email import probe_smtp_reachable
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    get_secrets_settings.cache_clear()
    email_mod._reachability_cache.clear()

    smtp_cls = MagicMock()
    monkeypatch.setattr(aiosmtplib, "SMTP", smtp_cls)

    reachable, issue = await probe_smtp_reachable(pool=None)

    get_secrets_settings.cache_clear()
    email_mod._reachability_cache.clear()
    assert (reachable, issue) == (False, None)
    smtp_cls.assert_not_called()


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
async def test_send_magic_link_failure_emits_event_and_returns_failed(monkeypatch) -> None:
    """A failing real send emits a magic_link_delivery_failed system event, then returns FAILED.

    The event carries email_hash + error_class (never the raw email or the
    bearer-token link); the sender returns MagicLinkDelivery.FAILED rather than
    raising so callers react without swallowing an exception.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    import aiosmtplib
    from jarvis_common.email import MagicLinkDelivery, _hash_email, send_magic_link
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
            result = await send_magic_link("user@example.com", raw_link, pool=pool)

    assert result is MagicLinkDelivery.FAILED

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
    from jarvis_common.email import MagicLinkDelivery, _hash_email, send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")
    get_secrets_settings.cache_clear()

    pool = MagicMock(name="pool")

    with patch.object(aiosmtplib, "send", new_callable=AsyncMock):
        with patch("jarvis_common.email.log_event", new_callable=AsyncMock) as mock_log_event:
            result = await send_magic_link(
                "user@example.com", "https://example.com/verify?token=abc", pool=pool
            )

    get_secrets_settings.cache_clear()

    assert result is MagicLinkDelivery.DELIVERED
    mock_log_event.assert_awaited_once()
    kwargs = mock_log_event.call_args.kwargs
    assert kwargs["level"] == "info"
    assert kwargs["message"] == "magic_link_sent"
    assert kwargs["context"]["email_hash"] == _hash_email("user@example.com")


@pytest.mark.asyncio
async def test_send_magic_link_unconfigured_returns_dropped_unconfigured(monkeypatch) -> None:
    """No SMTP host/sender → DROPPED_UNCONFIGURED and no delivery attempt."""
    from unittest.mock import AsyncMock, patch

    import aiosmtplib
    from jarvis_common.email import MagicLinkDelivery, send_magic_link
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("DEV_SMTP_LOG_ONLY", raising=False)
    get_secrets_settings.cache_clear()

    with patch.object(aiosmtplib, "send", new_callable=AsyncMock) as mock_send:
        result = await send_magic_link(
            "user@example.com", "https://example.com/verify?token=abc", pool=None
        )

    get_secrets_settings.cache_clear()
    assert result is MagicLinkDelivery.DROPPED_UNCONFIGURED
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_magic_link_private_host_returns_dropped_private_host(monkeypatch) -> None:
    """A non-public relay without opt-in → DROPPED_PRIVATE_HOST and no send."""
    from unittest.mock import AsyncMock, patch

    import aiosmtplib
    from jarvis_common.email import MagicLinkDelivery, send_magic_link
    from jarvis_common.pinned_transport import PinnedDestinationRejectedError
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("SMTP_HOST", "mail.internal")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.delenv("ALLOW_PRIVATE_SMTP_HOST", raising=False)
    monkeypatch.delenv("DEV_SMTP_LOG_ONLY", raising=False)
    get_secrets_settings.cache_clear()

    with patch.object(aiosmtplib, "send", new_callable=AsyncMock) as mock_send:
        with patch(
            "jarvis_common.email.connect_pinned_socket",
            new=AsyncMock(side_effect=PinnedDestinationRejectedError("not permitted")),
        ):
            result = await send_magic_link(
                "user@example.com", "https://example.com/verify?token=abc", pool=None
            )

    get_secrets_settings.cache_clear()
    assert result is MagicLinkDelivery.DROPPED_PRIVATE_HOST
    mock_send.assert_not_awaited()


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
        with patch(
            "jarvis_common.email.connect_pinned_socket",
            new=AsyncMock(return_value=socket.socket()),
        ):
            await send_magic_link(
                "user@example.com", "https://example.com/verify?token=abc", pool=None
            )

    get_secrets_settings.cache_clear()
    assert mock_send.call_args.kwargs["timeout"] == SMTP_SEND_TIMEOUT_SECONDS
    assert mock_send.call_args.kwargs["port"] is None
    assert mock_send.call_args.kwargs["sock"] is not None


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

    Rendering uses literal replacement, so URL content is never interpreted as
    part of a formatting expression.
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
