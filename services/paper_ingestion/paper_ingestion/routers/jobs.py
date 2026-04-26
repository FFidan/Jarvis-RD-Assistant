"""Jobs REST endpoints for paper_ingestion service.

Thin shim over :func:`jarvis_common.jobs_router.build_jobs_router` — see that
module for endpoint contracts and shared invariants (LE-002 ownership
coercion, SYM-002 mutable-default fix, ``noop.test`` toggle).

Internal-only kinds (``paper.download``, ``papers.scan_local``,
``extraction.single``, ``citations.batch_fetch``, ``digest.weekly``,
``paper.summarize``) are deliberately excluded from the public allowlist —
they are only triggered by the service itself.

Per-kind payloads are validated through a Pydantic discriminated union, so
unknown kinds and missing / wrong-typed required fields are rejected with
HTTP 422 before the handler runs (PI-EDGE-002).
"""

from __future__ import annotations

from typing import Literal

from jarvis_common import jobs as jobs_lib
from jarvis_common.jobs_router import build_jobs_router
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, limiter

# ---------------------------------------------------------------------------
# Per-kind payload schemas — one model per public job kind.
# Wire format:
#   {"kind": "paper.process", "payload": {"paper_id": 42}}
# ---------------------------------------------------------------------------


class PulseGeneratePayload(BaseModel):
    kind: Literal["pulse.generate"]
    # Optional ISO timestamp used for deterministic testing only.
    now: str | None = None


class PaperProcessPayload(BaseModel):
    kind: Literal["paper.process"]
    paper_id: int
    force: bool = False


class PaperAnalyzePayload(BaseModel):
    kind: Literal["paper.analyze"]
    paper_id: int


class PapersBatchProcessPayload(BaseModel):
    kind: Literal["papers.batch_process"]
    paper_ids: list[int]


class PapersBatchSummarizePayload(BaseModel):
    kind: Literal["papers.batch_summarize"]
    paper_ids: list[int]


class ExtractionBatchPayload(BaseModel):
    kind: Literal["extraction.batch"]
    paper_ids: list[int]


class NoopTestPayload(BaseModel):
    """Test-only handler — only accepted when JARVIS_ENABLE_TEST_JOBS=1."""

    kind: Literal["noop.test"]
    # Allow any extra keys so test callers can attach markers without schema changes.
    model_config = {"extra": "allow"}


PI_PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "pulse.generate": PulseGeneratePayload,
    "paper.process": PaperProcessPayload,
    "paper.analyze": PaperAnalyzePayload,
    "papers.batch_process": PapersBatchProcessPayload,
    "papers.batch_summarize": PapersBatchSummarizePayload,
    "extraction.batch": ExtractionBatchPayload,
    "noop.test": NoopTestPayload,
}

PI_PUBLIC_JOB_KINDS: frozenset[str] = frozenset(
    {
        "pulse.generate",
        "paper.process",
        "paper.analyze",
        "papers.batch_process",
        "papers.batch_summarize",
        "extraction.batch",
    }
)


def _extract_paper_id(payload: dict) -> int | None:
    """Return ``payload['paper_id']`` when it is a real ``int``, else ``None``.

    WS-6B-α: scopes the factory's ownership check to single-paper kinds
    (``paper.process``, ``paper.analyze``).  Batch kinds use ``paper_ids:
    list[int]`` and skip per-paper authorisation here — the worker validates
    each id when it processes the batch.
    """
    paper_id = payload.get("paper_id")
    return paper_id if isinstance(paper_id, int) else None


router = build_jobs_router(
    service_name="paper_ingestion",
    public_kinds=PI_PUBLIC_JOB_KINDS,
    get_db_pool=get_db_pool,
    limiter=limiter,
    payload_schemas=PI_PAYLOAD_SCHEMAS,  # discriminated mode → 422 on shape errors
    paper_ownership_extractor=_extract_paper_id,
)

# ---------------------------------------------------------------------------
# Re-exports — preserve the public symbol surface tests + main.py rely on.
# ---------------------------------------------------------------------------
_HANDLERS = {r.endpoint.__name__: r.endpoint for r in router.routes}  # type: ignore[attr-defined]
create_job = _HANDLERS["create_job"]
get_job = _HANDLERS["get_job"]
list_jobs = _HANDLERS["list_jobs"]
stream_job = _HANDLERS["stream_job"]
cancel_job = _HANDLERS["cancel_job"]

CreateJobRequest = router.create_job_request_model  # type: ignore[attr-defined]

__all__ = [
    "PI_PUBLIC_JOB_KINDS",
    "PI_PAYLOAD_SCHEMAS",
    "PulseGeneratePayload",
    "PaperProcessPayload",
    "PaperAnalyzePayload",
    "PapersBatchProcessPayload",
    "PapersBatchSummarizePayload",
    "ExtractionBatchPayload",
    "NoopTestPayload",
    "CreateJobRequest",
    "router",
    "jobs_lib",
    "create_job",
    "get_job",
    "list_jobs",
    "stream_job",
    "cancel_job",
]
