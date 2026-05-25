"""Middleware-wiring smoke test for telegram_bot internal_api (W5-CF5)."""


def test_internal_api_has_session_and_slowapi_middleware() -> None:
    from telegram_bot.internal_api import _internal_app

    middleware_class_names = {m.cls.__name__ for m in _internal_app.user_middleware}

    assert "SessionMiddleware" in middleware_class_names, (
        f"SessionMiddleware missing from internal_api; got {middleware_class_names}"
    )
    assert "SlowAPIMiddleware" in middleware_class_names, (
        f"SlowAPIMiddleware missing from internal_api; got {middleware_class_names}"
    )
