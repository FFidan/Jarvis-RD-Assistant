"""Unit tests for jarvis_common.sentry.maybe_init_sentry."""

from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

import pytest
from jarvis_common.sentry import maybe_init_sentry


def test_no_op_when_dsn_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    with patch("sentry_sdk.init") as mock_init:
        maybe_init_sentry("test-svc")
        mock_init.assert_not_called()


def test_no_op_when_dsn_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "   ")
    with patch("sentry_sdk.init") as mock_init:
        maybe_init_sentry("test-svc")
        mock_init.assert_not_called()


def test_initializes_sentry_with_correct_args(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = "https://fake@example.com/0"
    monkeypatch.setenv("SENTRY_DSN", dsn)

    mock_init = MagicMock()
    mock_set_tag = MagicMock()

    with (
        patch("sentry_sdk.init", mock_init),
        patch("sentry_sdk.set_tag", mock_set_tag),
    ):
        maybe_init_sentry("test-svc")

    mock_init.assert_called_once_with(
        dsn=dsn,
        send_default_pii=False,
        traces_sample_rate=0.0,
        transport=ANY,
    )
    mock_set_tag.assert_called_once_with("service", "test-svc")


def test_service_name_tag_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://fake@example.com/1")

    captured_tags: list[tuple[str, str]] = []

    def fake_set_tag(key: str, value: str) -> None:
        captured_tags.append((key, value))

    with (
        patch("sentry_sdk.init"),
        patch("sentry_sdk.set_tag", fake_set_tag),
    ):
        maybe_init_sentry("paper_ingestion")

    assert ("service", "paper_ingestion") in captured_tags


def test_quarantine_disables_sentry_before_dsn_use(monkeypatch, tmp_path) -> None:
    quarantine = tmp_path / ".outbound-quarantine.json"
    quarantine.touch()
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    monkeypatch.setenv("SENTRY_DSN", "https://restored@example.com/1")

    with patch("sentry_sdk.init") as init:
        maybe_init_sentry("paper_ingestion")

    init.assert_not_called()


def test_initialized_sentry_drops_events_when_quarantine_begins(monkeypatch, tmp_path) -> None:
    """An initialized client must not hand later events to its transport."""
    import sentry_sdk

    quarantine = tmp_path / ".outbound-quarantine.json"
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(quarantine))
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.com/1")

    try:
        with patch("sentry_sdk.transport.HttpTransport._request") as request:
            maybe_init_sentry("paper_ingestion")
            quarantine.touch()
            sentry_sdk.capture_message("must remain local")
            sentry_sdk.flush(timeout=1)
        request.assert_not_called()
    finally:
        sentry_sdk.init(dsn=None)
