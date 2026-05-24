"""Shared auth test infrastructure: ASGI ``RoleMiddleware`` shim.

Cluster 6 of the 2026-05-24 polish-wave decomposition of ``jarvis_common.testing``.
"""

from __future__ import annotations

__all__ = ["RoleMiddleware"]

from typing import Any


class RoleMiddleware:
    """Minimal ASGI middleware that injects request.state.user_role before routing.

    When ``role`` is ``None`` the attribute is deliberately left absent,
    which exercises the API-key path in admin-gate tests.
    """

    def __init__(self, app: Any, role: str | None) -> None:
        """Wrap *app* and inject *role* into every HTTP request's state."""
        self._app = app
        self._role = role

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Set ``request.state.user_role`` before forwarding the ASGI call."""
        if scope["type"] == "http" and self._role is not None:
            from starlette.requests import Request

            request = Request(scope)
            request.state.user_role = self._role
        await self._app(scope, receive, send)
