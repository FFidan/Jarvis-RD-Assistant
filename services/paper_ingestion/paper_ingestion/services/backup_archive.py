"""Framework-independent backup archive inventory and restore-state storage.

Filename validation, restore-point grouping, manifest reading, and the durable
private-record writers used by the administrative backup routes. This module
owns the read-only archive mount and the trigger-volume records it reads or
writes, so the router layer stays request handling only.
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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, status
from jarvis_common.auth import restore_status_token_file
from jarvis_common.migrations import required_code_schema
from jarvis_common.paths import secure_path
from pydantic import ValidationError

from paper_ingestion.models.backups import (
    BackupEntry,
    InboxRestorePoint,
    RestorePoint,
    RestorePointFile,
)

logger = logging.getLogger(__name__)

# Directory the postgres_backups volume is mounted at (read-only) in this service.
_BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backups"))
# The one-shot restore job writes an inbox inventory containing names and booleans,
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


def _last_run_flag(run: dict | None, key: str) -> bool | None:
    """Read a boolean truthfulness field, degrading anything unrecorded to None.

    Records written before a field existed simply omit it, and a non-boolean value
    is no more trustworthy than a missing one, so both report "unknown" rather than
    asserting a capture that may not have happened.
    """
    if run is None:
        return None
    value = run.get(key)
    return value if isinstance(value, bool) else None


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
    by ``restore.sh --inbox-manifest`` in the one-shot restore job; the app only
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


def _atomic_write_private_json(
    path: Path, payload: Mapping[str, object], *, mode: int = 0o600
) -> None:
    """Durably replace one private JSON record in the trusted trigger volume.

    A random ``O_EXCL|O_NOFOLLOW`` temporary file is written and fsynced before
    ``os.replace`` publishes it; the parent directory is then fsynced so a clean
    return means both content and directory entry reached the filesystem. Callers
    use the restore-state lock when replacement must be ordered with another
    sentinel. Any failure removes only the uncommitted random temporary name.

    Parameters
    ----------
    path : Path
        Destination record. A pre-existing symlink here is replaced, not
        followed: the temporary is created ``O_NOFOLLOW`` and published with
        ``os.replace``, so only the directory entry at ``path`` changes.
    payload : Mapping[str, object]
        JSON-serializable record written compactly.
    mode : int, optional
        Permission bits of the published record, set exactly via ``fchmod``
        (umask-independent) so a wider mode is not silently narrowed on a
        hardened host. Defaults to ``0o600`` (owner-only). Callers that must
        expose a non-secret record to a co-mounted reader pass a wider mode such
        as ``0o644``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(tmp, flags, mode)
        try:
            # os.open's mode is umask-subject; force the exact bits so a wider
            # mode (e.g. the 0o644 upload grant a co-mounted reader must read)
            # survives a hardened host umask. Safe: the temp is a fresh
            # O_EXCL|O_NOFOLLOW file in a directory this service owns.
            os.fchmod(fd, mode)
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
    and expiry through its read-only trigger mount. The hardened writer stages a
    random ``O_EXCL|O_NOFOLLOW`` temporary and ``os.replace``s it over the
    destination, so a pre-existing symlink at the grant path is replaced rather
    than followed and partial reads are impossible; an I/O failure logs and
    returns ``False``.
    """
    payload = {
        "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    try:
        _atomic_write_private_json(_UPLOAD_GRANT, payload, mode=0o644)
    except OSError as exc:
        logger.error("upload grant write failed: %r", exc)
        return False
    return True
