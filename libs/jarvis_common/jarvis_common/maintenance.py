"""Restore maintenance and post-restore outbound-quarantine enforcement.

The age-limited ``.maintenance`` marker returns ``503`` with ``Retry-After``
for non-exempt requests while a restore prepares to change data. A stale soft
marker is ignored after ``MAINTENANCE_MAX_AGE_S`` so a pre-destructive crash
cannot leave the service unavailable indefinitely.

The durable ``.destructive`` marker begins at the first database drop, receives
no heartbeat, and never expires automatically. It is removed only after a clean
restore or a failure before data changed; otherwise requests remain fail-closed
until recovery completes. Independently, ``.outbound-quarantine.json`` can keep
credential-bearing egress disabled after local reads resume.

The ASGI middleware forwards non-maintenance requests without buffering, which
preserves streaming responses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from jarvis_common.paths import read_regular_json_file

logger = logging.getLogger(__name__)

# Mid-restore reloads retain health, progress, setup-status, and static-asset
# access. Data routes remain unavailable. Restore-status authentication is
# enforced separately, and acknowledgement becomes reachable only after both
# maintenance markers are clear.
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
_QUARANTINE_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_QUARANTINE_RECOVERY_ROUTES = frozenset(
    {
        ("POST", "/api/admin/backups/restore/acknowledge"),
        ("POST", "/api/auth/api-key-session"),
        ("POST", "/api/auth/verify"),
        ("POST", "/api/auth/passkeys/capability"),
        ("POST", "/api/auth/passkeys/login/begin"),
        ("POST", "/api/auth/passkeys/login/finish"),
        ("POST", "/api/auth/logout"),
    }
)
_QUARANTINE_DETAIL = "This restored deployment is read-only until outbound credentials are reviewed"
# Off-host restore rotation marker: restore.sh writes an integer epoch here after it
# rebinds the postgres role + materializes ./secrets, so each postgres-connecting
# service can self-restart onto the rotated secrets (mirrors the sentinel-path resolve).
SECRETS_ROTATED_MARKER = (
    Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".secrets_rotated"
)


class OutboundQuarantineStateError(ValueError):
    """Raised when an existing quarantine record is not trustworthy."""


class OutboundEgressBlockedError(RuntimeError):
    """Raised when restored credentials remain quarantined.

    Background tasks retry this error. Network sinks also raise it if quarantine
    becomes active after an earlier request or scheduler check.
    """


class OutboundQuarantineBlockedError(OutboundEgressBlockedError):
    """Raised when quarantine — not restore maintenance — is what blocks the work.

    Restore maintenance ends on its own; quarantine waits for a person to review
    the restored credentials. Callers that retry both use this distinction to
    stop retrying the quarantined case instead of waiting forever.
    """


@dataclass(frozen=True, slots=True)
class OutboundQuarantineState:
    """Validated non-secret state for one completed off-host restore.

    Attributes
    ----------
    restore_id : str
        Lowercase identifier shared with the restore request and browser token.
    source : str
        Always ``inbox``; same-host restores never create or clear quarantine.
    requested_at : str
        Original aware restore-request timestamp.
    completed_at : str
        Aware timestamp recorded before destructive maintenance was lifted.
    review_state : str
        Exact state marker ``awaiting_review``.
    """

    restore_id: str
    source: str
    requested_at: str
    completed_at: str
    review_state: str


def outbound_quarantine_file() -> Path:
    """Resolve the quarantine sentinel beside the active restore trigger dir.

    Returns
    -------
    pathlib.Path
        Environment override when set, otherwise the record in the active
        ``BACKUP_TRIGGER_DIR``.
    """
    trigger_dir = Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger"))
    return Path(
        os.environ.get(
            "OUTBOUND_QUARANTINE_SENTINEL",
            str(trigger_dir / ".outbound-quarantine.json"),
        )
    )


def maintenance_active() -> bool:
    """Return ``True`` while a maintenance sentinel currently gates serving.

    Mirrors :class:`MaintenanceMiddleware` with the same env-overridable paths
    and max age: the durable destructive sentinel is active regardless of age;
    the soft sentinel is active only while fresher than the max age. Never
    raises — an unreadable soft sentinel means "not active".

    Returns
    -------
    bool
        Whether HTTP and background work must remain paused for restore.
    """
    destructive_path = os.environ.get(
        "MAINTENANCE_DESTRUCTIVE_SENTINEL", _DEFAULT_DESTRUCTIVE_SENTINEL_PATH
    )
    if os.path.exists(destructive_path):
        return True
    sentinel_path = os.environ.get("MAINTENANCE_SENTINEL", _DEFAULT_SENTINEL_PATH)
    max_age_s = int(os.environ.get("MAINTENANCE_MAX_AGE_S", str(_DEFAULT_MAX_AGE_S)))
    try:
        mtime = os.stat(sentinel_path).st_mtime
    except OSError:
        return False
    return time.time() - mtime <= max_age_s


def outbound_quarantine_active() -> bool:
    """Return ``True`` when a restore has not yet cleared outbound quarantine.

    Quarantine is intentionally independent of maintenance: a successful off-host
    restore may resume local reads while all credential-bearing egress remains
    blocked pending review. Existence is the fail-closed authority; malformed or
    unreadable content must never silently re-enable outbound work.

    Returns
    -------
    bool
        ``True`` for every existing directory entry, including dangling links or
        malformed records. Only a bound restore session, the configured owner,
        or ``jarvis-research restore acknowledge <restore-id>`` can remove it.
    """
    return os.path.lexists(outbound_quarantine_file())


def read_outbound_quarantine() -> OutboundQuarantineState | None:
    """Read and validate the durable non-secret off-host restore record.

    Absence means no quarantine. Any existing state that is unreadable, linked,
    malformed, or outside the exact schema raises so callers remain fail-closed.

    Returns
    -------
    OutboundQuarantineState or None
        Validated current state, or ``None`` only when no directory entry exists.

    Raises
    ------
    OutboundQuarantineStateError
        If an existing record cannot be trusted. Callers must not treat this as
        absence or permit egress.
    """
    path = outbound_quarantine_file()
    if not os.path.lexists(path):
        return None
    try:
        data = read_regular_json_file(path)
    except (OSError, ValueError, TypeError) as exc:
        raise OutboundQuarantineStateError("quarantine state is unreadable") from exc
    expected = {
        "version",
        "restore_id",
        "source",
        "requested_at",
        "completed_at",
        "review_state",
    }
    if not isinstance(data, dict) or set(data) != expected:
        raise OutboundQuarantineStateError("quarantine state has an invalid shape")
    restore_id = data["restore_id"]
    if not isinstance(restore_id, str) or re.fullmatch(r"[0-9a-f]{32}", restore_id) is None:
        raise OutboundQuarantineStateError("quarantine restore ID is invalid")
    try:
        requested = datetime.fromisoformat(data["requested_at"])
        completed = datetime.fromisoformat(data["completed_at"])
    except (TypeError, ValueError) as exc:
        raise OutboundQuarantineStateError("quarantine timestamps are invalid") from exc
    if (
        data["version"] != 1
        or data["source"] != "inbox"
        or data["review_state"] != "awaiting_review"
        or requested.tzinfo is None
        or completed.tzinfo is None
        or requested.astimezone(UTC) > completed.astimezone(UTC)
    ):
        raise OutboundQuarantineStateError("quarantine state is inconsistent")
    return OutboundQuarantineState(
        restore_id=restore_id,
        source=data["source"],
        requested_at=data["requested_at"],
        completed_at=data["completed_at"],
        review_state=data["review_state"],
    )


def secrets_rotated_since(started_at: float) -> bool:
    """Return whether mounted secrets changed after a service started.

    The shared marker contains the Unix timestamp of the latest refresh. A
    missing, unreadable, or malformed marker is treated as no newer refresh.

    Parameters
    ----------
    started_at : float
        Process start time expressed as a Unix epoch.

    Returns
    -------
    bool
        Whether a newer valid marker requires the service to reload secrets.
    """
    try:
        epoch = float(SECRETS_ROTATED_MARKER.read_text().strip())
    except (OSError, ValueError):
        return False
    return epoch > started_at


def ensure_outbound_egress_allowed(operation_label: str) -> None:
    """Refuse a credential-bearing sink while outbound quarantine exists.

    Call this immediately before opening a network connection or handing a
    restored credential to another process. The existence check is deliberately
    separate from record parsing: malformed, unreadable, and linked directory
    entries remain a fail-closed authority.

    Parameters
    ----------
    operation_label : str
        Non-secret operation name used only in the server log.

    Raises
    ------
    OutboundEgressBlockedError
        If any outbound-quarantine directory entry exists.
    """
    if not outbound_quarantine_active():
        return
    logger.info("block %s: outbound quarantine awaiting restore review", operation_label)
    raise OutboundEgressBlockedError(
        "outbound egress is disabled pending restored credential review"
    )


def maintenance_skip_reason(job_label: str) -> Literal["restore", "quarantine"] | None:
    """Return which condition pauses background work, or ``None`` if neither does.

    Both conditions pause the same work, but they end differently: restore
    maintenance clears itself, while quarantine waits for a person to review the
    restored credentials. A caller that must bound its waiting reads the reason;
    one that only needs to pause calls :func:`skip_for_maintenance`.

    Parameters
    ----------
    job_label : str
        Non-secret label included in the skip log.

    Returns
    -------
    {"restore", "quarantine"} or None
        The active condition, checked in that order.
    """
    if maintenance_active():
        logger.info("skip %s: maintenance in progress", job_label)
        return "restore"
    if outbound_quarantine_active():
        logger.info("skip %s: outbound quarantine awaiting restore review", job_label)
        return "quarantine"
    return None


def skip_for_maintenance(job_label: str) -> bool:
    """Return whether background work must pause.

    Restore maintenance and outbound quarantine both pause background work.
    Credential-bearing network calls also use
    :func:`ensure_outbound_egress_allowed` immediately before sending data.
    Any quarantine entry is active, including one with malformed contents.

    Parameters
    ----------
    job_label : str
        Non-secret label included in the skip log.

    Returns
    -------
    bool
        Whether the caller must return without doing background work.
    """
    return maintenance_skip_reason(job_label) is not None


class MaintenanceMiddleware:
    """Enforce restore maintenance and post-restore read-only HTTP policy.

    The soft sentinel expires by age; the destructive sentinel never expires.
    Health checks, initial setup, static assets, and progress polling remain
    reachable. During outbound quarantine, safe reads remain available but
    mutations return ``503`` except for owner sign-in, logout, and restore
    acknowledgement. Network-call checks also protect work accepted earlier.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        sentinel_path: str | None = None,
        destructive_sentinel_path: str | None = None,
        max_age_s: int | None = None,
        exempt_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        """Configure sentinel locations and exact maintenance exemptions.

        Parameters
        ----------
        app : ASGIApp
            Downstream ASGI application.
        sentinel_path : str or None
            Optional soft-maintenance sentinel override.
        destructive_sentinel_path : str or None
            Optional durable destructive-phase sentinel override.
        max_age_s : int or None
            Maximum age of the soft sentinel before it is ignored.
        exempt_prefixes : tuple[str, ...] or None
            Prefixes that remain available during maintenance; outbound
            quarantine still applies its independent exact mutation policy.
        """
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
        """Handle one ASGI request under the current maintenance policy.

        Parameters
        ----------
        scope : Scope
            ASGI connection scope.
        receive : Receive
            ASGI event receiver.
        send : Send
            ASGI event sender.

        Returns
        -------
        None
            Returns after forwarding the request or sending a maintenance
            response.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope["path"]
        method = scope.get("method", "GET").upper()
        if (
            outbound_quarantine_active()
            and method not in _QUARANTINE_SAFE_METHODS
            and (method, path) not in _QUARANTINE_RECOVERY_ROUTES
        ):
            await self._send_quarantined(send)
            return

        if path.startswith(self.exempt_prefixes):
            await self.app(scope, receive, send)
            return

        # The destructive marker never expires or receives heartbeats. It begins
        # at the first database drop and is cleared only by a clean restore or a
        # failure before data changed. While present, every non-exempt request
        # remains unavailable regardless of marker age.
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

    @staticmethod
    async def _send_quarantined(send: Send) -> None:
        body = json.dumps({"detail": _QUARANTINE_DETAIL}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def configure_maintenance(app: FastAPI) -> None:
    """Register :class:`MaintenanceMiddleware` reading its env-var defaults."""
    app.add_middleware(MaintenanceMiddleware)
