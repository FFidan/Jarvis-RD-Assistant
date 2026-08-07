"""Pydantic models for the administrative backup and restore surface.

These describe the archive inventory, restore points, restore progress, and
retention records exchanged by the ``/api/admin/backups`` routes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


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
    last_run_vectors_captured : bool or None
        Whether that attempt captured the vector store, or ``None`` when unknown.
    last_run_s3_complete : bool or None
        Whether its off-site copy is complete, or ``None`` when unknown.
    trigger_pending : bool
        Whether an on-demand backup request awaits the sidecar.

    """

    backup_dir_available: bool
    archive_count: int
    last_run_at: datetime | None  # newest-archive mtime (last *success* proxy)
    last_attempt_at: datetime | None  # from .last_run.json; last run that was attempted
    last_run_succeeded: bool | None  # from .last_run.json; None when unknown
    # A succeeded run still reports these separately: it means a complete restorable
    # LOCAL set exists, which stays true when the vector store was unreachable or the
    # off-site copy failed. Records written before these fields existed report None.
    last_run_vectors_captured: bool | None
    last_run_s3_complete: bool | None
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
    vectors_captured : bool or None
        Whether the vector store was captured, or ``None`` when unrecorded.
    s3_complete : bool or None
        Whether the off-site copy is complete, or ``None`` when unrecorded.

    """

    attempted_at: datetime | None
    succeeded: bool | None
    stores: dict[str, str]
    vectors_captured: bool | None
    s3_complete: bool | None


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
    allow_unknown_schema : bool
        Explicit consent for sets that record no usable database schema
        version, whose compatibility therefore cannot be checked.

    """

    timestamp: str
    confirm: str
    # "local" (default) restores from the read-only /backups mount; "inbox" restores
    # an operator-staged archive set from the sidecar's restore_inbox (off-host DR).
    # The default keeps every existing caller/test valid.
    source: Literal["local", "inbox"] = "local"
    allow_missing_pdfs: bool = False
    allow_unknown_schema: bool = False


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


class DeleteOutcome(BaseModel):
    """Outcome of the sidecar's last prune run, read from ``.last_delete.json``.

    Attributes
    ----------
    state : {"recorded", "no_deletions_recorded"}
        Whether an outcome record exists and could be parsed.
    deleted : list of str
        Archive filenames the sidecar removed.
    skipped : list of str
        Human-readable ``"<timestamp> (<reason>)"`` lines for restore points the
        sidecar kept. The sidecar writes strings, not structured entries.
    at : str or None
        When the prune ran.
    reason : str or None
        Why the run refused or limited itself, when it did.
    remaining_restore_points : int or None
        Complete restore points still held after the run.

    """

    state: Literal["recorded", "no_deletions_recorded"]
    deleted: list[str] = []
    skipped: list[str] = []
    at: str | None = None
    reason: str | None = None
    remaining_restore_points: int | None = None
