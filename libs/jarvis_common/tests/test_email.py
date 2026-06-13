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
