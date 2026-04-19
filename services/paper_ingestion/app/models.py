"""Pydantic v2 data models for the Paper Ingestion Service.

Maps to the PostgreSQL schema defined in db/init.sql and provides
request/response schemas for all API endpoints.
"""

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Enums ---


class TopicRef(BaseModel):
    """Lightweight topic reference passed to source polling methods.

    Used by PaperSource.fetch_new_since() so sources can filter by topic
    without a round-trip to the database.
    """

    id: int
    name: str
    description: str | None = None
    query_terms: list[str] = []


class SourceType(str, Enum):
    """Supported paper source types."""

    ARXIV = "arxiv"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    LOCAL = "local"
    OPENALEX = "openalex"
    PUBMED = "pubmed"


class PaperStatus(str, Enum):
    """User-facing paper reading status."""

    NEW = "new"
    READING = "reading"
    READ = "read"
    ARCHIVED = "archived"
    STARRED = "starred"


class Confidence(str, Enum):
    """Summary confidence level based on quote verification pass rate.

    HIGH = 100% of quotes verified, MEDIUM = >50%, LOW = <=50%.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# --- Core Domain Models ---


class PaperBase(BaseModel):
    """Fields common to paper creation and response."""

    external_id: str = Field(..., max_length=500)
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

    pass


class PaperResponse(PaperBase):
    """Full paper representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    pdf_local_path: str | None = None
    pdf_downloaded: bool = False
    is_read: bool = False
    discovered_at: datetime | None = None
    priority_score: float | None = None
    created_at: datetime


class ChunkResponse(BaseModel):
    """A single text chunk from a paper."""

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


class UserStateResponse(BaseModel):
    """User reading state for a paper."""

    status: str = "new"
    rating: int | None = None
    user_notes: str | None = None
    flagged: bool = False


class UserStateUpsert(BaseModel):
    """Request body for creating/updating user state on a paper."""

    status: Literal["new", "reading", "read", "archived", "starred"] | None = None
    rating: int | None = Field(None, ge=1, le=5)
    user_notes: str | None = None
    flagged: bool | None = None


class DashboardMetrics(BaseModel):
    """Aggregate metrics for the dashboard home page."""

    total_papers: int
    unread_papers: int
    pending_papers: int
    due_cards: int
    active_projects: int
    topic_count: int
    nudge_count: int
    onboarding_stage: str = "needs_topics"


class PaperDetailResponse(BaseModel):
    """Paper with its summary and chunks."""

    paper: PaperResponse
    summary: SummaryResponse | None = None
    chunks: list[ChunkResponse] = Field(default_factory=list)
    user_state: UserStateResponse | None = None


# --- Verification Models ---


class VerificationResult(BaseModel):
    """Result of verifying a single quote against source text."""

    quote: str
    verified: bool
    match_type: str | None = None  # "exact" | "fuzzy" | None
    match_score: float | None = None  # 0.0-1.0, only for fuzzy
    matched_text: str | None = None  # actual text that matched
    chunk_id: int | None = None
    page_number: int | None = None
    matched_span_start: int | None = None  # byte offset of matched_text in full_text (O(1) lookup)


class VerificationReport(BaseModel):
    """Aggregate verification results for a full summary."""

    total_findings: int
    verified_count: int
    failed_count: int
    pass_rate: float  # verified_count / total_findings
    confidence: Confidence
    results: list[VerificationResult]


# --- Request Models ---


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


# --- Topics Models ---


class TopicCreate(BaseModel):
    name: str = Field(..., max_length=255)
    query_terms: list[str] = Field(..., min_length=1)
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    enabled: bool = True

    @field_validator("query_terms")
    @classmethod
    def validate_query_terms(cls, value: list[str]) -> list[str]:
        cleaned = [term.strip() for term in value]
        if not all(cleaned):
            raise ValueError("query_terms must not contain blank strings")
        return cleaned


class TopicUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    query_terms: list[str] | None = Field(default=None, min_length=1)
    category: str | None = None
    description: str | None = Field(default=None, max_length=1000)
    enabled: bool | None = None

    @field_validator("query_terms")
    @classmethod
    def validate_query_terms(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = [term.strip() for term in value]
        if not all(cleaned):
            raise ValueError("query_terms must not contain blank strings")
        return cleaned


class TopicResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    query_terms: list[str]
    category: str | None = None
    description: str | None = None
    enabled: bool = True
    created_at: datetime


# --- Settings Models ---


class ConfigEntry(BaseModel):
    key: str
    value: Any  # JSONB values


class NudgeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    nudge_type: str
    cron_expression: str
    enabled: bool
    config: dict = Field(default_factory=dict)
    last_fired_at: datetime | None = None
    created_at: datetime


class NudgeUpdate(BaseModel):
    cron_expression: str | None = None
    enabled: bool | None = None
    config: dict | None = None


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_type: SourceType
    enabled: bool
    config: dict = Field(default_factory=dict)
    priority: int = 1
    display_order: int = 0
    created_at: datetime


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    config: dict | None = None
    priority: int | None = None
    display_order: int | None = None


class NoteCreate(BaseModel):
    """Request body for creating a paper note."""

    user_note: str = Field(..., min_length=1, max_length=5000)
    highlight_text: str | None = None
    page_number: int | None = Field(default=None, ge=1)


class NoteUpdate(BaseModel):
    """Request body for updating a paper note."""

    user_note: str | None = Field(default=None, min_length=1, max_length=5000)
    highlight_text: str | None = None
    page_number: int | None = Field(default=None, ge=1)


class NoteResponse(BaseModel):
    """Response for a paper note."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    user_note: str
    highlight_text: str | None = None
    page_number: int | None = None
    created_at: datetime


class AskRequest(BaseModel):
    """Request body for conversational RAG on a paper."""

    question: str = Field(..., min_length=1, max_length=2000)
    max_chunks: int = Field(default=5, ge=1, le=10)


# --- Author Tracking Models ---


class TrackedAuthorCreate(BaseModel):
    """Request body for creating a tracked author."""

    author_name: str = Field(..., min_length=1, max_length=500)
    s2_author_id: str | None = None


class TrackedAuthorUpdate(BaseModel):
    """Request body for updating a tracked author."""

    enabled: bool | None = None
    s2_author_id: str | None = None


class TrackedAuthorResponse(BaseModel):
    """Response for a tracked author record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    author_name: str
    s2_author_id: str | None = None
    source: str
    enabled: bool
    last_checked_at: datetime | None = None
    created_at: datetime


class AutoDetectResponse(BaseModel):
    """Response for auto-detect authors endpoint."""

    added: int
    already_tracked: int
    authors: list[TrackedAuthorResponse]


class AuthorCheckResponse(BaseModel):
    """Response for check tracked authors endpoint."""

    new_papers: int
    authors_checked: int


# --- Feed Models ---


class FeedPaper(PaperResponse):
    """Paper with joined summary and user-state fields for the feed."""

    summary_brief: str | None = None
    tldr: str | None = None
    confidence: Confidence | None = None
    user_status: str | None = None
    rating: int | None = None
    priority_level: str | None = None
    has_chunks: bool = False
    has_summary: bool = False
    recommendation_score: float | None = None
    recommendation_reason: str | None = None
    recommendation_modes: list[str] | None = None


class FeedResponse(BaseModel):
    """Paginated response for the What's New paper feed."""

    papers: list[FeedPaper]
    total: int
    search_mode: str = "filtered"


class CrossPaperAskRequest(BaseModel):
    """Request body for cross-paper RAG queries."""

    question: str = Field(..., min_length=1, max_length=1000)
    max_chunks: int = Field(default=10, ge=1, le=20)
    max_papers: int = Field(default=5, ge=1, le=15)
    decompose: bool = Field(default=True)


class DiscoverRequest(BaseModel):
    """Request body for seed-based paper discovery."""

    paper_ids: list[int] = Field(..., min_length=1, max_length=10)
    limit: int = Field(default=10, ge=1, le=50)
    score_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


# --- Citation Graph Models ---


class CitationRelation(BaseModel):
    """A citation relationship between two papers."""

    source_paper_id: int
    cited_paper_id: int
    citation_context: str | None = None
    is_influential: bool | None = None
    intent: list[str] = Field(default_factory=list)


class CitationFetchResponse(BaseModel):
    """Response after fetching citations for a paper."""

    citations_added: int
    references_added: int
    stubs_created: int


class GraphNode(BaseModel):
    """A node in the citation graph."""

    id: int
    title: str
    citation_count: int = 0
    published_date: date | None = None
    is_stub: bool = False
    display_size: int = 20


class GraphEdge(BaseModel):
    """An edge in the citation graph."""

    source: int
    target: int
    is_influential: bool | None = None
    context: str | None = None


class CitationGraphResponse(BaseModel):
    """Full citation graph with nodes and edges."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]


# --- Knowledge Graph Models ---


class EntityCreate(BaseModel):
    """Request body for entity data from LLM extraction."""

    name: str
    entity_type: str
    description: str | None = None


class EntityResponse(BaseModel):
    """Response for an entity."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    canonical_name: str
    entity_type: str
    description: str | None = None
    metadata: dict = Field(default_factory=dict)
    paper_count: int = 1
    created_at: datetime | None = None
    display_size: int = 20


class RelationshipCreate(BaseModel):
    """Relationship data from LLM extraction."""

    source_entity: str  # entity name
    target_entity: str  # entity name
    relationship_type: str
    evidence_quote: str | None = None
    confidence: float = 1.0


class RelationshipResponse(BaseModel):
    """Response for an entity relationship."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_entity_id: int
    target_entity_id: int
    relationship_type: str
    paper_id: int | None = None
    page_number: int | None = None
    evidence_quote: str | None = None
    confidence: float = 1.0
    created_at: datetime | None = None


class EntityExtractionResponse(BaseModel):
    """Response after extracting entities from a paper."""

    entities_added: int
    relationships_added: int
    entities_merged: int
    dropped_relationships: int = 0
    saved_by_full_text_verify: int = 0
    """Number of relationships whose evidence was only found beyond the LLM
    context window cap and saved by the full-text verifier path."""


class KnowledgeGraphResponse(BaseModel):
    """Full knowledge graph data."""

    entities: list[EntityResponse]
    relationships: list[RelationshipResponse]
    entity_type_counts: dict[str, int] = Field(default_factory=dict)


class EntityDetailResponse(BaseModel):
    """Entity with its relationships and papers."""

    entity: EntityResponse
    relationships: list[RelationshipResponse]
    papers: list[dict] = Field(default_factory=list)


class KGQueryResponse(BaseModel):
    """Response for knowledge graph queries."""

    results: list[dict] = Field(default_factory=list)
    query: str


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


# --- Structured Extraction Models ---


class ExtractionField(BaseModel):
    """A single field definition within an extraction template."""

    name: str
    label: str
    description: str
    type: str = "text"  # text, number, list


class ExtractionTemplateCreate(BaseModel):
    """Request body for creating an extraction template."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    fields: list[ExtractionField] = Field(..., min_length=1)
    is_default: bool = False


class ExtractionTemplateUpdate(BaseModel):
    """Request body for updating an extraction template."""

    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    fields: list[ExtractionField] | None = None
    is_default: bool | None = None


class ExtractionTemplateResponse(BaseModel):
    """Response for an extraction template."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    fields: list[ExtractionField]
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


class ExtractedField(BaseModel):
    """A single extracted field value with evidence."""

    value: Any = None
    quote: str | None = None
    verified: bool = False
    confidence: float = 0.0
    chunk_id: int | None = None
    page_number: int | None = None


class ExtractionResponse(BaseModel):
    """Response for a paper extraction."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    paper_id: int
    template_id: int
    extractions: dict[str, ExtractedField]
    extraction_model: str | None = None
    created_at: datetime


class ExtractionRequest(BaseModel):
    """Request body for extracting fields from a paper."""

    template_id: int


class BatchExtractionRequest(BaseModel):
    """Request body for batch extraction."""

    paper_ids: list[int] = Field(..., min_length=1, max_length=50)
    template_id: int


class BatchExtractionResponse(BaseModel):
    """Response for batch extraction."""

    extracted: int
    failed: int
    skipped: int


class ExtractionTableRow(BaseModel):
    """A row in the cross-paper extraction table."""

    paper_id: int
    paper_title: str
    extractions: dict[str, ExtractedField]


# --- Endpoint Response Models ---


class SystemModelsResponse(BaseModel):
    """Response for GET /api/system/models."""

    status: str
    installed: list[dict[str, Any]]
    hardware: dict[str, Any]
    current: dict[str, Any]
    issues: dict[str, str]


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


class FeedbackResponse(BaseModel):
    """Response for POST /api/papers/{paper_id}/feedback."""

    paper_id: int
    rating: int | None = None
    flagged: bool | None = None
    status: str


class PaperPriorityResponse(BaseModel):
    """Response for POST /api/papers/{paper_id}/priority."""

    paper_id: int
    priority_score: float
    priority_level: str


class RecomputePrioritiesResponse(BaseModel):
    """Response for POST /api/papers/recompute-priorities."""

    updated: int


class AskSourceItem(BaseModel):
    """A single source item in an ask response."""

    content: str | None = None
    page_number: int | None = None
    score: float | None = None
    paper_id: int | None = None
    paper_title: str | None = None
    chunk_id: int | None = None


class AskResponse(BaseModel):
    """Response for POST /api/papers/{paper_id}/ask and POST /api/ask."""

    answer: str
    sources: list[AskSourceItem] = Field(default_factory=list)


class BatchProcessResponse(BaseModel):
    """Response for POST /api/papers/batch-process."""

    queued: int
    total_unprocessed: int
    skipped_missing_pdf: int
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


class BatchCitationFetchResponse(BaseModel):
    """Response for POST /api/citations/batch-fetch."""

    queued: int
    message: str


class BatchEntityExtractResponse(BaseModel):
    """Response for POST /api/knowledge-graph/extract-entities/batch."""

    extracted: int
    failed: int
    total: int


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


# --- Pulse models ---


class PulseCardResponse(BaseModel):
    """A single scored card within a Pulse deck."""

    card_id: int
    paper_id: int
    paper_title: str
    paper_authors: list[str]
    paper_url: str | None
    rank: int
    score: float
    llm_relevance: int | None
    llm_novelty: int | None
    reasoning: str | None
    signals: dict[str, float]


class PulseDeckResponse(BaseModel):
    """A full Pulse deck for one day, including all scored cards."""

    deck_id: int
    deck_date: date
    card_count: int
    generated_at: datetime
    cards: list[PulseCardResponse]
    stats: dict
    degraded_reason: str | None = None


class PulseGenerateResponse(BaseModel):
    """Response for POST /api/pulse/generate — returns job_id immediately."""

    job_id: str
    status: str


class PulseStatsResponse(BaseModel):
    """Aggregate Pulse pipeline stats over a sliding window of past runs."""

    window_days: int
    decks_generated: int
    avg_candidates: float | None
    avg_llm_calls: float | None
    avg_duration_s: float | None
    last_run_at: datetime | None
    last_error: str | None
    degraded_reason: str | None = None


class PulseRateRequest(BaseModel):
    """Body for POST /api/pulse/rate."""

    paper_id: int
    rating: Literal["up", "down", "save", "dismiss", "open"]
