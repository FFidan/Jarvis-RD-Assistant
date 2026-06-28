"""Sentinel-driven maintenance-mode middleware (P6 one-click restore).

Two sentinels gate serving while the postgres-backup sidecar runs a restore --
no docker.sock needed, and every exempt-allowlist path keeps serving under both:

* The soft ``.maintenance`` sentinel is the age-expiring anti-brick for the
  PRE-destruction quiesce window: while it exists and is fresher than
  ``MAINTENANCE_MAX_AGE_S`` (default 1800s) each non-exempt request gets
  ``503`` + ``Retry-After``; older than the max age it is ignored so a crashed
  restore can never brick the stack permanently.
* The durable ``.destructive`` sentinel (``MAINTENANCE_DESTRUCTIVE_SENTINEL``)
  is never heartbeated and never auto-expires: restore.sh touches it at the DB
  DROP boundary and removes it only on the same clean / pre-DROP-failure lift
  that clears ``.maintenance``. While present each non-exempt request gets
  ``503`` regardless of age, so a SIGKILLed restore that left a half-restored
  DB stays fail-closed until an operator clears it.

An absent sentinel pair means normal serving. The middleware is pure-ASGI (it
mirrors
:class:`jarvis_common.app_factory.RawClientStashMiddleware`): on the
non-maintenance path it calls ``self.app(scope, receive, send)`` unchanged, so
SSE/streaming routes are never buffered.
"""

from __future__ import annotations

import json
import os
import time

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

# Paths a mid-restore browser reload must still reach: the health probe, the
# restore-progress poll, the pre-auth setup-status read that drives the app
# shell on load, and static assets. The front end renders identity from its
# persisted store (App.tsx: isAuthenticated/isSessionValid are client-side
# reads), so the only bootstrap *server* read is GET /api/setup/status -- the
# data routes are intentionally NOT exempt and return a degraded 503 during a
# restore. (The progress poll surviving the brief DB-down window inside a
# restore is a separate Gate-P6 concern of the status endpoint + the FE poll,
# not of this allowlist.)
DEFAULT_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/health",
    "/api/admin/backups/restore/status",
    "/api/setup/status",
    "/assets",
    "/static",
    "/favicon",
)

_DEFAULT_SENTINEL_PATH = "/backup-trigger/.maintenance"
_DEFAULT_DESTRUCTIVE_SENTINEL_PATH = "/backup-trigger/.destructive"
_DEFAULT_MAX_AGE_S = 1800
# Retry-After advertised to clients: short so a polling progress view re-checks
# promptly, rather than the full staleness window.
_RETRY_AFTER_S = 30


class MaintenanceMiddleware:
    """Return ``503`` while a fresh maintenance sentinel exists (pure-ASGI)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        sentinel_path: str | None = None,
        destructive_sentinel_path: str | None = None,
        max_age_s: int | None = None,
        exempt_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self.app = app
        self.sentinel_path = (
            sentinel_path
            if sentinel_path is not None
            else os.environ.get("MAINTENANCE_SENTINEL", _DEFAULT_SENTINEL_PATH)
        )
        self.destructive_sentinel_path = (
            destructive_sentinel_path
            if destructive_sentinel_path is not None
            else os.environ.get(
                "MAINTENANCE_DESTRUCTIVE_SENTINEL", _DEFAULT_DESTRUCTIVE_SENTINEL_PATH
            )
        )
        self.max_age_s = (
            max_age_s
            if max_age_s is not None
            else int(os.environ.get("MAINTENANCE_MAX_AGE_S", str(_DEFAULT_MAX_AGE_S)))
        )
        self.exempt_prefixes = (
            exempt_prefixes if exempt_prefixes is not None else DEFAULT_EXEMPT_PREFIXES
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        if path.startswith(self.exempt_prefixes):
            await self.app(scope, receive, send)
            return

        # Durable fail-closed: the destructive-phase sentinel is NEVER heartbeated
        # and NEVER auto-expires. restore.sh touches it once the DROP window opens
        # and removes it ONLY on the same clean / pre-DROP-failure lift that clears
        # .maintenance. If present -> 503 with no age gate: a SIGKILLed restore that
        # left a half-restored DB stays 503 until an operator clears it. (The soft
        # .maintenance 1800s expiry remains the anti-brick for the PRE-destruction
        # quiesce window only.)
        if os.path.exists(self.destructive_sentinel_path):
            await self._send_unavailable(send)
            return

        try:
            mtime = os.stat(self.sentinel_path).st_mtime
        except OSError:
            await self.app(scope, receive, send)
            return

        if time.time() - mtime <= self.max_age_s:
            await self._send_unavailable(send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_unavailable(send: Send) -> None:
        body = json.dumps({"detail": "Restore in progress", "retry_after": _RETRY_AFTER_S}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(_RETRY_AFTER_S).encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def configure_maintenance(app: FastAPI) -> None:
    """Register :class:`MaintenanceMiddleware` reading its env-var defaults."""
    app.add_middleware(MaintenanceMiddleware)
