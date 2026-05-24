"""Shared auth test infrastructure: ASGI ``RoleMiddleware`` shim and
``_default_authenticated_user`` autouse fixture helper.

Cluster 6 of the 2026-05-24 polish-wave decomposition of ``jarvis_common.testing``.
"""

from __future__ import annotations

__all__ = ["RoleMiddleware", "_apply_default_authenticated_user"]

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType


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


@contextmanager
def _apply_default_authenticated_user(app: Any, routers_pkg: ModuleType):
    """Context manager: patch all strict user-id resolvers in *routers_pkg* to return 1.

    Intended for use inside a per-service autouse pytest fixture. Both the
    module-level symbol monkeypatch (for direct router-body callers) and the
    ``app.dependency_overrides`` entry (for ``Depends``-wired routes) are
    restored on exit.

    Parameters
    ----------
    app:
        The FastAPI application whose ``dependency_overrides`` to patch.
    routers_pkg:
        The service's ``routers`` package (e.g. ``paper_ingestion.routers``).
    """
    import importlib
    import pkgutil
    from unittest.mock import AsyncMock

    from jarvis_common.auth import current_user_id_strict_with_owner_override

    resolver_names = (
        "current_user_id_strict",
        "current_user_id_strict_with_owner_override",
    )
    saved: list[tuple[object, str, object]] = []
    for mod_info in pkgutil.iter_modules(routers_pkg.__path__):
        module = importlib.import_module(f"{routers_pkg.__name__}.{mod_info.name}")
        for name in resolver_names:
            if hasattr(module, name):
                saved.append((module, name, getattr(module, name)))
                setattr(module, name, AsyncMock(return_value=1))

    override_added = current_user_id_strict_with_owner_override not in app.dependency_overrides
    if override_added:
        app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: 1
    try:
        yield
    finally:
        for module, name, original in saved:
            setattr(module, name, original)
        if override_added:
            app.dependency_overrides.pop(current_user_id_strict_with_owner_override, None)
