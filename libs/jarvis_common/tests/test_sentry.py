"""Unit tests for jarvis_common.sentry.maybe_init_sentry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
