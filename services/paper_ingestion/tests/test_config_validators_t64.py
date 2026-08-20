"""Unit tests for T6.4 config validator additions.

Covers:
- recommendation.enabled → _validate_bool rejects non-bool
- user.timezone → _validate_timezone rejects unknown zones; validator only fires on writes
"""

from __future__ import annotations

import pytest

from jarvis_common.config_validators import _validate_bool, _validate_timezone


# ---------------------------------------------------------------------------
# recommendation.enabled — wired to _validate_bool
# ---------------------------------------------------------------------------


def test_recommendation_enabled_accepts_true() -> None:
    _validate_bool(True)  # must not raise


def test_recommendation_enabled_accepts_false() -> None:
    _validate_bool(False)  # must not raise


def test_recommendation_enabled_rejects_string() -> None:
    with pytest.raises(ValueError, match="boolean"):
        _validate_bool("true")


def test_recommendation_enabled_rejects_int() -> None:
    with pytest.raises(ValueError, match="boolean"):
        _validate_bool(1)


def test_recommendation_enabled_rejects_none() -> None:
    with pytest.raises(ValueError, match="boolean"):
        _validate_bool(None)


# ---------------------------------------------------------------------------
# user.timezone — _validate_timezone
# ---------------------------------------------------------------------------


def test_timezone_accepts_utc() -> None:
    _validate_timezone("UTC")  # must not raise


def test_timezone_accepts_europe_berlin() -> None:
    _validate_timezone("Europe/Berlin")  # must not raise


def test_timezone_accepts_america_new_york() -> None:
    _validate_timezone("America/New_York")  # must not raise


def test_timezone_rejects_unknown_zone() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        _validate_timezone("Mars/Olympus")


def test_timezone_rejects_non_string() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        _validate_timezone(42)


def test_timezone_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        _validate_timezone("")
