"""Configuration-encryption separation contract for Telegram startup."""

from __future__ import annotations


def test_main_has_no_configuration_encryption_reload_hook() -> None:
    """Telegram startup must not import or register Fernet cache reloading."""
    import telegram_bot.main as main_module

    assert not hasattr(main_module, "reload_fernet_on_sighup")
