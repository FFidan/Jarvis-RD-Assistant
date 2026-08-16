"""Bounded renewal-cookie transport for nginx authorization subrequests."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

AUTH_COOKIE_HEADER_PREFIX = "X-Jarvis-Set-Cookie-"
MAX_AUTH_COOKIES = 4

_SET_COOKIE = b"set-cookie"
_RELAY_PREFIX = AUTH_COOKIE_HEADER_PREFIX.lower().encode("ascii")


class AuthCookieRelayMiddleware:
    """Convert repeated auth-response cookies into numbered internal headers.

    Nginx's authorization-subrequest variables expose only the first repeated
    ``Set-Cookie`` response header. Platform therefore transports the bounded
    cookie set through distinct headers that nginx can restore individually on
    the external response. Other routes retain ordinary ``Set-Cookie`` headers.

    Parameters
    ----------
    app : ASGIApp
        Wrapped Platform application.
    authorize_path : str, default="/internal/authorize"
        Exact internal authorization path whose response is converted.
    maximum_cookies : int, default=4
        Maximum number of cookie headers accepted from the auth response.

    Raises
    ------
    ValueError
        If ``authorize_path`` is not absolute or ``maximum_cookies`` is not
        positive.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        authorize_path: str = "/internal/authorize",
        maximum_cookies: int = MAX_AUTH_COOKIES,
    ) -> None:
        if not authorize_path.startswith("/"):
            raise ValueError("authorize_path must be absolute")
        if maximum_cookies <= 0:
            raise ValueError("maximum_cookies must be positive")
        self._app = app
        self._authorize_path = authorize_path
        self._maximum_cookies = maximum_cookies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Relay one ASGI request, converting auth-response cookie headers.

        Parameters
        ----------
        scope : Scope
            ASGI connection scope.
        receive : Receive
            ASGI receive callable.
        send : Send
            ASGI send callable.
        """
        if scope["type"] != "http" or scope.get("path") != self._authorize_path:
            await self._app(scope, receive, send)
            return

        async def send_with_cookie_relay(message: Message) -> None:
            """Rewrite one authorization response before sending it downstream."""
            if message["type"] != "http.response.start":
                await send(message)
                return

            headers = message.get("headers", [])
            cookies = [value for name, value in headers if name.lower() == _SET_COOKIE]
            retained = [
                (name, value)
                for name, value in headers
                if name.lower() != _SET_COOKIE and not name.lower().startswith(_RELAY_PREFIX)
            ]
            if len(cookies) > self._maximum_cookies:
                message["status"] = 500
            else:
                retained.extend(
                    (
                        f"{AUTH_COOKIE_HEADER_PREFIX}{index}".lower().encode("ascii"),
                        cookie,
                    )
                    for index, cookie in enumerate(cookies, start=1)
                )
            message["headers"] = retained
            await send(message)

        await self._app(scope, receive, send_with_cookie_relay)


__all__ = [
    "AUTH_COOKIE_HEADER_PREFIX",
    "AuthCookieRelayMiddleware",
    "MAX_AUTH_COOKIES",
]
