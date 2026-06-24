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

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from jarvis_common.audit import log_audit
from jarvis_common.auth import require_admin
from pydantic import BaseModel

from paper_ingestion.deps import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/backups", tags=["admin", "backups"])
# Session-only admin auth — exempt from the global verify_api_key dep.
router.auth_exempt = True  # type: ignore[attr-defined]

# Directory the postgres_backups volume is mounted at (read-only) in this service.
_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
# Sentinel flag-file in a small RW volume the sidecar loop polls each iteration.
_TRIGGER_SENTINEL = Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".backup_now"

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


class RestoreLastRun(BaseModel):
    attempted_at: datetime | None
    succeeded: bool | None
    stores: dict[str, str]


class RestorePointsResponse(BaseModel):
    restore_points: list[RestorePoint]
    retention_days: int | None
    last_run: RestoreLastRun | None


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


def _group_restore_points(entries: list[BackupEntry]) -> list[RestorePoint]:
    """Group archive entries by their %Y%m%d_%H%M%S timestamp into restore points."""
    groups: dict[str, list[BackupEntry]] = {}
    for e in entries:
        m = _TS_RE.search(e.filename)
        if m:
            groups.setdefault(m.group(1), []).append(e)
    points: list[RestorePoint] = []
    for ts, members in groups.items():
        stores = sorted({m.store for m in members})
        collections = sorted(qm.group(1) for m in members if (qm := _QDRANT_RE.match(m.filename)))
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
            )
        )
    points.sort(key=lambda p: p.created_at, reverse=True)
    return points


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
    return {"status": "scheduled"}


@router.get("/{name}/download", dependencies=[Depends(require_admin)])
@limiter.limit("10/minute")
async def download_backup(name: str, request: Request) -> FileResponse:
    """Stream a single backup archive to an authenticated admin."""
    _validate_name(name)
    path = _BACKUP_DIR / name
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


def _caller_id(request: Request) -> str | None:
    caller_id = getattr(request.state, "user_id", None)
    return str(caller_id) if caller_id is not None else None
