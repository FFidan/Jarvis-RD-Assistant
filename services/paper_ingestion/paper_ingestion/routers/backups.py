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

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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
from jarvis_common.migrations import required_code_schema
from jarvis_common.owner import resolve_owner_identity
from jarvis_common.paths import secure_path
from pydantic import BaseModel, Field, ValidationError

from paper_ingestion.deps import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/backups", tags=["admin", "backups"])

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
# The backup sidecar writes an inbox inventory containing names and booleans,
# without paths or key contents. The application reads it from the existing
# trigger volume and does not mount the restore inbox.
_INBOX_MANIFEST = (
    Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".inbox_manifest.json"
)
# The application writes a short-lived upload grant for the restore uploader,
# which reads it through the trigger volume. Archive bytes and the operator key
# go directly to the uploader.
_UPLOAD_GRANT = Path(os.environ.get("BACKUP_TRIGGER_DIR", "/backup-trigger")) / ".upload_grant.json"
_RESTORE_STATE_LOCK_FILENAME = ".restore_state.lock"

RESTORE_ACKNOWLEDGEMENT_PHRASE = "I HAVE REVIEWED RESTORED CREDENTIALS"
_RESTORE_TOKEN_TTL = timedelta(hours=2)

# Strict allowlist for the five archive shapes scripts/backup.sh emits:
#   jarvis_<ts>.sql.gz[.enc] · litellm_<ts>.sql.gz[.enc]
#   pdfs_<ts>.tar.gz[.enc] · secrets_<ts>.tar.gz[.enc]
#   qdrant_<collection>_<ts>.snapshot[.enc]
# <ts> = %Y%m%d_%H%M%S (backup.sh). The regex pins the whole string and
# permits no path separators or '..', blocking traversal to /run/secrets/*.
_TS = r"\d{8}_\d{6}"
_FILENAME_RE = re.compile(
    rf"^(?:jarvis_{_TS}\.sql\.gz(?:\.enc)?"
    rf"|litellm_{_TS}\.sql\.gz(?:\.enc)?"
    rf"|pdfs_{_TS}\.tar\.gz(?:\.enc)?"
    rf"|secrets_{_TS}\.tar\.gz(?:\.enc)?"
    rf"|qdrant_[A-Za-z0-9_-]+_{_TS}\.snapshot(?:\.enc)?)$"
)
# Globs used to enumerate the directory (mirror the five shapes; '*' here is a
# filesystem glob, NOT regex — every match is re-validated by _FILENAME_RE).
_ARCHIVE_GLOBS = (
    "jarvis_*.sql.gz",
    "jarvis_*.sql.gz.enc",
    "litellm_*.sql.gz",
    "litellm_*.sql.gz.enc",
    "pdfs_*.tar.gz",
    "pdfs_*.tar.gz.enc",
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
    """One downloadable archive in the local backup store.

    Attributes
    ----------
    filename : str
        Validated archive basename.
    store : str
        Logical store represented by the archive.
    size_bytes : int
        Archive size in bytes.
    modified_at : datetime
        Filesystem modification time.
    encrypted : bool
        Whether the archive uses the encrypted filename form.

    """

    filename: str
    store: str  # jarvis | litellm | pdfs | secrets | qdrant
    size_bytes: int
    modified_at: datetime
    encrypted: bool


class BackupStatus(BaseModel):
    """Summary of local backup availability and trigger state.

    Attributes
    ----------
    backup_dir_available : bool
        Whether the mounted backup directory can be read.
    archive_count : int
        Number of validated archives currently present.
    last_run_at : datetime or None
        Modification time of the newest archive, used as a success proxy.
    last_attempt_at : datetime or None
        Timestamp recorded by the most recent backup attempt.
    last_run_succeeded : bool or None
        Recorded outcome of that attempt, or ``None`` when unknown.
    trigger_pending : bool
        Whether an on-demand backup request awaits the sidecar.

    """

    backup_dir_available: bool
    archive_count: int
    last_run_at: datetime | None  # newest-archive mtime (last *success* proxy)
    last_attempt_at: datetime | None  # from .last_run.json; last run that was attempted
    last_run_succeeded: bool | None  # from .last_run.json; None when unknown
    trigger_pending: bool


class RestorePointFile(BaseModel):
    """One validated archive belonging to a restore point.

    Attributes
    ----------
    filename : str
        Validated archive basename.
    store : str
        Logical store represented by the archive.
    size_bytes : int
        Archive size in bytes.
    encrypted : bool
        Whether the archive is encrypted at rest.

    """

    filename: str
    store: str
    size_bytes: int
    encrypted: bool


class RestorePoint(BaseModel):
    """Restore-set metadata derived from archives sharing one timestamp.

    Attributes
    ----------
    timestamp : str
        Backup-set timestamp used to bind every archive in the point.
    created_at : datetime
        Creation time derived from the backup timestamp.
    stores : list[str]
        Logical stores present in the set.
    qdrant_collections : list[str]
        Qdrant collection names represented by snapshots.
    complete : bool
        Whether the set satisfies the current restore completeness rules.
    has_pdfs : bool
        Whether a PDF archive is present.
    legacy_missing_pdfs : bool
        Whether a signed older set is eligible for explicit PDF-loss consent.
    encrypted : bool
        Whether every security-sensitive archive is encrypted.
    total_size_bytes : int
        Combined size of the point's archives.
    files : list[RestorePointFile]
        Validated archives that comprise the point.
    app_version : str or None
        Application version recorded by the manifest, when available.
    schema_version : int or None
        Database schema version recorded by the manifest, when available.
    compat : {"same", "older", "newer", "unknown"}
        Compatibility classification against the running code.

    """

    timestamp: str
    created_at: datetime
    stores: list[str]
    qdrant_collections: list[str]
    complete: bool
    has_pdfs: bool
    legacy_missing_pdfs: bool
    encrypted: bool
    total_size_bytes: int
    files: list[RestorePointFile]
    app_version: str | None = None
    schema_version: int | None = None
    compat: Literal["same", "older", "newer", "unknown"] = "unknown"


class RestoreLastRun(BaseModel):
    """Sanitized outcome of the most recent restore attempt.

    Attributes
    ----------
    attempted_at : datetime or None
        Recorded start time, when available.
    succeeded : bool or None
        Restore outcome, or ``None`` when no trustworthy result exists.
    stores : dict[str, str]
        Per-store status labels without archive content or credentials.

    """

    attempted_at: datetime | None
    succeeded: bool | None
    stores: dict[str, str]


class RestorePointsResponse(BaseModel):
    """Restore inventory and retention metadata returned to an administrator.

    Attributes
    ----------
    restore_points : list[RestorePoint]
        Validated local restore sets.
    retention_days : int or None
        Effective age-retention window, when configured.
    last_run : RestoreLastRun or None
        Sanitized result of the latest restore attempt.

    """

    restore_points: list[RestorePoint]
    retention_days: int | None
    last_run: RestoreLastRun | None


class RestoreStep(BaseModel):
    """Named step and state reported by the restore sidecar.

    Attributes
    ----------
    name : str
        Human-readable step label.
    status : str
        Current sidecar status for the step.

    """

    name: str
    status: str


class RestoreRequest(BaseModel):
    """Validated request to restore one local or staged backup set.

    Attributes
    ----------
    timestamp : str
        Exact restore-point timestamp selected by the operator.
    confirm : str
        Typed destructive-action confirmation phrase.
    source : {"local", "inbox"}
        Read-only local archive store or staged off-host inbox.
    allow_missing_pdfs : bool
        Explicit consent for eligible legacy sets without saved PDFs.

    """

    timestamp: str
    confirm: str
    # "local" (default) restores from the read-only /backups mount; "inbox" restores
    # an operator-staged archive set from the sidecar's restore_inbox (off-host DR).
    # The default keeps every existing caller/test valid.
    source: Literal["local", "inbox"] = "local"
    allow_missing_pdfs: bool = False


class RestoreAcknowledgement(BaseModel):
    """Exact operator acknowledgement for one quarantined off-host restore.

    Attributes
    ----------
    restore_id : str
        Lowercase 128-bit identifier of the current quarantine record.
    source : {"inbox"}
        Inbox-only source discriminator; local restores never use quarantine.
    confirm : str
        Typed credential-review phrase checked by the route.

    """

    restore_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source: Literal["inbox"]
    confirm: str


class InboxRestorePoint(BaseModel):
    """One off-host restore point staged in the restore_inbox, per the sidecar manifest.

    Names + booleans only — no paths, no key contents. ``complete`` mirrors
    restore.sh's own completeness gate. ``has_secrets`` flags a bundled
    ``secrets_<ts>`` archive; ``has_pdfs`` and ``legacy_missing_pdfs`` distinguish
    current sets from signed pre-v1.2 sets; ``has_key`` flags the one-time operator
    key the off-host restore requires.
    """

    timestamp: str
    complete: bool
    has_secrets: bool
    has_key: bool
    has_pdfs: bool
    legacy_missing_pdfs: bool


class RestoreStatus(BaseModel):
    """Sanitized, database-independent restore progress state.

    Attributes
    ----------
    state : str
        Overall restore state reported to the polling client.
    current_step : str or None
        Current human-readable step, when one is active.
    steps : list[RestoreStep]
        Per-step progress returned by the sidecar.
    safety_backup_ts : str or None
        Timestamp of the pre-restore safety backup, when created.
    started_at : str or None
        Restore start timestamp.
    finished_at : str or None
        Restore completion timestamp.
    error : str or None
        Sanitized failure detail.
    manual_steps_required : bool
        Whether operator follow-up remains after the data restore.
    phase : str or None
        Sidecar phase used to distinguish destructive progress.
    restore_id : str or None
        Current off-host quarantine identifier, when review is pending.
    source : {"local", "inbox"} or None
        Archive source associated with the current quarantine.
    quarantine : {"none", "awaiting_review", "unreadable"}
        Sanitized outbound-quarantine state. ``unreadable`` remains fail closed
        without exposing or guessing identity fields.

    """

    state: str
    current_step: str | None = None
    steps: list[RestoreStep] = []
    safety_backup_ts: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    manual_steps_required: bool = False
    phase: str | None = None
    restore_id: str | None = None
    source: Literal["local", "inbox"] | None = None
    quarantine: Literal["none", "awaiting_review", "unreadable"] = "none"


class DeleteRequest(BaseModel):
    """Typed confirmation for deleting a restore point.

    Attributes
    ----------
    confirm : str
        Exact destructive-action phrase checked by the delete route.

    """

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
    if name.startswith("pdfs_"):
        return "pdfs"
    if name.startswith("secrets_"):
        return "secrets"
    return "qdrant"


def _validate_name(name: str) -> None:
    """Reject anything that is not one of the five known archive shapes.

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


def _last_run_succeeded(run: dict | None) -> bool | None:
    """Return the accurate ``last_run_succeeded`` value for ``/status``.

    A maintenance-skip run (the sidecar stood down for an in-flight restore)
    leaves ``succeeded`` at its startup-default ``false`` — it is never
    flipped before the skip branch's early ``exit 0`` — so the raw value
    would misreport a deliberate stand-down as "last backup attempt failed".
    ``None`` ("unknown"/"no attempt to judge") is already a valid, supported
    value for this field, so a skipped run reports that instead of a new
    field or state.
    """
    if run is None or run.get("skipped_maintenance"):
        return None
    return run.get("succeeded")


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


def _legacy_manifest_names(ts: str) -> list[str] | None:
    """Return a well-formed legacy manifest inventory, otherwise ``None``."""
    manifest = _read_manifest(ts)
    if not isinstance(manifest, dict) or manifest.get("timestamp") != ts or "run_id" in manifest:
        return None
    archives = manifest.get("archives")
    if not isinstance(archives, list):
        return None
    manifest_names: list[str] = []
    for archive in archives:
        if not isinstance(archive, dict):
            return None
        filename = archive.get("filename")
        if not isinstance(filename, str):
            return None
        manifest_names.append(filename)
    return manifest_names


def _legacy_manifest_has_signature(ts: str) -> bool:
    """Return whether the legacy manifest has a regular HMAC sidecar."""
    try:
        signature_path = secure_path(_BACKUP_DIR, f"manifest_{ts}.json.hmac")
        if signature_path.is_symlink() or not signature_path.is_file():
            return False
        signature = signature_path.read_text().strip()
    except (OSError, ValueError):
        return False
    return re.fullmatch(r"[0-9a-f]{64}", signature) is not None


def _legacy_manifest_missing_pdfs(ts: str, member_filenames: set[str]) -> bool:
    """Identify a pre-v1.2 inventory with a well-formed signature sidecar.

    This service intentionally has no backup authentication key. The privileged
    restore sidecar verifies the signature before any mutation.
    """
    manifest_names = _legacy_manifest_names(ts)
    if manifest_names is None:
        return False
    if (
        len(set(manifest_names)) != len(manifest_names)
        or set(manifest_names) != member_filenames
        or any(name.startswith("pdfs_") for name in manifest_names)
    ):
        return False
    return _legacy_manifest_has_signature(ts)


def _restore_point_completeness(ts: str, members: list[BackupEntry]) -> tuple[bool, bool, bool]:
    store_counts = {
        store: sum(member.store == store for member in members)
        for store in {member.store for member in members}
    }
    member_filenames = {member.filename for member in members}
    has_pdfs = store_counts.get("pdfs", 0) > 0
    legacy_missing_pdfs = not has_pdfs and _legacy_manifest_missing_pdfs(ts, member_filenames)

    core_stores = {"jarvis", "litellm"}
    if has_pdfs:
        core_stores.add("pdfs")
    core_members = [member for member in members if member.store in core_stores]
    core_encryption = {member.encrypted for member in core_members}
    coherent_encryption = (
        bool(core_members)
        and len(core_encryption) == 1
        and all(member.encrypted == core_members[0].encrypted for member in members)
    )
    encrypted_core = coherent_encryption and core_members[0].encrypted
    secrets_count = store_counts.get("secrets", 0)
    secrets_complete = secrets_count == 1 if encrypted_core else secrets_count <= 1

    required_counts = store_counts.get("jarvis", 0) == 1 and store_counts.get("litellm", 0) == 1
    if not legacy_missing_pdfs:
        required_counts = required_counts and store_counts.get("pdfs", 0) == 1

    logical_roles: list[str] = []
    for member in members:
        qdrant_match = _QDRANT_RE.match(member.filename)
        logical_roles.append(f"qdrant:{qdrant_match.group(1)}" if qdrant_match else member.store)
    duplicate_role = len(logical_roles) != len(set(logical_roles))
    complete = required_counts and secrets_complete and coherent_encryption and not duplicate_role
    return has_pdfs, legacy_missing_pdfs, complete


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
        collections = sorted({qm.group(1) for m in members if (qm := _QDRANT_RE.match(m.filename))})
        has_pdfs, legacy_missing_pdfs, complete = _restore_point_completeness(ts, members)
        app_version, schema_version, compat = _manifest_compat(
            ts, {m.filename for m in members}, code_max
        )
        points.append(
            RestorePoint(
                timestamp=ts,
                created_at=max(m.modified_at for m in members),
                stores=stores,
                qdrant_collections=collections,
                complete=complete,
                has_pdfs=has_pdfs,
                legacy_missing_pdfs=legacy_missing_pdfs,
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


def _fsync_directory(directory: Path) -> None:
    """Persist directory changes after updating restore state."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    """Write all bytes to an already-open state file."""
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting restore state")
        view = view[written:]


def _atomic_write_private_json(path: Path, payload: dict[str, object]) -> None:
    """Durably replace one private JSON record in the trusted trigger volume.

    A random ``O_EXCL|O_NOFOLLOW`` temporary file is written and fsynced before
    ``os.replace`` publishes it; the parent directory is then fsynced so a clean
    return means both content and directory entry reached the filesystem. Callers
    use the restore-state lock when replacement must be ordered with another
    sentinel. Any failure removes only the uncommitted random temporary name.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
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
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _restore_state_lock_file() -> Path:
    return restore_status_token_file().parent / _RESTORE_STATE_LOCK_FILENAME


@contextmanager
def _restore_state_lock() -> Iterator[None]:
    """Serialize restore request publication and acknowledgement on one host.

    Containers sharing the trigger volume open the same file with ``O_NOFOLLOW``
    and reject non-regular or multiply linked lock files. While the exclusive
    lock is held, request creation checks quarantine and publishes one matching
    token record and request file; acknowledgement removes the matching token and
    quarantine in order. The lock does not coordinate separate hosts.

    Yields
    ------
    None
        Control while the exclusive trigger-volume lock is held.

    Raises
    ------
    OSError
        If the lock cannot be securely opened, validated, or acquired.

    """
    path = _restore_state_lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise OSError("restore state lock is not a singly-linked regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_status_token(
    token: str,
    *,
    restore_id: str,
    source: Literal["local", "inbox"],
    requested_at: str,
    expires_at: datetime | None = None,
) -> bool:
    """Persist the current restore-session record without its raw token.

    The record is committed before the request file so status polling remains
    available if the restore replaces session rows. Polling may reuse the token;
    acknowledgement consumes the matching record.

    Parameters
    ----------
    token : str
        Raw restore-session token to hash; it is never written to server storage.
    restore_id : str
        Lowercase 128-bit restore identifier.
    source : {"local", "inbox"}
        Archive source stored in the restore-session record.
    requested_at : str
        Aware request timestamp shared with the restore sentinel.
    expires_at : datetime or None
        Exact aware expiry returned to the browser. When omitted, the standard
        two-hour lifetime is measured at this call.

    Returns
    -------
    bool
        ``True`` only after the private record and parent directory are durable;
        ``False`` for invalid binding data or an I/O failure.

    """
    if re.fullmatch(r"[0-9a-f]{32}", restore_id) is None:
        return False
    try:
        requested = datetime.fromisoformat(requested_at)
    except ValueError:
        return False
    expiry = expires_at or datetime.now(UTC) + _RESTORE_TOKEN_TTL
    if (
        requested.tzinfo is None
        or expiry.tzinfo is None
        or expiry <= requested
        or expiry <= datetime.now(UTC)
    ):
        return False
    path = restore_status_token_file()
    payload = {
        "version": 2,
        "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "expires_at": expiry.isoformat(),
        "restore_id": restore_id,
        "source": source,
        "requested_at": requested_at,
    }
    try:
        _atomic_write_private_json(path, payload)
    except OSError as exc:
        logger.error("restore status-token write failed: %r", exc)
        return False
    return True


def _write_upload_grant(token: str) -> bool:
    """Persist a one-time off-host upload grant's hash and 30-minute expiry.

    The raw token is returned once and never stored. The uploader authorizes a
    presented ``X-Upload-Grant`` by hashing it and matching this record. Mode
    ``0644`` permits the separate uploader container to read the non-secret hash
    and expiry through its read-only trigger mount. Atomic replacement prevents
    partial reads; an I/O failure logs and returns ``False``.
    """
    payload = {
        "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    tmp = _UPLOAD_GRANT.parent / f"{_UPLOAD_GRANT.name}.tmp"
    try:
        _UPLOAD_GRANT.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload))
        os.chmod(tmp, 0o644)
        os.replace(tmp, _UPLOAD_GRANT)
    except OSError as exc:
        logger.error("upload grant write failed: %r", exc)
        tmp.unlink(missing_ok=True)
        return False
    return True


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
