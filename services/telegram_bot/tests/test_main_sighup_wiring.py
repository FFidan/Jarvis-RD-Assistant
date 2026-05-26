"""Assert that telegram_bot main() registers the SIGHUP Fernet-cache handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_main_calls_reload_fernet_on_sighup() -> None:
    """main() must call reload_fernet_on_sighup before entering the event loop."""
    with (
        patch("telegram_bot.main.reload_fernet_on_sighup") as mock_reload,
        patch("telegram_bot.main.BotConfig.from_env") as mock_config,
        patch("telegram_bot.main.Application.builder") as mock_builder,
    ):
        mock_config.return_value = MagicMock()
        mock_app = MagicMock()
        chain = mock_builder.return_value.token.return_value.post_init.return_value
        chain.post_shutdown.return_value.build.return_value = mock_app

        from telegram_bot.main import main

        main()

        mock_reload.assert_called_once()
