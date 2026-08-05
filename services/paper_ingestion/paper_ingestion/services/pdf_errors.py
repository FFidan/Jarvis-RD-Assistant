"""Error types shared across the PDF workflow service modules.

Defined below every other workflow module so that locks, reclamation and
reconciliation can raise them without importing each other.
"""


class PDFRecordMissingError(RuntimeError):
    """Raised when a paper row can no longer accept a downloaded PDF pointer."""


class PDFSourceSupersededError(RuntimeError):
    """Raised when a paper's source URL moves away from the one a run derived content from."""


class PDFUserFacingError(RuntimeError):
    """Raised with a message written for the person who asked for the run.

    Subclasses ``RuntimeError`` so the synchronous process route keeps
    producing its sanitized 502; the job handler translates it into a
    ``JobError`` so the remediation text survives into the job error payload
    instead of collapsing to a generic failure.
    """


class PDFRebuildNotPermittedError(RuntimeError):
    """Raised when a run that discards derived content cannot name a holding requester."""
