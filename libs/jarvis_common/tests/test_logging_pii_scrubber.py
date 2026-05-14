"""Tests for the structlog PII scrubber processor (L-07)."""

from __future__ import annotations

from jarvis_common.logging_config import DEFAULT_PII_KEYS, _make_pii_scrubber


def test_pii_scrubber_drops_sensitive_keys() -> None:
    """All default PII keys must be removed from the event dict."""
    scrubber = _make_pii_scrubber(DEFAULT_PII_KEYS)

    event_dict = {
        "event": "user logged in",
        "user_id": "abc-123",
        "email": "alice@example.com",
        "token": "tk-deadbeef",
        "body": "POST payload",
        "api_key": "sk-1234",
        "password": "hunter2",
        "secret": "shh",
        "Authorization": "Bearer xyz",
        "X-API-Key": "header-key",
        "request_id": "req-1",
    }
    out = scrubber(None, "info", event_dict)

    for sensitive in DEFAULT_PII_KEYS:
        assert sensitive not in out, f"{sensitive!r} must be scrubbed"
    # Non-PII keys are preserved
    assert out["event"] == "user logged in"
    assert out["user_id"] == "abc-123"
    assert out["request_id"] == "req-1"


def test_pii_scrubber_accepts_custom_keys() -> None:
    """Callers can override the default set with a custom frozenset."""
    custom = frozenset({"custom_secret", "tracking_id"})
    scrubber = _make_pii_scrubber(custom)

    event_dict = {
        "event": "x",
        "custom_secret": "drop me",
        "tracking_id": "drop me too",
        "email": "kept@example.com",  # NOT in custom set → kept
    }
    out = scrubber(None, "info", event_dict)

    assert "custom_secret" not in out
    assert "tracking_id" not in out
    assert out["email"] == "kept@example.com"


def test_pii_scrubber_no_error_when_keys_absent() -> None:
    """Missing keys are silently ignored (no KeyError)."""
    scrubber = _make_pii_scrubber(DEFAULT_PII_KEYS)
    out = scrubber(None, "info", {"event": "no PII here"})
    assert out == {"event": "no PII here"}
