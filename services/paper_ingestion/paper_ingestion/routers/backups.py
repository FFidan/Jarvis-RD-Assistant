"""Admin Backup panel — list / status / download / on-demand trigger.

The disaster-recovery archives produced by ``scripts/backup.sh`` (in the
``postgres-backup`` sidecar) land in the ``postgres_backups`` volume, mounted
read-only into this service at ``/backups``. These archives contain ALL
platform secrets (the ``secrets_*.tar.gz`` is the full Docker-secret set,
plaintext when no backup key is configured), so every route here requires an
**admin browser session** (``Depends(require_admin)``) — never the ops
X-API-Key, which must not reach secret-bearing archives.

The app container (python:3.12-slim) cannot run ``pg_dump``/``backup.sh`` and
has no docker.sock, so the on-demand trigger writes a sentinel flag-file into a
small RW volume shared with the sidecar; the sidecar loop runs a backup
immediately when it sees the flag, then removes it.

Registered in main.py with ``dependencies=[]`` + ``router.auth_exempt=True``
(same exemption shape as admin.py) so a browser session need not send X-API-Key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from jarvis_common.audit import log_audit
from jarvis_common.auth import (
    require_admin,
    require_admin_or_api_key,
    restore_status_bearer_valid,
    restore_status_token_file,
    verify_api_key,
)
from jarvis_common.event_log import log_event
from jarvis_common.migrations import required_code_schema
from jarvis_common.paths import secure_path
from pydantic import BaseModel, Field, ValidationError

from paper_ingestion.deps import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/backups", tags=["admin", "backups"])
# Session-only admin auth — exempt from the global verify_api_key dep.
router.auth_exempt = True  # type: ignore[attr-defined]

# Directory the postgres_backups volume is mounted at (read-only) in this service.
_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
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
# Off-host (inbox) restore inventory: the postgres-backup sidecar's
# ``restore.sh --inbox-manifest`` writes this sanitized listing (names + booleans
# only, never paths or key contents) each loop iteration. The app READS it from the
# already-mounted backup_trigger volume — it never mounts /restore-inbox and gains
# no new privilege.
_INBOX_MANIFEST = (
    Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".inbox_manifest.json"
)

# Strict allowlist for the four archive shapes scripts/backup.sh emits:
#   jarvis_<ts>.sql.gz[.enc] · litellm_<ts>.sql.gz[.enc]
#   secrets_<ts>.tar.gz[.enc] · qdrant_<collection>_<ts>.snapshot[.enc]
# <ts> = %Y%m%d_%H%M%S (backup.sh). The regex pins the whole string and
# permits no path separators or '..', blocking traversal to /run/secrets/*.
_TS = r"\d{8}_\d{6}"
_FILENAME_RE = re.compile(
    rf"^(?:jarvis_{_TS}\.sql\.gz(?:\.enc)?"
    rf"|litellm_{_TS}\.sql\.gz(?:\.enc)?"
    rf"|secrets_{_TS}\.tar\.gz(?:\.enc)?"
    rf"|qdrant_[A-Za-z0-9_-]+_{_TS}\.snapshot(?:\.enc)?)$"
)
# Globs used to enumerate the directory (mirror the four shapes; '*' here is a
# filesystem glob, NOT regex — every match is re-validated by _FILENAME_RE).
_ARCHIVE_GLOBS = (
    "jarvis_*.sql.gz",
    "jarvis_*.sql.gz.enc",
    "litellm_*.sql.gz",
    "litellm_*.sql.gz.enc",
    "secrets_*.tar.gz",
    "secrets_*.tar.gz.enc",
    "qdrant_*.snapshot",
    "qdrant_*.snapshot.enc",
)
# Parses the %Y%m%d_%H%M%S timestamp group key out of any allowlisted filename,
# and (for qdrant) the collection name between `qdrant_` and `_<ts>`.
_TS_RE = re.compile(rf"_({_TS})\.")
_QDRANT_RE = re.compile(rf"^qdrant_([A-Za-z0-9_-]+)_{_TS}\.snapshot(?:\.enc)?$")


class BackupEntry(BaseModel):
    filename: str
    store: str  # jarvis | litellm | secrets | qdrant
    size_bytes: int
    modified_at: datetime
    encrypted: bool


class BackupStatus(BaseModel):
    backup_dir_available: bool
    archive_count: int
    last_run_at: datetime | None  # newest-archive mtime (last *success* proxy)
    last_attempt_at: datetime | None  # from .last_run.json; last run that was attempted
    last_run_succeeded: bool | None  # from .last_run.json; None when unknown
    trigger_pending: bool


class RestorePointFile(BaseModel):
    filename: str
    store: str
    size_bytes: int
    encrypted: bool


class RestorePoint(BaseModel):
    timestamp: str
    created_at: datetime
    stores: list[str]
    qdrant_collections: list[str]
    complete: bool
    encrypted: bool
    total_size_bytes: int
    files: list[RestorePointFile]
    app_version: str | None = None
    schema_version: int | None = None
    compat: Literal["same", "older", "newer", "unknown"] = "unknown"


class RestoreLastRun(BaseModel):
    attempted_at: datetime | None
    succeeded: bool | None
    stores: dict[str, str]


class RestorePointsResponse(BaseModel):
    restore_points: list[RestorePoint]
    retention_days: int | None
    last_run: RestoreLastRun | None


class RestoreStep(BaseModel):
    name: str
    status: str


class RestoreRequest(BaseModel):
    timestamp: str
    confirm: str
    # "local" (default) restores from the read-only /backups mount; "inbox" restores
    # an operator-staged archive set from the sidecar's restore_inbox (off-host DR).
    # The default keeps every existing caller/test valid.
    source: Literal["local", "inbox"] = "local"


class InboxRestorePoint(BaseModel):
    """One off-host restore point staged in the restore_inbox, per the sidecar manifest.

    Names + booleans only — no paths, no key contents. ``complete`` mirrors
    restore.sh's own completeness gate (jarvis + litellm DB archives present);
    ``has_secrets`` flags a bundled ``secrets_<ts>`` archive; ``has_key`` flags the
    one-time operator key the off-host restore requires.
    """

    timestamp: str
    complete: bool
    has_secrets: bool
    has_key: bool


class RestoreStatus(BaseModel):
    state: str
    current_step: str | None = None
    steps: list[RestoreStep] = []
    safety_backup_ts: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    manual_steps_required: bool = False
    phase: str | None = None


class DeleteRequest(BaseModel):
    confirm: str


class RetentionConfig(BaseModel):
    """Backup retention policy the sidecar reads from ``.retention.json``.

    ``keep_last_n`` caps the number of restore points kept; ``max_age_days``
    overrides the age window. Either may be null (that dimension falls back to the
    sidecar's env default). Non-negative ints only.
    """

    keep_last_n: int | None = Field(default=None, ge=0)
    max_age_days: int | None = Field(default=None, ge=0)


def _classify(name: str) -> str:
    """Map a validated archive filename to its store type."""
    if name.startswith("jarvis_"):
        return "jarvis"
    if name.startswith("litellm_"):
        return "litellm"
    if name.startswith("secrets_"):
        return "secrets"
    return "qdrant"


def _validate_name(name: str) -> None:
    """Reject anything that is not one of the four known archive shapes.

    Pins the whole string and forbids path separators / '..', so a caller can
    never escape _BACKUP_DIR (e.g. into /run/secrets/*).
    """
    if "/" in name or "\\" in name or ".." in name or not _FILENAME_RE.match(name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid backup name")


def _list_entries() -> list[BackupEntry]:
    if not _BACKUP_DIR.is_dir():
        return []
    seen: dict[str, Path] = {}
    for pattern in _ARCHIVE_GLOBS:
        for p in _BACKUP_DIR.glob(pattern):
            if p.is_file() and _FILENAME_RE.match(p.name):
                seen[p.name] = p
    entries: list[BackupEntry] = []
    for name, p in seen.items():
        st = p.stat()
        entries.append(
            BackupEntry(
                filename=name,
                store=_classify(name),
                size_bytes=st.st_size,
                modified_at=datetime.fromtimestamp(st.st_mtime, tz=UTC),
                encrypted=name.endswith(".enc"),
            )
        )
    entries.sort(key=lambda e: e.modified_at, reverse=True)
    return entries


def _read_last_run() -> dict | None:
    """Read the sidecar's .last_run.json outcome record; None if absent/unreadable.

    The sidecar writes this on every run (even a FATAL one) so a failed attempt is
    visible — distinct from "no recent archive". Never raises: a missing or
    malformed file degrades to None rather than breaking /status or /restore-points.
    """
    try:
        return json.loads((_BACKUP_DIR / ".last_run.json").read_text())
    except (OSError, ValueError):
        return None


def _read_manifest(ts: str) -> dict | None:
    """Read manifest_<ts>.json metadata; None if absent/unreadable/malformed.

    Mirrors ``_read_last_run``'s degrade-to-None contract so a missing or corrupt
    manifest never breaks restore-point listing. The manifest is plaintext
    metadata (filenames, sha256 hex, version ints) written by ``backup.sh``.
    """
    try:
        manifest_path = secure_path(_BACKUP_DIR, f"manifest_{ts}.json")
        return json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None


def _read_inbox_manifest() -> list[InboxRestorePoint]:
    """Read the sidecar-authored .inbox_manifest.json; [] if absent/unreadable/malformed.

    Mirrors ``_read_manifest``'s degrade-to-safe contract. Each entry is re-validated
    through ``InboxRestorePoint`` so a corrupt/tampered manifest can never inject
    arbitrary fields — a bad entry is dropped, never surfaced. The manifest is written
    by ``restore.sh --inbox-manifest`` in the postgres-backup sidecar; the app only
    reads it (it never mounts /restore-inbox).
    """
    try:
        data = json.loads(_INBOX_MANIFEST.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    points: list[InboxRestorePoint] = []
    for item in data:
        try:
            points.append(InboxRestorePoint.model_validate(item))
        except ValidationError:
            continue
    return points


def _code_max_migration() -> int | None:
    """Highest schema version this build can load.

    Returns the max numbered migration in the mounted db/migrations directory,
    or — when that directory is empty/absent (the baseline lives in init.sql
    after a schema squash) — the baseline floor from db/SCHEMA_VERSION, so
    restore-point compat stays armed instead of degrading to "unknown". Only a
    genuine I/O/parse error on a present file degrades to None ("unknown").
    """
    migrations_dir = Path(os.environ.get("DB_MIGRATIONS_DIR", "/app/db/migrations"))
    try:
        versions = [int(p.name.split("_")[0]) for p in migrations_dir.glob("*.sql")]
    except (OSError, ValueError):
        return None
    return max(versions) if versions else required_code_schema()


def _compute_compat(
    schema_version: int | None, code_max: int | None
) -> Literal["same", "older", "newer", "unknown"]:
    """Coarse schema-version relation between a restore point and the running code."""
    if code_max is None or schema_version is None:
        return "unknown"
    if schema_version == code_max:
        return "same"
    return "older" if schema_version < code_max else "newer"


def _manifest_compat(
    ts: str, member_filenames: set[str], code_max: int | None
) -> tuple[str | None, int | None, Literal["same", "older", "newer", "unknown"]]:
    """Derive (app_version, schema_version, compat) from manifest_<ts>.json.

    Yields nulls + "unknown" unless the manifest is present, well-formed, and its
    archive filename set is a subset of the point's actual files (rejecting a
    phantom/incomplete manifest whose archives no longer exist on disk). Never
    raises — a malformed manifest degrades to "unknown".
    """
    manifest = _read_manifest(ts)
    if not isinstance(manifest, dict):
        return None, None, "unknown"
    archives = manifest.get("archives")
    if not isinstance(archives, list):
        return None, None, "unknown"
    manifest_names = {a.get("filename") for a in archives if isinstance(a, dict)}
    if not manifest_names or not manifest_names <= member_filenames:
        return None, None, "unknown"
    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        schema_version = None
    app_version = manifest.get("app_version")
    if not isinstance(app_version, str):
        app_version = None
    return app_version, schema_version, _compute_compat(schema_version, code_max)


def _group_restore_points(entries: list[BackupEntry]) -> list[RestorePoint]:
    """Group archive entries by their %Y%m%d_%H%M%S timestamp into restore points.

    Each point is annotated with the app/schema version recorded in its
    ``manifest_<ts>.json`` (when present, well-formed, and consistent with the
    point's actual files) plus a coarse ``compat`` vs the running code's migration
    set. A missing/malformed/incomplete manifest degrades to ``compat="unknown"``
    with null versions — it never raises.
    """
    groups: dict[str, list[BackupEntry]] = {}
    for e in entries:
        m = _TS_RE.search(e.filename)
        if m:
            groups.setdefault(m.group(1), []).append(e)
    code_max = _code_max_migration()
    points: list[RestorePoint] = []
    for ts, members in groups.items():
        stores = sorted({m.store for m in members})
        collections = sorted(qm.group(1) for m in members if (qm := _QDRANT_RE.match(m.filename)))
        app_version, schema_version, compat = _manifest_compat(
            ts, {m.filename for m in members}, code_max
        )
        points.append(
            RestorePoint(
                timestamp=ts,
                created_at=max(m.modified_at for m in members),
                stores=stores,
                qdrant_collections=collections,
                complete={"jarvis", "litellm", "secrets"}.issubset(stores),
                encrypted=all(m.encrypted for m in members),
                total_size_bytes=sum(m.size_bytes for m in members),
                files=[
                    RestorePointFile(
                        filename=m.filename,
                        store=m.store,
                        size_bytes=m.size_bytes,
                        encrypted=m.encrypted,
                    )
                    for m in members
                ],
                app_version=app_version,
                schema_version=schema_version,
                compat=compat,
            )
        )
    points.sort(key=lambda p: p.created_at, reverse=True)
    return points


def _write_status_token(token: str) -> bool:
    """Persist ONLY the sha256 + 2h expiry of a one-time restore-status token.

    The raw token is returned to the caller ONCE and never stored: ``restore_status``
    authorizes a presented token by hashing it and matching this file, DB-free (see
    ``jarvis_common.auth.restore_status_bearer_valid``). It lives in its OWN sentinel
    file — not ``.restore_request.json``, which ``restore.sh`` consumes before any
    status is written. Atomic tmp->replace at mode 0600; a new request overwrites it.
    Best-effort: an I/O failure logs and returns False (the restore is already queued;
    the poll simply falls back to the session/API-key gate).
    """
    path = restore_status_token_file()
    payload = {
        "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    }
    tmp = path.parent / f"{path.name}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("restore status-token write failed: %r", exc)
        tmp.unlink(missing_ok=True)
        return False
    return True


async def restore_status_auth(request: Request) -> None:
    """Authorize the restore-status poll: DB-free bearer token, or the existing gate.

    Three accepted credentials: (a) a valid one-time bearer token minted by
    ``request_restore`` — authorized with ZERO DB access, the only credential that
    survives after an in-flight restore has torn down the admin's session (the same
    token is validated at the global front door in ``jarvis_common.auth``); otherwise
    (b) the ops X-API-Key or (c) an admin browser session, via the existing
    ``verify_api_key`` + ``require_admin_or_api_key`` gate, unchanged.
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
        last_run_succeeded=run.get("succeeded") if run else None,
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


def _validate_local_restore(timestamp: str) -> None:
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


def _validate_inbox_restore(timestamp: str) -> None:
    """Validate an INBOX (off-host) restore target against the sidecar's inbox manifest.

    The point must be present in the manifest and complete (jarvis + litellm archives),
    and the one-time operator key must be staged. The local group/compat/manifest checks
    do not apply — restore.sh STEP 2 compat-gates the off-host archive itself before any
    destruction. Raises 404 (absent/incomplete) or 409 (no key); returns None when valid.
    """
    pt = next((p for p in _read_inbox_manifest() if p.timestamp == timestamp), None)
    if pt is None or not pt.complete:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No complete backup at that time",
        )
    if not pt.has_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload or drop the one-time operator key before restoring from the inbox.",
        )


@router.post(
    "/restore",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("3/minute")
async def request_restore(req: RestoreRequest, request: Request) -> dict[str, str]:
    """Schedule a one-click restore by writing a sentinel the sidecar consumes.

    The app gains no new privilege: it only writes a JSON request file into the
    shared trigger volume; the postgres-backup sidecar's ``restore.sh`` performs
    the destructive restore. Only the validated ``timestamp`` selects the archive
    set — a client filename is never accepted, closing path traversal.
    """
    if req.confirm != "RESTORE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Type RESTORE to confirm",
        )
    # Source-specific target validation (both raise on a bad target, return on valid).
    # local → the read-only /backups listing; inbox → the sidecar's inbox manifest.
    if req.source == "inbox":
        _validate_inbox_restore(req.timestamp)
    else:
        _validate_local_restore(req.timestamp)
    # Reject a duplicate request BEFORE auditing: an already-pending sentinel means
    # a restore is queued or running; no audit row should be produced for a no-op.
    if _RESTORE_SENTINEL.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A restore is already pending or running. "
                "Wait for it to complete before requesting another."
            ),
        )
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
    try:
        _RESTORE_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        # Atomic exclusive create: O_EXCL catches the TOCTOU race where two
        # concurrent requests both pass the exists() check above. write_text
        # would silently overwrite in that window.
        with _RESTORE_SENTINEL.open("x") as fh:
            fh.write(
                json.dumps(
                    {
                        "timestamp": req.timestamp,
                        "confirm": "RESTORE",
                        "source": req.source,
                        "requested_at": datetime.now(UTC).isoformat(),
                    }
                )
            )
    except FileExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A restore is already pending or running. "
                "Wait for it to complete before requesting another."
            ),
        )
    except OSError as exc:
        logger.error("restore request sentinel write failed: %r", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Restore sidecar trigger is unavailable. "
                "Ensure the postgres-backup service is running."
            ),
        ) from exc
    # Mint a one-time status token AFTER the request sentinel is committed: the
    # initiating admin's browser session dies when the restore rewrites the sessions
    # table, so this token is what keeps their progress poll (GET .../restore/status)
    # authorized DB-free. Only its hash + expiry are persisted; the raw token is
    # handed back ONCE here and never stored or logged.
    status_token = secrets.token_urlsafe(32)
    if _write_status_token(status_token):
        return {"status": "scheduled", "status_token": status_token}
    return {"status": "scheduled"}


@router.get(
    "/restore/status",
    response_model=RestoreStatus,
    dependencies=[Depends(restore_status_auth)],
)
@limiter.limit("60/minute")
async def restore_status(request: Request) -> RestoreStatus:
    """Report live restore progress from the sidecar's status file (NO DB write).

    Polled every few seconds AND must keep answering during the brief window in
    a restore where ``restore.sh`` drops/recreates the jarvis DB — exactly when a
    session lookup against that DB fails. So this route is gated by
    ``restore_status_auth``: an admin session, the ops X-API-Key, OR the one-time
    bearer token minted by ``request_restore`` — the last validated DB-free
    (``restore_status_bearer_valid``) so the initiating admin's poll survives even
    after the restore has dropped the session store. It exposes only progress (step
    names + state), never archive contents, so the wider gate is safe here; the
    destructive ``POST /restore`` stays session-admin-only.

    When a restore is queued but the sidecar (a few-second poll loop) has not yet
    written the first status, the request sentinel still exists — report
    ``state="pending"`` so the UI keeps tracking instead of treating the gap (or a
    leftover status file from a prior run) as "nothing running". A missing or
    malformed status file with no pending request degrades to ``state="idle"`` —
    it never 500s. The sidecar's extra ``drop_started`` key is ignored.
    """
    # The sidecar consumes the request sentinel before writing any status, so its
    # presence means a restore is queued and any existing status file is stale.
    if _RESTORE_SENTINEL.exists():
        return RestoreStatus(state="pending", current_step="Queued")
    try:
        data = json.loads(_RESTORE_STATUS.read_text())
    except (OSError, ValueError):
        return RestoreStatus(state="idle")
    try:
        return RestoreStatus.model_validate(data)
    except ValidationError:
        return RestoreStatus(state="idle")


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
