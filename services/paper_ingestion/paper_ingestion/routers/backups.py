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
#   secrets_<ts>.tar.gz[.enc] · qdrant_<collection>_<ts>.snapshot
# <ts> = %Y%m%d_%H%M%S (backup.sh:60). The regex pins the whole string and
# permits no path separators or '..', blocking traversal to /run/secrets/*.
_TS = r"\d{8}_\d{6}"
_FILENAME_RE = re.compile(
    rf"^(?:jarvis_{_TS}\.sql\.gz(?:\.enc)?"
    rf"|litellm_{_TS}\.sql\.gz(?:\.enc)?"
    rf"|secrets_{_TS}\.tar\.gz(?:\.enc)?"
    rf"|qdrant_[A-Za-z0-9_-]+_{_TS}\.snapshot)$"
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
)


class BackupEntry(BaseModel):
    filename: str
    store: str  # jarvis | litellm | secrets | qdrant
    size_bytes: int
    modified_at: datetime
    encrypted: bool


class BackupStatus(BaseModel):
    backup_dir_available: bool
    archive_count: int
    last_run_at: datetime | None
    trigger_pending: bool


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
    """Report sidecar reachability + the inferred last-run time (newest archive)."""
    entries = _list_entries()
    last = entries[0].modified_at if entries else None
    return BackupStatus(
        backup_dir_available=_BACKUP_DIR.is_dir(),
        archive_count=len(entries),
        last_run_at=last,
        trigger_pending=_TRIGGER_SENTINEL.exists(),
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
