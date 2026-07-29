"""Domain exceptions for paper_ingestion.

Raise these from service functions called by job handlers.
They map to JobError in the job layer — NOT to HTTPException.
"""

from jarvis_common.jobs import JobError


class PaperNotFoundError(JobError):
    """The requested paper does not exist in the database."""


class EmptyChunksError(JobError):
    """The paper has no processed chunks; run process-pdf first."""


class LLMError(JobError):
    """The LLM call failed with a non-retryable error."""


class SourceGenerationChangedError(JobError):
    """The paper changed after a derived-artifact run captured its inputs."""
