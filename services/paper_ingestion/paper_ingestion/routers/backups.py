"""Administrative backup, restore, download, and retention endpoints.

Archive-bearing routes require an administrator session; the operations API key
never authorizes archive access. Restore-status polling additionally accepts a
restore-session token so it remains available while restored session rows are
replaced. Off-host quarantine acknowledgement requires that token or the current
configured-owner session and consumes the token before outbound access resumes.

Backup and restore requests are published to a shared trigger volume for the
dedicated sidecar, keeping database tools and container control out of this
service.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from jarvis_common.audit import log_audit
from jarvis_common.auth import (
    current_user_id_strict,
    read_restore_status_token_record,
    require_admin,
    require_admin_or_api_key,
    restore_acknowledgement_bearer_valid,
    restore_status_bearer_valid,
    restore_status_token_file,
    verify_api_key,
)
from jarvis_common.event_log import log_event
from jarvis_common.maintenance import (
    OutboundQuarantineState,
    OutboundQuarantineStateError,
    outbound_quarantine_file,
    read_outbound_quarantine,
)
from jarvis_common.owner import resolve_owner_identity
from jarvis_common.paths import secure_path
from pydantic import ValidationError

from paper_ingestion.deps import limiter
from paper_ingestion.models.backups import (
    BackupEntry,
    BackupStatus,
    DeleteOutcome,
    DeleteRequest,
    InboxRestorePoint,
    RestoreAcknowledgement,
    RestoreLastRun,
    RestorePointsResponse,
    RestoreRequest,
    RestoreStatus,
    RetentionConfig,
)
from paper_ingestion.services.backup_archive import (
    _BACKUP_DIR,
    _FILENAME_RE,  # noqa: F401  # re-export
    _QDRANT_RE,  # noqa: F401  # re-export
    _RESTORE_TOKEN_TTL,
    _TS_RE,  # noqa: F401  # re-export
    _code_max_migration,  # noqa: F401  # re-export
    _fsync_directory,
    _group_restore_points,
    _last_run_flag,
    _last_run_succeeded,
    _list_entries,
    _read_inbox_manifest,
    _read_last_run,
    _read_manifest,
    _restore_state_lock,
    _validate_name,
    _write_all,
    _write_status_token,
    _write_upload_grant,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/backups", tags=["admin", "backups"])

# Sentinel flag-file in a small RW volume the sidecar loop polls each iteration.
_TRIGGER_SENTINEL = Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".backup_now"
# Restore request sentinel (this service writes it) + status file (the sidecar's
# restore.sh writes it). Same RW volume as the backup trigger; the app only ever
# writes a JSON request and reads a JSON status — it never runs the restore.
_RESTORE_SENTINEL = (
    Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".restore_request.json"
)
_RESTORE_STATUS = (
    Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".restore_status.json"
)
# Delete request sentinel (this service writes it) + retention config (this
# service reads/writes it). Same RW volume as the backup trigger; the actual
# ``rm`` of archives is performed by the sidecar's ``prune.sh`` — this service
# never deletes a file nor opens anything under _BACKUP_DIR (mounted read-only).
_DELETE_SENTINEL = (
    Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".delete_request.json"
)
_RETENTION_CONFIG = (
    Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".retention.json"
)
# Outcome of the sidecar's last prune run (``scripts/prune.sh`` writes it).
_LAST_DELETE = Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".last_delete.json"
RESTORE_ACKNOWLEDGEMENT_PHRASE = "I HAVE REVIEWED RESTORED CREDENTIALS"


async def restore_status_auth(request: Request) -> None:
    """Authorize a restore-status request.

    Accept the current restore token, the operations key, or an administrator
    browser session. The restore token is validated without database access so
    polling survives replacement of session rows. This check does not consume
    the token or acknowledge quarantine.

    Parameters
    ----------
    request : Request
        Current progress request and its candidate credentials.

    Raises
    ------
    HTTPException
        If none of the three status credentials is valid.

    """
    if restore_status_bearer_valid(request):
        return
    await verify_api_key(request, request.headers.get("X-API-Key"))
    await require_admin_or_api_key(request)


@router.get("", response_model=list[BackupEntry], dependencies=[Depends(require_admin)])
@limiter.limit("30/minute")
async def list_backups(request: Request) -> list[BackupEntry]:
    """List backup archive metadata (newest first)."""
    entries = _list_entries()
    await log_audit(
        request.app.state.db_pool,
        action="backup.list",
        resource="backups",
        user_id=_caller_id(request),
    )
    return entries


@router.get("/status", response_model=BackupStatus, dependencies=[Depends(require_admin)])
@limiter.limit("30/minute")
async def backup_status(request: Request) -> BackupStatus:
    """Report sidecar reachability, last-success (newest archive) and last-attempt.

    ``last_run_at`` (newest-archive mtime) is the last *success* proxy; the
    ``last_attempt_*`` fields come from the sidecar's ``.last_run.json`` so a
    failed run is visible as "attempted + failed", not "no recent backup".
    """
    entries = _list_entries()
    last = entries[0].modified_at if entries else None
    run = _read_last_run()
    return BackupStatus(
        backup_dir_available=_BACKUP_DIR.is_dir(),
        archive_count=len(entries),
        last_run_at=last,
        last_attempt_at=run.get("attempted_at") if run else None,
        last_run_succeeded=_last_run_succeeded(run),
        last_run_vectors_captured=_last_run_flag(run, "vectors_captured"),
        last_run_s3_complete=_last_run_flag(run, "s3_complete"),
        trigger_pending=_TRIGGER_SENTINEL.exists(),
    )


@router.get(
    "/restore-points",
    response_model=RestorePointsResponse,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("30/minute")
async def list_restore_points(request: Request) -> RestorePointsResponse:
    """List archives grouped into per-timestamp restore points (newest first)."""
    points = _group_restore_points(_list_entries())
    run = _read_last_run()
    last_run = None
    retention = None
    if run:
        retention = run.get("retention_days")
        last_run = RestoreLastRun(
            attempted_at=run.get("attempted_at"),
            succeeded=run.get("succeeded"),
            stores=run.get("stores") or {},
            vectors_captured=_last_run_flag(run, "vectors_captured"),
            s3_complete=_last_run_flag(run, "s3_complete"),
        )
    await log_audit(
        request.app.state.db_pool,
        action="backup.restore_points",
        resource="backups",
        user_id=_caller_id(request),
    )
    return RestorePointsResponse(
        restore_points=points,
        retention_days=retention,
        last_run=last_run,
    )


@router.get("/inbox", response_model=list[InboxRestorePoint], dependencies=[Depends(require_admin)])
@limiter.limit("30/minute")
async def list_inbox_restore_points(request: Request) -> list[InboxRestorePoint]:
    """List off-host restore points staged in the restore_inbox (sidecar-authored).

    The app never mounts /restore-inbox; it reads only the sanitized
    ``.inbox_manifest.json`` (names + booleans) the postgres-backup sidecar refreshes
    each loop iteration, so it gains no new destructive privilege. A missing or
    malformed manifest degrades to ``[]`` (e.g. the operator has not dropped an
    archive set yet) rather than erroring.
    """
    points = _read_inbox_manifest()
    await log_audit(
        request.app.state.db_pool,
        action="backup.inbox_list",
        resource="backups/inbox",
        user_id=_caller_id(request),
    )
    return points


@router.post("", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin)])
@limiter.limit("3/minute")
async def trigger_backup(request: Request) -> dict[str, str]:
    """Request an on-demand backup by writing a sentinel the sidecar loop polls.

    The app cannot run pg_dump itself; the postgres-backup sidecar checks for
    this flag each loop iteration and runs immediately, then removes it.
    """
    try:
        _TRIGGER_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _TRIGGER_SENTINEL.write_text(datetime.now(UTC).isoformat())
    except OSError as exc:
        logger.error("backup trigger sentinel write failed: %r", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Backup sidecar trigger is unavailable. "
                "Ensure the postgres-backup service is running."
            ),
        ) from exc
    await log_audit(
        request.app.state.db_pool,
        action="backup.trigger",
        resource="backups",
        user_id=_caller_id(request),
    )
    await log_event(
        pool=request.app.state.db_pool,
        level="info",
        category="job",
        source="backups",
        message="Backup requested",
        context={},
    )
    return {"status": "scheduled"}


def _validate_local_restore(timestamp: str, *, allow_missing_pdfs: bool) -> None:
    """Validate a LOCAL restore target against the read-only /backups listing.

    The point must exist and be complete, must not be newer than this deployment, and
    a present-but-unreadable manifest is rejected. Raises the matching HTTPException;
    returns None when the target is valid. (Extracted verbatim from ``request_restore``
    so the source branch stays flat.)
    """
    pt = next(
        (p for p in _group_restore_points(_list_entries()) if p.timestamp == timestamp),
        None,
    )
    if pt is None or not pt.complete:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No complete backup at that time",
        )
    if pt.legacy_missing_pdfs and not allow_missing_pdfs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This pre-v1.2 backup has no saved PDFs. Confirm that restoring it may "
                "leave papers without their local PDF files."
            ),
        )
    if pt.compat == "newer":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That backup is newer than this deployment",
        )
    # Reject a manifest that is present but unparseable: _read_manifest returns None
    # only on OSError/ValueError, so a present valid-but-no-schema_version manifest
    # still restores — only a structurally broken one is blocked. A path that escapes
    # _BACKUP_DIR is treated exactly like an absent manifest.
    try:
        manifest_present = secure_path(_BACKUP_DIR, f"manifest_{timestamp}.json").exists()
    except ValueError:
        manifest_present = False
    if manifest_present and _read_manifest(timestamp) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That backup's manifest is present but unreadable or incomplete",
        )


def _validate_inbox_restore(timestamp: str, *, allow_missing_pdfs: bool) -> None:
    """Validate an INBOX (off-host) restore target against the sidecar's inbox manifest.

    The point must be present in the manifest, complete, carry a secrets archive, and
    have its one-time operator key staged. A pre-v1.2 set without PDFs additionally
    needs the explicit missing-PDF authorization. The local group/compat/manifest checks
    do not apply — restore.sh authenticates and compatibility-gates the off-host archive
    before any destructive mutation. Raises 404 or 409; returns None when valid.
    """
    pt = next((p for p in _read_inbox_manifest() if p.timestamp == timestamp), None)
    if pt is None or not pt.complete:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No complete backup at that time",
        )
    if not pt.has_secrets:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This off-host backup has no secrets archive. Add it before restoring; "
                "JARVIS will not change data without it."
            ),
        )
    if not pt.has_pdfs:
        if not pt.legacy_missing_pdfs:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No complete backup at that time",
            )
        if not allow_missing_pdfs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This pre-v1.2 backup has no saved PDFs. Confirm that restoring it may "
                    "leave papers without their local PDF files."
                ),
            )
    if not pt.has_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload or drop the one-time operator key before restoring from the inbox.",
        )


def _ensure_restore_queue_available() -> None:
    """Reject a new restore while request, operation, or review state exists.

    Raises
    ------
    HTTPException
        With status 409 for a pending request, active lifecycle operation, or
        valid quarantine, or 503 when quarantine cannot be parsed safely.

    """
    try:
        quarantine = read_outbound_quarantine()
    except OutboundQuarantineStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Restore review state is unreadable. Nothing was queued; inspect it "
                "on the host before running jarvis-research restore acknowledge "
                "<restore-id>."
            ),
        ) from exc
    if quarantine is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Review and acknowledge the previous off-host restore before "
                "requesting another restore."
            ),
        )
    if os.path.lexists(_BACKUP_DIR / ".lifecycle" / "operation.state"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A lifecycle operation is already active. "
                "Wait for it to finish before requesting a restore."
            ),
        )
    if os.path.lexists(_RESTORE_SENTINEL):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A restore is already pending or running. "
                "Wait for it to complete before requesting another."
            ),
        )


def _write_restore_request(payload: dict[str, object]) -> None:
    """Exclusively publish a complete request after its restore session is durable.

    The request is fully written and fsynced under a random private name. A hard
    link then creates the final sentinel only if no directory entry already owns
    that name, preserving an existing request without an overwrite race. Removing
    the temporary link leaves a singly linked final file. The directory is fsynced
    before return; if that post-publication fsync fails, the visible request and
    its token record are retained and the event is logged because reporting failure
    could let the sidecar consume a request whose raw token was never returned.
    The caller holds :func:`_restore_state_lock` throughout.
    """
    _RESTORE_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    tmp = _RESTORE_SENTINEL.parent / f".{_RESTORE_SENTINEL.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(tmp, flags, 0o600)
        try:
            _write_all(fd, json.dumps(payload, separators=(",", ":")).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(tmp, _RESTORE_SENTINEL, follow_symlinks=False)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        tmp.unlink()
    except OSError:
        logger.error("restore request temporary-link cleanup failed", exc_info=True)
    try:
        _fsync_directory(_RESTORE_SENTINEL.parent)
    except OSError:
        logger.error("restore request directory fsync failed after publication", exc_info=True)


def _remove_current_token_record(restore_id: str) -> None:
    """Remove only the matching restore-session record after publication fails.

    The caller still holds the shared lock. Re-reading the strict v2 record and
    comparing its restore ID prevents a losing request from deleting a later
    request's token record. Missing, expired, malformed, or mismatched state is
    retained for fail-closed recovery rather than guessed away.
    """
    token_record = read_restore_status_token_record()
    if token_record is None or token_record.restore_id != restore_id:
        return
    restore_status_token_file().unlink()
    _fsync_directory(restore_status_token_file().parent)


@router.post(
    "/restore",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("3/minute")
async def request_restore(req: RestoreRequest, request: Request) -> dict[str, str]:
    """Schedule a sidecar restore with one bound restore-session token.

    The app gains no new privilege: it only writes a JSON request file into the
    shared trigger volume; the postgres-backup sidecar's ``restore.sh`` performs
    the destructive restore. Only the validated ``timestamp`` selects the archive
    set — a client filename is never accepted, closing path traversal.

    Pending and quarantine state is refused before audit. After audit succeeds,
    the route repeats the check under the shared lock, persists the hashed
    token record, and exclusively publishes the request sentinel. Publication
    failure removes only the matching record; concurrent callers cannot
    replace or delete an existing request.

    Parameters
    ----------
    req : RestoreRequest
        Validated restore target, source, compatibility override, and typed
        confirmation.
    request : Request
        Authenticated admin request used for audit identity and application state.

    Returns
    -------
    dict[str, str]
        Scheduled state, raw restore-session token, restore ID, and source.

    Raises
    ------
    HTTPException
        If confirmation, target validation, audit, recovery-state preflight, or
        pre-publication persistence fails. No new request is queued on those paths.

    """
    if req.confirm != "RESTORE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Type RESTORE to confirm",
        )
    # Source-specific target validation (both raise on a bad target, return on valid).
    # local → the read-only /backups listing; inbox → the sidecar's inbox manifest.
    if req.source == "inbox":
        _validate_inbox_restore(req.timestamp, allow_missing_pdfs=req.allow_missing_pdfs)
    else:
        _validate_local_restore(req.timestamp, allow_missing_pdfs=req.allow_missing_pdfs)
    # Reject stable no-op state before auditing. The same checks run again under
    # the same lock at commit time, because two requests may both pass this
    # preflight while their asynchronous audit writes are in progress.
    try:
        with _restore_state_lock():
            _ensure_restore_queue_available()
    except HTTPException:
        raise
    except OSError as exc:
        logger.error("restore state preflight failed: %r", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Restore recovery state is unavailable; nothing was queued.",
        ) from exc
    # Audit the destructive request BEFORE writing the sentinel: if the audit
    # write fails we 500 without ever queuing a restore (the action stays
    # consistent with what the operator is told), rather than firing a restore
    # the client believes failed.
    await log_audit(
        request.app.state.db_pool,
        action="backup.restore_requested",
        resource=f"backups/{req.timestamp}",
        user_id=_caller_id(request),
    )
    await log_event(
        pool=request.app.state.db_pool,
        level="info",
        category="job",
        source="backups",
        message="Restore requested",
        context={"timestamp": req.timestamp},
    )
    restore_id = secrets.token_hex(16)
    status_token = secrets.token_urlsafe(32)
    requested_at = datetime.now(UTC).isoformat()
    expires_at = datetime.now(UTC) + _RESTORE_TOKEN_TTL
    try:
        with _restore_state_lock():
            _ensure_restore_queue_available()
            if not _write_status_token(
                status_token,
                restore_id=restore_id,
                source=req.source,
                requested_at=requested_at,
                expires_at=expires_at,
            ):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Restore session is unavailable; nothing was queued.",
                )
            try:
                _write_restore_request(
                    {
                        "timestamp": req.timestamp,
                        "confirm": "RESTORE",
                        "source": req.source,
                        "allow_missing_pdfs": req.allow_missing_pdfs,
                        "allow_unknown_schema": req.allow_unknown_schema,
                        "requested_at": requested_at,
                        "restore_id": restore_id,
                    }
                )
            except FileExistsError:
                _remove_current_token_record(restore_id)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "A restore is already pending or running. "
                        "Wait for it to complete before requesting another."
                    ),
                ) from None
            except OSError:
                _remove_current_token_record(restore_id)
                raise
    except HTTPException:
        raise
    except OSError as exc:
        logger.error("restore request sentinel write failed: %r", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Restore sidecar trigger is unavailable. "
                "Ensure the postgres-backup service is running."
            ),
        ) from exc
    return {
        "status": "scheduled",
        "status_token": status_token,
        "restore_id": restore_id,
        "source": req.source,
        "expires_at": expires_at.isoformat(),
    }


async def restore_acknowledgement_auth(
    request: Request,
) -> Literal["token", "owner"]:
    """Authorize restore review by browser token or configured owner.

    An inbox-bound bearer is validated without database access so the initiating
    browser can recover after restored session rows disappear. Without that
    bearer, the request must carry an authenticated session for the configured,
    active administrator. The operations API key and an arbitrary administrator are
    insufficient. The route re-reads and consumes state under the restore-state
    lock.

    Parameters
    ----------
    request : Request
        Current FastAPI request after session middleware and application
        authentication have run.

    Returns
    -------
    Literal["token", "owner"]
        ``"token"`` for the restore-session bearer, otherwise ``"owner"``.

    Raises
    ------
    HTTPException
        If no current restore-session token or configured-owner session exists.

    """
    if restore_acknowledgement_bearer_valid(request):
        return "token"

    user_id = await current_user_id_strict(request)
    pool = getattr(getattr(request.app, "state", None), "db_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configured-owner recovery is temporarily unavailable.",
        )
    try:
        async with pool.acquire() as conn:
            owner = await resolve_owner_identity(conn)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - DB availability is an auth boundary here.
        logger.warning("configured-owner acknowledgement lookup failed", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configured-owner recovery is temporarily unavailable.",
        ) from exc
    if not owner.is_valid or owner.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the configured owner may acknowledge this restore.",
        )
    return "owner"


def _presented_bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" and token else None


def _current_quarantine_for_acknowledgement(
    acknowledgement: RestoreAcknowledgement,
) -> OutboundQuarantineState:
    """Load and bind acknowledgement input to the exact durable quarantine.

    Parameters
    ----------
    acknowledgement : RestoreAcknowledgement
        Exact restore ID and inbox source supplied by the operator.

    Returns
    -------
    OutboundQuarantineState
        Validated current state matching the acknowledgement.

    Raises
    ------
    HTTPException
        With status 409 when quarantine is absent or belongs to another restore,
        or 503 when the existing record is unreadable or malformed.

    """
    try:
        quarantine = read_outbound_quarantine()
    except OutboundQuarantineStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Restore review state is unreadable; quarantine remains active.",
        ) from exc
    if quarantine is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No off-host restore is awaiting acknowledgement.",
        )
    if (
        quarantine.restore_id != acknowledgement.restore_id
        or quarantine.source != acknowledgement.source
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Acknowledgement does not match the current off-host restore.",
        )
    return quarantine


def _remove_token_file() -> None:
    path = restore_status_token_file()
    path.unlink()
    _fsync_directory(path.parent)


def _remove_quarantine_file(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _acknowledge_outbound_quarantine(
    acknowledgement: RestoreAcknowledgement,
    request: Request,
    *,
    authority: Literal["token", "owner"],
) -> None:
    """Validate and consume acknowledgement state before clearing quarantine.

    The shared advisory lock coordinates acknowledgement with restore requests.
    For browser-token authentication, the stored record must match the quarantine
    ID, inbox source, request timestamp, and presented token before it is removed.
    The configured owner may proceed when that record is absent or damaged, but
    must still provide the exact quarantined restore ID.

    The browser token is removed before quarantine. An interruption between
    those writes therefore leaves outbound connections blocked; the configured
    owner or ``jarvis-research restore acknowledge <restore-id>`` can finish the
    acknowledgement.

    Parameters
    ----------
    acknowledgement : RestoreAcknowledgement
        Validated restore ID, inbox source, and typed confirmation phrase.
    request : Request
        Request carrying the raw restore token when ``authority`` is
        ``"token"``.
    authority : Literal["token", "owner"]
        Authentication result returned by :func:`restore_acknowledgement_auth`.

    Raises
    ------
    HTTPException
        If state is missing, mismatched, invalid, replayed, or cannot be durably
        consumed.

    """
    try:
        with _restore_state_lock():
            quarantine = _current_quarantine_for_acknowledgement(acknowledgement)
            token_path = restore_status_token_file()
            if authority == "token":
                token = _presented_bearer_token(request)
                token_record = read_restore_status_token_record()
                if token is None or token_record is None:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Restore session token is invalid or expired.",
                    )
                presented_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                if (
                    token_record.source != "inbox"
                    or token_record.restore_id != quarantine.restore_id
                    or token_record.requested_at != quarantine.requested_at
                    or not secrets.compare_digest(presented_hash, token_record.sha256)
                ):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Restore session token does not match this restore.",
                    )
                _remove_token_file()
            elif os.path.lexists(token_path):
                # The configured owner may proceed when the restore token is
                # expired, consumed, or malformed. Removing the file entry does
                # not follow links; any failure leaves quarantine active.
                _remove_token_file()

            try:
                _remove_quarantine_file(outbound_quarantine_file())
            except OSError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "The restore token was consumed but quarantine remains active. "
                        "Sign in as the configured owner or run jarvis-research "
                        "restore acknowledge <restore-id> on the host."
                    ),
                ) from exc
    except HTTPException:
        raise
    except OSError as exc:
        logger.error("restore acknowledgement state transition failed: %r", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Restore review state is unavailable; quarantine remains active.",
        ) from exc


@router.post("/restore/acknowledge")
@limiter.limit("5/minute")
async def acknowledge_restore(
    acknowledgement: RestoreAcknowledgement,
    request: Request,
    authority: Literal["token", "owner"] = Depends(restore_acknowledgement_auth),
) -> dict[str, str]:
    """Confirm review of restored credentials and allow outbound connections.

    The operator must submit the exact current restore ID and documented phrase.
    A current inbox restore token or the configured-owner session is revalidated
    under the restore-state lock. The token is consumed before quarantine is
    removed, so an interrupted write leaves outbound connections blocked. The
    operations API key and arbitrary admin sessions cannot authorize this route.

    Parameters
    ----------
    acknowledgement : RestoreAcknowledgement
        Exact restore ID, inbox source, and typed credential-review phrase.
    request : fastapi.Request
        Request carrying application state and any raw restore token.
    authority : {"token", "owner"}
        Authentication result from :func:`restore_acknowledgement_auth`.

    Returns
    -------
    dict[str, str]
        Acknowledged status and the exact restore ID whose quarantine was cleared.

    Raises
    ------
    HTTPException
        With status 400 for the wrong phrase, 401 or 403 for failed authentication,
        409 for absent or mismatched quarantine, or 503 when the fail-closed
        filesystem transition cannot complete.

    """
    if acknowledgement.confirm != RESTORE_ACKNOWLEDGEMENT_PHRASE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Type {RESTORE_ACKNOWLEDGEMENT_PHRASE} to confirm",
        )
    _acknowledge_outbound_quarantine(
        acknowledgement,
        request,
        authority=authority,
    )
    try:
        await log_audit(
            request.app.state.db_pool,
            action="backup.restore_acknowledged",
            resource=f"backups/restore/{acknowledgement.restore_id}",
            user_id=_caller_id(request),
            metadata={"authority": authority},
        )
    except Exception:  # noqa: BLE001 - the fail-closed state change already committed.
        logger.warning("restore acknowledgement audit write failed", exc_info=True)
    return {"status": "acknowledged", "restore_id": acknowledgement.restore_id}


@router.post("/upload-grant", dependencies=[Depends(require_admin)])
@limiter.limit("5/minute")
async def create_upload_grant(request: Request) -> dict[str, str | int]:
    """Create a short-lived token for an off-host archive upload.

    The response contains the raw token once. Server storage keeps only its hash
    and a 30-minute expiry. Upload traffic goes directly to the restore uploader,
    so this application does not receive archive bytes or the operator key.

    Parameters
    ----------
    request : fastapi.Request
        Authenticated administrator request and application state.

    Returns
    -------
    dict[str, str | int]
        Grant token and lifetime in seconds.

    Raises
    ------
    HTTPException
        With status 503 when the grant record cannot be written.

    """
    await log_audit(
        request.app.state.db_pool,
        action="backup.upload_grant",
        resource="restore-uploader",
        user_id=_caller_id(request),
    )
    token = secrets.token_urlsafe(32)
    if not _write_upload_grant(token):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload grant is unavailable. Ensure the postgres-backup service is running.",
        )
    return {"grant_token": token, "expires_in_seconds": 1800}


@router.get(
    "/restore/status",
    response_model=RestoreStatus,
    dependencies=[Depends(restore_status_auth)],
)
@limiter.limit("60/minute")
async def restore_status(request: Request) -> RestoreStatus:
    """Return sidecar restore progress without consulting the application database.

    Authentication accepts an admin session, the operations key, or the bound
    restore bearer. Bearer validation does not access the database, so polling
    continues after restored session rows replace the current session store. The response contains
    progress only; archive selection remains restricted to the admin-only restore
    request endpoint.

    A queued request without a status record returns ``pending``. Missing or
    malformed status without a queued request returns ``idle``. Unknown sidecar
    fields are ignored.

    Parameters
    ----------
    request : fastapi.Request
        Authenticated request; retained for FastAPI dependency and rate-limit
        integration without consulting the restored database here.

    Returns
    -------
    RestoreStatus
        Pending, idle, or validated sidecar progress without archive contents.

    """
    # The sidecar consumes the request sentinel before writing any status, so its
    # presence means a restore is queued and any existing status file is stale.
    if _RESTORE_SENTINEL.exists():
        progress = RestoreStatus(state="pending", current_step="Queued")
    else:
        try:
            data = json.loads(_RESTORE_STATUS.read_text())
        except (OSError, ValueError):
            progress = RestoreStatus(state="idle")
        else:
            try:
                progress = RestoreStatus.model_validate(data)
            except ValidationError:
                progress = RestoreStatus(state="idle")

    try:
        quarantine = read_outbound_quarantine()
    except OutboundQuarantineStateError:
        return progress.model_copy(update={"quarantine": "unreadable"})
    if quarantine is None:
        return progress
    return progress.model_copy(
        update={
            "restore_id": quarantine.restore_id,
            "source": "inbox",
            "quarantine": "awaiting_review",
        }
    )


@router.get("/{name}/download", dependencies=[Depends(require_admin)])
@limiter.limit("10/minute")
async def download_backup(name: str, request: Request) -> FileResponse:
    """Stream a single backup archive to an authenticated admin."""
    _validate_name(name)
    try:
        path = secure_path(_BACKUP_DIR, name)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path"
        ) from None
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backup not found")
    await log_audit(
        request.app.state.db_pool,
        action="backup.download",
        resource=f"backups/{name}",
        user_id=_caller_id(request),
    )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=name,
    )


def _restore_in_flight_timestamps() -> set[str]:
    """Timestamps a present restore is using (its target + its safety backup).

    Mirrors ``prune.sh``'s ``restore_in_flight_ts`` (defense in depth): a delete
    must never pull a running restore's source archive — or the safety backup it
    just took — out from under it. Reads both sentinels defensively; a missing or
    malformed file degrades to "no in-flight timestamps" and never raises. A bare
    sentinel *existence* does not block deleting an unrelated point — only a
    timestamp match does.
    """
    in_flight: set[str] = set()
    for path, key in ((_RESTORE_SENTINEL, "timestamp"), (_RESTORE_STATUS, "safety_backup_ts")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            value = data.get(key)
            if isinstance(value, str):
                in_flight.add(value)
    return in_flight


@router.post(
    "/restore-points/{timestamp}/delete",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("3/minute")
async def request_delete_restore_point(
    timestamp: str, req: DeleteRequest, request: Request
) -> dict[str, str]:
    """Schedule deletion of a restore point by writing a sentinel the sidecar consumes.

    The app gains no new privilege: it only writes a JSON request file into the
    shared trigger volume; the postgres-backup sidecar's ``prune.sh`` performs the
    actual ``rm`` (the ``/backups`` mount stays read-only here). Only a validated
    timestamp is accepted — a client filename is never trusted — and a point a
    running restore is using (its target or its safety backup) is refused so a
    delete can never undermine a live restore.
    """
    if req.confirm != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Type DELETE to confirm",
        )
    pt = next(
        (p for p in _group_restore_points(_list_entries()) if p.timestamp == timestamp),
        None,
    )
    if pt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No backup at that time",
        )
    if timestamp in _restore_in_flight_timestamps():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That restore point is in use by a running restore",
        )
    # Reject a duplicate request BEFORE auditing: an already-pending sentinel means
    # a delete is queued; no audit row should be produced for a no-op.
    if _DELETE_SENTINEL.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A delete is already pending. Wait for it to complete before requesting another."
            ),
        )
    # Audit the destructive request BEFORE writing the sentinel: a failed audit
    # 500s without ever queuing a delete, keeping the record consistent with what
    # the operator is told.
    await log_audit(
        request.app.state.db_pool,
        action="backup.delete_requested",
        resource=f"backups/{timestamp}",
        user_id=_caller_id(request),
    )
    await log_event(
        pool=request.app.state.db_pool,
        level="info",
        category="job",
        source="backups",
        message="Restore point deletion requested",
        context={"timestamp": timestamp},
    )
    try:
        _DELETE_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        # Atomic exclusive create (O_EXCL) closes the TOCTOU race where two
        # concurrent requests both pass the exists() check above.
        with _DELETE_SENTINEL.open("x") as fh:
            fh.write(
                json.dumps(
                    {
                        "timestamps": [timestamp],
                        "confirm": "DELETE",
                        "requested_at": datetime.now(UTC).isoformat(),
                        "version": 1,
                    }
                )
            )
    except FileExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A delete is already pending. Wait for it to complete before requesting another."
            ),
        )
    except OSError as exc:
        logger.error("delete request sentinel write failed: %r", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Backup sidecar trigger is unavailable. "
                "Ensure the postgres-backup service is running."
            ),
        ) from exc
    return {"status": "scheduled"}


@router.get("/delete-status", response_model=DeleteOutcome, dependencies=[Depends(require_admin)])
@limiter.limit("30/minute")
async def delete_status(request: Request) -> DeleteOutcome:
    """Report what the sidecar's last prune run deleted, skipped, and kept.

    Admin-gated like the backup listing: which restore points were destroyed is
    inventory, not restore progress, so the restore status bearer cannot read it.

    A missing, unreadable, or malformed outcome file reports
    ``no_deletions_recorded`` rather than failing — no prune has recorded an
    outcome on a fresh install.
    """
    try:
        data = json.loads(_LAST_DELETE.read_text())
    except (OSError, ValueError):
        return DeleteOutcome(state="no_deletions_recorded")
    if not isinstance(data, dict):
        return DeleteOutcome(state="no_deletions_recorded")
    try:
        return DeleteOutcome.model_validate({**data, "state": "recorded"})
    except ValidationError:
        return DeleteOutcome(state="no_deletions_recorded")


@router.get("/retention", response_model=RetentionConfig, dependencies=[Depends(require_admin)])
@limiter.limit("30/minute")
async def get_retention(request: Request) -> RetentionConfig:
    """Return the backup retention policy the sidecar reads (defaults when unset).

    A missing, unreadable, or malformed ``.retention.json`` degrades to the
    all-null default (both dimensions fall back to the sidecar env default) — it
    never 500s.
    """
    try:
        data = json.loads(_RETENTION_CONFIG.read_text())
        return RetentionConfig.model_validate(data)
    except (OSError, ValueError, ValidationError):
        return RetentionConfig()


@router.put("/retention", response_model=RetentionConfig, dependencies=[Depends(require_admin)])
@limiter.limit("10/minute")
async def put_retention(config: RetentionConfig, request: Request) -> RetentionConfig:
    """Persist the retention policy to the trigger volume the backup sidecar reads.

    Written to a file (not the DB ``user_config``) because the bash sidecar reads
    it directly and cannot query the database; the app already owns this RW trigger
    volume and gains no new privilege.
    """
    try:
        _RETENTION_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        _RETENTION_CONFIG.write_text(
            json.dumps({"keep_last_n": config.keep_last_n, "max_age_days": config.max_age_days})
        )
    except OSError as exc:
        logger.error("retention config write failed: %r", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Backup sidecar trigger is unavailable. "
                "Ensure the postgres-backup service is running."
            ),
        ) from exc
    await log_audit(
        request.app.state.db_pool,
        action="backup.retention_updated",
        resource="backups/retention",
        user_id=_caller_id(request),
    )
    await log_event(
        pool=request.app.state.db_pool,
        level="info",
        category="config",
        source="backups",
        message="Retention policy updated",
        context={},
    )
    return config


def _caller_id(request: Request) -> str | None:
    caller_id = getattr(request.state, "user_id", None)
    return str(caller_id) if caller_id is not None else None
