"""Focused tests for make_bot_config callable-injection contract.

Covers:
- Negative: passing a wrong-shape class (bare ``object``) raises TypeError clearly.
- Positive: passing the real BotConfig class returns an object with the expected attribute.
"""

import pytest
from jarvis_common.testing_telegram import make_bot_config


def test_make_bot_config_wrong_shape_raises_type_error() -> None:
    """object(**kwargs) rejects keyword arguments — caller gets a clear TypeError."""
    with pytest.raises(TypeError):
        make_bot_config(object, jarvis_base_url="https://example.test")


def test_make_bot_config_real_class_returns_expected_attribute() -> None:
    """Passing the real BotConfig class produces an instance with the overridden field."""
    from telegram_bot.config import BotConfig  # lazy import: no module-level cross-service dep

    result = make_bot_config(BotConfig, jarvis_base_url="https://example.test")
    assert result.jarvis_base_url == "https://example.test"
