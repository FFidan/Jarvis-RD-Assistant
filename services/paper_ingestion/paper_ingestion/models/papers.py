"""Paper-centric Pydantic models.

Core paper records, chunks, summaries, user state, feed, feedback,
priority, discovery, search, and paper-source configuration.
"""

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from jarvis_common.crypto import mask_secret
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Keys whose values must never appear in plaintext in API responses.
_SECRET_KEY_NAMES: frozenset[str] = frozenset(
    {"api_key", "client_secret", "token", "password", "secret", "bearer"}
)

# --- Enums ---


class SourceType(str, Enum):
    """Supported paper source types."""

    ARXIV = "arxiv"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    LOCAL = "local"
    OPENALEX = "openalex"
    PUBMED = "pubmed"
    ZOTERO = "zotero"


class Confidence(str, Enum):
    """Summary confidence level based on quote verification pass rate.

    NONE = no findings to verify, HIGH = 100% verified, MEDIUM = >50%, LOW = <=50%.
    """

    NONE = "NONE"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# --- Core Paper Models ---


class PaperBase(BaseModel):
    """Fields common to paper creation and response."""

    external_id: str = Field(..., max_length=255)
    source_type: SourceType
    title: str = Field(..., max_length=1000)
    authors: list[str]
    abstract: str | None = Field(default=None, max_length=50000)
    published_date: date | None = None
    url: str = Field(..., max_length=2000)
    pdf_url: str | None = Field(default=None, max_length=2000)
    citation_count: int = 0
    metadata: dict = Field(default_factory=dict)

    @field_validator("url", "pdf_url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith(("http://", "https://", "local://")):
            raise ValueError("URL must start with http://, https://, or local://")
        return v


class PaperCreate(PaperBase):
    """Used when inserting a new paper from a source."""

    # H.11 cross-ref: the literal union below is mirrored in
    # frontend/src/types/index.ts ('inbox'|'to_read'|'reading'|'done'|'trash'
    # for LifecycleState plus the discovery-origin enum below). When you
    # add or rename a discovery_origin value here, also update the
    # frontend type and any constants in frontend/src/components/feed/.
    discovery_origin: Literal["user_initiated", "pulse", "recommender", "citation_batch"] = (
        "user_initiated"
    )


class PaperResponse(PaperBase):
    """Full paper representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    discovery_origin: Literal["user_initiated", "pulse", "recommender", "citation_batch"] = (
        "user_initiated"  # immutable after insert
    )
    pdf_local_path: str | None = None
    pdf_downloaded: bool = False
    discovered_at: datetime | None = None
    priority_score: float | None = None
    created_at: datetime


class ChunkResponse(BaseModel):
    """A single text chunk from a processed paper."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    chunk_index: int
    content: str
    page_number: int | None = None
    start_char: int | None = None
    end_char: int | None = None
    embedding_id: str | None = None
    created_at: datetime


class KeyFinding(BaseModel):
    """A single finding within a paper summary.

    The ``verified`` flag starts as False and is set by the quote
    verification pipeline in ``verification.py``.
    """

    finding: str
    quote: str
    page_number: int | None = None
    chunk_id: int | None = None
    verified: bool = False
    snapshot_path: str | None = None


class CrossReference(BaseModel):
    """A link between two related papers discovered via cross-reference check."""

    related_paper_id: int
    relationship: str
    explanation: str
    related_quote: str | None = None
    content_generation: int = 0


class SummaryResponse(BaseModel):
    """LLM-generated summary with verified citations."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    summary_brief: str
    summary_detailed: str
    tldr: str | None = None
    key_findings: list[KeyFinding]
    methodology: str | None = None
    limitations: str | None = None
    relevance_notes: str | None = None
    confidence: Confidence
    cross_references: list[CrossReference]
    llm_model: str | None = None
    summary_verified: bool = False
    created_at: datetime
    # Not persisted — set only on fresh summarize responses.
    coverage: float | None = None
    passes: int | None = None


class UserStateResponse(BaseModel):
    """User reading state for a paper.

    H.11 cross-ref: the ``state`` and ``state_before_trash`` literal unions
    below MUST stay in sync with ``LifecycleState`` and ``StateBeforeTrash``
    in ``frontend/src/types/index.ts``. The frontend uses the same string
    values verbatim, so any addition / rename / removal here is a
    breaking schema change requiring a coordinated frontend update.
    """

    state: Literal["inbox", "to_read", "reading", "done", "trash"]
    state_before_trash: Literal["inbox", "to_read", "reading", "done"] | None = None
    starred: bool = False
    rating: int | None = None
    user_notes: str | None = None
    flagged: bool = False
    updated_at: datetime | None = None


class RecentFeedback(BaseModel):
    """Most recent feedback by current user (UI affordance state)."""

    signal: Literal["positive", "negative"]
    source: str
    created_at: datetime


class PaperDetailResponse(BaseModel):
    """Paper with its summary, chunks, user state, and most recent feedback."""

    paper: PaperResponse
    summary: SummaryResponse | None = None
    chunks: list[ChunkResponse] = Field(default_factory=list)
    user_state: UserStateResponse | None = None
    recent_feedback: RecentFeedback | None = None
    has_project_links: bool = False
    # True when the most recent paper.process / paper.analyze job for this
    # paper+user terminated in `failed` (procrastinate_jobs.status). Lets the
    # left Pipeline rail (PaperTOC) show ✗ from the SAME persisted failure
    # source ActionsSidebar already polls via getJob — no parallel status.
    processing_failed: bool = False


# --- Search / Request Models ---


class SearchRequest(BaseModel):
    """Request body for POST /api/search and POST /api/search-preview.

    Supports both legacy single-source format and new multi-source format:
    - Legacy: ``{"source": "arxiv", ...}``
    - Legacy alias: ``{"source": "both", ...}`` → expands to arxiv + semantic_scholar
    - New: ``{"source_types": ["arxiv", "pubmed"], ...}``
    """

    query: str = Field(..., min_length=1, max_length=500)
    # Legacy field kept for backward compat; migrated to source_types by validator.
    source: SourceType | None = None
    source_types: list[SourceType] = Field(
        default_factory=lambda: [SourceType.ARXIV],
        min_length=1,
    )
    max_results: int = Field(default=10, ge=1, le=200)
    year_from: int | None = Field(default=None, ge=1900, le=2100)
    year_to: int | None = Field(default=None, ge=1900, le=2100)
    sort_by: Literal["relevance", "date"] = "relevance"
    author: str | None = Field(default=None, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_source(cls, data: Any) -> Any:
        """Migrate legacy ``source`` field to ``source_types``.

        Handles:
        - ``{"source": "both"}`` → ``{"source_types": ["arxiv", "semantic_scholar"]}``
        - ``{"source": "arxiv"}`` → ``{"source_types": ["arxiv"]}``
        - ``{"source_types": [...]}`` → pass through unchanged
        """
        if not isinstance(data, dict):
            return data
        if "source_types" not in data and "source" in data:
            source_val = data.get("source")
            if source_val == "both":
                data = dict(data)
                data["source_types"] = ["arxiv", "semantic_scholar"]
                data.pop("source", None)
            elif source_val is not None:
                data = dict(data)
                data["source_types"] = [source_val]
                # Keep source for backward compat (callers may still read it)
        return data


# --- Internal Models ---


class ChunkForEmbedding(BaseModel):
    """A chunk prepared for embedding (before DB insertion)."""

    chunk_index: int
    content: str
    page_number: int | None = None
    start_char: int
    end_char: int


class PaperSourceConfig(BaseModel):
    """Configuration row from the paper_sources table."""

    id: int
    source_type: SourceType
    enabled: bool
    config: dict = Field(default_factory=dict)


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_type: SourceType
    enabled: bool
    config: dict = Field(default_factory=dict)
    priority: int = 1
    display_order: int = 0
    created_at: datetime

    @model_validator(mode="after")
    def _redact_secrets(self) -> "SourceResponse":
        """Mask known secret keys in config so they are never leaked in API responses."""
        if isinstance(self.config, dict):
            redacted = {
                k: (mask_secret(str(v)) if k.lower() in _SECRET_KEY_NAMES and v else v)
                for k, v in self.config.items()
            }
            object.__setattr__(self, "config", redacted)
        return self


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    config: dict | None = None
    priority: int | None = None
    display_order: int | None = None


# --- Feed Models ---


class FeedPaper(PaperResponse):
    """Paper with joined summary and user-state fields for the feed.

    H.11 cross-ref: ``state`` / ``state_before_trash`` mirror the
    ``LifecycleState`` / ``StateBeforeTrash`` types in
    ``frontend/src/types/index.ts``. Keep them in lockstep.
    """

    summary_brief: str | None = None
    tldr: str | None = None
    confidence: Confidence | None = None
    state: Literal["inbox", "to_read", "reading", "done", "trash"] = "inbox"
    state_before_trash: Literal["inbox", "to_read", "reading", "done"] | None = None
    starred: bool = False
    rating: int | None = None
    priority_level: str | None = None
    has_chunks: bool = False
    has_summary: bool = False
    recommendation_score: float | None = None
    recommendation_reason: str | None = None
    recommendation_modes: list[str] | None = None
    note_match_count: int = 0
    note_snippet: str | None = None
    recent_feedback: RecentFeedback | None = None


class FeedResponse(BaseModel):
    """Paginated response for the What's New paper feed."""

    papers: list[FeedPaper]
    total: int
    search_mode: str = "filtered"


class DiscoverRequest(BaseModel):
    """Request body for seed-based paper discovery."""

    paper_ids: list[int] = Field(..., min_length=1, max_length=10)
    limit: int = Field(default=10, ge=1, le=50)
    score_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


# --- Priority Helpers ---


def compute_priority(
    relevance_scores: list[float],
    discovered_at: datetime | None,
    citation_count: int | None,
    now: datetime,
) -> float:
    """Compute composite priority score for a paper.

    Parameters
    ----------
    relevance_scores : list[float]
        Relevance scores from all linked topics.
    discovered_at : datetime or None
        When the paper was discovered.
    citation_count : int or None
        Number of citations.
    now : datetime
        Current time for recency calculation.

    Returns
    -------
    float
        Priority score between 0.0 and 1.0.
    """
    relevance = max(relevance_scores) if relevance_scores else 0.0
    recency = 0.5  # Default if no discovered_at
    if discovered_at:
        days_old = (now - discovered_at).total_seconds() / 86400
        recency = max(0.0, 1.0 - days_old / 30)
    citation_boost = min(1.0, (citation_count or 0) / 100)
    return round(0.5 * relevance + 0.3 * recency + 0.2 * citation_boost, 4)


def priority_level(score: float | None) -> str:
    """Convert priority score to display level.

    Parameters
    ----------
    score : float or None
        Priority score between 0.0 and 1.0.

    Returns
    -------
    str
        One of ``"must-read"``, ``"recommended"``, ``"background"``, or ``"unscored"``.
    """
    if score is None:
        return "unscored"
    if score > 0.7:
        return "must-read"
    if score > 0.4:
        return "recommended"
    return "background"


# --- Endpoint Response Models ---


class SystemModelsResponse(BaseModel):
    """Response for GET /api/system/models."""

    status: str
    installed: list[dict[str, Any]]
    hardware: dict[str, Any]
    current: dict[str, Any]
    issues: dict[str, str]
    catalog: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    reviewed_choices: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    embedding_contract: dict[str, Any] = Field(default_factory=dict)
    # Advisory per-VRAM default-model recommendation.  Always present; never
    # mutates config.  confirm_on_target=True on individual aliases means the
    # recommendation has not been validated via a live bench on the target GPU.
    hardware_recommendation: dict[str, Any] = Field(default_factory=dict)


class ProcessPdfResponse(BaseModel):
    """Response for POST /api/process-pdf/{paper_id}."""

    paper_id: int
    chunk_count: int
    status: str


class ScanLocalPdfsResponse(BaseModel):
    """Response for POST /api/scan-local-pdfs."""

    scanned: int
    imported: int
    skipped: int


class MarkReadResponse(BaseModel):
    """Response for PUT /api/papers/{paper_id}/read."""

    status: str
    paper_id: int


class RelevanceScoreResponse(BaseModel):
    """Response for POST /api/relevance-score."""

    paper_id: int
    topic_id: int
    relevance_score: float


class FeedbackRequest(BaseModel):
    """Body for POST /api/papers/{paper_id}/feedback."""

    signal: Literal["positive", "negative"]
    source: Literal[
        "pulse_thumbs",
        "feed_thumbs",
        "paper_detail_thumbs",
        "dismiss_combined",
    ]
    reason: str | None = None


class FeedbackResponse(BaseModel):
    """Response for POST /api/papers/{paper_id}/feedback."""

    paper_id: int
    signal: Literal["positive", "negative"]
    source: str
    created_at: datetime


class PaperPriorityResponse(BaseModel):
    """Response for POST /api/papers/{paper_id}/priority."""

    paper_id: int
    priority_score: float
    priority_level: str


class RecomputePrioritiesResponse(BaseModel):
    """Response for POST /api/papers/recompute-priorities."""

    updated: int


class FeedbackListItem(BaseModel):
    """One row in GET /api/recommendation_feedback response."""

    paper_id: int
    title: str
    signal: Literal["positive", "negative"]
    source: Literal[
        "pulse_thumbs",
        "feed_thumbs",
        "paper_detail_thumbs",
        "dismiss_combined",
    ]
    reason: str | None = None
    topic_id: int | None = None
    topic_name: str | None = None
    created_at: datetime


class FeedbackListResponse(BaseModel):
    """Response for GET /api/recommendation_feedback."""

    items: list[FeedbackListItem]
    total: int


class DeleteFeedbackResponse(BaseModel):
    """Response for DELETE /api/recommendation_feedback/{topic_id}."""

    deleted: int
    topic_id: int


class BatchProcessResponse(BaseModel):
    """Response for POST /api/papers/batch-process."""

    queued: int
    total_unprocessed: int
    skipped_missing_pdf: int
    job_id: str | None = None


class BatchSummarizeResponse(BaseModel):
    """Response for POST /api/papers/batch-summarize.

    ``job_id`` is ``None`` when no unsummarised papers were found.
    """

    total_unsummarized: int
    job_id: str | None = None


class WeeklyDigestResponse(BaseModel):
    """Response for GET /api/digest/weekly."""

    topics: list[dict[str, Any]] = Field(default_factory=list)
    total_papers: int
    period_start: str
    period_end: str


class DiscoveryResultItem(BaseModel):
    """A single discovered paper in POST /api/discover response."""

    paper_id: int
    title: str
    authors: list[str]
    url: str
    similarity_score: float
    matching_snippet: str = ""


class PaperBriefResponse(BaseModel):
    """A lightweight paper entry for selector dropdowns."""

    id: int
    title: str
    source_type: str | None = None
    published_date: date | None = None


# --- Hybrid Search / Similar Papers Models ---


class HybridSearchResult(BaseModel):
    """A single result from POST /api/papers/search-hybrid."""

    id: int
    title: str
    authors: list[str]
    url: str
    abstract: str | None = None
    published_date: date | None = None
    rrf_score: float
    bm25_rank: int | None = None
    semantic_rank: int | None = None


class SimilarPaperResult(BaseModel):
    """A single result from GET /api/similar/{paper_id}."""

    paper_id: int
    title: str
    authors: list[str]
    url: str
    similarity_score: float
    matching_snippet: str = ""


# --- Analytics Models ---


class PapersBySourceItem(BaseModel):
    """A single row from GET /analytics/papers-by-source."""

    source_type: str
    count: int


class PapersByStatusItem(BaseModel):
    """A single row from GET /analytics/papers-by-status."""

    status: str
    count: int


# --- Paper Lifecycle Action Models ---


class BulkActionRequest(BaseModel):
    """Body for POST /api/papers/bulk."""

    paper_ids: list[int] = Field(..., min_length=1, max_length=500)
    action: Literal[
        "save",
        "skip",
        "trash",
        "mark_reading",
        "mark_done",
        "restore",
        "star",
        "unstar",
        "feedback_positive",
        "feedback_negative",
        "hard_delete",  # bulk permanent delete (only valid on state='trash')
    ]


class BulkActionFailure(BaseModel):
    """A single failed paper in a POST /api/papers/bulk response."""

    paper_id: int
    error: str


class BulkActionResponse(BaseModel):
    """Response for POST /api/papers/bulk.

    Partial failures are reported per-paper; ``failed`` carries a safe,
    operator-diagnostic ``error`` code (never raw exception text).
    """

    succeeded: list[int] = Field(default_factory=list)
    failed: list[BulkActionFailure] = Field(default_factory=list)


class TopicFacetCount(BaseModel):
    """Per-topic count for the feed facet rail (§ Topic section)."""

    topic_id: int
    name: str
    count: int


class FeedCountsResponse(BaseModel):
    """Response for GET /api/papers/feed/counts (10 named views).

    UI v3 additive facets (by_source, by_topic, untagged) are scoped to the
    caller's user_library exactly as the named-view counts above.
    """

    inbox: int
    library: int
    reading_list: int
    reading: int
    done: int
    starred: int
    trash: int
    active: int
    kept: int
    all_non_trash: int
    # UI v3 facet rail — additive, always present (empty when no library).
    by_source: dict[str, int] = Field(default_factory=dict)
    by_topic: list[TopicFacetCount] = Field(default_factory=list)
    untagged: int = 0


class AnnotationsRequest(BaseModel):
    """Body for PUT /api/papers/{paper_id}/annotations."""

    rating: int | None = Field(default=None, ge=1, le=5)
    user_notes: str | None = None
    flagged: bool | None = None


class ProcessBatchRequest(BaseModel):
    """Body for POST /api/papers/process_batch."""

    paper_ids: list[int] = Field(..., min_length=1, max_length=50)
